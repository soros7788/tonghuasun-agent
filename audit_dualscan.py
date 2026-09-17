#!/usr/bin/env python3
"""Correct dualscan three_way_unison audit."""
import glob, json, collections

files = glob.glob("/home/gorgesoros39/chan_logs/dualscan_*.json")
print(f"文件总数: {len(files)}")

unison_vals = []
r0_dir_vals = []
r1_dir_vals = []
r2_dir_vals = []
gate_vals = []
interval_bullish = []
unison_true = []
total = 0

for f in sorted(files):
    try: data = json.load(open(f))
    except: continue
    for e in data.get("dual", []):
        total += 1
        rs = e.get("recursive_summary", {})
        it = e.get("interval_timing", {})
        unison_vals.append(rs.get("three_way_unison"))
        r0_dir_vals.append(rs.get("r0_dir"))
        r1_dir_vals.append(rs.get("r1_dir"))
        r2_dir_vals.append(rs.get("r2_dir"))
        gate_vals.append(e.get("gate"))
        interval_bullish.append(it.get("bullish_confirmed"))
        if rs.get("three_way_unison") == True:
            unison_true.append(e)

print(f"\n总 entry: {total}")
print(f"\n=== 真实分布 ===")
print(f"three_way_unison:        {dict(collections.Counter(unison_vals))}")
print(f"r0_dir (趋势):           {dict(collections.Counter(r0_dir_vals))}")
print(f"r1_dir (线段):           {dict(collections.Counter(r1_dir_vals))}")
print(f"r2_dir (走势类型):       {dict(collections.Counter(r2_dir_vals))}")
print(f"gate:                    {dict(collections.Counter(gate_vals))}")
print(f"interval.bullish_confirmed ≥2: {sum(1 for x in interval_bullish if x is not None and x >= 2)}/{sum(1 for x in interval_bullish if x is not None)}")

print(f"\n=== three_way_unison=true 样本 ({len(unison_true)} 条) ===")
for i, e in enumerate(unison_true, 1):
    rs = e.get("recursive_summary", {})
    it = e.get("interval_timing", {})
    print(f"  {i:>2}. {e.get('code')} {e.get('name',''):>4} gate={e.get('gate'):>7} dlp={e.get('dlp',0):.4f} "
          f"R0={rs.get('r0_dir')} R1={rs.get('r1_dir')} R2={rs.get('r2_dir')} "
          f"ib={it.get('bullish_confirmed')}/{it.get('total')} score={e.get('score')} vm={e.get('vm')}")
