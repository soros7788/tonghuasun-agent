#!/bin/bash
# status.sh — 一键检查双机扫描 + merge 状态
# 用法: bash status.sh [--vma-only] [--vmb-only]

set -euo pipefail

VMA_HOST="gorgesoros39@136.66.64.228"
VMB_HOST="katelolita7788@35.212.190.147"
VMA_KEY="${HOME}/.ssh/hermes_key"

VMA_ONLY=false
VMB_ONLY=false
for a in "$@"; do
  case "$a" in --vma-only) VMA_ONLY=true ;; --vmb-only) VMB_ONLY=true ;; esac
done

divider() { echo "────────────────────────────────────────────────"; }

# ----------------------------------------------------------------------------
check_side() {
  local label="$1" host="$2" key="${3:-}"
  local SSH="ssh"
  [ -n "$key" ] && SSH="ssh -i $key"

  echo ""
  divider
  echo "📡  $label ($host)"
  divider

  $SSH -o ConnectTimeout=8 -o StrictHostKeyChecking=no "$host" 2>&1 '
    echo "=== systemd timers ==="
    systemctl --user list-timers --all --no-pager 2>/dev/null | grep -E "meet|chan" || echo "  (无 meet/chan timer)"
    echo ""
    echo "=== 进程 ==="
    ps aux | grep -E "meet_in_the_middle|dual2_scan" | grep -v grep | awk "{print \$11, \$12, \$13}" || echo "  (无)"
    echo ""
    echo "=== 账本 ==="
    ls -lh ~/chan_logs/scan_ledger*.jsonl 2>/dev/null | tail -5 || echo "  (无)"
    echo ""
    echo "=== 最近 3 条 log ==="
    tail -3 ~/chan_logs/meet*.log 2>/dev/null || echo "  (无)"
    echo ""
    echo "=== chan-merge 日志 ==="
    tail -3 ~/chan_logs/merge.log 2>/dev/null || echo "  (无)"
    echo ""
    echo "=== 今日输出 ==="
    ls -lh ~/chan_logs/merged_*$(date +%Y%m%d)* ~/chan_logs/v3_positions_*$(date +%Y%m%d)* 2>/dev/null || echo "  (无)"
  ' 2>&1 || echo "  ⚠️ SSH 连接失败"
}

# ----------------------------------------------------------------------------
if ! $VMB_ONLY; then
  check_side "VM-A 升序" "$VMA_HOST" "$VMA_KEY"
fi

if ! $VMA_ONLY; then
  check_side "VM-B 降序" "$VMB_HOST" ""
fi

echo ""
divider
echo "✅ 检查完成"
divider
