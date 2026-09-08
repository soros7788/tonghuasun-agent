#!/usr/bin/env python3
"""preload_kline_cache — 在 import tradingagents 之前注入 kline_cache 本地 mirror.

用法:
  PYTHONPATH=/path/to/preload_kline_cache.py python dual_scan.py --near 30 --sleep 1
  # 或:
  python -c "import preload_kline_cache; preload_kline_cache.install(); from dual_scan import main; main()"

原理: monkeypatch ak.stock_zh_a_minute 全局引用, 优先读本地 CSV mirror, miss fallback 真 AKShare.

CSV mirror 约定 (sync_market_kline_v2.py 生成):
  文件名: {code}_{period}.csv    e.g. 000001_5m.csv, 600001_30m.csv
  列: day,open,high,low,close,volume,amount  (与 akshare 原始返回一致)

注入后 dual_scan / chanlun_strategy 等全部自动走本地读, 无需改 sealed 代码.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd

# ── 配置 ────────────────────────────────────────────────────────────────────
DEFAULT_MIRROR = os.environ.get(
    "KLINE_CACHE_DIR",
    str(Path.home() / "TradingAgents-CN" / "kline_cache"),
)
PERIOD_MAP = {
    "1": "1m",
    "5": "5m",
    "30": "30m",
    "day": "day",
}
# akshare 的 adjust 参数映射到 mirror 文件名里不需要 (无 adjust 后缀)

_installed = False
_mirror_dir: Optional[Path] = None
_hit = 0
_miss = 0
_fallback = 0
_total = 0


def _strip_provider_symbol(symbol: str) -> str:
    """akshare symbol 'sh600001' / 'sz000001' → mirror code '600001' / '000001'."""
    s = symbol.lower()
    if s.startswith(("sh", "sz", "bj")):
        return s[2:]
    return symbol


def _mirror_lookup(code: str, period: str) -> Optional[pd.DataFrame]:
    """查本地 mirror CSV, 命中返回 DataFrame (列名与 akshare 一致), miss 返回 None."""
    global _hit
    assert _mirror_dir is not None
    mirror_period = PERIOD_MAP.get(period, period)
    csv_path = _mirror_dir / f"{code}_{mirror_period}.csv"
    if not csv_path.exists():
        return None
    try:
        df = pd.read_csv(csv_path)
        if df.empty or "day" not in df.columns:
            return None
        _hit += 1
        return df
    except Exception:
        return None


def install(mirror_dir: Optional[str] = None) -> bool:
    """monkeypatch ak.stock_zh_a_minute, 全局生效. 返回是否成功注入."""
    global _installed, _mirror_dir
    if _installed:
        return True

    _mirror_dir = Path(mirror_dir or DEFAULT_MIRROR)
    if not _mirror_dir.exists():
        # mirror 不存在就不装, 让调用方正常走 AKShare
        print(f"[preload_kline_cache] mirror 不存在: {_mirror_dir} → 跳过注入")
        return False

    try:
        import akshare as ak
    except ImportError:
        print("[preload_kline_cache] akshare 未安装 → 跳过注入")
        return False

    original_fetch = ak.stock_zh_a_minute

    def cached_fetch(symbol: str, period: str = "5", adjust: str = "", **kwargs) -> pd.DataFrame:
        """优先本地 mirror, miss fallback."""
        global _miss, _fallback, _total
        _total += 1

        code = _strip_provider_symbol(symbol)
        cached = _mirror_lookup(code, period)
        if cached is not None:
            return cached

        _miss += 1
        _fallback += 1
        return original_fetch(symbol=symbol, period=period, adjust=adjust, **kwargs)

    ak.stock_zh_a_minute = cached_fetch
    _installed = True
    print(f"[preload_kline_cache] ✅ 已注入 ak.stock_zh_a_minute → mirror={_mirror_dir}")
    return True


def stats() -> str:
    global _installed, _hit, _miss, _fallback, _total
    if not _installed:
        return "[preload_kline_cache] 未注入"
    hit_rate = _hit / _total * 100 if _total else 0
    return (
        f"[preload_kline_cache] 命中 {_hit}/{_total} ({hit_rate:.1f}%) "
        f"| fallback AKShare {_fallback} 次 | mirror={_mirror_dir}"
    )


# 自动 install (当 `python -c "import preload_kline_cache"` 时)
auto = os.environ.get("KLINE_CACHE_AUTOINSTALL", "1") == "1"
if auto:
    install()
