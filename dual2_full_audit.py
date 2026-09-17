#!/usr/bin/env python3
"""dual2_scan 正式版 full audit — 遍历所有 dualscan_*.json + scan_ledger 交叉验证"""
import glob, json, collections

# 1) 全部 dualscan (dual2_scan 实时生成)
files = sorted(glob.glob("/home/gorgesoros39/chan_logs/dualscan_*.json"))
print(f"=== dual2_scan 正式版 full audit ===")
print(f"dualscan_*.json 文件数: {len(files)}")

unison_true_entries = []
all_entries = 0
r2_dist = collections.Counter()
r0_dist = collections.Counter()
r1_dist = collections.Counter()
gate_dist = collections.Counter()

for f in files:
    try:
        data = json.load(open(f))
    except Exception as e:
        continue
    for e in data.get("dual", []):
        all_entries += 1
        rs = e.get("recursive_summary", {})
        unison = rs.get("three_way_unison")
        r2_dist[rs.get("r2_dir")] += 1
        r0_dist[rs.get("r0_dir")] += 1
        r1_dist[rs.get("r1_dir")] += 1
        gate_dist[e.get("gate")] += 1
        if unison == True:
            unison_true_entries.append({
                "code": e.get("code"),
                "gate": e.get("gate"),
                "dlp": e.get("dlp"),
                "r0": rs.get("r0_dir"),
                "r1": rs.get("r1_dir"),
                "r2": rs.get("r2_dir"),
                "ib": e.get("interval_timing", {}).get("bullish_confirmed"),
                "score": e.get("score"),
                "vm": e.get("vm"),
                "file": f.split("/")[-1],
            })

print(f"dual entries 总数: {all_entries}")
print(f"  gate 分布:    {dict(gate_dist)}")
print(f"  r0_dir 分布:  {dict(r0_dist)}")
print(f"  r1_dir 分布:  {dict(r1_dist)}")
print(f"  r2_dir 分布:  {dict(r2_dist)}")

# 2) 去重 (同一 code 多次出现取最新文件)
by_code = {}
for s in unison_true_entries:
    code = s["code"]
    # 后出现的覆盖
    by_code[code] = s

print(f"\n=== three_way_unison=true (原始 {len(unison_true_entries)} 条, 去重 {len(by_code)} 只) ===")
for i, (code, s) in enumerate(sorted(by_code.items()), 1):
    print(f"  {i:>2}. {s['code']} gate={s['gate']:>7} dlp={s['dlp']} R0={s['r0']} R1={s['r1']} R2={s['r2']} ib={s['ib']} score={s['score']} vm={s['vm']} [{s['file'][:20]}]")

# 3) 交叉验证 scan_ledger
LEDGER = "/home/gorgesoros39/chan_logs/scan_ledger.jsonl"
print(f"\n=== scan_ledger 交叉验证 ===")
ledger_gate = collections.Counter()
ledger_codes = set()
pass_codes = set()
with open(LEDGER) as fh:
    for line in fh:
        d = json.loads(line.strip())
        ledger_gate[d.get("gate")] += 1
        ledger_codes.add(d.get("code"))
        if d.get("gate") == "PASS":
            pass_codes.add(d.get("code"))
print(f"scan_ledger gate 分布: {dict(ledger_gate)}")
print(f"scan_ledger unique codes: {len(ledger_codes)}")
print(f"scan_ledger PASS codes: {len(pass_codes)}")

# 4) three_way_unison=true 的 code 在 ledger 里是什么 gate?
print(f"\n=== three_way_unison codes 在 scan_ledger 里的 gate ===")
for code, s in sorted(by_code.items()):
    # 找 ledger 里最新的这条 code
    ledger_rows = []
    with open(LEDGER) as fh:
        for line in fh:
            d = json.loads(line.strip())
            if d.get("code") == code:
                ledger_rows.append(d)
    latest = ledger_rows[-1] if ledger_rows else None
    if latest:
        print(f"  {code}: dual2 gate={s['gate']} ledger gate={latest['gate']} ledger dlp={latest.get('dlp')} ledger ts={latest.get('ts')}")
    else:
        print(f"  {code}: dual2 gate={s['gate']} NOT IN LEDGER")

# 5) PASS + three_way_unison 的候选（交易级）
print(f"\n=== 交易级候选: three_way_unison=true AND gate=PASS AND dlp 非空 ===")
tradeable = [s for s in by_code.values()
             if s["gate"] == "PASS" and s["dlp"] is not None and s["dlp"] > 0]
print(f"数量: {len(tradeable)}")
for i, s in enumerate(tradeable, 1):
    print(f"  {i}. {s['code']} dlp={s['dlp']:.4f} R2={s['r2']} ib={s['ib']} score={s['score']}")

print("\nDONE")
