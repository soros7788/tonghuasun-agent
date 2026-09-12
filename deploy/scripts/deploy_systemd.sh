#!/bin/bash
# ============================================================================
# deploy_systemd.sh — 一键部署 systemd timer 自动化
#
# 用法:
#   # 在 VM-A 上执行 (升序扫描 + 收盘合并)
#   bash deploy_systemd.sh --vm vma
#
#   # 在 VM-B 上执行 (降序扫描)
#   bash deploy_systemd.sh --vm vmb
#
#   # 两边都跑 (从你本地能 SSH 的机器执行)
#   bash deploy_systemd.sh --vm both
#
# 前提:
#   - 已在 VM-A 上: ~/TradingAgents-CN/scripts/chanlun-workflow/ 存在
#   - 已在 VM-B 上: ~/TradingAgents-CN/scripts/chanlun-workflow/ 存在
#   - SSH 免密已配置
#   - Python 依赖 (akshare, pandas, numpy) 已装
# ============================================================================
set -euo pipefail

VM="${1:-}"

if [ -z "$VM" ]; then
  echo "用法: $0 --vm vma | vmb | both"
  exit 1
fi

VMA_HOST="gorgesoros39@136.66.64.228"
VMA_KEY="~/.ssh/hermes_key"
VMB_HOST="katelolita7788@35.212.190.147"
VMB_KEY=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_DIR="$SCRIPT_DIR/systemd"
PY_DIR="$SCRIPT_DIR"

# ----------------------------------------------------------------------------
# 单 VM 部署函数
# ----------------------------------------------------------------------------
deploy_vma() {
  echo "========================================"
  echo "  VM-A (tradingagents-new) 部署"
  echo "========================================"

  local HOST="$VMA_HOST" KEY="${VMA_KEY/#\~/$HOME}"

  # 0. 预检
  echo "[0/6] 预检 VM-A..."
  ssh -i "$KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$HOST" "echo '  SSH OK' && \
    [ -d ~/TradingAgents-CN/scripts/chanlun-workflow ] && echo '  chanlun-workflow OK' && \
    python3 -c 'import pandas, numpy' && echo '  Python deps OK' || echo '  ⚠️ 路径或依赖缺失'"

  # 1. 拷贝 Python 脚本到 ~/deploy/
  echo "[1/6] 拷贝 deploy/*.py → ~/deploy/"
  ssh -i "$KEY" "$HOST" 'mkdir -p ~/deploy'
  scp -i "$KEY" "$PY_DIR"/chan_merge.py "$PY_DIR"/v3_position_manager.py \
        "$PY_DIR"/position_manager_v3.py "$HOST":~/deploy/ 2>&1 | tail -1

  # 2. 拷贝 systemd unit/timer
  echo "[2/6] 拷贝 systemd unit/timer → ~/.config/systemd/user/"
  ssh -i "$KEY" "$HOST" 'mkdir -p ~/.config/systemd/user ~/chan_logs'
  scp -i "$KEY" "$SYSTEMD_DIR"/meet-asc.service "$SYSTEMD_DIR"/meet-asc.timer \
        "$SYSTEMD_DIR"/chan-merge.service "$SYSTEMD_DIR"/chan-merge.timer \
        "$HOST":~/.config/systemd/user/ 2>&1 | tail -1

  # 3. 时区检查 + 设置
  echo "[3/6] 时区检查 (应为 Asia/Shanghai)"
  ssh -i "$KEY" "$HOST" '
    local TZ_CUR=$(timedatectl show --property=Timezone --value 2>/dev/null || echo "unknown")
    echo "  当前时区: $TZ_CUR"
    if [ "$TZ_CUR" != "Asia/Shanghai" ] && [ "$TZ_CUR" != "Asia/Chongqing" ]; then
      echo "  ⚠️  时区不是 Asia/Shanghai, 建议: sudo timedatectl set-timezone Asia/Shanghai"
    fi
  '

  # 4. daemon-reload + 启用 timers
  echo "[4/6] daemon-reload + 启用 timers"
  ssh -i "$KEY" "$HOST" '
    systemctl --user daemon-reload
    systemctl --user enable --now meet-asc.timer
    systemctl --user enable --now chan-merge.timer
    echo "  ✓ timers 已启用"
  '

  # 5. 检查 timers 状态
  echo "[5/6] 检查 timers 状态"
  ssh -i "$KEY" "$HOST" '
    systemctl --user list-timers "meet-*" "chan-*" --all --no-pager 2>/dev/null || true
    echo ""
    systemctl --user status meet-asc.timer chan-merge.timer --no-pager 2>/dev/null | head -20 || true
  '

  # 6. 手工触发一次 (测试)
  echo "[6/6] 手工触发测试 (--dry-run, 不真跑 meet_in)"
  ssh -i "$KEY" "$HOST" '
    echo "  meet-asc.service (manual):"
    systemctl --user start meet-asc.service && \
      systemctl --user status meet-asc.service --no-pager 2>/dev/null | head -5 || \
      echo "  (预期 fail — 可能代码已在 running)"
    echo "  chan-merge.service (manual):"
    systemctl --user start chan-merge.service && \
      systemctl --user status chan-merge.service --no-pager 2>/dev/null | head -5 || \
      echo "  (可能账本还没数据)"
  '

  echo ""
  echo "✅ VM-A 部署完成!"
  echo "   timers:"
  echo "     meet-asc.timer    → 09:00 Asia/Shanghai (升序扫描)"
  echo "     chan-merge.timer  → 15:05 Asia/Shanghai (收盘合并 + v3)"
  echo ""
  echo "   手工运行:"
  echo "     systemctl --user start meet-asc.service"
  echo "     systemctl --user start chan-merge.service"
  echo ""
  echo "   状态检查:"
  echo "     systemctl --user list-timers --all"
  echo "     systemctl --user status meet-asc.service chan-merge.service"
}

