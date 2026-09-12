#!/bin/bash
# meet_scan_launch.sh — pgrep 防重 + 账本旋转 + 启动 meet_in_the_middle
# 用法: meet_scan_launch.sh asc  |  meet_scan_launch.sh desc

set -e

SIDE="${1:-asc}"
LOGDIR=~/chan_logs
mkdir -p $LOGDIR

SCRIPTDIR=~/TradingAgents-CN/scripts/chanlun-workflow
CODES=~/TradingAgents-CN/kline_cache/_codes_mainboard.txt
LEDGER=~/chan_logs/scan_ledger.jsonl

# VM-B 信息 (VM-A 写 VM-B 账本, VM-B 本地写自己的账本)
VMB_HOST=katelolita7788@35.212.190.147

# ---- pgrep 防重 ----
EXIST=$(pgrep -f "meet_in_the_middle_scan.*--side $SIDE" || true)
if [ -n "$EXIST" ]; then
  echo "$(date +%H:%M:%S) [launch] side=$SIDE worker already running (PID=$EXIST) -> SKIP"
  exit 0
fi

# ---- 账本旋转 ----
if [ -f "$LEDGER" ]; then
  TS=$(date +%Y%m%d_%H%M%S)
  mv "$LEDGER" "$LOGDIR/scan_ledger.${SIDE}.${TS}.jsonl" 2>/dev/null || true
fi

# ---- 启动 ----
cd $SCRIPTDIR

COMMON_ARGS="--side $SIDE --codes $CODES --ledger $LEDGER --dual2-dir ."

if [ "$SIDE" = "asc" ]; then
  # VM-A 升序: 本地扫, 经 SSH 写 VM-B 共享账本
  # 覆盖 KLINE_CACHE_DIR 到本地缓存 (非 FUSE GDrive)
  KLINE_LOCAL=~/kline_cache_local
  if [ -d "$KLINE_LOCAL" ]; then
    export KLINE_CACHE_DIR="$KLINE_LOCAL"
  fi
  nohup env MITM_LEDGER="$LEDGER" MITM_SLEEP="0.3" \
    python3 meet_in_the_middle_scan.py $COMMON_ARGS \
    --ssh-host "$VMB_HOST" --ssh-key ~/.ssh/hermes_key \
    > $LOGDIR/meet_asc.log 2>&1 &
  echo "$(date +%H:%M:%S) [launch] asc PID=$! KLINE=$KLINE_CACHE_DIR"
else
  # VM-B 降序: 本地扫 + 本地 O_APPEND 账本
  # 需要在 VM-B 上执行, 这里只是 VM-A 转发
  echo "$(date +%H:%M:%S) [launch] desc — forwarding to VM-B..."
  ssh "$VMB_HOST" "bash -s" << REMOTE_EOF
set -e
cd ~/TradingAgents-CN/scripts/chanlun-workflow
CODES=~/TradingAgents-CN/kline_cache/_codes_mainboard.txt
LEDGER=~/chan_logs/scan_ledger.jsonl
mkdir -p ~/chan_logs

EXIST=\$(pgrep -f "meet_in_the_middle_scan.*--side desc" || true)
if [ -n "\$EXIST" ]; then
  echo "[VM-B launch] already running PID=\$EXIST -> SKIP"
  exit 0
fi

if [ -f "\$LEDGER" ]; then
  mv "\$LEDGER" ~/chan_logs/scan_ledger.desc.\$(date +%Y%m%d_%H%M%S).jsonl 2>/dev/null || true
fi

KLINE_LOCAL=~/TradingAgents-CN/kline_cache
export KLINE_CACHE_DIR="\$KLINE_LOCAL"
nohup env MITM_LEDGER="\$LEDGER" MITM_SLEEP="0.3" \
  python3 meet_in_the_middle_scan.py \
  --side desc --codes "\$CODES" --ledger "\$LEDGER" --dual2-dir . \
  > ~/chan_logs/meet_desc.log 2>&1 &
echo "[VM-B launch] desc PID=\$!"
REMOTE_EOF
fi

echo "$(date +%H:%M:%S) [launch] done"
