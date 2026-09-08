#!/bin/bash
# collect_dualscan_results — 从 VM-A / VM-B 拉 dualscan JSON + 汇合 + push GDrive
# 用法: bash collect_dualscan_results.sh
set -euo pipefail

GDRIVE_REMOTE="${GDRIVE_REMOTE:-gdrive_a}"
GDRIVE_PATH="${GDRIVE_PATH:-dualscan_results}"
LOCAL_DIR="/workspace/dualscan_results"
TS=$(TZ=Asia/Shanghai date +%Y%m%d_%H%M%S)

mkdir -p "$LOCAL_DIR"

echo "=== [$TS] 开始收集 dualscan 结果 ==="

# VM-A
A_JSON=$(proxychains4 -f /tmp/pc.conf sshpass -p 'Gorgesoros9-Trading2026!' \
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 gorgesoros39@136.66.64.228 \
  'ls -t ~/chan_logs/dualscan_*.json 2>/dev/null | head -1' 2>&1 | tail -1)
if [ -n "$A_JSON" ]; then
  proxychains4 -f /tmp/pc.conf sshpass -p 'Gorgesoros9-Trading2026!' \
    scp -o StrictHostKeyChecking=no gorgesoros39@136.66.64.228:"$A_JSON" "$LOCAL_DIR/VM-A_$(basename $A_JSON)"
  echo "✅ VM-A: $(basename $A_JSON) ($(wc -c < $LOCAL_DIR/VM-A_$(basename $A_JSON)) bytes)"
else
  echo "⚠️ VM-A 无 JSON"
fi

# VM-B
B_JSON=$(proxychains4 -f /tmp/pc.conf ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  -i /root/.ssh/hermes_key katelolita7788@35.212.190.147 \
  'ls -t ~/chan_logs/dualscan*.json 2>/dev/null | head -1' 2>&1 | tail -1)
if [ -n "$B_JSON" ]; then
  proxychains4 -f /tmp/pc.conf scp -o StrictHostKeyChecking=no -i /root/.ssh/hermes_key \
    katelolita7788@35.212.190.147:"$B_JSON" "$LOCAL_DIR/VM-B_$(basename $B_JSON)"
  echo "✅ VM-B: $(basename $B_JSON) ($(wc -c < $LOCAL_DIR/VM-B_$(basename $B_JSON)) bytes)"
else
  echo "⚠️ VM-B 无 JSON"
fi

# 汇合 + 汇总
python3 << PYEOF
import json, glob, os
local = "$LOCAL_DIR"
combined = {"ts": "$TS", "sources": {}, "all_pass": [], "summary": {}}
total_codes, pass_codes = 0, 0
for f in sorted(glob.glob(f"{local}/VM-*.json")):
    src = os.path.basename(f).split("_")[0]  # VM-A / VM-B
    try:
        d = json.load(open(f))
    except Exception as e:
        combined["sources"][src] = f"ERROR: {e}"
        continue
    dual = d.get("dual", [])
    pass_list = [r for r in dual if r.get("gate") == "PASS"]
    total_codes += len(dual)
    pass_codes += len(pass_list)
    combined["sources"][src] = {
        "stage1": d.get("stage1"),
        "total": len(dual),
        "pass": len(pass_list),
        "blocked": sum(1 for r in dual if r.get("gate") == "BLOCKED"),
        "neutral": sum(1 for r in dual if r.get("gate") == "NEUTRAL"),
        "codes": [r["code"] for r in dual],
        "pass_codes": [r["code"] for r in pass_list],
    }
    combined["all_pass"].extend(pass_list)

combined["summary"] = {
    "total_checked": total_codes,
    "total_pass": pass_codes,
    "pass_rate": f"{pass_codes/total_codes*100:.1f}%" if total_codes else "N/A",
}

out = f"{local}/combined_{TS}.json"
json.dump(combined, open(out, "w"), ensure_ascii=False, indent=2)
print(f"✅ 汇合: {out}")
print(f"   总计 {total_codes} 只, PASS {pass_codes} ({combined['summary']['pass_rate']})")
for src, info in combined["sources"].items():
    if isinstance(info, dict):
        print(f"   {src}: {info.get('pass', '?')}/{info.get('total', '?')} PASS")
PYEOF

# push GDrive
if command -v rclone &>/dev/null; then
  rclone copy "$LOCAL_DIR/" "${GDRIVE_REMOTE}:${GDRIVE_PATH}/" 2>&1 | tail -3
  echo "✅ 已 push ${GDRIVE_REMOTE}:${GDRIVE_PATH}/"
else
  echo "⚠️ 本机无 rclone, 请在有 rclone 的机器上手动 push $LOCAL_DIR/"
fi
