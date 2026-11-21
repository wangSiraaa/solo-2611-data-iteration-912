"""导览段编译：把"某产品版 / 某 clip 实例 / 某段源样本"编译成设备整包。

核心语义
--------
- 一个 :class:`Segment` 明确引用 ``product_id``（译制版本）、``clip_id``（clip
  实例）与源样本半开区间 ``[src_a, src_b)``。源区间必须是该 clip 自身
  ``[a, b)`` 的子区间；**不在该 clip 内时直接拒绝，绝不拿引用同一素材的
  另一个 clip 顶替**。
- 播放范围按原项目的换算关系确定：源位置 ``p`` 对应输出时刻
  ``t = start + (p - a) / (源采样率 × 速度)``。输出采样率固定 16000，
  段覆盖的输出样本是满足 ``t0 <= n/16000 < t1`` 的全部 ``n``，
  即 ``n ∈ [ceil(16000·t0), ceil(16000·t1))``。
- 实际 PCM 由产品在该输出样本区间上的真实混音渲染得到（重叠 clip 一并混入，
  与 :mod:`audiomix.engine` 的求和 / 单次恰半取偶 / 饱和语义完全一致），
  不是只取单条素材。
- 段内按设备固定包长 ``packet_samples`` 切成整包，末包可短。
- 包身份 = ``SHA-256(译制版本 ‖ 字节长度 ‖ 实际 PCM 字节)``；两个包只有在
  **译制版本、实际 PCM 字节及长度**都一致时才可复用（内容寻址，自动去重）。
"""
from __future__ import annotations

import hashlib
import os
import wave
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

import numpy as np

from .engine import (
    OUTPUT_RATE, product_clip_view, render_sample_range, validate_product,
)
from .rationals import as_r, dump_r

__all__ = [
    "SegmentError", "Segment", "Packet", "PacketStore", "ProductLibrary",
    "packet_identity",
]


class SegmentError(ValueError):
    """段引用非法（未知产品/clip、源区间不在该 clip 内等）。"""


