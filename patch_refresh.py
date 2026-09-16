#!/usr/bin/env python3
"""Patch universe_kline_refresh.py: add retry + explicit logging for fetch_minute/append_only"""
import re, sys

TARGET = "/home/gorgesoros39/TradingAgents-CN/scripts/chanlun-workflow/universe_kline_refresh.py"

with open(TARGET) as f:
    src = f.read()

changes = 0

# 1) Patch fetch_minute: add retry loop
old = '''def fetch_minute(code: str, tf: str) -> pd.DataFrame:
    prefix = "sh" if code.startswith("6") else "sz"
    return ak.stock_zh_a_minute(symbol=f"{prefix}{code}", period=MIN_PERIOD[tf])'''

new = '''def fetch_minute(code: str, tf: str, retries: int = 3) -> pd.DataFrame:
    """Fetch minute K-line with retry. akshare occasionally returns empty/jitters."""
    prefix = "sh" if code.startswith("6") else "sz"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            df = ak.stock_zh_a_minute(symbol=f"{prefix}{code}", period=MIN_PERIOD[tf], adjust="qfq")
            if df is None or len(df) == 0:
                last_err = "empty_result"
                time.sleep(0.5 * attempt); continue
            return df
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(0.5 * attempt)
    print(f"  [WARN] fetch_minute failed {code}/{tf} after {retries} attempts: {last_err}", file=sys.stderr)
    return pd.DataFrame()'''

if old in src:
    src = src.replace(old, new)
    print("PATCH 1 OK: fetch_minute retry")
    changes += 1
else:
    print("PATCH 1 SKIP: pattern not found (fetch_minute)")

# 2) Patch append_only error handling - make errors visible  
old2 = '''    except Exception as e:
        return {"code": code, "tf": tf, "ok": False, "error": str(e)[:80]}
    if df_live is None or len(df_live) == 0:
        return {"code": code, "tf": tf, "ok": False, "error": "empty"}'''

new2 = '''    except Exception as e:
        err_msg = str(e)[:120]
        print(f"  [ERROR] append_only {code}/{tf}: {err_msg}", file=sys.stderr)
        return {"code": code, "tf": tf, "ok": False, "error": err_msg}
    if df_live is None or len(df_live) == 0:
        print(f"  [WARN] append_only {code}/{tf}: empty result (retries exhausted)", file=sys.stderr)
        return {"code": code, "tf": tf, "ok": False, "error": "empty"}'''

if old2 in src:
    src = src.replace(old2, new2)
    print("PATCH 2 OK: append_only error logging")
    changes += 1
else:
    print("PATCH 2 SKIP: pattern not found (append_only)")

if changes == 0:
    print("NO CHANGES MADE - aborting")
    sys.exit(1)

# Backup + write
import shutil
shutil.copy2(TARGET, TARGET + ".bak")
with open(TARGET, "w") as f:
    f.write(src)
print(f"\nPatched {changes} sections. Backup at {TARGET}.bak")
