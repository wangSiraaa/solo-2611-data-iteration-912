#!/usr/bin/env bash
# 一键验证：手算案例 + 数据仓案例 + 边界语义 + 导览编排 + 两个规模案例
set -euo pipefail
cd "$(dirname "$0")"
python3 -m tests.test_hand_calc /tmp/audiomix_verify_hand
python3 -m tests.test_archive
python3 -m tests.test_edge
python3 -m tests.test_guide
python3 -m demo.scale_demo /tmp/audiomix_verify_scale
python3 -m demo.guide_scale_demo /tmp/audiomix_verify_guide
echo
echo "================ 全量验证通过 ================"
