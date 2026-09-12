#!/bin/bash
# ============================================================================
# manual_install_vma.sh — 在 VM-A (tradingagents-new) 上本地执行
#
# 使用方法 (三选一):
#
#   A. 你已经 scp 到 VM-A:
#      scp deploy/*.py deploy/scripts/manual_install_vma.sh \
#         katelolita7788@35.212.190.147:~/deploy/
#      ssh katelolita7788@35.212.190.147 'cd ~/deploy && bash manual_install_vma.sh'
#
#   B. 直接从 /workspace 复制 (如果 VM-A 能 NFS/共享):
#      bash manual_install_vma.sh
#
#   C. 我 (另一个能 SSH 的 agent) 帮你推:
#      在 WorkBuddy 那边执行:
#        scp -r /workspace/deploy gorgesoros39@136.66.64.228:~/deploy/
#        ssh gorgesoros39@136.66.64.228 'cd ~/deploy && bash manual_install_vma.sh'
#
# 前提:
#   - VM-A 上 Python 3.10+ + pandas + numpy 已装
#   - ~/TradingAgents-CN/scripts/chanlun-workflow/ 已存在 (含 meet_in_the_middle_scan.py)
#   - ~/.ssh/hermes_key 存在 (用于 VM-A → VM-B 的 SSH)
# ============================================================================
set -euo pipefail

USER_HOME=~
DEPLOY_DIR="${USER_HOME}/deploy"
SYSTEMD_DIR="${USER_HOME}/.config/systemd/user"
LOGDIR="${USER_HOME}/chan_logs"
SCRIPTDIR="${USER_HOME}/TradingAgents-CN/scripts/chanlun-workflow"

echo "========================================"
echo "  VM-A manual install (asc + merge)"
echo "========================================"
echo "  USER_HOME  = $USER_HOME"
echo "  DEPLOY_DIR = $DEPLOY_DIR"
echo "  SCRIPTDIR  = $SCRIPTDIR"
echo "  SYSTEMD    = $SYSTEMD_DIR"
echo ""

# ----------------------------------------------------------------------------
# 0. 预检
# ----------------------------------------------------------------------------
echo "[0/7] 预检..."
ERRS=()

[ -d "$SCRIPTDIR" ] || ERRS+=("SCRIPTDIR 不存在: $SCRIPTDIR")
[ -f "$SCRIPTDIR/meet_in_the_middle_scan.py" ] || ERRS+=("meet_in_the_middle_scan.py 不在 $SCRIPTDIR")

python3 -c "import pandas, numpy" 2>/dev/null || ERRS+=("pandas/numpy 未装: pip3 install pandas numpy")

[ -d "${USER_HOME}/.ssh" ] || ERRS+=("~/.ssh/ 不存在")
[ -f "${USER_HOME}/.ssh/hermes_key" ] || echo "  ⚠️  hermes_key 不在 (VM-B 跨机器 SSH 会失败)"

TZ=$(timedatectl show --property=Timezone --value 2>/dev/null || echo "unknown")
if [ "$TZ" != "Asia/Shanghai" ] && [ "$TZ" != "Asia/Chongqing" ]; then
  echo "  ⚠️  时区 $TZ (应为 Asia/Shanghai)"
fi

