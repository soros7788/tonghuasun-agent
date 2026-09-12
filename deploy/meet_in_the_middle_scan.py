#!/usr/bin/env python3
"""Meet-in-the-middle 会合扫描调度器

设计: 升序指针 (VM-A, 000001→) + 降序指针 (VM-B, ←605599)
      双指针相向遍历同一份排序宇宙, 共享账本 (VM-B O_APPEND).
      每只 code 被升 + 降 各扫一次, 会合时自动终止.

账本格式 (JSONL):
  {"code": "000001", "gate": "PASS", "vm": "A", "trend_score": 0.5, "detail": "...", "ts": "..."}

用法:
  python3 meet_in_the_middle_scan.py --side asc  --codes _codes_mainboard.txt
  python3 meet_in_the_middle_scan.py --side desc --codes _codes_mainboard.txt
  python3 meet_in_the_middle_scan.py --side asc  --codes _codes_mainboard.txt --ledger ~/chan_logs/scan_ledger.jsonl
"""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SH_TZ = timezone(timedelta(hours=8))

# VM-B 上账本路径 (两边都写同一个)
DEFAULT_LEDGER = os.environ.get("MITM_LEDGER", "~/chan_logs/scan_ledger.jsonl")
# 升/降序停止阈值: 双方账本里所有 code 都出现 ≥2 次 (升+降) 或指针相遇
MEET_STOP_DUPLEX = 2
# 每 N 码 refresh 一次账本
REFRESH_INTERVAL = 20
# 单码 scan timeout (秒)
SCAN_TIMEOUT = 90
# scan 间隔 (秒)
SCAN_SLEEP = float(os.environ.get("MITM_SLEEP", "0.5"))


def load_codes(path: str, side: str) -> list[str]:
    """加载排序宇宙, 按 side 切升/降序."""
    with open(path) as f:
        codes = [ln.strip() for ln in f if ln.strip()]
    # 升序: 自然序 (已排序)
    # 降序: 反转
    if side == "desc":
        codes = list(reversed(codes))
    print(f"[init] side={side} universe={len(codes)} first={codes[0]} last={codes[-1]}")
    return codes


def run_dual2(code: str, dual2_dir: str, kline_override: str | None = None) -> dict:
    """调 dual2_scan.py 扫单码, 返回 gate + scores."""
    env = os.environ.copy()
    if kline_override:
        env["KLINE_CACHE_DIR"] = kline_override

    # 简单 subprocess 调用 (单码, 直接拿 JSON stdout)
    cmd = [sys.executable, "dual2_scan.py", "--codes", code]
    try:
        proc = subprocess.run(
            cmd, cwd=dual2_dir, env=env,
            capture_output=True, text=True, timeout=SCAN_TIMEOUT,
        )
        # dual2_scan 应该把最终 JSON 写到 stdout 最后一段
        # 这里简化: 从 stderr/log 文件里 grep 最后一行 gate
        # 实际生产建议 dual2_scan --json-out 直接产出
        gate = "UNKNOWN"
        trend_score = 0.0
        seg_score = 0.0
        # 从 proc.stderr 里扫 (dual2_scan 用了 print(..., flush=True))
        for line in (proc.stdout + "\n" + proc.stderr).splitlines():
            if f"[{code}]" in line and "→" in line:
                parts = line.split("→")
                if len(parts) >= 2:
                    gate = parts[1].strip().split()[0]
            if "trend_score=" in line:
                try:
                    trend_score = float(line.split("trend_score=")[1].split()[0])
                except (IndexError, ValueError): pass
        return {
            "code": code, "gate": gate,
            "trend_score": trend_score, "seg_score": seg_score,
            "ok": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {"code": code, "gate": "TIMEOUT", "trend_score": 0.0, "seg_score": 0.0, "ok": False}
    except Exception as e:
        return {"code": code, "gate": "ERROR", "trend_score": 0.0, "seg_score": 0.0, "ok": False}


def append_ledger_local(ledger: str, entry: dict) -> None:
    """O_APPEND 本地账本写."""
    Path(ledger).parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def append_ledger_via_ssh(remote_host: str, remote_ledger: str, entry: dict,
                          ssh_key: str | None = None) -> None:
    """经 SSH 追写远程账本 (VM-A → VM-B)."""
    remote_json = json.dumps(entry, ensure_ascii=False)
    cmd = ["ssh"]
    if ssh_key:
        cmd += ["-i", ssh_key]
    cmd += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            remote_host,
            f"cat >> {remote_ledger}"]
    proc = subprocess.run(cmd, input=remote_json + "\n", text=True,
                          capture_output=True, timeout=10)
    if proc.returncode != 0:
        # 失败 fallback: 本地临时存
        print(f"[WARN] ssh ledger write failed: {proc.stderr.strip()}", flush=True)
        fallback = Path.home() / "chan_logs" / "ledger_fallback.jsonl"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        with open(fallback, "a") as f:
            f.write(remote_json + "\n")