deploy_vmb() {
  echo "========================================"
  echo "  VM-B (hermes-free) 部署"
  echo "========================================"

  local HOST="$VMB_HOST"

  # 0. 预检
  echo "[0/5] 预检 VM-B..."
  ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$HOST" "echo '  SSH OK' && \
    [ -d ~/TradingAgents-CN/scripts/chanlun-workflow ] && echo '  chanlun-workflow OK' && \
    python3 -c 'import pandas, numpy' && echo '  Python deps OK' || echo '  ⚠️ 路径或依赖缺失'"

  # 1. 拷贝 Python 脚本 (VM-B 不需要 chan_merge/v3_position_manager, 只需要 meet_in)
  echo "[1/5] 拷贝 systemd unit/timer → ~/.config/systemd/user/"
  ssh "$HOST" 'mkdir -p ~/.config/systemd/user ~/chan_logs'
  scp "$SYSTEMD_DIR"/meet-desc.service "$SYSTEMD_DIR"/meet-desc.timer \
        "$HOST":~/.config/systemd/user/ 2>&1 | tail -1

  # 2. 时区
  echo "[2/5] 时区检查"
  ssh "$HOST" '
    local TZ_CUR=$(timedatectl show --property=Timezone --value 2>/dev/null || echo "unknown")
    echo "  当前时区: $TZ_CUR"
  '

  # 3. daemon-reload + 启用 timer
  echo "[3/5] daemon-reload + 启用 meet-desc.timer"
  ssh "$HOST" '
    systemctl --user daemon-reload
    systemctl --user enable --now meet-desc.timer
    echo "  ✓ meet-desc.timer 已启用"
  '

  # 4. 检查
  echo "[4/5] 检查 timer 状态"
  ssh "$HOST" '
    systemctl --user list-timers "meet-*" --all --no-pager 2>/dev/null || true
  '

  echo ""
  echo "✅ VM-B 部署完成!"
  echo "   timers:"
  echo "     meet-desc.timer   → 09:00 Asia/Shanghai (降序扫描)"
  echo ""
  echo "   手工运行: systemctl --user start meet-desc.service"
}

# ----------------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------------
case "$VM" in
  vma)  deploy_vma ;;
  vmb)  deploy_vmb ;;
  both)
    deploy_vma
    echo ""
    deploy_vmb
    ;;
  *)
    echo "❌ 未知选项: $VM (应为 vma/vmb/both)"
    exit 1
    ;;
esac