if [ ${#ERRS[@]} -gt 0 ]; then
  echo "❌ 预检失败:"
  for e in "${ERRS[@]}"; do echo "    - $e"; done
  exit 1
fi
echo "  ✅ 全部通过"

# ----------------------------------------------------------------------------
# 1. 确保 DEPLOY_DIR 有 Python 脚本
# ----------------------------------------------------------------------------
echo "[1/7] 部署 Python 脚本 → $DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR"

# 如果脚本在旁边 (通过 scp 已带过来)
SCRIPT_SRC=""
for d in "$DEPLOY_DIR" "$(dirname "$0")" "$(dirname "$0")/.." /workspace/deploy; do
  if [ -f "$d/chan_merge.py" ]; then
    SCRIPT_SRC="$d"; break
  fi
done

if [ -n "$SCRIPT_SRC" ]; then
  cp -v "$SCRIPT_SRC/chan_merge.py" "$DEPLOY_DIR/"
  cp -v "$SCRIPT_SRC/v3_position_manager.py" "$DEPLOY_DIR/"
  cp -v "$SCRIPT_SRC/position_manager_v3.py" "$DEPLOY_DIR/"
  echo "  ✅ 已从 $SCRIPT_SRC 复制"
else
  echo "  ⚠️  找不到 Python 脚本源目录"
  echo "     请手动 scp 过来: chan_merge.py, v3_position_manager.py, position_manager_v3.py"
  echo "     目标: $DEPLOY_DIR/"
fi

# ----------------------------------------------------------------------------
# 2. 创建 systemd unit/timer 文件
# ----------------------------------------------------------------------------
echo "[2/7] 创建 systemd unit/timer → $SYSTEMD_DIR"
mkdir -p "$SYSTEMD_DIR"

# --- meet-asc.service ---
cat > "$SYSTEMD_DIR/meet-asc.service" << 'UNIT_EOF'
[Unit]
Description=Meet-in-the-middle asc scan (VM-A, 升序)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/TradingAgents-CN/scripts/chanlun-workflow
Environment=MITM_LEDGER=%h/chan_logs/scan_ledger.jsonl
Environment=MITM_SLEEP=0.3
Environment=KLINE_CACHE_DIR=%h/kline_cache_local

ExecStartPre=/bin/bash -c '
  mkdir -p %h/chan_logs && \
  if [ -f %h/chan_logs/scan_ledger.jsonl ]; then \
    mv %h/chan_logs/scan_ledger.jsonl \
       %h/chan_logs/scan_ledger.asc.$(date +%Y%m%d_%H%M%S).jsonl 2>/dev/null || true; \
  fi
'
ExecStart=/usr/bin/python3 meet_in_the_middle_scan.py \
    --side asc \
    --codes %h/TradingAgents-CN/kline_cache/_codes_mainboard.txt \
    --ledger %h/chan_logs/scan_ledger.jsonl \
    --dual2-dir . \
    --ssh-host katelolita7788@35.212.190.147 \
    --ssh-key %h/.ssh/hermes_key

ExecStopPost=/bin/bash -c '
  pkill -f "meet_in_the_middle_scan.*--side asc" || true
'
Restart=no
StandardOutput=append:%h/chan_logs/meet_asc.log
StandardError=append:%h/chan_logs/meet_asc.log
SyslogIdentifier=meet-asc

[Install]
WantedBy=default.target
UNIT_EOF

# --- meet-asc.timer ---
cat > "$SYSTEMD_DIR/meet-asc.timer" << 'TIMER_EOF'
[Unit]
Description=Start meet-in asc scan at market open (VM-A)

[Timer]
OnCalendar=*-*-* 09:00:00 Asia/Shanghai
RandomizedDelaySec=60
Persistent=true
Unit=meet-asc.service

[Install]
WantedBy=timers.target
TIMER_EOF

# --- chan-merge.service ---
cat > "$SYSTEMD_DIR/chan-merge.service" << 'UNIT_EOF'
[Unit]
Description=Post-market chan_merge + v3 position (VM-A)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/TradingAgents-CN/scripts/chanlun-workflow
ExecStart=/bin/bash -c '
  DEPLOY=%h/deploy
  LOGDIR=%h/chan_logs
  VMB_HOST=katelolita7788@35.212.190.147
  VMB_KEY=%h/.ssh/hermes_key
  mkdir -p $LOGDIR
  echo "[$(date +%H:%M:%S)] chan_merge start" >> $LOGDIR/merge.log
  python3 $DEPLOY/chan_merge.py \
      -i $LOGDIR/scan_ledger.jsonl \
      --vmb-host "$VMB_HOST" \
      --vmb-ledger ~/chan_logs/scan_ledger.jsonl \
      --vmb-key "$VMB_KEY" \
      --include-blocked \
      -o $LOGDIR/merged_$(date +%Y%m%d).json
  python3 $DEPLOY/v3_position_manager.py \
      -c $LOGDIR/merged_$(date +%Y%m%d).json \
      --source chan_merge \
      --regime strong --grade A --asset 50000 \
      -o $LOGDIR/v3_positions_$(date +%Y%m%d).json
  echo "[$(date +%H:%M:%S)] chan_merge done" >> $LOGDIR/merge.log
'
StandardOutput=append:%h/chan_logs/merge.log
StandardError=append:%h/chan_logs/merge.log
SyslogIdentifier=chan-merge

[Install]
WantedBy=default.target
UNIT_EOF

# --- chan-merge.timer ---
cat > "$SYSTEMD_DIR/chan-merge.timer" << 'TIMER_EOF'
[Unit]
Description=Run chan_merge + v3 after market close (VM-A)

[Timer]
OnCalendar=*-*-* 15:05:00 Asia/Shanghai
Persistent=true
Unit=chan-merge.service

[Install]
WantedBy=timers.target
TIMER_EOF

echo "  ✅ 4 个 unit/timer 已写入 $SYSTEMD_DIR/"
ls -la "$SYSTEMD_DIR/"

# ----------------------------------------------------------------------------
# 3. daemon-reload + enable timers
# ----------------------------------------------------------------------------
echo "[3/7] daemon-reload + enable timers"
systemctl --user daemon-reload 2>&1
systemctl --user enable --now meet-asc.timer 2>&1
systemctl --user enable --now chan-merge.timer 2>&1
echo "  ✅ timers 已启用"

# ----------------------------------------------------------------------------
# 4. 检查 timers 状态
# ----------------------------------------------------------------------------
echo "[4/7] timers 状态"
systemctl --user list-timers --all --no-pager 2>&1 | grep -E "meet|chan" || echo "  (还没触发过, 显示为空)"

# ----------------------------------------------------------------------------
# 5. 手工触发测试 (不真跑 meet_in, 只测 chan_merge)
# ----------------------------------------------------------------------------
echo "[5/7] 手工触发 chan-merge.service (测试)"
systemctl --user start chan-merge.service 2>&1 || echo "  ⚠️  可能账本还没数据 (正常)"
sleep 2
systemctl --user status chan-merge.service --no-pager 2>&1 | head -8 || true

# ----------------------------------------------------------------------------
# 6. 验证 Python 脚本能 import
# ----------------------------------------------------------------------------
echo "[6/7] Python 脚本 import 验证"
cd "$DEPLOY_DIR"
python3 -c "import chan_merge; print('  ✅ chan_merge import OK')"
python3 -c "import v3_position_manager; print('  ✅ v3_position_manager import OK')"
python3 -c "import position_manager_v3; print('  ✅ position_manager_v3 import OK')"

# ----------------------------------------------------------------------------
# 7. 完成
# ----------------------------------------------------------------------------
echo ""
echo "========================================"
echo "  ✅ VM-A 部署完成!"
echo "========================================"
echo ""
echo "  自动调度:"
echo "    09:00 Asia/Shanghai → meet-asc.timer (升序扫描)"
echo "    15:05 Asia/Shanghai → chan-merge.timer (收盘合并 + v3)"
echo ""
echo "  手工运行:"
echo "    systemctl --user start meet-asc.service"
echo "    systemctl --user start chan-merge.service"
echo ""
echo "  状态检查:"
echo "    systemctl --user list-timers --all"
echo "    systemctl --user status meet-asc.service chan-merge.service"
echo "    tail -f ~/chan_logs/meet_asc.log"
echo ""
echo "  卸载:"
echo "    systemctl --user disable --now meet-asc.timer chan-merge.timer"
echo "    rm ~/.config/systemd/user/meet-asc.* ~/.config/systemd/user/chan-merge.*"
echo "    systemctl --user daemon-reload"
