"""导览编译与装载规划测试：直接复刻题目里的 B/C 入口、D 汇合等手算场景。

每个场景都用真实短 WAV 素材经 audiomix 时间轴 -> 产品 -> 实际混音渲染
-> 设备整包，验证范围查询、字节、复用与规划证据。
"""
import shutil
from fractions import Fraction as F

import numpy as np

from audiomix.assets import AssetRegistry, sequence
from audiomix.guide import (
    DeviceProfile, Hall, Planner, PlayWindow as PW, RouteMap,
    TransmitWindow as TW, compile_guide, simulate_path,
    summarize_all_routes, verify_plan_symbolic,
)
from audiomix.engine import render_reference, render_sample_range
from audiomix.packets import (
    PacketStore, ProductLibrary, Segment, SegmentError, packet_identity,
)
from audiomix.timeline import TimelineEditor


# ------------------------------------------------------------------ 工具 ----

class Env:
    """构造素材 / 时间轴 / 产品并登记进产品书库。"""

    def __init__(self, base):
        shutil.rmtree(base, ignore_errors=True)
        self.base = base
        self.reg = AssetRegistry(base)
        self.lib = ProductLibrary()

    def add_product(self, name, clips, assets):
        """assets: list[(values, rate)]；clips: list[spec(asset_idx,...)]。"""
        ed = TimelineEditor(self.base, self.reg, name=name)
        ids = [self.reg.register(sequence(vals), rate,
                                 note=f"{name}-{i}").asset_id
               for i, (vals, rate) in enumerate(assets)]
        specs = []
        for c in clips:
            s = dict(c)
            s["asset_id"] = ids[s.pop("asset_idx")]
            specs.append(s)
        ed.insert(specs)
        prod = ed.publish()
        pcm = {a["asset_id"]: self.reg.load_pcm(a["asset_id"])
               for a in prod["assets"]}
        pid = self.lib.add(prod, pcm)
        return pid, ids


def _seg(pid, clip_id, a, b):
    return Segment(pid, clip_id, a, b)


def _hall(hid, windows, succ=()):
    return Hall(hid, windows, succ)


# --------------------------------------------------------- 范围查询/字节 ----

def test_segment_resolution_and_bytes(base="/tmp/ag_seg"):
    """段范围按原项目速度/裁边换算；实际字节来自真实混音。"""
    env = Env(base)
    # 16k 序列 0..7，clip [2,6) 从输出 0 开始、2 倍速
    pid, _ = env.add_product("p", [
        {"clip_id": "c1", "asset_idx": 0, "a": 2, "b": 6, "start": "0",
         "speed": "2"},
    ], [([0, 100, 200, 300, 400, 500, 600, 700], 16000)])
    seg = _seg(pid, "c1", 3, 5)
    info = seg.resolve(env.lib)
    # p=3 -> t=(3-2)/(16000*2)=1/32000 -> n0=ceil(16000/32000)=1
    # p=5 -> t=3/32000 -> n1=ceil(16000*3/32000)=ceil(3/2)=2
    assert info["output_sample_range"] == [1, 2], info
    store = PacketStore()
    pkts = seg.compile(env.lib, 1600, store)
    assert len(pkts) == 1 and pkts[0].length == 1
    # 2 倍速在 n=1（t=1/16000）取源位置 p = 2 + 16000*2/16000 = 4 -> 样本 400
    assert pkts[0].samples().tolist() == [400], pkts[0].samples().tolist()
    # 包身份与内联 PCM 字节一致
    assert pkts[0].packet_id == packet_identity(pid, pkts[0].pcm)
    assert store.read_range(pkts[0].packet_id, 0, 1).tolist() == [400]
    print("[OK] 段播放范围与真实 PCM 字节（2 倍速 + 源区间裁边）")

    # 不在该 clip 内的源范围必须拒绝，不能拿同素材的其它实例顶替
    pid2, _ = env.add_product("p2", [
        {"clip_id": "c1", "asset_idx": 0, "a": 2, "b": 6, "start": "0"},
        {"clip_id": "c2", "asset_idx": 0, "a": 0, "b": 8, "start": "1"},
    ], [([0, 100, 200, 300, 400, 500, 600, 700], 16000)])
    try:
        # [0,2) 在 c2 内但不在 c1 内：不许借用 c2
        _seg(pid2, "c1", 0, 2).resolve(env.lib)
        raise AssertionError("应拒绝 clip 外源区间")
    except SegmentError as e:
        assert "不在 clip" in str(e)
    print("[OK] 源范围不在该 clip 内 -> 拒绝，不拿别的实例顶替")

    # 同一包在不同译制版本下不可复用（版本参与身份）
    assert packet_identity("v1", b"\x00\x00") != packet_identity("v2", b"\x00\x00")
    # 同版本、同字节、同长度 -> 同一包（内容寻址复用）
    assert packet_identity("v1", b"\x01\x00") == packet_identity("v1", b"\x01\x00")
    print("[OK] 包复用仅当译制版本 + 字节 + 长度全一致")


