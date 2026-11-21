"""规模导览案例：四十多个展厅交叉汇合，用真实短 WAV 生成设备整包复演。

覆盖要求：
- 路线图有限无环，一个入口多厅、多路邓汇合；
- 每段引用 (产品版, clip 实例, 源样本区间)，按原项目速度/裁边换算播放范围；
- 按设备固定包长切整包，异厅共享同一译制版本与字节 -> 内容寻址复用；
- 用 AND-OR 规划给出非预知装载策略，并**符号验证**全部路线；
  不逐条保存完整游览线路的声音（包仓里每包只存一份）；
- 比较两种硬件租用条件，输出无停顿路线可行性结论。

为避免对指数多条完整路线逐条存声音，规划与验证都在
``(展厅, 窗口号, 缓存)`` 状态空间上进行；路线条数只用 DP 计数。
"""
from __future__ import annotations

import json
import os
import shutil
import sys

import numpy as np

from audiomix.assets import AssetRegistry, ramp, tone
from audiomix.guide import (
    DeviceProfile, Hall, PlayWindow, Planner, RouteMap, TransmitWindow,
    compare_profiles, compile_guide, simulate_path, summarize_all_routes,
    verify_plan_symbolic,
)
from audiomix.packets import PacketStore, ProductLibrary, Segment
from audiomix.timeline import TimelineEditor

BASE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/audiomix_guide_scale"
RNG = np.random.default_rng(20261121)


# ---------------------------------------------------------------- 素材/产品 ----

def build_products(base: str):
    """3 个译制版本产品，各含若干 clip；返回 (lib, [(pid, [clip_id...])])."""
    reg = AssetRegistry(os.path.join(base, "registry"))
    lib = ProductLibrary()
    versions = []
    for v in range(3):
        assets = []
        for i in range(4):
            rate = (8000, 16000, 48000)[i % 3]
            if i % 2 == 0:
                pcm = ramp(rate, n=600 + 50 * i, start=-15000 + v * 500,
                           stop=15000 - i * 300)
            else:
                pcm = tone(rate, freq=180.0 + 37 * i + 11 * v, seconds=0.04,
                           amp=0.25 + 0.1 * i)
            assets.append(reg.register(
                pcm, rate, note=f"v{v}-asset{i}").asset_id)
        ed = TimelineEditor(os.path.join(base, "registry"), reg,
                            name=f"version{v}")
        specs = []
        for c in range(6):
            aid = assets[c % len(assets)]
            rec = reg.get(aid)
            a = 0
            b = min(rec.sample_count, 240 + 20 * c)
            start = str(c)  # 同产品内 clip 不重叠，便于按段裁取
            speed = ["1", "1", "2", "1/2", "3/2", "1"][c]
            specs.append({"clip_id": f"v{v}-clip{c}", "asset_id": aid,
                          "a": a, "b": b, "start": start, "speed": speed,
                          "gain": ["1", "1/2", "3/2", "1"][c % 4]})
        ed.insert(specs)
        prod = ed.publish()
        pcm = {a["asset_id"]: reg.load_pcm(a["asset_id"])
               for a in prod["assets"]}
        pid = lib.add(prod, pcm)
        versions.append((pid, [s["clip_id"] for s in specs]))
    return lib, versions


# ---------------------------------------------------------------- 路线图 ----

def build_route_map(versions):
    """分层 DAG：入口 -> 3 层（每层多厅）-> 汇合大厅。

    每个展厅合同为 [Play(首段), Transmit(K), Play(次段)]：
    首段需在入厅前已随设备带来（无厅内传输可依赖），次段可厅内补传。
    多厅共享同一段引用 => 物理包复用；多路在后面汇合。
    """
    pids = [v[0] for v in versions]
    clips = {v[0]: v[1] for v in versions}

    def seg(version_i, clip_i, src_a, src_b):
        pid = pids[version_i]
        return Segment(pid, clips[pid][clip_i], src_a, src_b)

    # 依据层/列确定性地挑段，让部分展厅重复引用同一 (版本,clip,区间)
    def first_seg(layer, col):
        vi = (layer + col) % 3
        ci = (layer * 2 + col) % 6
        a = 5 + (col % 3) * 7
        return seg(vi, ci, a, a + 60)

    def second_seg(layer, col):
        vi = (layer + 1 + col) % 3
        ci = (layer + 3 + col) % 6
        a = 10 + (col % 2) * 11
        return seg(vi, ci, a, a + 55)

    # 共享"地标"段落：每隔几个厅都用到同一段，制造跨厅复用
    landmark = seg(0, 0, 0, 80)

    halls: dict[str, Hall] = {}
    K = 8  # 厅内传输额度（足够大，使瓶颈落在缓存容量而非额度）

    layers = [
        ["L0"],  # 入口
        ["L1a", "L1b", "L1c", "L1d", "L1e", "L1f"],
        ["L2a", "L2b", "L2c", "L2d", "L2e", "L2f", "L2g", "L2h"],
        ["L3a", "L3b", "L3c", "L3d", "L3e", "L3f", "L3g"],
        ["L4a", "L4b", "L4c", "L4d", "L4e", "L4f", "L4g", "L4h"],
        ["L5a", "L5b", "L5c", "L5d", "L5e", "L5f"],
        ["M0"],  # 汇合
        ["L6a", "L6b", "L6c", "L6d"],
        ["L7a", "L7b", "L7c"],
        ["N0"],  # 终点
    ]
    # 1 + 6+8+7+8+6+1+4+3+1 = 45 个展厅

    # 入口首段：地标段；之后可补传
    halls["L0"] = Hall("L0",
                       [PlayWindow([landmark]),
                        TransmitWindow(K),
                        PlayWindow([first_seg(0, 0)])],
                       successors=layers[1])

    # L1..L5 通用分层：交叉边连下一层相邻两厅（天然汇合）
    for li in range(1, 6):
        layer = layers[li]
        nxt = layers[li + 1]
        for ci, name in enumerate(layer):
            firsts = [first_seg(li, ci)]
            if (li + ci) % 3 == 0:
                firsts.append(landmark)      # 偶发地标复用
            seconds = [second_seg(li, ci)]
            succ = tuple(dict.fromkeys(
                nxt[(ci + d) % len(nxt)] for d in range(2)))
            halls[name] = Hall(name,
                               [PlayWindow(firsts),
                                TransmitWindow(K),
                                PlayWindow(seconds)],
                               successors=succ)

    # 汇合厅 M0 接收上一层全部（L5 各厅都指向 M0）
    halls["M0"] = Hall("M0",
                       [PlayWindow([landmark, first_seg(3, 0)]),
                        TransmitWindow(K),
                        PlayWindow([second_seg(3, 1)])],
                       successors=layers[7])
    for name in layers[5]:
        halls[name] = Hall(name, halls[name].windows, successors=("M0",))

    # M0 之后再有若干层并最终汇合到 N0
    for li in range(7, 9):
        layer, nxt = layers[li], layers[li + 1]
        for ci, name in enumerate(layer):
            succ = (nxt[ci % len(nxt)],) if len(nxt) == 1 else tuple(
                dict.fromkeys(nxt[(ci + d) % len(nxt)] for d in range(2)))
            halls[name] = Hall(name,
                               [PlayWindow([first_seg(li, ci)]),
                                TransmitWindow(K),
                                PlayWindow([second_seg(li, ci)])],
                               successors=succ)
    halls["N0"] = Hall("N0",
                       [PlayWindow([landmark, second_seg(5, 0)])])

    return RouteMap("L0", halls, preload_budget=8)


