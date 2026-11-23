#!/bin/bash
# 批三·大画布 A/B 配对驱动：交替 基线(5708=_cvbase) / 新树(5707)，每对独立 Chrome。
# 用法: bash scripts/canvas_ab_pair.sh <轮数> [起始轮]
N=${1:-20}; START=${2:-1}
for ((i=START; i<START+N; i++)); do
  # 奇数轮先 A 后 B、偶数轮先 B 后 A——抵消顺序漂移
  if (( i % 2 )); then
    node tests/canvas_perf2.mjs http://127.0.0.1:5708 work_073 "pa${i}-base" 1440 900
    node tests/canvas_perf2.mjs http://127.0.0.1:5707 work_073 "pa${i}-new"  1440 900
  else
    node tests/canvas_perf2.mjs http://127.0.0.1:5707 work_073 "pa${i}-new"  1440 900
    node tests/canvas_perf2.mjs http://127.0.0.1:5708 work_073 "pa${i}-base" 1440 900
  fi
  echo "=== pair $i done ==="
done