# ------------------------------------------- B/C 分支：容量 1 不可行 ----

def _bc_route_map(pid, b_packet_seg, c_packet_seg):
    """入口问答后揭晓去 B 或 C；各自首包不同；门口不能补传。

    入口合同：传输窗口(budget=2) -> 播放(入口首段可以空) ...
    简化：入口只有一个传输窗口，然后揭晓分支；B/C 各有播放首包且无传输。
    """
    entrance = _hall("A", [TW(2)], succ=("B", "C"))
    b = _hall("B", [PW([b_packet_seg])])
    c = _hall("C", [PW([c_packet_seg])])
    return RouteMap("A", {"A": entrance, "B": b, "C": c})


def test_bc_capacity1_infeasible_capacity2_feasible(base="/tmp/ag_bc"):
    env = Env(base)
    # B 首包与 C 首包不同：两条独立产品版（译制版本不同 => 包必不同）
    pb, _ = env.add_product("b", [
        {"clip_id": "cb", "asset_idx": 0, "a": 0, "b": 2, "start": "0"},
    ], [([1000, 2000], 16000)])
    pc, _ = env.add_product("c", [
        {"clip_id": "cc", "asset_idx": 0, "a": 0, "b": 2, "start": "0"},
    ], [([3000, 4000], 16000)])
    rm = _bc_route_map(pb, _seg(pb, "cb", 0, 2), _seg(pc, "cc", 0, 2))

    # 容量 1：两条路各自都能排装载表，但入口传 2 包放不下
    d1 = DeviceProfile(packet_samples=1600, cache_capacity=1, name="cap1")
    g1 = compile_guide(rm, env.lib, d1)
    r1 = Planner(g1).plan()
    assert not r1.feasible, r1.to_json()
    cert = r1.certificate
    assert cert["reason"] == "capacity", cert
    assert cert["cache_capacity"] == 1
    assert len(cert["frontier_required"]) == 2
    assert set(cert["branches"]) == {"B", "C"}
    assert all(len(v) == 1 for v in cert["branches"].values())
    print("[OK] 容量1：入口须同时承诺 B/C 两首包 -> 冲突证据 (capacity, 需要2>1)")

    # 容量 2：可行，入口一次传两包
    d2 = DeviceProfile(packet_samples=1600, cache_capacity=2, name="cap2")
    g2 = compile_guide(rm, env.lib, d2)
    r2 = Planner(g2).plan()
    assert r2.feasible, r2.to_json()
    for path in (["A", "B"], ["A", "C"]):
        rec = simulate_path(g2, r2, path)
        tx = [s for s in rec["steps"] if s["type"] == "transmit"][0]
        assert len(tx["transmit"]) == 2, tx
    print("[OK] 容量2：入口一个动作传两包，两条路均可回放")

    # 容量 1 但 B/C 用同一译制版且首包真正相同 -> 可行
    ps, _ = env.add_product("shared", [
        {"clip_id": "cs", "asset_idx": 0, "a": 0, "b": 2, "start": "0"},
    ], [([1000, 2000], 16000)])
    # B、C 都引用同一产品版、同一 clip、同一源区间 => 真正同一包
    rm_same = _bc_route_map(ps, _seg(ps, "cs", 0, 2), _seg(ps, "cs", 0, 2))
    g_same = compile_guide(rm_same, env.lib, d1)
    r_same = Planner(g_same).plan()
    assert r_same.feasible, r_same.to_json()
    assert len(g_same.store) == 1, "两条分支应共用同一个物理包"
    for path in (["A", "B"], ["A", "C"]):
        simulate_path(g_same, r_same, path)
    print("[OK] 容量1 + 同译制版同首包 -> 复用同一包，可行")