def read_ledger_counts(ledger: str, ssh_host: str | None = None,
                       ssh_key: str | None = None) -> dict[str, int]:
    """读账本里每只 code 的出现次数."""
    if ssh_host:
        # 远程读
        cmd = ["ssh"]
        if ssh_key: cmd += ["-i", ssh_key]
        cmd += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                ssh_host, f"cat {ledger} 2>/dev/null | wc -l"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return {}
        # 全量读
        cmd[-1] = f"cat {ledger} 2>/dev/null"
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        lines = proc.stdout.strip().splitlines() if proc.returncode == 0 else []
    else:
        p = Path(ledger)
        if not p.exists():
            return {}
        lines = p.read_text().splitlines()

    counts: dict[str, int] = {}
    for line in lines:
        try:
            rec = json.loads(line)
            c = rec.get("code", "")
            counts[c] = counts.get(c, 0) + 1
        except json.JSONDecodeError:
            continue
    return counts


def is_meet(counts: dict[str, int], total_codes: int,
            seen_codes: set[str], threshold: int = MEET_STOP_DUPLEX) -> bool:
    """会合终止判定: 所有已扫 code 都出现 ≥threshold 次."""
    if not seen_codes:
        return False
    # 只要有任何已扫 code 还没达到 threshold, 就继续
    for code in seen_codes:
        if counts.get(code, 0) < threshold:
            return False
    # 另外: 如果已经扫了 ≥ total * 1.0 且全达 threshold — 安全保险
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", choices=["asc", "desc"], required=True)
    ap.add_argument("--codes", default="_codes_mainboard.txt", help="排序宇宙文件")
    ap.add_argument("--ledger", default=None, help="账本路径 (默认 ~/chan_logs/scan_ledger.jsonl)")
    ap.add_argument("--dual2-dir", default=".", help="dual2_scan.py 所在 dir")
    ap.add_argument("--kline-override", default=None,
                    help="覆盖 KLINE_CACHE_DIR (VM-A 本地缓存用)")
    ap.add_argument("--ssh-host", default=None,
                    help="经 SSH 写远程账本 (VM-A 写 VM-B)")
    ap.add_argument("--ssh-key", default=None)
    ap.add_argument("--max-codes", type=int, default=None, help="调试用: 只扫前 N 码")
    args = ap.parse_args()

    ledger = args.ledger or DEFAULT_LEDGER
    ledger_path = os.path.expanduser(ledger)
    codes = load_codes(args.codes, args.side)
    if args.max_codes:
        codes = codes[:args.max_codes]

    vm_tag = "A" if args.side == "asc" else "B"
    seen: set[str] = set()
    meet_count = 0
    stop_reason = ""

    print(f"[start] side={args.side} vm={vm_tag} total={len(codes)} "
          f"ledger={ledger_path} ssh_host={args.ssh_host or 'local'} "
          f"kline={args.kline_override or 'default'}", flush=True)

    for idx, code in enumerate(codes, 1):
        t0 = time.time()

        # 1. 跑 dual2_scan
        result = run_dual2(code, args.dual2_dir, args.kline_override)
        elapsed = time.time() - t0

        # 2. 写账本
        entry = {
            "code": code, "gate": result["gate"],
            "vm": vm_tag, "trend_score": result["trend_score"],
            "seg_score": result["seg_score"],
            "ts": datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }
        if args.ssh_host:
            append_ledger_via_ssh(args.ssh_host, ledger_path, entry, args.ssh_key)
        else:
            append_ledger_local(ledger_path, entry)

        seen.add(code)
        meet_count += 1

        # 3. 定期 refresh 账本, 判定会合
        if meet_count % REFRESH_INTERVAL == 0:
            counts = read_ledger_counts(ledger_path, args.ssh_host, args.ssh_key)
            duplexed = sum(1 for c in seen if counts.get(c, 0) >= MEET_STOP_DUPLEX)
            total_duplex = sum(1 for v in counts.values() if v >= MEET_STOP_DUPLEX)
            print(f"[meet-check] seen={len(seen)} duplexed={duplexed}/{len(seen)} "
                  f"total_duplex={total_duplex} ledger_size={len(counts)}", flush=True)

            # 会合: 已扫 code 全部双写完成
            if is_meet(counts, len(codes), seen):
                stop_reason = "ALL_DUPLEXED"
                break
            # 保险: 扫过全部宇宙
            if idx >= len(codes):
                stop_reason = "UNIVERSE_EXHAUSTED"
                break

        # 每 10 码打进度
        if idx % 10 == 0 or idx == 1:
            print(f"[{idx}/{len(codes)}] {code} → {result['gate']} "
                  f"(trend={result['trend_score']:.2f}) {elapsed:.1f}s", flush=True)

        time.sleep(SCAN_SLEEP)

    # 最终会合检查
    if not stop_reason:
        counts = read_ledger_counts(ledger_path, args.ssh_host, args.ssh_key)
        if is_meet(counts, len(codes), seen):
            stop_reason = "FINAL_MEET"
        else:
            stop_reason = "FINISHED_NO_MEET"

    print(f"[done] side={args.side} vm={vm_tag} scanned={len(seen)} stop={stop_reason}", flush=True)


if __name__ == "__main__":
    main()