def _ceil_frac(x: Fraction) -> int:
    return -((-x.numerator) // x.denominator)


def packet_identity(version: str, pcm: bytes) -> str:
    """包身份：译制版本 + 实际 PCM 字节 + 字节长度三者共同决定。"""
    h = hashlib.sha256()
    h.update(version.encode("utf-8"))
    h.update(b"\x00")
    h.update(len(pcm).to_bytes(8, "little", signed=False))
    h.update(pcm)
    return h.hexdigest()


@dataclass(frozen=True)
class Packet:
    """一个设备整包：不可变 PCM 正文 + 身份元数据。"""

    packet_id: str
    version: str
    pcm: bytes          # 小端有符号 16bit 原始字节

    @property
    def length(self) -> int:
        """样本数。"""
        return len(self.pcm) // 2

    @property
    def nbytes(self) -> int:
        return len(self.pcm)

    def samples(self) -> np.ndarray:
        return np.frombuffer(self.pcm, dtype="<i2").astype(np.int16)

    def describe(self, with_hex: bool = False) -> dict[str, Any]:
        d = {
            "packet_id": self.packet_id,
            "version": self.version,
            "length": self.length,
            "nbytes": self.nbytes,
            "sha256": hashlib.sha256(self.pcm).hexdigest(),
        }
        if with_hex:
            d["pcm_hex"] = self.pcm.hex()
        return d

    def write_wav(self, path: str, sample_rate: int = OUTPUT_RATE) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(self.pcm)
        return path


class PacketStore:
    """内容寻址包仓：相同 (版本, 字节, 长度) 只存一份。"""

    def __init__(self) -> None:
        self._packets: dict[str, Packet] = {}

    def add(self, version: str, pcm: bytes) -> Packet:
        pid = packet_identity(version, pcm)
        pkt = self._packets.get(pid)
        if pkt is None:
            pkt = Packet(packet_id=pid, version=version, pcm=bytes(pcm))
            self._packets[pid] = pkt
        return pkt

    def get(self, packet_id: str) -> Packet:
        if packet_id not in self._packets:
            raise KeyError(f"未知包: {packet_id}")
        return self._packets[packet_id]

    def contains(self, packet_id: str) -> bool:
        return packet_id in self._packets

    def __len__(self) -> int:
        return len(self._packets)

    def ids(self) -> list[str]:
        return sorted(self._packets)

    def packets(self) -> list[Packet]:
        return [self._packets[k] for k in sorted(self._packets)]

    def read_range(self, packet_id: str, offset: int, length: int) -> np.ndarray:
        """按包内样本下标范围 [offset, offset+length) 读取真实字节。"""
        arr = self.get(packet_id).samples()
        if offset < 0 or length < 0 or offset + length > len(arr):
            raise IndexError(
                f"包 {packet_id} 范围 [{offset},{offset+length}) 越界（长度 {len(arr)}）")
        return arr[offset:offset + length].copy()

    def concatenate(self, packet_ids: Iterable[str]) -> np.ndarray:
        """按给定次序拼接若干包的真实 PCM（用于逐路回放）。"""
        pieces = [self.get(pid).samples() for pid in packet_ids]
        if not pieces:
            return np.empty(0, dtype=np.int16)
        return np.concatenate(pieces).astype(np.int16)

    def write_packet_wav(self, packet_id: str, path: str) -> str:
        return self.get(packet_id).write_wav(path)

    def total_bytes(self) -> int:
        return sum(p.nbytes for p in self._packets.values())


class ProductLibrary:
    """按 product_id 装载不可变产品及其实素材字节。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[dict[str, Any], dict[str, np.ndarray]]] = {}
        self._views: dict[str, list[dict[str, Any]]] = {}

    def add(self, product: dict[str, Any],
            pcm_by_asset: dict[str, np.ndarray]) -> str:
        validate_product(product, pcm_by_asset)
        pid = product["product_id"]
        self._items[pid] = (product, pcm_by_asset)
        self._views[pid] = product_clip_view(product)
        return pid

    def get(self, product_id: str) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
        if product_id not in self._items:
            raise SegmentError(f"未知产品版: {product_id}")
        return self._items[product_id]

    def view(self, product_id: str) -> list[dict[str, Any]]:
        self.get(product_id)
        return self._views[product_id]

    def clip(self, product_id: str, clip_id: str) -> dict[str, Any]:
        product, _ = self.get(product_id)
        for c in product["clips"]:
            if c["clip_id"] == clip_id:
                return c
        raise SegmentError(f"产品版 {product_id} 中没有 clip 实例 {clip_id}")


@dataclass(frozen=True)
class Segment:
    """一段导览声音：引用 (产品版, clip 实例, 源样本区间)。"""

    product_id: str
    clip_id: str
    src_a: int
    src_b: int

    # ------------------------- 查询 ------------------------- #
    def resolve(self, lib: ProductLibrary) -> dict[str, Any]:
        """解析该次引用下的播放范围与源映射（不渲染）。"""
        product, _ = lib.get(self.product_id)
        clip = lib.clip(self.product_id, self.clip_id)
        rates = {a["asset_id"]: int(a["sample_rate"]) for a in product["assets"]}
        rate = rates[clip["asset_id"]]
        a, b = int(clip["a"]), int(clip["b"])
        if not (a <= self.src_a < self.src_b <= b):
            raise SegmentError(
                f"源区间 [{self.src_a},{self.src_b}) 不在 clip {self.clip_id} 的 "
                f"[{a},{b}) 内；不能拿引用同一素材的其它 clip 实例顶替")
        start = as_r(clip["start"])
        speed = as_r(clip["speed"])
        if speed <= 0:
            raise SegmentError(f"clip {self.clip_id} 速度必须为正")
        t0 = start + Fraction(self.src_a - a, rate) / speed
        t1 = start + Fraction(self.src_b - a, rate) / speed
        n0 = _ceil_frac(t0 * OUTPUT_RATE)
        n1 = _ceil_frac(t1 * OUTPUT_RATE)
        return {
            "product_id": self.product_id,
            "clip_id": self.clip_id,
            "asset_id": clip["asset_id"],
            "asset_rate": rate,
            "clip_source_range": [a, b],
            "segment_source_range": [self.src_a, self.src_b],
            "clip_start": dump_r(start),
            "speed": dump_r(speed),
            "gain": clip["gain"],
            "output_time_range": [dump_r(t0), dump_r(t1)],
            "output_sample_range": [n0, n1],
            "output_samples": n1 - n0,
            "mapping": "p = a + (n/16000 - start) * asset_rate * speed",
        }

    # ------------------------- 编译 ------------------------- #
    def compile(self, lib: ProductLibrary, packet_samples: int,
                store: PacketStore) -> list[Packet]:
        """渲染该段真实混音并按固定包长切包，写入内容寻址包仓。"""
        if packet_samples <= 0:
            raise ValueError("包长必须为正")
        info = self.resolve(lib)
        product, pcm = lib.get(self.product_id)
        view = lib.view(self.product_id)
        n0, n1 = info["output_sample_range"]
        packets: list[Packet] = []
        pos = n0
        while pos < n1:
            hi = min(pos + packet_samples, n1)
            samples = render_sample_range(
                product, pcm, pos, hi, clips_view=view, validate=False)
            pkt = store.add(self.product_id, samples.astype("<i2").tobytes())
            packets.append(pkt)
            pos = hi
        return packets

    def to_json(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "clip_id": self.clip_id,
            "src_a": self.src_a,
            "src_b": self.src_b,
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "Segment":
        return Segment(str(obj["product_id"]), str(obj["clip_id"]),
                       int(obj["src_a"]), int(obj["src_b"]))
