#!/usr/bin/env python3
"""chan_merge — 双机 meet-in-the-middle 账本合并器

合并 VM-A (升序) + VM-B (降序) 两边的 scan_ledger.jsonl, 去重, gate 过滤, 排序,
输出结构化候选池 JSON (可直接喂给 v3_position_manager.py)。

账本 JSONL 行格式 (来自 meet_in_the_middle_scan.py):
  {"code": "000001", "gate": "PASS", "vm": "A", "trend_score": 0.5,
   "seg_score": 0.6, "dlp": 0.55, "tier": "CORE", "ts": "..."}

用法:
  # 合并双机账本 (VM-A 本地 + VM-B SSH 拉)
  python3 chan_merge.py \
      --ledger ~/chan_logs/scan_ledger.jsonl \
      --ledger-vmb katelolita7788@35.212.190.147:~/chan_logs/scan_ledger.jsonl \
      --output merged_candidates.json

  # 只合并本地 (已拉回)
  python3 chan_merge.py -i vm_a.jsonl vm_b.jsonl -o merged.json

  # 从 dualscan JSON 生成 (不依赖账本, 直接读 dualscan 输出)
  python3 chan_merge.py --from-dualscan dualscan_20260909_fixed.json -o merged.json

  # 全量输出 (含 BLOCKED)
  python3 chan_merge.py -i ledger.jsonl --include-blocked -o all.json
"""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

SH_TZ = timezone(timedelta(hours=8))

# gate 优先级 (越小越好)
GATE_RANK = {"PASS": 0, "OBSERVE": 1, "BLOCKED": 2, "UNKNOWN": 3}
# tier 优先级
TIER_RANK = {"CORE": 0, "OBSERVE": 1, "EDGE": 2, "UNKNOWN": 3}


def load_jsonl(path: str, ssh_host: str | None = None, ssh_key: str | None = None) -> list[dict]:
    """加载 JSONL, 支持 SSH 拉取远端文件."""
    if ssh_host:
        # SSH 拉远端
        cmd = ["ssh"]
        if ssh_key:
            cmd += ["-i", ssh_key]
        cmd += ["-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
                ssh_host, f"cat {path}"]
        print(f"[load] SSH → {ssh_host}:{path}")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            print(f"  ⚠️ SSH 失败: {proc.stderr.strip()[:200]}")
            return []
        raw = proc.stdout
    else:
        # 本地
        print(f"[load] 本地 → {path}")
        try:
            with open(path) as f:
                raw = f.read()
        except FileNotFoundError:
            print(f"  ⚠️ 文件不存在: {path}")
            return []

    records = []
    for i, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            records.append(r)
        except json.JSONDecodeError as e:
            print(f"  ⚠️ line {i} JSON 错误: {e}")
    print(f"  → {len(records)} 条")
    return records


def load_dualscan_json(path: str) -> list[dict]:
    """从 dualscan 输出 JSON 加载候选 (不依赖账本)."""
    with open(path) as f:
        data = json.load(f)
    dual = data.get("dual", [])
    records = []
    for d in dual:
        summary = d.get("recursive_summary", {})
        records.append({
            "code": d["code"],
            "name": d.get("name", ""),
            "gate": d.get("gate", "UNKNOWN"),
            "tier": d.get("tier", "UNKNOWN"),
            "dlp": d.get("dlp", 0),
            "price": d.get("price"),
            "ratio": d.get("ratio"),
            "stage1_confirmed": d.get("stage1_confirmed", False),
            "trend_score": summary.get("trend_score", 0),
            "seg_score": summary.get("segment_score", 0),
            "alignment": d.get("alignment", "unverified"),
            "recursive_direction": d.get("recursive_direction", ""),
            "interval_timing": d.get("interval_timing", {}),
            "source": f"dualscan:{path}",
        })
    print(f"[load] dualscan → {path} → {len(records)} 条")
    return records


def dedup(records: list[dict]) -> list[dict]:
    """按 code 去重: 取 gate 最优, 再取 trend_score 最高."""
    best: dict[str, dict] = {}
    for r in records:
        code = r["code"]
        if code not in best:
            best[code] = r
            continue
        cur = best[code]
        # gate 优先
        if GATE_RANK.get(r.get("gate", "UNKNOWN"), 99) < GATE_RANK.get(cur.get("gate", "UNKNOWN"), 99):
            best[code] = r
        elif GATE_RANK.get(r.get("gate", "UNKNOWN"), 99) == GATE_RANK.get(cur.get("gate", "UNKNOWN"), 99):
            # gate 相同 → trend_score 高者胜
            if r.get("trend_score", 0) > cur.get("trend_score", 0):
                best[code] = r
    return list(best.values())


def filter_gate(records: list[dict], include_blocked: bool = False) -> list[dict]:
    """gate 过滤."""
    if include_blocked:
        return records
    return [r for r in records if r.get("gate") in ("PASS", "OBSERVE")]


def sort_records(records: list[dict]) -> list[dict]:
    """排序: gate → tier → dlp desc → code."""
    return sorted(records, key=lambda r: (
        GATE_RANK.get(r.get("gate", "UNKNOWN"), 99),
        TIER_RANK.get(r.get("tier", "UNKNOWN"), 99),
        -(r.get("dlp") or 0),
        r.get("code", ""),
    ))


