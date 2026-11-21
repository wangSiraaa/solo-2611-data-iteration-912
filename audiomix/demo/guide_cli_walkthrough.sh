#!/usr/bin/env bash
# 导览编排端到端走查：素材/产品 -> 路线图 -> 编译整包 -> 规划/冲突 -> 回放/比较
set -euo pipefail
ROOT=/tmp/audiomix_guide_cli
rm -rf "$ROOT"; mkdir -p "$ROOT"
REG="$ROOT/registry"
PY="python3 -m audiomix.cli"
PYTHON=${PYTHONPATH:+$PYTHONPATH:}.; export PYTHONPATH=$PYTHON
cd "$(dirname "$0")/.."

echo "== 1. 登记素材并建两个产品版（B/C 首包不同） =="
A=$($PY asset-seq --root "$REG" --rate 16000 --values 100,200,300,400 --note a \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')
B=$($PY asset-seq --root "$REG" --rate 16000 --values 500,600,700,800 --note b \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["asset_id"])')
$PY tl-insert --root "$REG" --clip-id c1 --asset-id "$A" --a 0 --b 4 --start 0 >/dev/null
$PY tl-publish --root "$REG" --out "$ROOT/p1.json" >/dev/null
$PY tl-insert --root "$REG" --clip-id c2 --asset-id "$B" --a 0 --b 4 --start 0 >/dev/null
$PY tl-publish --root "$REG" --out "$ROOT/p2.json" >/dev/null
P1=$(python3 -c "import json;print(json.load(open('$ROOT/p1.json'))['product_id'])")
P2=$(python3 -c "import json;print(json.load(open('$ROOT/p2.json'))['product_id'])")
echo "  p1=$P1  p2=$P2"

echo "== 2. 写 B/C 分支路线图（入口预算 2 包，B/C 门口不可补传） =="
python3 - "$ROOT" "$P1" "$P2" <<'PY'
import json, sys
root, p1, p2 = sys.argv[1:4]
rm = {"entrance": "A", "preload_budget": 2, "halls": [
  {"hall_id": "A", "windows": [{"type": "transmit", "budget": 2}],
   "successors": ["B", "C"]},
  {"hall_id": "B", "windows": [{"type": "play", "segments": [
      {"product_id": p1, "clip_id": "c1", "src_a": 0, "src_b": 4}]}],
   "successors": []},
  {"hall_id": "C", "windows": [{"type": "play", "segments": [
      {"product_id": p2, "clip_id": "c2", "src_a": 0, "src_b": 4}]}],
   "successors": []}]}
json.dump(rm, open(root + "/route_map.json", "w"), ensure_ascii=False, indent=2)
PY
$PY route-map-validate --route-map "$ROOT/route_map.json"

echo "== 3. 容量 1：入口须同时承诺两首包 -> 冲突证据 =="
set +e
$PY guide-plan --route-map "$ROOT/route_map.json" --product "$ROOT/p1.json" \
    --product "$ROOT/p2.json" --registry-root "$REG" --cache 1 \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);c=d["certificate"];print("  feasible=",d["feasible"],"reason=",c["reason"],"需",len(c["frontier_required"]),"包 > 容量",c["cache_capacity"])'
set -e

echo "== 4. 容量 2：编译整包 + 规划 + 符号验证 + 回放 =="
$PY guide-compile --route-map "$ROOT/route_map.json" --product "$ROOT/p1.json" \
    --product "$ROOT/p2.json" --registry-root "$REG" --cache 2 \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  物理整包:",d["distinct_packets"],"字节:",d["packet_bytes"])'
$PY guide-verify --route-map "$ROOT/route_map.json" --product "$ROOT/p1.json" \
    --product "$ROOT/p2.json" --registry-root "$REG" --cache 2
$PY guide-replay --route-map "$ROOT/route_map.json" --product "$ROOT/p1.json" \
    --product "$ROOT/p2.json" --registry-root "$REG" --cache 2 \
    --route A B --wav "$ROOT/route_ab.wav" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  路线",d["path"],"播放",d["played_packets"],"WAV",d["wav"])'

echo "== 5. 比较两种硬件租用条件 =="
$PY guide-compare --route-map "$ROOT/route_map.json" --product "$ROOT/p1.json" \
    --product "$ROOT/p2.json" --registry-root "$REG" --cache 1 2 \
    | python3 -c 'import sys,json;[print("  cache",x["device"]["cache_capacity"],"feasible",x["feasible"]) for x in json.load(sys.stdin)]'
echo "导览命令走查通过"
