#!/bin/bash
# ============================================================================
# manual_install_vmb.sh — 在 VM-B (hermes-free) 上本地执行
#
# 使用方法 (同 manual_install_vma.sh):
#   scp deploy/scripts/manual_install_vmb.sh \
#      katelolita7788@35.212.190.147:~/deploy/
#   ssh katelolita7788@35.212.190.147 'cd ~/deploy && bash manual_install_vmb.sh'
#
# 前提:
#   - VM-B 上 Python 3.10+ + pandas + numpy 已装
#   - ~/TradingAgents-CN/scripts/chanlun-workflow/ 已存在 (含 meet_in_the_middle_scan.py)
# ============================================================================
set -euo pipefail

USER_HOME=~
SYSTEMD_DIR="${USER_HOME}/.config/systemd/user"
LOGDIR="${USER_HOME}/chan_logs"
SCRIPTDIR="${USER_HOME}/TradingAgents-CN/scripts/chanlun-workflow"

echo "========================================"
echo "  VM-B manual install (desc)"
echo "========================================"
echo ""

# ----------------------------------------------------------------------------
# 0. 预检
# ----------------------------------------------------------------------------
echo "[0/4] 预检..."
ERRS=()
[ -d "$SCRIPTDIR" ] || ERRS+=("SCRIPTDIR 不存在: $SCRIPTDIR")
[ -f "$SCRIPTDIR/meet_in_the_middle_scan.py" ] || ERRS+=("meet_in_the_middle_scan.py 不在 $SCRIPTDIR")
python3 -c "import pandas, numpy" 2>/dev/null || ERRS+=("pandas/numpy 未装: pip3 install pandas numpy")

if [ ${#ERRS[@]} -gt 0 ]; then
  echo "❌ 预检失败:"
  for e in "${ERRS[@]}"; do echo "    - $e"; done
  exit 1
fi
echo "  ✅ 全部通过"

# ----------------------------------------------------------------------------
# 1. 创建 systemd unit/timer
# ----------------------------------------------------------------------------
echo "[1/4] 创建 systemd unit/timer → $SYSTEMD_DIR"
mkdir -p "$SYSTEMD_DIR"

cat > "$SYSTEMD_DIR/meet-desc.service" << 'UNIT_EOF'
[Unit]
Description=Meet-in-the-middle desc scan (VM-B, 降序)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/TradingAgents-CN/scripts/chanlun-workflow
Environment=MITM_LEDGER=%h/chan_logs/scan_ledger.jsonl
Environment=MITM_SLEEP=0.3
Environment=KLINE_CACHE_DIR=%h/TradingAgents-CN/kline_cache

ExecStartPre=/bin/bash -c '
  mkdir -p %h/chan_logs && \
  if [ -f %h/chan_logs/scan_ledger.jsonl ]; then \
    mv %h/chan_logs/scan_ledger.jsonl \
       %h/chan_logs/scan_ledger.desc.$(date +%Y%m%d_%H%M%S).jsonl 2>/dev/null || true; \
  fi
'
ExecStart=/usr/bin/python3 meet_in_the_middle_scan.py \
    --side desc \
    --codes %h/TradingAgents-CN/kline_cache/_codes_mainboard.txt \
    --ledger %h/chan_logs/scan_ledger.jsonl \
    --dual2-dir .

ExecStopPost=/bin/bash -c '
  pkill -f "meet_in_the_middle_scan.*--side desc" || true
'
Restart=no
StandardOutput=append:%h/chan_logs/meet_desc.log
StandardError=append:%h/chan_logs/meet_desc.log
SyslogIdentifier=meet-desc

[Install]
WantedBy=default.target
UNIT_EOF

cat > "$SYSTEMD_DIR/meet-desc.timer" << 'TIMER_EOF'
[Unit]
Description=Start meet-in desc scan at market open (VM-B)

[Timer]
OnCalendar=*-*-* 09:00:00 Asia/Shanghai
RandomizedDelaySec=60
Persistent=true
Unit=meet-desc.service

[Install]
WantedBy=timers.target
TIMER_EOF

echo "  ✅ meet-desc.service + meet-desc.timer 已写入"

# ----------------------------------------------------------------------------
# 2. daemon-reload + enable
# ----------------------------------------------------------------------------
echo "[2/4] daemon-reload + enable meet-desc.timer"
systemctl --user daemon-reload 2>&1
systemctl --user enable --now meet-desc.timer 2>&1
echo "  ✅ timer 已启用"

# ----------------------------------------------------------------------------
# 3. 状态
# ----------------------------------------------------------------------------
echo "[3/4] timer 状态"
systemctl --user list-timers --all --no-pager 2>&1 | grep -E "meet-desc" || echo "  (还没触发过)"

# ----------------------------------------------------------------------------
# 4. 完成
# ----------------------------------------------------------------------------
echo ""
echo "========================================"
echo "  ✅ VM-B 部署完成!"
echo "========================================"
echo ""
echo "  自动调度:"
echo "    09:00 Asia/Shanghai → meet-desc.timer (降序扫描)"
echo ""
echo "  手工运行: systemctl --user start meet-desc.service"
echo "  状态检查: systemctl --user status meet-desc.service"
echo "  日志: tail -f ~/chan_logs/meet_desc.log"
echo ""
echo "  卸载:"
echo "    systemctl --user disable --now meet-desc.timer"
echo "    rm ~/.config/systemd/user/meet-desc.*"
echo "    systemctl --user daemon-reload"