# ---------------------------------------------------------------- 主流程 ----

def main() -> None:
    shutil.rmtree(BASE, ignore_errors=True)
    os.makedirs(BASE, exist_ok=True)
    lib, versions = build_products(BASE)
    rm = build_route_map(versions)
    rm.save(os.path.join(BASE, "route_map.json"))

    nc = len(rm.halls)
    nr = rm.route_count()
    print(f"路线图：{nc} 个展厅、{nr} 条完整入口路线（DP 计数，不逐条展开）")

    # ---- 两种硬件租用条件比较 ---- #
    tight = DeviceProfile(packet_samples=1600, cache_capacity=7, name="租用-小缓存")
    roomy = DeviceProfile(packet_samples=1600, cache_capacity=8, name="租用-大缓存")
    cmp = compare_profiles(rm, lib, [tight, roomy])
    for c in cmp:
        print(f"  {c['device']['name']}: feasible={c['feasible']} "
              f"包数={c['distinct_packets']} 字节={c['packet_bytes']} "
              f"入口预装需求={c['entry_preload_required']}")

    # 在宽松条件下完整编译 + 规划 + 符号验证
    store = PacketStore()
    guide = compile_guide(rm, lib, roomy, store=store)
    plan = Planner(guide).plan()
    assert plan.feasible, plan.to_json()
    verify = verify_plan_symbolic(guide, plan)
    print(f"[OK] 大缓存策略可行；符号验证 {verify['states']} 个状态 / "
          f"{verify['play_windows_checked']} 播放窗 / "
          f"{verify['transmit_windows_checked']} 传输窗，"
          f"峰值占用 {verify['max_cache_occupancy']}≤{roomy.cache_capacity}")

    # 内容寻址复用：物理包数应远小于"各厅段数之和"
    total_segments = sum(
        1 for h in rm.halls.values() for w in h.windows
        if isinstance(w, PlayWindow) for _ in w.segments)
    print(f"[OK] {total_segments} 处段引用编译为 {len(store)} 个物理整包，"
          f"共 {store.total_bytes()} 字节（每包仅存一份）")

    # ---- 真实包回放：抽样少量路线，读真实字节 & 导出 WAV ---- #
    routes = rm.enumerate_routes(limit=2000)
    sample_paths = [routes[0], routes[len(routes) // 2], routes[-1]]
    import wave
    for i, path in enumerate(sample_paths):
        rec = simulate_path(guide, plan, path)
        pcm = store.concatenate(rec["played_packets"])
        wav = os.path.join(BASE, f"route_{i}.wav")
        with wave.open(wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm.astype("<i2").tobytes())
        print(f"[OK] 回放路线 {i}: {'/'.join(path)} -> {len(pcm)} 样本，"
              f"WAV={os.path.basename(wav)}")
    # 全路线汇总（不存声音）
    summary = summarize_all_routes(guide, plan, limit=5000)
    print(f"[OK] 全 {summary['route_count']} 条路线逐路核对通过；"
          f"仅需 {summary['distinct_packets']} 个共享包")

    # ---- 检查：小缓存确实因分支承诺冲突而不可行（给出证据） ---- #
    denied = cmp[0]
    assert not denied["feasible"]
    cert = denied["certificate"]
    print(f"[OK] 小缓存不可行证据：展厅 {cert['hall']} "
          f"reason={cert['reason']}，需同时驻留 "
          f"{len(cert.get('frontier_required', cert.get('union_required', [])))} 包 "
          f"> 容量 {denied['device']['cache_capacity']}")
    with open(os.path.join(BASE, "comparison.json"), "w", encoding="utf-8") as fh:
        json.dump({"route_map": rm.to_json(), "comparison": cmp,
                   "verify": verify,
                   "segments": total_segments,
                   "distinct_packets": len(store)},
                  fh, ensure_ascii=False, indent=2)
    print(f"\n规模导览案例通过。输出目录：{BASE}")


if __name__ == "__main__":
    main()