# ---------------------------------------------- D 汇合：不同余包 ----

def test_merge_at_d_different_carry(base="/tmp/ag_merge"):
    """两条路在 D 汇合，各自带来不同余包；比较统一预装 vs 知情后装载。"""
    env = Env(base)
    # B 路带来 x 包；C 路带来 y 包；D 播放 z 包（汇合后共同需要）
    px, _ = env.add_product("x", [
        {"clip_id": "cx", "asset_idx": 0, "a": 0, "b": 2, "start": "0"}],
        [([1111, 1112], 16000)])
    py, _ = env.add_product("y", [
        {"clip_id": "cy", "asset_idx": 0, "a": 0, "b": 2, "start": "0"}],
        [([2221, 2222], 16000)])
    pz, _ = env.add_product("z", [
        {"clip_id": "cz", "asset_idx": 0, "a": 0, "b": 2, "start": "0"}],
        [([3331, 3332], 16000)])

    seg_x, seg_y, seg_z = (_seg(px, "cx", 0, 2), _seg(py, "cy", 0, 2),
                           _seg(pz, "cz", 0, 2))
    # A -> {B, C} -> D。A 入口能预装；B/C 播放各自余包，也各有一个传输窗口；
    # D 播放 z。
    A = _hall("A", [TW(3)], succ=("B", "C"))
    B = _hall("B", [PW([seg_x]), TW(1)], succ=("D",))
    C = _hall("C", [PW([seg_y]), TW(1)], succ=("D",))
    D = _hall("D", [PW([seg_z])])
    rm = RouteMap("A", {"A": A, "B": B, "C": C, "D": D})

    d = DeviceProfile(packet_samples=1600, cache_capacity=3, name="merge")
    g = compile_guide(rm, env.lib, d)
    # 3 个不同包（x,y,z）
    assert len(g.store) == 3
    plan = Planner(g).plan()
    assert plan.feasible, plan.to_json()

    rec_b = simulate_path(g, plan, ["A", "B", "D"])
    rec_c = simulate_path(g, plan, ["A", "C", "D"])
    tx_a = [s for s in rec_b["steps"] if s["type"] == "transmit"][0]
    # A 揭晓后，两条路保留的包不同：B 保留 x 丢 y，C 保留 y 丢 x。
    seg_x_pkt = g.segment_info[("B", 0, 0)]["packets"][0]
    seg_y_pkt = g.segment_info[("C", 0, 0)]["packets"][0]
    seg_z_pkt = g.segment_info[("D", 0, 0)]["packets"][0]
    reveal_b = [s for s in rec_b["steps"]
                if s["type"] == "reveal" and s["hall"] == "A"][0]
    reveal_c = [s for s in rec_c["steps"]
                if s["type"] == "reveal" and s["hall"] == "A"][0]
    keep_b, keep_c = set(reveal_b["cache_after"]), set(reveal_c["cache_after"])
    assert seg_x_pkt in keep_b and seg_y_pkt not in keep_b
    assert seg_y_pkt in keep_c and seg_x_pkt not in keep_c
    assert keep_b != keep_c
    # 入口统一预装 x,y；z 留到知道上一站后再各自补传（各 1 额度）。
    assert sorted(tx_a["transmit"]) == sorted([seg_x_pkt, seg_y_pkt])
    for rec in (rec_b, rec_c):
        tx = [s for s in rec["steps"]
              if s["type"] == "transmit" and s["hall"] in ("B", "C")][0]
        assert tx["transmit"] == [seg_z_pkt]
    print("  入口统一预装 x,y（2 包）；B/C 各知情后补传 z；"
          "B 路保留 x、C 路保留 y -> 两条来路携带不同")
    # 两条来路在 D 的物理包确实可以不同（x vs y 仍驻留，或已被丢弃）
    # 关键：策略必须可行且两条路都能播 z。
    for rec in (rec_b, rec_c):
        assert any(s["type"] == "play" and s["hall"] == "D" for s in rec["steps"])
    print("[OK] D 汇合：入口统一预装 + 知情后分路装载，两条路均可回放")

    # 对比：若 B/C 门口都禁止传输（去掉 TW），容量 3 仍可行（入口须预装 x,y,z）
    B2 = _hall("B", [PW([seg_x])], succ=("D",))
    C2 = _hall("C", [PW([seg_y])], succ=("D",))
    rm2 = RouteMap("A", {"A": A, "B": B2, "C": C2, "D": D})
    plan2 = Planner(compile_guide(rm2, env.lib, d)).plan()
    assert plan2.feasible, plan2.to_json()
    g2 = compile_guide(rm2, env.lib, d)
    p2 = Planner(g2).plan()
    rec2 = simulate_path(g2, p2, ["A", "B", "D"])
    tx_a2 = [s for s in rec2["steps"] if s["type"] == "transmit"][0]
    assert len(tx_a2["transmit"]) == 3, tx_a2
    print("[OK] 门口禁止补传：入口须统一预装 3 包（知情后分路装载免去 z）")


