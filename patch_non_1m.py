#!/usr/bin/env python3
"""Patch non-1m TFs (day/5m/15m/30m/60m) for key codes that got stuck in 1970-row window."""
import sys, time, re
from pathlib import Path
import pandas as pd
import akshare as ak

K = Path.home() / "kline_cache_local"

def fetch_minute_full(code: str, period_min: int, retries: int = 4) -> pd.DataFrame:
    pf = "sh" if code.startswith("6") else "sz"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            df = ak.stock_zh_a_minute(symbol=f"{pf}{code}", period=period_min, adjust="qfq")
            if df is None or len(df) == 0:
                last_err = "empty"; time.sleep(0.6 * attempt); continue
            return df
        except Exception as e:
            last_err = f"{type(e).__name__}:{str(e)[:60]}"; time.sleep(0.8 * attempt)
    print(f"    FAIL after {retries}: {last_err}"); return pd.DataFrame()

def fetch_day(code: str, retries: int = 4) -> pd.DataFrame:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            df = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq", start_date="20240101", end_date="20261231")
            if df is None or len(df) == 0:
                last_err = "empty"; time.sleep(0.6 * attempt); continue
            return df
        except Exception as e:
            last_err = f"{type(e).__name__}:{str(e)[:60]}"; time.sleep(1.0 * attempt)
    print(f"    FAIL after {retries}: {last_err}"); return pd.DataFrame()

COL_MAP = {
    "时间": "datetime", "日期": "datetime",
    "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
    "成交量": "volume", "成交额": "amount",
}
def norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    new_cols = []
    for c in df.columns:
        clean = re.sub(r"[\uFF08(].*?[\uFF09)]", "", str(c)).strip()
        new_cols.append(COL_MAP.get(clean, c))
    df.columns = new_cols
    keep = [c for c in ["datetime", "open", "high", "low", "close", "volume", "amount"] if c in df.columns]
    return df[keep]

def merge_save(code: str, tf: str, new_df: pd.DataFrame):
    f = K / f"{code}_{tf}.csv"
    old_n = len(open(f).readlines()) if f.exists() else 0
    if len(new_df) == 0:
        print(f"    {tf}: no data"); return
    new_df = norm_cols(new_df)
    if f.exists():
        old_df = pd.read_csv(f)
        try:
            old_df = norm_cols(old_df)
        except Exception:
            pass
        new_df = pd.concat([old_df, new_df]).drop_duplicates(subset="datetime", keep="last").sort_values("datetime")
    new_df.to_csv(f, index=False)
    last_dt = new_df.iloc[-1]["datetime"] if len(new_df) else "?"
    print(f"    {tf}: {old_n} -> {len(new_df)} rows, last={last_dt}")

TF_MIN = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}

codes = sys.argv[1:]
if not codes:
    # Default: codes we know have stuck non-1m
    codes = ["603737", "000016", "603811"]

print(f"PATCH_NON_1M: {len(codes)} codes: {codes}\n")
for code in codes:
    print(f"[{code}]")
    # day
    df = fetch_day(code)
    merge_save(code, "day", df); time.sleep(1.0)
    # minute TFs
    for tf, pmin in TF_MIN.items():
        df = fetch_minute_full(code, pmin)
        merge_save(code, tf, df); time.sleep(0.6)
    print()
print("DONE")