def merge_ledgers(
    local_paths: list[str],
    vmb_ledger: str | None = None,
    vmb_host: str | None = None,
    vmb_key: str | None = None,
    dualscan_path: str | None = None,
    include_blocked: bool = False,
) -> dict:
    """主流程: 加载 → 去重 → 过滤 → 排序 → 汇总."""
    all_records: list[dict] = []

    # 1. 本地 JSONL
    for p in local_paths:
        all_records.extend(load_jsonl(p))

    # 2. VM-B 远端
    if vmb_ledger and vmb_host:
        all_records.extend(load_jsonl(vmb_ledger, ssh_host=vmb_host, ssh_key=vmb_key))

    # 3. dualscan JSON (独立路径)
    if dualscan_path:
        all_records.extend(load_dualscan_json(dualscan_path))

    if not all_records:
        print("❌ 空输入 — 没有任何记录")
        sys.exit(1)

    total_before = len(all_records)

    # 4. 去重
    records = dedup(all_records)
    after_dedup = len(records)

    # 5. gate 过滤
    records_filtered = filter_gate(records, include_blocked)
    after_filter = len(records_filtered)

    # 6. 排序
    records_sorted = sort_records(records_filtered)

    # 7. 汇总
    from collections import Counter
    gate_dist = Counter(r.get("gate", "?") for r in records)
    tier_dist = Counter(r.get("tier", "?") for r in records)

    ts = datetime.now(SH_TZ).isoformat(timespec="seconds")

    result = {
        "ts": ts,
        "meta": {
            "total_raw": total_before,
            "after_dedup": after_dedup,
            "after_filter": after_filter,
            "include_blocked": include_blocked,
            "sources": local_paths + ([f"{vmb_host}:{vmb_ledger}"] if vmb_ledger else []) + ([dualscan_path] if dualscan_path else []),
        },
        "gate_distribution": dict(gate_dist),
        "tier_distribution": dict(tier_dist),
        "candidates": records_sorted,
    }

    # 8. 打印汇总
    print(f"\n{'='*60}")
    print(f"📊 chan_merge 汇总")
    print(f"{'='*60}")
    print(f"  原始:   {total_before} 条")
    print(f"  去重后: {after_dedup} 条")
    print(f"  过滤后: {after_filter} 条 {'(含 BLOCKED)' if include_blocked else '(仅 PASS+OBSERVE)'}")
    print(f"\n  Gate 分布:")
    for g in ["PASS", "OBSERVE", "BLOCKED", "UNKNOWN"]:
        if g in gate_dist:
            print(f"    {g:8s} {gate_dist[g]:>4}")
    print(f"\n  Tier 分布 (过滤后):")
    filtered_tiers = Counter(r.get("tier", "?") for r in records_sorted)
    for t in ["CORE", "OBSERVE", "EDGE", "UNKNOWN"]:
        if t in filtered_tiers:
            print(f"    {t:8s} {filtered_tiers[t]:>4}")
    if records_sorted:
        top5 = records_sorted[:5]
        print(f"\n  Top 5 候选:")
        for r in top5:
            print(f"    {r['code']} {r.get('gate','?'):7s} tier={r.get('tier','?'):7s} dlp={r.get('dlp',0):.3f} trend={r.get('trend_score',0):.1f}")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="chan_merge — 双机 meet-in 账本合并器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 合并本地 2 个账本
  chan_merge.py -i vm_a.jsonl vm_b.jsonl -o merged.json

  # 合并 + 从 dualscan 补数据
  chan_merge.py -i ledger.jsonl --from-dualscan dualscan_fixed.json -o merged.json

  # 从 VM-B SSH 拉
  chan_merge.py -i local.jsonl \\
      --vmb-host katelolita7788@35.212.190.147 \\
      --vmb-ledger ~/chan_logs/scan_ledger.jsonl \\
      -o merged.json
        """,
    )
    parser.add_argument("-i", "--input", action="append", default=[],
                        help="本地 JSONL 账本 (可多次指定)")
    parser.add_argument("--from-dualscan", default=None,
                        help="从 dualscan JSON 加载候选 (不依赖账本)")
    parser.add_argument("--vmb-host", default=None,
                        help="VM-B SSH 用户@host (如 katelolita7788@35.212.190.147)")
    parser.add_argument("--vmb-ledger", default=None,
                        help="VM-B 远端账本路径 (需配 --vmb-host)")
    parser.add_argument("--vmb-key", default=None,
                        help="VM-B SSH 私钥路径")
    parser.add_argument("-o", "--output", default="merged_candidates.json",
                        help="输出 JSON 路径 (默认: merged_candidates.json)")
    parser.add_argument("--include-blocked", action="store_true",
                        help="包含 BLOCKED gate (默认只保留 PASS+OBSERVE)")
    args = parser.parse_args()

    result = merge_ledgers(
        local_paths=args.input,
        vmb_ledger=args.vmb_ledger,
        vmb_host=args.vmb_host,
        vmb_key=args.vmb_key,
        dualscan_path=args.from_dualscan,
        include_blocked=args.include_blocked,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n💾 {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