# ------------------------------------------- 非预知性：不许偷看分支 ----

def test_non_anticipatory(base="/tmp/ag_nona"):
    """揭晓前只做已观察历史决定的动作；分支揭晓后才按分支处理。"""
    env = Env(base)
    pb, _ = env.add_product("nb", [
        {"clip_id": "cb", "asset_idx": 0, "a": 0, "b": 2, "start": "0"}],
        [([500, 600], 16000)])
    pc, _ = env.add_product("nc", [
        {"clip_id": "cc", "asset_idx": 0, "a": 0, "b": 2, "start": "0"}],
        [([700, 800], 16000)])
    rm = _bc_route_map(pb, _seg(pb, "cb", 0, 2), _seg(pc, "cc", 0, 2))
    g = compile_guide(rm, env.lib,
                      DeviceProfile(1600, 2, "cap2"))
    plan = Planner(g).plan()
    key = ("A", 0, frozenset())
    act_b = plan.actions[key]
    # 入口只有一个动作，B/C 两条路回放时入口动作必须完全相同
    rec_b = simulate_path(g, plan, ["A", "B"])
    rec_c = simulate_path(g, plan, ["A", "C"])
    tx_b = [s for s in rec_b["steps"] if s["type"] == "transmit"][0]
    tx_c = [s for s in rec_c["steps"] if s["type"] == "transmit"][0]
    assert tx_b["transmit"] == tx_c["transmit"] == list(act_b.transmit)
    print("[OK] 非预知：入口同一已观察历史下动作唯一，不依赖后揭晓的 B/C")


