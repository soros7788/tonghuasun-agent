
import glob, json
files = glob.glob("/home/gorgesoros39/chan_logs/dualscan_*.json")
samples = []
for f in sorted(files):
    try: data = json.load(open(f))
    except: continue
    for e in data.get("dual", []):
        rs = e.get("recursive_summary", {})
        if rs.get("three_way_unison") == True:
            it = e.get("interval_timing", {})
            samples.append({
                "code": e.get("code"),
                "name": e.get("name",""),
                "gate": e.get("gate"),
                "dlp": e.get("dlp") if e.get("dlp") is not None else "N/A",
                "r0": rs.get("r0_dir"),
                "r1": rs.get("r1_dir"),
                "r2": rs.get("r2_dir"),
                "ib": it.get("bullish_confirmed"),
                "score": e.get("score"),
                "vm": e.get("vm"),
            })
print(json.dumps(samples, ensure_ascii=False, indent=2))
