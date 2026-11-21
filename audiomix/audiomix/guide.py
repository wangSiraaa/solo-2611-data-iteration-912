"""有声导览：展厅路线图、设备整包与"边走边选路"的装载编排。

模型
----
- **路线图**是有向无环图：一个入口可通往多个展厅，多条路也可在后面汇合。
- **展厅合同**是一列有序窗口：

  * :class:`PlayWindow`：轮到播放时，所列整包必须已在设备缓存中；
  * :class:`TransmitWindow`：窗口内最多可传 ``budget`` 个任意已编译整包。

  合同按顺序处理。第一个播放窗口即入厅首包；在它之前没有传输窗口时，
  该包必须在入厅时就已随设备带来。全部窗口处理完后，才揭晓下一厅
  （后继分支）。因此在一个已观察历史下做出的提前传输，不能依赖后来才
  揭晓的路线。
- **设备**参数：固定包长（样本）、缓存容量（整包数）。满时可先丢包。
  本模型里丢包永远允许（只是丢弃已带来的数据），容量只约束"任意时刻
  驻留整包数"。两包只有译制版本、实际 PCM 字节及长度一致才复用
  （见 :mod:`audiomix.packets` 的内容寻址包仓）。
- 揭晓分支后，设备可丢弃该分支不再需要的包以释放容量；不同来路到达
  同一展厅的听众，实际带来的包集合可以不同。

规划
----
给出**非预知**（non-anticipatory）装载策略，使路线图里**每一条**完整路线
都能在所有播放窗口按时播放，并遵守传输额度与缓存容量。策略是
``(展厅, 窗口号, 当前缓存) -> 动作`` 的函数；分支揭晓前的传输只看已观察
历史，同一状态下动作唯一。

不可行时给出**冲突证据**：在某个揭晓/传输处，各分支在"下一次可传输之前"
必须驻留的整包集合无法同时满足容量或传输额度。

关键剪枝
--------
若某传输窗口之后、下一次可传输之前，沿各分支必须就绪的整包并集为
``frontier``，则它必须在**本窗口结束**时全部驻留（之后再无补传机会，
播放窗口之间丢包也无法回收未带来的数据）。于是：

* ``|frontier| > 容量`` 或 ``|frontier − 现有缓存| > 本窗口额度`` 立即冲突。

对小规模包集合（"可预装 extras" 能影响可行性的情形）做完整 AND-OR 搜索；
对四五十厅的大图用 frontier 强制集合的策略，避免指数枚举游览线路。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .packets import (
    PacketStore, ProductLibrary, Segment,
)

__all__ = [
    "PlayWindow", "TransmitWindow", "Hall", "RouteMap", "DeviceProfile",
    "CompiledGuide", "compile_guide", "Planner", "PlanResult",
    "Action", "simulate_path", "summarize_all_routes", "compare_profiles",
    "verify_plan_symbolic",
]


# ============================ 合同与路线图 ============================ #

@dataclass(frozen=True)
class PlayWindow:
    """播放窗口：这些段的全部整包在播放时必须就绪。"""

    segments: tuple[Segment, ...]

    def __init__(self, segments: Iterable[Segment]):
        object.__setattr__(self, "segments", tuple(segments))

    def to_json(self) -> dict[str, Any]:
        return {"type": "play",
                "segments": [s.to_json() for s in self.segments]}

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "PlayWindow":
        return PlayWindow(Segment.from_json(s) for s in obj["segments"])


@dataclass(frozen=True)
class TransmitWindow:
    budget: int

    def __post_init__(self) -> None:
        if self.budget < 0:
            raise ValueError("传输额度不能为负")

    def to_json(self) -> dict[str, Any]:
        return {"type": "transmit", "budget": self.budget}

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "TransmitWindow":
        return TransmitWindow(int(obj["budget"]))


Window = PlayWindow | TransmitWindow


def _window_from_json(obj: dict[str, Any]) -> Window:
    if obj["type"] == "play":
        return PlayWindow.from_json(obj)
    if obj["type"] == "transmit":
        return TransmitWindow.from_json(obj)
    raise ValueError(f"未知窗口类型: {obj.get('type')!r}")


@dataclass(frozen=True)
class Hall:
    hall_id: str
    windows: tuple[Window, ...] = ()
    successors: tuple[str, ...] = ()

    def __init__(self, hall_id: str, windows: Iterable[Window] = (),
                 successors: Iterable[str] = ()):
        object.__setattr__(self, "hall_id", hall_id)
        object.__setattr__(self, "windows", tuple(windows))
        object.__setattr__(self, "successors", tuple(successors))

    def to_json(self) -> dict[str, Any]:
        return {
            "hall_id": self.hall_id,
            "windows": [w.to_json() for w in self.windows],
            "successors": list(self.successors),
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "Hall":
        return Hall(obj["hall_id"],
                    [_window_from_json(w) for w in obj.get("windows", [])],
                    obj.get("successors", []))


@dataclass(frozen=True)
class RouteMap:
    entrance: str
    halls: dict[str, Hall]
    preload_budget: int = 0

    def __post_init__(self) -> None:
        if self.entrance not in self.halls:
            raise ValueError(f"入口 {self.entrance} 不在展厅集合中")
        if self.preload_budget < 0:
            raise ValueError("入口预装额度不能为负")
        for h in self.halls.values():
            for s in h.successors:
                if s not in self.halls:
                    raise ValueError(f"展厅 {h.hall_id} 的后继 {s} 未定义")
        self._check_acyclic()

    def to_json(self) -> dict[str, Any]:
        return {
            "entrance": self.entrance,
            "preload_budget": self.preload_budget,
            "halls": [self.halls[h].to_json() for h in sorted(self.halls)],
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> "RouteMap":
        halls = {}
        for h in obj["halls"]:
            hall = Hall.from_json(h)
            halls[hall.hall_id] = hall
        return RouteMap(obj["entrance"], halls,
                        int(obj.get("preload_budget", 0)))

    def _check_acyclic(self) -> None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {h: WHITE for h in self.halls}

        def dfs(u: str) -> None:
            color[u] = GRAY
            for v in self.halls[u].successors:
                if color[v] == GRAY:
                    raise ValueError(f"路线图必须无环：检测到回边 {u}->{v}")
                if color[v] == WHITE:
                    dfs(v)
            color[u] = BLACK

        for h in self.halls:
            if color[h] == WHITE:
                dfs(h)

    def reachable(self) -> set[str]:
        seen: set[str] = set()
        stack = [self.entrance]
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            stack.extend(self.halls[u].successors)
        return seen

    def route_count(self) -> int:
        """完整路线条数（DP，绝不逐条枚举）。"""
        memo: dict[str, int] = {}

        def f(u: str) -> int:
            if not self.halls[u].successors:
                return 1
            if u not in memo:
                memo[u] = sum(f(v) for v in self.halls[u].successors)
            return memo[u]

        return f(self.entrance)

    def enumerate_routes(self, limit: int = 100_000) -> list[list[str]]:
        """枚举全部完整路线；超过 limit 抛错，防止意外指数膨胀。"""
        out: list[list[str]] = []

        def dfs(u: str, path: list[str]) -> None:
            if len(out) > limit:
                raise RuntimeError(f"路线数超过上限 {limit}，拒绝逐条枚举")
            path = path + [u]
            succ = self.halls[u].successors
            if not succ:
                out.append(path)
                return
            for v in succ:
                dfs(v, path)

        dfs(self.entrance, [])
        return out

    # ------------------------- 文件 I/O ------------------------- #
    def save(self, path: str) -> str:
        import json
        import os
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, ensure_ascii=False, indent=2)
        return path

    @staticmethod
    def load(path: str) -> "RouteMap":
        import json
        with open(path, encoding="utf-8") as fh:
            return RouteMap.from_json(json.load(fh))


# ============================ 设备与编译 ============================ #

@dataclass(frozen=True)
class DeviceProfile:
    packet_samples: int
    cache_capacity: int
    name: str = "device"

    def __post_init__(self) -> None:
        if self.packet_samples <= 0:
            raise ValueError("包长必须为正")
        if self.cache_capacity < 0:
            raise ValueError("缓存容量不能为负")

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "packet_samples": self.packet_samples,
            "cache_capacity": self.cache_capacity,
        }


class CompiledGuide:
    """把路线图里的段按设备包长编译成内容寻址整包（全图共享包仓）。"""

    def __init__(self, route_map: RouteMap, device: DeviceProfile,
                 store: PacketStore):
        self.route_map = route_map
        self.device = device
        self.store = store
        self.window_packets: dict[tuple[str, int], tuple[str, ...]] = {}
        self.window_required: dict[tuple[str, int], frozenset[str]] = {}
        self.segment_info: dict[tuple[str, int, int], dict[str, Any]] = {}
        self.origins: dict[str, list[dict[str, Any]]] = {}
        self._entry_req: dict[str, frozenset[str]] = {}
        self._need_all: dict[str, frozenset[str]] = {}

    # ---------------- 编译 / 查询 ---------------- #
    def _compile(self, lib: ProductLibrary) -> None:
        for hall in self.route_map.halls.values():
            for wi, w in enumerate(hall.windows):
                if not isinstance(w, PlayWindow):
                    continue
                seq: list[str] = []
                for si, seg in enumerate(w.segments):
                    info = seg.resolve(lib)
                    packets = seg.compile(lib, self.device.packet_samples,
                                          self.store)
                    pids = [p.packet_id for p in packets]
                    seq.extend(pids)
                    # 每个包在该段内的输出样本跨度（末包可短），便于逐包核对。
                    span = info["output_sample_range"]
                    per_packet = []
                    pos = span[0]
                    for p in packets:
                        per_packet.append({
                            "packet_id": p.packet_id,
                            "output_sample_range": [pos, pos + p.length],
                            "length": p.length,
                            "nbytes": p.nbytes,
                        })
                        pos += p.length
                    self.segment_info[(hall.hall_id, wi, si)] = info | {
                        "packets": pids,
                        "packet_spans": per_packet,
                    }
                    for pe in per_packet:
                        self.origins.setdefault(pe["packet_id"], []).append({
                            "hall": hall.hall_id,
                            "window": wi,
                            "segment": seg.to_json(),
                            "output_sample_range": pe["output_sample_range"],
                        })
                self.window_packets[(hall.hall_id, wi)] = tuple(seq)
                self.window_required[(hall.hall_id, wi)] = frozenset(seq)

    def packets_of(self, hall: str, wi: int) -> tuple[str, ...]:
        return self.window_packets.get((hall, wi), ())

    def _req_until_transmit(self, hall: str) -> frozenset[str]:
        """进入 hall 后、到下一次可传输之前必须驻留的整包并集。

        可传输机会即传输窗口，遇到即停止。若整条路到终点都没有传输窗口，
        则并入沿途全部播放需求以及后继（递归）。
        """
        if hall in self._entry_req:
            return self._entry_req[hall]
        acc: set[str] = set()
        for wi, w in enumerate(self.route_map.halls[hall].windows):
            if isinstance(w, PlayWindow):
                acc |= self.window_required[(hall, wi)]
            else:
                self._entry_req[hall] = frozenset(acc)
                return self._entry_req[hall]
        for s in self.route_map.halls[hall].successors:
            acc |= self.entry_req(s)
        self._entry_req[hall] = frozenset(acc)
        return self._entry_req[hall]

    entry_req = _req_until_transmit

    def after_transmit_req(self, hall: str, wi: int) -> frozenset[str]:
        """本传输窗口之后、下一次可传输之前必须驻留的整包并集（frontier）。"""
        acc: set[str] = set()
        windows = self.route_map.halls[hall].windows
        for j in range(wi + 1, len(windows)):
            w = windows[j]
            if isinstance(w, PlayWindow):
                acc |= self.window_required[(hall, j)]
            else:
                return frozenset(acc)
        for s in self.route_map.halls[hall].successors:
            acc |= self.entry_req(s)
        return frozenset(acc)

    def need_all(self, hall: str) -> frozenset[str]:
        """从该展厅出发（含所有可达后继）全部播放窗口需求的并集。"""
        if hall in self._need_all:
            return self._need_all[hall]
        acc: set[str] = set()
        for wi, w in enumerate(self.route_map.halls[hall].windows):
            if isinstance(w, PlayWindow):
                acc |= self.window_required[(hall, wi)]
        for s in self.route_map.halls[hall].successors:
            acc |= self.need_all(s)
        self._need_all[hall] = frozenset(acc)
        return self._need_all[hall]


def compile_guide(route_map: RouteMap, lib: ProductLibrary,
                  device: DeviceProfile,
                  store: PacketStore | None = None) -> CompiledGuide:
    store = store if store is not None else PacketStore()
    cg = CompiledGuide(route_map, device, store)
    cg._compile(lib)
    return cg


# ============================ 规划动作与结果 ============================ #

@dataclass(frozen=True)
class Action:
    """一个传输窗口的确定性动作：先丢弃 drop，再传入 transmit。"""

    transmit: tuple[str, ...]
    drop: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {"transmit": list(self.transmit), "drop": list(self.drop)}


@dataclass
class PlanResult:
    feasible: bool
    device: DeviceProfile
    actions: dict[tuple[str, int, frozenset[str]], Action]
    reveal_drops: dict[tuple[str, str, frozenset[str]], tuple[str, ...]]
    certificate: Optional[dict[str, Any]] = None
    stats: dict[str, Any] = field(default_factory=dict)
    initial_preload: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "device": self.device.to_json(),
            "initial_preload": list(self.initial_preload),
            "certificate": self.certificate,
            "stats": self.stats,
        }


class _SearchLimit(RuntimeError):
    pass


# ============================ 规划器 ============================ #

class Planner:
    """AND-OR 搜索：存在性 + 非预知策略 + 冲突证据。"""

    #: 包全集不超过此规模时，完整搜索"可预装 extras"，结果精确；
    #: 否则用 frontier 强制集合策略（一定合法，且不枚举路线）。
    FULL_SEARCH_PACKET_LIMIT = 12

    def __init__(self, guide: CompiledGuide, node_limit: int = 600_000):
        self.g = guide
        self.device = guide.device
        self.cap = guide.device.cache_capacity
        self.node_limit = node_limit
        self.ok: dict[tuple[str, int, frozenset[str]], bool] = {}
        self.fail: dict[tuple[str, int, frozenset[str]], dict[str, Any]] = {}
        self.actions: dict[tuple[str, int, frozenset[str]], Action] = {}
        self.reveal_drops: dict[tuple[str, str, frozenset[str]], tuple[str, ...]] = {}
        self.nodes = 0
        self._full_mode = False

    # ------------------------- 对外 ------------------------- #
    def plan(self) -> PlanResult:
        rm = self.g.route_map
        entrance = rm.entrance
        self._full_mode = (len(self.g.need_all(entrance))
                           <= self.FULL_SEARCH_PACKET_LIMIT)
        limited = False
        # 开门前可使用入口合同写明的预装额度：从空缓存先装若干整包。
        preload = self._plan_preload(entrance)
        try:
            feasible = self._solve_hall(entrance, 0, frozenset(preload))
        except _SearchLimit:
            feasible = False
            limited = True
        cert = None if feasible else self._best_certificate()
        if limited and cert is None:
            cert = {"kind": "search_limit", "hall": entrance,
                    "detail": f"搜索节点超过上限 {self.node_limit}"}
        # 可行性结论是否"精确"：
        #  - 可行：策略本身即见证（对全部状态逐路/符号验证通过），恒为精确。
        #  - 完整搜索：不可行也精确。
        #  - frontier 模式不可行：frontier 是任何可行最终缓存的必需子集且与历史
        #    缓存无关，故 |frontier|>容量 为确凿证明；must_transmit>额度 依赖
        #    具体历史缓存，保守地不标精确。
        exact = feasible or self._full_mode
        if (not feasible and cert is not None
                and cert.get("reason") == "capacity"):
            exact = True
        stats = {
            "nodes_explored": self.nodes,
            "search_mode": "full" if self._full_mode else "frontier",
            "feasibility_exact": exact,
            "route_count": rm.route_count(),
            "distinct_packets": len(self.g.store),
            "cache_capacity": self.cap,
            "preload_budget": rm.preload_budget,
        }
        return PlanResult(feasible, self.device, self.actions,
                          self.reveal_drops, cert, stats,
                          initial_preload=tuple(sorted(preload)))

    def _plan_preload(self, entrance: str) -> frozenset[str]:
        """入口预装：额度内优先装"入库即需"的整包（确定性排序）。"""
        budget = self.g.route_map.preload_budget
        if budget <= 0:
            return frozenset()
        budget = min(budget, self.cap)  # 预装也受缓存容量约束
        need = self.g.entry_req(entrance)
        ranked = _rank_by_urgency(need, entrance, self.g)
        return frozenset(ranked[:budget])

    # ------------------------- 搜索 ------------------------- #
    @staticmethod
    def _key(hall: str, wi: int, cache: frozenset[str]):
        return (hall, wi, cache)

    def _solve_hall(self, hall: str, wi: int, cache: frozenset[str]) -> bool:
        key = self._key(hall, wi, cache)
        if key in self.ok:
            return self.ok[key]
        self.nodes += 1
        if self.nodes > self.node_limit:
            raise _SearchLimit()

        h = self.g.route_map.halls[hall]
        if wi >= len(h.windows):
            ok = self._at_reveal(hall, cache, key)
            self.ok[key] = ok
            return ok

        w = h.windows[wi]
        if isinstance(w, PlayWindow):
            req = self.g.window_required[(hall, wi)]
            if not req <= cache:
                self.ok[key] = False
                self.fail[key] = {
                    "kind": "play_missing",
                    "hall": hall,
                    "window": wi,
                    "missing_packets": sorted(req - cache),
                    "cache": sorted(cache),
                    "detail": "轮到播放时所需整包尚未就绪",
                }
                return False
            ok = self._solve_hall(hall, wi + 1, cache)
            self.ok[key] = ok
            if not ok:
                self.fail[key] = self.fail.get(
                    (hall, wi + 1, cache), _unknown_failure(hall))
            return ok

        assert isinstance(w, TransmitWindow)
        ok = self._solve_transmit(hall, wi, cache, w.budget, key)
        self.ok[key] = ok
        return ok

    def _at_reveal(self, hall: str, cache: frozenset[str], key) -> bool:
        succ = self.g.route_map.halls[hall].successors
        if not succ:
            return True
        # 揭晓前缓存必须已能支持"任一所选分支"的入库需求：分支未知，不能
        # 假定走哪条。揭晓后才可按选定分支丢弃其子树永不需要的包。
        failed = []
        for s in succ:
            req = self.g.entry_req(s)
            cache_s = frozenset(cache & self.g.need_all(s))  # 揭晓后安全丢弃
            if not req <= cache_s:
                failed.append(s)
                self.fail.setdefault(
                    self._key(s, 0, cache_s),
                    {"kind": "entry_missing", "hall": s,
                     "missing_packets": sorted(req - cache_s),
                     "cache": sorted(cache_s)})
                continue
            if not self._solve_hall(s, 0, cache_s):
                failed.append(s)
        if not failed:
            for s in succ:
                need = self.g.need_all(s)
                drop = tuple(sorted(cache - need))
                if drop:
                    self.reveal_drops[(hall, s, cache)] = drop
            return True

        groups = {s: sorted(self.g.entry_req(s)) for s in succ}
        mandatory = set().union(*(self.g.entry_req(s) for s in succ))
        reason = "capacity" if len(mandatory) > self.cap else "descendant"
        self.fail[key] = {
            "kind": "branch_conflict",
            "hall": hall,
            "reason": reason,
            "cache_capacity": self.cap,
            "cache_at_reveal": sorted(cache),
            "branches": groups,
            "union_required": sorted(mandatory),
            "union_size": len(mandatory),
            "failed_successors": failed,
            "detail": (
                f"在 {hall} 揭晓分支时，覆盖所有分支的入库需求并集为 "
                f"{len(mandatory)} 个整包"
                + (f"，超过缓存容量 {self.cap}" if len(mandatory) > self.cap
                   else "，但存在分支无法从其后续合同继续")
            ),
            "child_failures": {
                s: self.fail.get(self._key(s, 0,
                                           frozenset(cache & self.g.need_all(s))))
                for s in failed
                if self.fail.get(self._key(s, 0,
                                           frozenset(cache & self.g.need_all(s))))
            },
        }
        return False

    def _solve_transmit(self, hall: str, wi: int, cache: frozenset[str],
                        budget: int, key) -> bool:
        g = self.g
        frontier = g.after_transmit_req(hall, wi)
        mandatory_add = frontier - cache

        if len(frontier) > self.cap:
            self.fail[key] = self._conflict(
                hall, wi, cache, budget, frontier, reason="capacity",
                detail=(f"下一次可传输之前必须同时驻留 {len(frontier)} 个整包，"
                        f"超过缓存容量 {self.cap}；无论本窗口怎么传都无法承诺所有分支"))
            return False
        if len(mandatory_add) > budget:
            self.fail[key] = self._conflict(
                hall, wi, cache, budget, frontier, reason="transmit_budget",
                detail=(f"覆盖所有分支需在本窗口补传 {len(mandatory_add)} 个整包，"
                        f"超过传输额度 {budget}"))
            return False

        universe = g.need_all(hall)
        extras_pool = universe - cache - frontier

        # 候选（按确定性次序）：frontier 必留；extras 与保留旧包按需枚举。
        if self._full_mode:
            max_extra = min(budget - len(mandatory_add),
                            max(0, self.cap - len(frontier)))
            extra_choices = _subsets_up_to(sorted(extras_pool), max_extra)
            keep_pool = sorted(cache - frontier)
            keep_choices = _subsets_up_to(
                keep_pool, max(0, self.cap - len(frontier)))
        else:
            # frontier 模式：除"只装必要"外，再给一个"尽量预装"的贪心候选，
            # 以覆盖连续传输窗口里本窗口额度更大、需提前囤包的合法情形。
            extra_choices = [()]
            max_extra = min(budget - len(mandatory_add),
                            max(0, self.cap - len(frontier)))
            if max_extra > 0 and extras_pool:
                greedy = _greedy_preload(
                    extras_pool, hall, wi, self.g, max_extra)
                if greedy:
                    extra_choices.append(greedy)
            # 只保留与后续相关、且装得下的旧包，确定性地择优
            keep_pool = sorted((cache & universe) - frontier)
            room = max(0, self.cap - len(frontier))
            keep_choices = [tuple(keep_pool[:room])]

        for extras in extra_choices:
            base = set(frontier) | set(extras)
            if len(base) > self.cap:
                continue
            room = self.cap - len(base)
            for keep in keep_choices:
                if len(keep) > room:
                    continue
                final = frozenset(base | set(keep))
                transmit = tuple(sorted(final - cache))
                if len(transmit) > budget:
                    continue
                drop = tuple(sorted(cache - final))
                if self._solve_hall(hall, wi + 1, final):
                    self.actions[key] = Action(transmit=transmit, drop=drop)
                    return True

        self.fail[key] = self._conflict(
            hall, wi, cache, budget, frontier, reason="exhausted",
            detail="所有可行的补传/丢弃组合都无法保障后续全部分支")
        return False

    def _conflict(self, hall: str, wi: int, cache: frozenset[str],
                  budget: int, frontier: frozenset[str],
                  reason: str, detail: str) -> dict[str, Any]:
        by_succ = {s: sorted(self.g.entry_req(s))
                   for s in self.g.route_map.halls[hall].successors}
        return {
            "kind": "branch_conflict",
            "hall": hall,
            "transmit_window": wi,
            "reason": reason,
            "cache_capacity": self.cap,
            "transmit_budget": budget,
            "cache_before": sorted(cache),
            "frontier_required": sorted(frontier),
            "must_transmit": sorted(frontier - cache),
            "branches": by_succ,
            "detail": detail,
        }

    def _best_certificate(self) -> dict[str, Any]:
        priority = {"capacity": 0, "transmit_budget": 0,
                    "branch_conflict": 1, "entry_missing": 1}
        best, best_score = None, None
        for f in self.fail.values():
            if not isinstance(f, dict):
                continue
            score = (priority.get(f.get("reason"), 2),
                     0 if f.get("reason") in ("capacity", "transmit_budget") else 1,
                     -len(f.get("union_required",
                                f.get("frontier_required", []))))
            if best_score is None or score < best_score:
                best_score, best = score, f
        return best if best is not None else {
            "kind": "unknown", "detail": "无可行策略，且未收集到具体证据"}


def _unknown_failure(hall: str) -> dict[str, Any]:
    return {"kind": "unknown", "hall": hall, "detail": "子状态不可行"}


def _rank_by_urgency(pool: Iterable[str], hall: str,
                     guide: CompiledGuide) -> list[str]:
    """按沿路线首次被需要的先后给整包排序（BFS 遍历合同窗口）。"""
    order: dict[str, int] = {}
    counter = 0
    queue = [hall]
    visited: set[str] = set()
    while queue:
        h = queue.pop(0)
        if h in visited:
            continue
        visited.add(h)
        for j, w in enumerate(guide.route_map.halls[h].windows):
            if isinstance(w, PlayWindow):
                for pid in guide.window_packets.get((h, j), ()):
                    if pid not in order:
                        order[pid] = counter
                        counter += 1
        queue.extend(guide.route_map.halls[h].successors)
    pool = list(pool)
    return sorted(pool, key=lambda p: (order.get(p, 1 << 30), p))


def _greedy_preload(extras_pool: set[str], hall: str, wi: int,
                    guide: CompiledGuide, max_extra: int) -> tuple[str, ...]:
    """确定性贪心预装：按"最早被需要"给 extras 排序，取前 max_extra 个。"""
    if max_extra <= 0 or not extras_pool:
        return ()
    order: dict[str, int] = {}
    seen = 0

    def visit(h: str, depth: int) -> None:
        nonlocal seen
        if depth > 64:
            return
        for j, w in enumerate(guide.route_map.halls[h].windows):
            if isinstance(w, PlayWindow):
                for pid in guide.window_packets.get((h, j), ()):
                    if pid in extras_pool and pid not in order:
                        order[pid] = seen
                        seen += 1
        for s in guide.route_map.halls[h].successors:
            visit(s, depth + 1)

    visit(hall, 0)
    ranked = sorted(extras_pool, key=lambda p: (order.get(p, 1 << 30), p))
    return tuple(ranked[:max_extra])


def _subsets_up_to(items: list[str], k: int) -> list[tuple[str, ...]]:
    """大小 0..k 的子集，按大小升序；过大时只给少量确定性候选。"""
    if k <= 0:
        return [()]
    if len(items) > 16:
        return [(), tuple(items[:k])]
    out: list[tuple[str, ...]] = []
    for r in range(0, min(k, len(items)) + 1):
        out.extend(itertools.combinations(items, r))
    return out


def verify_plan_symbolic(guide: CompiledGuide, plan: PlanResult,
                         state_limit: int = 2_000_000) -> dict[str, Any]:
    """不枚举完整路线，只在 ``(展厅, 窗口号, 缓存)`` 状态空间上验证策略。

    从入口空缓存出发，按策略的确定动作推进；每个展厅末尾对所有后继分支
    分别应用揭晓丢弃并继续。由于同一状态只访问一次，状态数远小于路线数，
    因而能覆盖四五十厅交叉汇合的大图而不复制任何整包字节。任何播放缺包、
    超额度、超容量都会抛错。
    """
    rm = guide.route_map
    cap = guide.device.cache_capacity
    seen: set[tuple[str, int, frozenset[str]]] = set()
    stack = [(rm.entrance, 0, frozenset(plan.initial_preload))]
    plays = 0
    transmissions = 0
    max_cache = 0
    while stack:
        if len(seen) > state_limit:
            raise RuntimeError(f"状态数超过上限 {state_limit}")
        hall, wi, cache = stack.pop()
        key = (hall, wi, cache)
        if key in seen:
            continue
        seen.add(key)
        max_cache = max(max_cache, len(cache))
        h = rm.halls[hall]
        if wi >= len(h.windows):
            succ = h.successors
            if not succ:
                continue
            for s in succ:
                need = guide.need_all(s)
                cache_s = frozenset(cache & need)
                stack.append((s, 0, cache_s))
            continue
        w = h.windows[wi]
        if isinstance(w, PlayWindow):
            req = guide.window_required[(hall, wi)]
            missing = req - cache
            if missing:
                raise AssertionError(
                    f"策略在 {hall} 窗口 {wi} 缺包 {sorted(missing)}")
            plays += 1
            stack.append((hall, wi + 1, cache))
        else:
            act = plan.actions.get(key)
            if act is None:
                raise AssertionError(
                    f"策略在 {hall} 窗口 {wi} 无动作 (cache={sorted(cache)})")
            if len(act.transmit) > w.budget:
                raise AssertionError(f"{hall} 窗口 {wi} 超过传输额度 {w.budget}")
            for pid in act.transmit:
                if not guide.store.contains(pid):
                    raise AssertionError(f"策略传入未编译包 {pid}")
            nxt = (set(cache) - set(act.drop)) | set(act.transmit)
            if len(nxt) > cap:
                raise AssertionError(f"{hall} 窗口 {wi} 超过缓存容量 {cap}")
            transmissions += 1
            stack.append((hall, wi + 1, frozenset(nxt)))
    return {
        "states": len(seen),
        "play_windows_checked": plays,
        "transmit_windows_checked": transmissions,
        "max_cache_occupancy": max_cache,
        "cache_capacity": cap,
    }


# ============================ 逐路回放 / 收听记录 ============================ #

def simulate_path(guide: CompiledGuide, plan: PlanResult,
                  path: list[str]) -> dict[str, Any]:
    """给定一条具体路线（展厅 id 序列），按策略确定性地回放。

    验证每个播放窗口所需整包均就绪、传输额度与缓存容量均满足，并返回
    包含真实整包字节引用与逐段播放范围的收听记录。
    """
    rm = guide.route_map
    if not path or path[0] != rm.entrance:
        raise ValueError(f"路线必须以入口 {rm.entrance} 开始：{path}")
    cache: set[str] = set(plan.initial_preload)
    steps: list[dict[str, Any]] = []
    played_packets: list[str] = []
    total_bytes = 0
    if plan.initial_preload:
        steps.append({
            "type": "preload", "transmitted": list(plan.initial_preload),
            "budget": rm.preload_budget, "cache_after": sorted(cache),
        })

    for idx, hall_id in enumerate(path):
        if hall_id not in rm.halls:
            raise ValueError(f"未知展厅 {hall_id}")
        if idx > 0 and hall_id not in rm.halls[path[idx - 1]].successors:
            raise ValueError(f"{path[idx - 1]} 没有通向 {hall_id} 的后继")
        h = rm.halls[hall_id]
        for wi, w in enumerate(h.windows):
            if isinstance(w, PlayWindow):
                req = guide.window_required[(hall_id, wi)]
                missing = req - cache
                if missing:
                    raise AssertionError(
                        f"路线 {path} 在 {hall_id} 窗口 {wi} 缺包 {sorted(missing)}")
                order = guide.window_packets[(hall_id, wi)]
                pieces = []
                for si, seg in enumerate(w.segments):
                    info = guide.segment_info[(hall_id, wi, si)]
                    pieces.append({
                        "segment": seg.to_json(),
                        "output_sample_range": info["output_sample_range"],
                        "output_samples": info["output_samples"],
                        "asset_rate": info["asset_rate"],
                        "packets": info["packets"],
                        "packet_spans": info["packet_spans"],
                    })
                for pid in order:
                    total_bytes += guide.store.get(pid).nbytes
                played_packets.extend(order)
                steps.append({
                    "hall": hall_id, "window": wi, "type": "play",
                    "packet_order": list(order),
                    "segments": pieces,
                })
            else:
                key = Planner._key(hall_id, wi, frozenset(cache))
                act = plan.actions.get(key)
                if act is None:
                    raise AssertionError(
                        f"路线 {path} 在 {hall_id} 窗口 {wi} 缺少策略动作 (cache={sorted(cache)})")
                if len(act.transmit) > w.budget:
                    raise AssertionError(f"{hall_id} 窗口 {wi} 传输超额度")
                cache.difference_update(act.drop)
                for pid in act.transmit:
                    if not guide.store.contains(pid):
                        raise AssertionError(f"策略要求传入未编译的包 {pid}")
                    cache.add(pid)
                if len(cache) > guide.device.cache_capacity:
                    raise AssertionError(f"{hall_id} 窗口 {wi} 缓存超容量")
                steps.append({
                    "hall": hall_id, "window": wi, "type": "transmit",
                    "budget": w.budget,
                    "transmit": list(act.transmit),
                    "drop": list(act.drop),
                    "cache_after": sorted(cache),
                })
        # 揭晓下一厅
        if idx + 1 < len(path):
            nxt = path[idx + 1]
            cache_before = sorted(cache)
            drop = plan.reveal_drops.get(
                (hall_id, nxt, frozenset(cache)), ())
            cache.difference_update(drop)
            steps.append({
                "hall": hall_id, "type": "reveal", "next": nxt,
                "cache_before_drop": cache_before,
                "drop_unneeded": list(drop), "cache_after": sorted(cache),
            })
        else:
            steps.append({"hall": hall_id, "type": "end",
                          "cache_after": sorted(cache)})

    return {
        "path": path,
        "device": guide.device.to_json(),
        "steps": steps,
        "played_packets": played_packets,
        "unique_played_packets": sorted(set(played_packets)),
        "total_played_bytes": total_bytes,
        "final_cache": sorted(cache),
    }


def summarize_all_routes(guide: CompiledGuide, plan: PlanResult,
                         limit: int = 100_000) -> dict[str, Any]:
    """不逐条存声音，只按路线核对策略并把"实际传了哪些包"汇总成统计。

    对每条完整路线用 :func:`simulate_path` 回放，只保留计数/字节/包集合，
    不复制整包字节（字节在共享包仓里单份存在）。
    """
    routes = guide.route_map.enumerate_routes(limit=limit)
    per_route = []
    for path in routes:
        rec = simulate_path(guide, plan, path)
        transmitted: set[str] = set()
        for s in rec["steps"]:
            if s["type"] == "transmit":
                transmitted.update(s["transmit"])
        per_route.append({
            "route": "/".join(path),
            "transmitted_packets": len(transmitted),
            "played_packets": len(rec["played_packets"]),
            "total_played_bytes": rec["total_played_bytes"],
            "final_cache": rec["final_cache"],
        })
    return {
        "route_count": len(routes),
        "distinct_packets": len(guide.store),
        "distinct_packet_bytes": guide.store.total_bytes(),
        "per_route": per_route,
    }


def compare_profiles(route_map: RouteMap, lib: ProductLibrary,
                     profiles: Iterable[DeviceProfile],
                     node_limit: int = 600_000) -> list[dict[str, Any]]:
    """比较两种（或更多）硬件租用条件下是否有可承诺的无停顿策略。

    结果对每个 profile 给出是否可行、冲突证据、包仓字节与策略规模，
    供主办方在"展厅是否开放"前比较。
    """
    out = []
    for dev in profiles:
        store = PacketStore()
        guide = compile_guide(route_map, lib, dev, store=store)
        plan = Planner(guide, node_limit=node_limit).plan()
        entry_need = guide.entry_req(route_map.entrance)
        out.append({
            "device": dev.to_json(),
            "feasible": plan.feasible,
            "runtime_stall_free": plan.feasible,
            "certificate": plan.certificate,
            "distinct_packets": len(store),
            "packet_bytes": store.total_bytes(),
            "entry_preload_required": len(entry_need),
            "stats": plan.stats,
        })
    return out
