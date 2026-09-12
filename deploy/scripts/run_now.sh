#!/bin/bash
# run_now.sh — 手工立即触发某个 systemd service
# 用法:
#   bash run_now.sh vma asc          # VM-A 立即跑升序扫描
#   bash run_now.sh vma merge        # VM-A 立即跑 chan_merge + v3
#   bash run_now.sh vmb desc         # VM-B 立即跑降序扫描

set -euo pipefail

VMA_HOST="gorgesoros39@136.66.64.228"
VMB_HOST="katelolita7788@35.212.190.147"
VMA_KEY="${HOME}/.ssh/hermes_key"

VM="${1:-}"
ACTION="${2:-}"

usage() {
  cat <<EOF
用法: $0 <vm> <action>

vm:   vma | vmb
action:
  asc      — 启动升序扫描 (meet-asc.service)
  desc     — 启动降序扫描 (meet-desc.service)
  merge    — 收盘合并 + v3 (chan-merge.service)

示例:
  $0 vma asc       # VM-A 立即跑升序扫描
  $0 vma merge     # VM-A 立即跑 chan_merge + v3
  $0 vmb desc      # VM-B 立即跑降序扫描
EOF
  exit 1
}

[ -z "$VM" ] || [ -z "$ACTION" ] && usage

case "$VM" in
  vma) HOST="$VMA_HOST"; KEY="$VMA_KEY" ;;
  vmb) HOST="$VMB_HOST"; KEY="" ;;
  *) echo "❌ 未知 vm: $VM"; usage ;;
esac

case "$ACTION" in
  asc)   SVC="meet-asc.service" ;;
  desc)  SVC="meet-desc.service" ;;
  merge) SVC="chan-merge.service" ;;
  *) echo "❌ 未知 action: $ACTION"; usage ;;
esac

SSH="ssh"
[ -n "$KEY" ] && SSH="ssh -i $KEY"

echo "🎯 $VM → $SVC ($ACTION)"
echo "   → $HOST"
echo ""

$SSH -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$HOST" "
  set -e
  # 先停掉可能的旧进程
  if [ '$ACTION' = 'asc' ]; then pkill -f 'meet_in_the_middle.*--side asc' || true; fi
  if [ '$ACTION' = 'desc' ]; then pkill -f 'meet_in_the_middle.*--side desc' || true; fi

  # 启动
  systemctl --user start $SVC
  sleep 2
  echo ''
  echo '=== status ==='
  systemctl --user status $SVC --no-pager 2>/dev/null | head -15
  echo ''
  echo '=== 进程 ==='
  ps aux | grep -E 'meet_in|dual2' | grep -v grep | awk '{print \$11, \$12, \$13}' || echo '  (无)'
  echo ''
  echo '=== 最新 log ==='
  tail -5 ~/chan_logs/meet*.log 2>/dev/null || true
  tail -5 ~/chan_logs/merge.log 2>/dev/null || true
" 2>&1
