#!/usr/bin/env python3
"""fix_1m_cache.py - bulk fix stale _1m.csv caches"""
import sys, os, time, argparse
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import akshare as ak

KLINE_DIR = Path(os.environ.get("KLINE_CACHE_DIR",
    "/home/gorgesoros39/kline_cache_local"))
ONE_MIN_CAP = 5000

def _code_bs(code):
    # akshare stock_zh_a_minute: NO DOT! sh603737 / sz000001
    return f"sh{code}" if code.startswith(("6", "9")) else f"sz{code}"

def fetch_1m_akshare(code, retries=3):
    for attempt in range(1, retries + 1):
        try:
            sym = _code_bs(code)
            df = ak.stock_zh_a_minute(symbol=sym, period="1", adjust="qfq")
            if df is None or len(df) == 0:
                print(f"  [{code}] akshare empty (attempt {attempt})")
                time.sleep(0.5); continue
            if "day" not in df.columns and "date" in df.columns:
                df = df.rename(columns={"date": "day"})
            df["day"] = pd.to_datetime(df["day"]).dt.strftime("%Y-%m-%d %H:%M:%S")
            wanted = ["day", "open", "high", "low", "close", "volume", "amount"]
            for c in wanted:
                if c not in df.columns: df[c] = pd.NA
            return df[wanted]
        except Exception as e:
            print(f"  [{code}] akshare error (attempt {attempt}/{retries}): {type(e).__name__}: {e}")
            time.sleep(0.5)
    return None

def merge_csv(code, new_df, target_dir=None):
    if target_dir is None: target_dir = KLINE_DIR
    target = Path(target_dir) / f"{code}_1m.csv"
    old_rows = 0; old_last = ""
    if target.exists():
        try:
            old_df = pd.read_csv(target, dtype={"day": str})
            old_df["day"] = pd.to_datetime(old_df["day"]).dt.strftime("%Y-%m-%d %H:%M:%S")
            old_rows = len(old_df)
            old_last = old_df["day"].iloc[-1]
            merged = pd.concat([old_df, new_df], ignore_index=True)
            merged = merged.drop_duplicates(subset=["day"], keep="last").sort_values("day")
        except Exception as e:
            print(f"  [{code}] old CSV read fail ({e}), using new only")
            merged = new_df
    else:
        merged = new_df
    if len(merged) > ONE_MIN_CAP:
        merged = merged.tail(ONE_MIN_CAP).reset_index(drop=True)
    merged.to_csv(target, index=False)
    return old_rows, old_last, len(merged), merged["day"].iloc[-1]

def fix_code(code):
    print(f"[{code}]", end=" ", flush=True)
    t0 = time.time()
    new_df = fetch_1m_akshare(code)
    elapsed = time.time() - t0
    if new_df is None:
        print(f"X akshare FAILED ({elapsed:.1f}s)")
        return False
    old_n, old_last, new_n, new_last = merge_csv(code, new_df)
    print(f"v {old_n}->{new_n} rows ({elapsed:.1f}s), last: {old_last} -> {new_last}")
    return True

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*")
    ap.add_argument("--all-stuck", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--stale-days", type=int, default=2)
    args = ap.parse_args()
    if not args.codes and not args.all_stuck:
        print("Usage: fix_1m_cache.py CODE1 CODE2 ...  OR  --all-stuck [--limit N] [--stale-days 2]")
        sys.exit(1)
    if args.all_stuck:
        today = datetime.now()
        cutoff = (today - timedelta(days=args.stale_days)).strftime("%Y-%m-%d")
        stuck = []
        for csv in sorted(KLINE_DIR.glob("*_1m.csv")):
            try:
                tail = pd.read_csv(csv, usecols=["day"], dtype=str).tail(1)
                last_date = tail["day"].iloc[0][:10] if len(tail) > 0 else ""
                if last_date < cutoff:
                    code = csv.stem.replace("_1m", "")
                    row_n = sum(1 for _ in open(csv)) - 1
                    stuck.append((code, last_date, row_n))
            except: pass
        if args.limit > 0: stuck = stuck[:args.limit]
        print(f"=== Stuck 1m files (last_date < {cutoff}): {len(stuck)} ===")
        for c, d, n in stuck[:15]:
            print(f"  {c}: last={d} rows={n}")
        codes = [c for c, _, _ in stuck]
    else:
        codes = args.codes
    ok = fail = 0
    for i, code in enumerate(codes):
        print(f"\n[{i+1}/{len(codes)}]", end=" ")
        try:
            if fix_code(code): ok += 1
            else: fail += 1
        except Exception as e:
            print(f"  UNEXPECTED: {e}"); fail += 1
        time.sleep(0.3)
    print(f"\n=== DONE: {ok} ok, {fail} fail ===")

if __name__ == "__main__":
    main()