def test_range_render_matches_reference(base="/tmp/ag_range"):
    """任意样本区间渲染 = 参考解释器同一区间（导览切包语义基准）。"""
    env = Env(base)
    pid, _ = env.add_product("rp", [
        {"clip_id": "c1", "asset_idx": 0, "a": 0, "b": 5, "start": "0",
         "speed": "1/2", "fade_in": "1/16000"},
        {"clip_id": "c2", "asset_idx": 1, "a": 0, "b": 4, "start": "1/8000",
         "gain": "2"},
    ], [([0, 100, 200, 300, 400], 8000), ([10, 20, 30, 40], 16000)])
    product, pcm = env.lib.get(pid)
    T = F(1, 500)
    ref = render_reference(product, pcm, T)
    for (lo, hi) in [(0, 1), (1, 3), (0, 8), (5, 12), (0, len(ref))]:
        got = render_sample_range(product, pcm, lo, hi)
        assert np.array_equal(got, ref[lo:hi]), (lo, hi, got, ref[lo:hi])
    print("[OK] 任意样本区间渲染与参考解释器逐样本一致")


def test_packet_reuse_rules(base="/tmp/ag_reuse"):
    """同版本同字节 -> 同包；版本不同 -> 不同包；长度不同 -> 不同包。"""
    store = PacketStore()
    p1 = store.add("v1", b"\x01\x00\x02\x00")
    p1b = store.add("v1", b"\x01\x00\x02\x00")
    p2 = store.add("v2", b"\x01\x00\x02\x00")   # 字节同、版本不同
    p3 = store.add("v1", b"\x01\x00")            # 版本同、长度不同
    assert p1.packet_id == p1b.packet_id
    assert len(store) == 3
    assert p2.packet_id != p1.packet_id and p3.packet_id != p1.packet_id
    print("[OK] 包复用严格按 版本+字节+长度；任一不同即不同包")


def test_all_routes_replay_and_symbolic_agree(base="/tmp/ag_all"):
    """所有路线逐路回放与符号验证一致，且共享包仓字节不随路线复制。"""
    env = Env(base)
    pa, _ = env.add_product("A", [
        {"clip_id": "ca", "asset_idx": 0, "a": 0, "b": 3, "start": "0"}],
        [([1, 2, 3], 16000)])
    pb, _ = env.add_product("B", [
        {"clip_id": "cb", "asset_idx": 0, "a": 0, "b": 3, "start": "0"}],
        [([4, 5, 6], 16000)])
    pc, _ = env.add_product("C", [
        {"clip_id": "cc", "asset_idx": 0, "a": 0, "b": 3, "start": "0"}],
        [([7, 8, 9], 16000)])
    sa, sb, sc = (_seg(pa, "ca", 0, 3), _seg(pb, "cb", 0, 3),
                  _seg(pc, "cc", 0, 3))
    # A -> {B,C} -> D（汇合），D 需要 sc
    rm = RouteMap("A", {
        "A": Hall("A", [PW([sa]), TW(2)], ("B", "C")),
        "B": Hall("B", [TW(1)], ("D",)),
        "C": Hall("C", [PW([sb]), TW(1)], ("D",)),
        "D": Hall("D", [PW([sc])]),
    }, preload_budget=1)
    g = compile_guide(rm, env.lib, DeviceProfile(1600, 3, "r"))
    plan = Planner(g).plan()
    assert plan.feasible, plan.to_json()
    sym = verify_plan_symbolic(g, plan)
    summary = summarize_all_routes(g, plan)
    assert summary["route_count"] == len(rm.enumerate_routes()) == 2
    # 共享包仓只存物理一份
    assert summary["distinct_packets"] == len(g.store) == 3
    print(f"[OK] 逐路回放/符号验证一致：{sym['states']} 状态，"
          f"{summary['route_count']} 条路线共享 {len(g.store)} 包")


def run_all():
    test_segment_resolution_and_bytes()
    test_range_render_matches_reference()
    test_packet_reuse_rules()
    test_bc_capacity1_infeasible_capacity2_feasible()
    test_merge_at_d_different_carry()
    test_non_anticipatory()
    test_all_routes_replay_and_symbolic_agree()
    print("导览编译与规划测试全部通过")


if __name__ == "__main__":
    run_all()
