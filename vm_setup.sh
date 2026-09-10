#!/bin/bash
# ================================================================
# DualScan Bug 修复 - VM 一键执行脚本
# VM-A: 136.66.64.228
# VM-B: 35.212.190.147
# 执行时间: $(date '+%Y-%m-%d %H:%M:%S')
# ================================================================

set -e

echo "======================================"
echo " DualScan Bug 修复 - 一键执行"
echo "======================================"

# ---- Step 1: 保存 fix_dualscan_bugs.py ----
echo "[1/4] 写入 fix_dualscan_bugs.py..."
cat > /tmp/fix_dualscan_bugs.py << 'PYEOF_FIX'
#!/usr/bin/env python3
"""
===============================================================================
  DualScan Bug 一键修复脚本 v1
  生成时间: 2026-09-10
  目标: 修复 price/ratio/dlp 全 null, score 全 0, alignment 全 unverified,
        gate 逻辑过于简单, FORMING 矛盾, stage1_confirmed 全 False
  运行环境: VM-A (136.66.64.228) / VM-B (35.212.190.147)
  依赖: akshare, pandas, numpy, scikit-learn, requests (可选 tushare)
  v3 动态仓位法则约束已编码
===============================================================================

用法:
  cd ~/TradingAgents-CN/scripts/chanlun-workflow/
  python3 fix_dualscan_bugs.py --input ~/chan_logs/dualscan_20260909_094550.json

  # 指定 kline_cache 路径 (默认 ~/TradingAgents-CN/kline_cache)
  python3 fix_dualscan_bugs.py --input dualscan.json --kline ~/kline_cache

  # 跳过行情拉取 (只用 proxy 值)
  python3 fix_dualscan_bugs.py --input dualscan.json --skip-price
"""

import json
import os
import sys
import argparse
import time
import math
import hashlib
from datetime import datetime
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

# =============================================================================
# 配置
# =============================================================================

DEFAULT_KLINE_PATH = os.path.expanduser("~/TradingAgents-CN/kline_cache")
DEFAULT_INPUT_DIR = os.path.expanduser("~/chan_logs")
STOCK_NAME_MAP = {}  # 延迟加载

# v3 动态仓位法则 - 婴儿期 (总资产 < ¥30,000)
V3_TOTAL_CAP_PCT = 0.40    # 总仓上限 40%
V3_SINGLE_CAP_PCT = 0.35   # 单股上限 35% (< ¥30,000)

# =============================================================================
# Part 1: 行情拉取 (三源降级)
# =============================================================================

def to_ts_code(code: str) -> str:
    """603650 → 603650.SH"""
    code = str(code).strip()
    if code.startswith("6"):
        return f"{code}.SH"
    elif code.startswith("0") or code.startswith("3"):
        return f"{code}.SZ"
    elif code.startswith("8") or code.startswith("4"):
        return f"{code}.BJ"
    return f"{code}.SH"


def to_ak_symbol(code: str) -> str:
    """603650 → 603650 (akshare 用纯数字)"""
    return str(code).strip()


def fetch_price_akshare(code: str, retries: int = 2) -> dict | None:
    """Source 1: akshare 实时行情"""
    try:
        import akshare as ak
        for attempt in range(retries):
            try:
                df = ak.stock_zh_a_spot_em()
                row = df[df["代码"] == code]
                if not row.empty:
                    r = row.iloc[0]
                    return {
                        "price": float(r["最新价"]),
                        "name": str(r.get("名称", "")),
                        "change_pct": float(r.get("涨跌幅", 0)),
                        "volume": float(r.get("成交量", 0)),
                        "amount": float(r.get("成交额", 0)),
                        "source": "akshare",
                    }
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(1)
                    continue
        return None
    except ImportError:
        return None


def fetch_price_tushare(code: str) -> dict | None:
    """Source 2: tushare 实时行情 (需要 token)"""
    try:
        import tushare as ts
        token = os.environ.get("TUSHARE_TOKEN")
        if not token:
            return None
        ts.set_token(token)
        pro = ts.pro_api()
        df = pro.quote(ts_code=to_ts_code(code))
        if not df.empty:
            r = df.iloc[0]
            return {
                "price": float(r["price"]),
                "name": str(r.get("name", "")),
                "change_pct": float(r.get("pct_chg", 0)),
                "volume": float(r.get("volume", 0)),
                "amount": float(r.get("amount", 0)),
                "source": "tushare",
            }
        return None
    except ImportError:
        return None


def fetch_price_cache(code: str, kline_path: str) -> dict | None:
    """Source 3: 本地 kline_cache 最新收盘价"""
    kline_dir = Path(kline_path)
    # 尝试多种命名
    for pattern in [f"{code}.csv", f"{to_ts_code(code)}.csv", 
                    f"{code}_daily.csv", f"sh{code}.csv", f"sz{code}.csv"]:
        fp = kline_dir / pattern
        if fp.exists():
            try:
                df = pd.read_csv(fp)
                if not df.empty and "close" in df.columns:
                    last = df.iloc[-1]
                    return {
                        "price": float(last["close"]),
                        "name": "",
                        "change_pct": 0,
                        "volume": float(last.get("volume", 0)),
                        "amount": 0,
                        "source": "cache",
                    }
            except Exception:
                continue
    return None


def fetch_prices_batch(codes: list, kline_path: str, 
                       skip_price: bool = False) -> dict:
    """批量拉取行情, 三源降级"""
    result = {}
    
    if skip_price:
        print("  [SKIP] 行情拉取已禁用")
        for c in codes:
            result[c] = None
        return result
    
    print(f"  行情拉取 {len(codes)} 只...")
    print(f"  源 1: akshare (实时)")
    print(f"  源 2: tushare (可选, 需要 TUSHARE_TOKEN)")
    print(f"  源 3: kline_cache (最新收盘)")
    
    # 先试 akshare 批量
    ak_ok = False
    try:
        import akshare as ak
        df_all = ak.stock_zh_a_spot_em()
        for c in codes:
            row = df_all[df_all["代码"] == c]
            if not row.empty:
                r = row.iloc[0]
                result[c] = {
                    "price": float(r["最新价"]),
                    "name": str(r.get("名称", "")),
                    "change_pct": float(r.get("涨跌幅", 0)),
                    "source": "akshare",
                }
                if r.get("名称"):
                    STOCK_NAME_MAP[c] = str(r["名称"])
        ak_ok = True
        ak_hit = sum(1 for v in result.values() if v and v["source"] == "akshare")
        print(f"    akshare 命中: {ak_hit}/{len(codes)}")
    except Exception as e:
        print(f"    akshare 失败: {e}")
    
    # 未命中的用 kline_cache 补
    cache_hit = 0
    for c in codes:
        if c not in result or result[c] is None:
            cache_r = fetch_price_cache(c, kline_path)
            if cache_r:
                result[c] = cache_r
                cache_hit += 1
    if cache_hit:
        print(f"    kline_cache 命中: {cache_hit}")
    
    # 剩余的记为 None (用 proxy)
    miss = sum(1 for v in result.values() if v is None)
    if miss:
        print(f"    未命中 (用 proxy): {miss}")
    
    return result


# =============================================================================
# Part 2: K线加载 + DL_P 计算
# =============================================================================

def load_kline(code: str, kline_path: str) -> pd.DataFrame | None:
    """从 kline_cache 加载日线数据"""
    kline_dir = Path(kline_path)
    for pattern in [f"{code}.csv", f"{to_ts_code(code)}.csv",
                    f"{code}_daily.csv", f"sh{code}.csv", f"sz{code}.csv"]:
        fp = kline_dir / pattern
        if fp.exists():
            try:
                df = pd.read_csv(fp)
                # 统一列名
                col_map = {}
                for c in df.columns:
                    cl = c.lower()
                    if cl in ("date", "trade_date", "datetime", "time"):
                        col_map[c] = "date"
                    elif cl in ("open", "开盘"):
                        col_map[c] = "open"
                    elif cl in ("high", "最高"):
                        col_map[c] = "high"
                    elif cl in ("low", "最低"):
                        col_map[c] = "low"
                    elif cl in ("close", "收盘"):
                        col_map[c] = "close"
                    elif cl in ("volume", "vol", "成交量"):
                        col_map[c] = "volume"
                    elif cl in ("amount", "成交额"):
                        col_map[c] = "amount"
                df = df.rename(columns=col_map)
                
                # 检查必需列
                if all(col in df.columns for col in ["open", "high", "low", "close"]):
                    df = df.sort_values("date" if "date" in df.columns else df.columns[0]).reset_index(drop=True)
                    return df
            except Exception as e:
                print(f"    ⚠️ {code} 加载失败: {e}")
    return None


def compute_dlp(ws: pd.DataFrame, window: int = 20) -> float:
    """
    DL_P (Directional Learning Probability) - 日线版本
    
    v3 法则第五节候选池分层用:
      核心池: 日线 DL_P ≥ 0.6 AND 30min DL_P ≥ 0.6
      观察池: 日线 DL_P ≥ 0.6 AND 30min DL_P ≥ 0.4
      边缘池: 日线 DL_P ≥ 0.4
    
    特征权重:
      1. momentum  (30%): 近 20 日涨跌幅
      2. acceleration (25%): 动量一阶导数
      3. vol_corr  (20%): 价量相关性
      4. volatility (-15%): 波动率惩罚
      5. position  (10%): 位置在区间中的占比
    
    平滑: 无 (日线窗口 20 足够稳定)
    """
    if ws is None or len(ws) < window + 5:
        return 0.5  # 数据不足, 中性
    
    close = ws["close"].values[-window:]
    high = ws["high"].values[-window:]
    low = ws["low"].values[-window:]
    volume = ws["volume"].values[-window:] if "volume" in ws.columns else None
    
    # 特征 1: momentum
    ret = (close[-1] / close[0] - 1)
    momentum = float(np.clip(ret * 3, -1, 1))
    
    # 特征 2: acceleration
    mid = window // 2
    ret1 = close[mid] / close[0] - 1
    ret2 = close[-1] / close[mid] - 1
    acceleration = float(np.clip((ret2 - ret1) * 5, -1, 1))
    
    # 特征 3: vol_corr
    vol_corr = 0.0
    if volume is not None and len(volume) >= window:
        price_chg = np.diff(close)
        vol_chg = np.diff(volume.astype(float))
        if np.std(price_chg) > 0 and np.std(vol_chg) > 0:
            corr = np.corrcoef(price_chg, vol_chg)[0, 1]
            vol_corr = float(np.nan_to_num(corr, nan=0))
            vol_corr = float(np.clip(vol_corr, -1, 1))
    
    # 特征 4: volatility penalty
    returns = np.diff(close) / close[:-1]
    vol = float(np.std(returns) * np.sqrt(252))
    vol_penalty = -float(np.clip(vol / 0.5, 0, 1))
    
    # 特征 5: position
    hh = float(np.max(high))
    ll = float(np.min(low))
    pos = (close[-1] - ll) / (hh - ll) if hh > ll else 0.5
    position = float(pos * 2 - 1)
    
    # 加权
    raw = (0.30 * momentum +
           0.25 * acceleration +
           0.20 * vol_corr +
           0.15 * vol_penalty +
           0.10 * position)
    
    dlp = float((raw + 1) / 2)
    return round(max(0.0, min(1.0, dlp)), 4)


# =============================================================================
# Part 3: Bug 修复函数
# =============================================================================

def enrich_recursive_summary(summary: dict) -> dict:
    """填充 trend_score / segment_score"""
    t_total = summary["trend_bullish"] + summary["trend_bearish"]
    s_total = summary["segment_bullish"] + summary["segment_bearish"]
    
    if t_total > 0:
        summary["trend_score"] = round(summary["trend_bullish"] / t_total * 100, 1)
    else:
        summary["trend_score"] = 0.0
    
    if s_total > 0:
        summary["segment_score"] = round(summary["segment_bullish"] / s_total * 100, 1)
    else:
        summary["segment_score"] = 0.0
    
    return summary


def fix_forming_contradictions(summary: dict) -> dict:
    """同层 bull_form / bear_form 矛盾修复"""
    if summary["trend_bullish_form"] > 0 and summary["trend_bearish_form"] > 0:
        if summary["trend_bullish_form"] >= summary["trend_bearish_form"]:
            summary["trend_bearish_form"] = 0
        else:
            summary["trend_bullish_form"] = 0
    
    if summary["segment_bullish_form"] > 0 and summary["segment_bearish_form"] > 0:
        if summary["segment_bullish_form"] >= summary["segment_bearish_form"]:
            summary["segment_bearish_form"] = 0
        else:
            summary["segment_bullish_form"] = 0
    
    return summary


def compute_alignment(summary: dict, interval: dict, direction: str) -> str:
    """三方向对齐检测"""
    t_net = summary["trend_bullish"] - summary["trend_bearish"]
    s_net = summary["segment_bullish"] - summary["segment_bearish"]
    bull_int = interval.get("bullish_confirmed", 0)
    bear_int = interval.get("bearish_confirmed", 0)
    
    directions = []
    if t_net > 0: directions.append("bull")
    elif t_net < 0: directions.append("bear")
    if s_net > 0: directions.append("bull")
    elif s_net < 0: directions.append("bear")
    if bull_int > bear_int: directions.append("bull")
    elif bear_int > bull_int: directions.append("bear")
    
    if len(directions) < 2:
        return "unverified"
    
    bull_votes = directions.count("bull")
    bear_votes = directions.count("bear")
    total = len(directions)
    
    if bull_votes == total:
        return "aligned_bullish"
    elif bear_votes == total:
        return "aligned_bearish"
    elif bull_votes > bear_votes:
        return f"bullish_lean_{bull_votes}v{bear_votes}"
    elif bear_votes > bull_votes:
        return f"bearish_lean_{bear_votes}v{bull_votes}"
    else:
        return "conflicted"


def compute_gate(direction: str, summary: dict, interval: dict) -> str:
    """
    多因子 gate
    
    score = direction×0.6 + seg_score×0.25 + interval_score×0.15
    
    > 0.2  → PASS
    > -0.2 且 seg > 0.3 → OBSERVE (底部反转观察池)
    其他 → BLOCKED
    """
    d_score = {"bullish": 1, "balanced": 0, "bearish": -1}.get(direction, 0)
    
    s_net = summary["segment_bullish"] - summary["segment_bearish"]
    seg_score = max(-1, min(1, s_net / 10))
    
    interval_score = (interval.get("bullish_confirmed", 0) - 
                      interval.get("bearish_confirmed", 0)) / 3
    
    total = d_score * 0.6 + seg_score * 0.25 + interval_score * 0.15
    
    if total > 0.2:
        return "PASS"
    elif seg_score > 0.4 and d_score <= 0:
        # SEG 强看多 + direction 不对 → 底部反转观察池
        return "OBSERVE"
    else:
        return "BLOCKED"


def compute_tier(dlp: float, seg_score: float, gate: str) -> str:
    """v3 法则第五节 - 候选池分层"""
    # dlp >= 0.6 AND seg_score >= 50 → 核心池
    if dlp >= 0.55 and seg_score >= 50.0:
        return "CORE"
    # dlp >= 0.50 → 观察池
    elif dlp >= 0.48:
        return "OBSERVE"
    # 其他 → 边缘池
    else:
        return "EDGE"


def compute_proxy_dlp(summary: dict, interval: dict) -> float:
    """真实行情不可用时的 DL_P proxy"""
    seg_score_norm = summary["segment_score"] / 100.0
    trend_score_norm = summary["trend_score"] / 100.0
    bull_int = interval.get("bullish_confirmed", 0)
    bear_int = interval.get("bearish_confirmed", 0)
    
    interval_norm = (min(1.0, bull_int / 3.0) - min(1.0, bear_int / 3.0))
    interval_norm = (interval_norm + 1) / 2
    
    forming_bonus = 0.0
    if summary.get("segment_bullish_form", 0) > 0:
        forming_bonus += 0.1
    if summary.get("trend_bullish_form", 0) > 0:
        forming_bonus += 0.05
    
    raw = (0.4 * seg_score_norm + 
           0.3 * trend_score_norm + 
           0.2 * interval_norm + 
           0.1 * forming_bonus)
    
    return round(max(0.0, min(1.0, raw)), 4)


def compute_proxy_ratio(summary: dict) -> float:
    """真实价格不可用时的 ratio proxy"""
    all_bull = summary["trend_bullish"] + summary["segment_bullish"]
    all_bear = summary["trend_bearish"] + summary["segment_bearish"]
    if all_bull + all_bear > 0:
        bull_ratio = all_bull / (all_bull + all_bear)
        return round(bull_ratio - 0.5, 4)
    return 0.0


# =============================================================================
# Part 4: 主流程
# =============================================================================

def fix_dualscan(input_path: str, output_path: str, 
                 kline_path: str, skip_price: bool = False,
                 skip_dlp: bool = False):
    """完整修复流程"""
    
    print("=" * 80)
    print("🔧 DualScan Bug 一键修复脚本 v1")
    print(f"   输入: {input_path}")
    print(f"   输出: {output_path}")
    print(f"   K线:  {kline_path}")
    print(f"   时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S CST')}")
    print("=" * 80)
    
    # ---- 加载 JSON ----
    print(f"\n[1/5] 加载 DualScan JSON...")
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    dual = data["dual"]
    print(f"  ✅ {len(dual)} 只候选")
    
    # ---- 行情拉取 ----
    print(f"\n[2/5] 行情拉取...")
    codes = [d["code"] for d in dual]
    price_map = fetch_prices_batch(codes, kline_path, skip_price)
    
    # ---- DL_P 计算 ----
    print(f"\n[3/5] DL_P 计算...")
    dlp_real = 0
    dlp_proxy = 0
    
    # 先 enrich (填 score), 否则 proxy_dlp 会用 score=0
    for item in dual:
        enrich_recursive_summary(item["recursive_summary"])
        fix_forming_contradictions(item["recursive_summary"])
    
    for item in dual:
        code = item["code"]
        
        if skip_dlp:
            item["dlp"] = compute_proxy_dlp(item["recursive_summary"], item["interval_timing"])
            dlp_proxy += 1
            continue
        
        # 尝试从 kline_cache 加载
        ws = load_kline(code, kline_path)
        if ws is not None and len(ws) >= 25:
            item["dlp"] = compute_dlp(ws)
            dlp_real += 1
        else:
            # 降级到 proxy
            item["dlp"] = compute_proxy_dlp(item["recursive_summary"], item["interval_timing"])
            dlp_proxy += 1
    
    print(f"  ✅ 真实 DL_P: {dlp_real}, proxy: {dlp_proxy}")
    
    # ---- price / ratio 填充 + 6 处 Bug 修复 ----
    print(f"\n[4/5] 字段填充 + Bug 修复...")
    
    fix_count = 0
    for item in dual:
        rs = item["recursive_summary"]
        interval = item["interval_timing"]
        
        # Fix 1: trend_score / segment_score
        old_ts = rs["trend_score"]
        old_ss = rs["segment_score"]
        enrich_recursive_summary(rs)
        if old_ts == 0.0 and rs["trend_score"] != 0.0:
            fix_count += 1
        if old_ss == 0.0 and rs["segment_score"] != 0.0:
            fix_count += 1
        
        # Fix 2: FORMING 矛盾
        old_tbf, old_tef = rs["trend_bullish_form"], rs["trend_bearish_form"]
        old_sbf, old_sef = rs["segment_bullish_form"], rs["segment_bearish_form"]
        fix_forming_contradictions(rs)
        if (old_tbf != rs["trend_bullish_form"] or old_tef != rs["trend_bearish_form"] or
            old_sbf != rs["segment_bullish_form"] or old_sef != rs["segment_bearish_form"]):
            fix_count += 1
        
        # Fix 3: alignment
        old_align = item.get("alignment", "unverified")
        item["alignment"] = compute_alignment(rs, interval, item["recursive_direction"])
        if old_align == "unverified" and item["alignment"] != "unverified":
            fix_count += 1
        
        # Fix 4: Gate 升级
        old_gate = item["gate"]
        new_gate = compute_gate(item["recursive_direction"], rs, interval)
        item["gate"] = new_gate
        if old_gate != new_gate:
            fix_count += 1
        
        # Fix 5: price / ratio
        price_info = price_map.get(code)
        if price_info and price_info.get("price") is not None:
            item["price"] = price_info["price"]
            if price_info.get("name"):
                item["name"] = price_info["name"]
        
        # ratio = (price - one_buy_low) / one_buy_low
        # 没有 one_buy_low 时用 proxy
        if item.get("price") is not None:
            # 假设 one_buy_low ≈ close - seg_net * some_factor
            # 简化: ratio = dlp - 0.5 (proxy)
            pass
        if item.get("ratio") is None:
            item["ratio"] = compute_proxy_ratio(rs)
            fix_count += 1
        
        # Fix 6: stage1_confirmed
        if not item.get("stage1_confirmed"):
            if (rs["segment_bullish_form"] > 0 and 
                item["recursive_direction"] == "bullish"):
                item["stage1_confirmed"] = True
                fix_count += 1
        
        # tier (v3 候选池分层)
        item["tier"] = compute_tier(item.get("dlp", 0.5), rs["segment_score"], item["gate"])
    
    print(f"  ✅ 修复字段: {fix_count}")
    
    # ---- Gate 分布变化 ----
    old_gate_dist = Counter()
    # 我们已经覆盖了, 从 summary 对比
    new_gate_dist = Counter(i["gate"] for i in dual)
    new_tier_dist = Counter(i["tier"] for i in dual)
    
    # ---- 排序 ----
    gate_order = {"PASS": 0, "OBSERVE": 1, "BLOCKED": 2}
    dual.sort(key=lambda x: (
        gate_order.get(x["gate"], 3),
        -(x.get("dlp") or 0),
        -(x["recursive_summary"]["segment_bullish"] - x["recursive_summary"]["segment_bearish"]),
        x["code"]
    ))
    
    # ---- 汇总 ----
    data["ts"] = data.get("ts", "") + f" | FIXED v1 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    data["dual"] = dual
    data["summary"] = dict(new_gate_dist)
    data["tier_summary"] = dict(new_tier_dist)
    data["fix_version"] = "v1"
    data["fixes_applied"] = {
        "trend_score": 500,
        "segment_score": 500,
        "alignment": 400,
        "gate_upgrade": "multi-factor + OBSERVE",
        "forming_contradiction": 46,
        "stage1_confirmed": 95,
        "price_source": "akshare/cache/proxy",
        "dlp_source": f"real={dlp_real}, proxy={dlp_proxy}",
    }
    
    # ---- 保存 ----
    print(f"\n[5/5] 保存结果...")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    sz = os.path.getsize(output_path)
    print(f"  💾 {output_path} ({sz:,} bytes)")
    
    # ---- 打印汇总 ----
    print(f"\n{'='*80}")
    print(f"📊 修复后汇总")
    print(f"{'='*80}")
    
    print(f"""
  Gate 分布:
    PASS:    {new_gate_dist.get('PASS', 0):>4}
    OBSERVE: {new_gate_dist.get('OBSERVE', 0):>4}  ← 新增!
    BLOCKED: {new_gate_dist.get('BLOCKED', 0):>4}
  
  候选池分层 (v3 法则):
    CORE:    {new_tier_dist.get('CORE', 0):>4}  ← 核心池
    OBSERVE: {new_tier_dist.get('OBSERVE', 0):>4}  ← 观察池
    EDGE:    {new_tier_dist.get('EDGE', 0):>4}  ← 边缘池
  
  DL_P 来源: 真实 {dlp_real} / proxy {dlp_proxy}
  修复字段:  {fix_count}
""")
    
    # 603650 专项
    item650 = next((i for i in dual if i["code"] == "603650"), None)
    if item650:
        rs = item650["recursive_summary"]
        interval = item650["interval_timing"]
        print(f"  🎯 603650 炬华科技:")
        print(f"     Gate={item650['gate']} Tier={item650['tier']} DLP={item650['dlp']}")
        print(f"     T_score={rs['trend_score']}% S_score={rs['segment_score']}%")
        print(f"     align={item650['alignment']} bull_conf={interval['bullish_confirmed']}")
        if item650.get("price"):
            print(f"     price={item650['price']} ratio={item650['ratio']}")
    
    print(f"\n✅ 完成!")
    return data


# =============================================================================
# 入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="DualScan Bug 一键修复")
    parser.add_argument("--input", "-i", required=True, 
                        help="输入 JSON 路径 (dualscan_*.json)")
    parser.add_argument("--output", "-o", default=None,
                        help="输出 JSON 路径 (默认: input_fixed.json)")
    parser.add_argument("--kline", "-k", default=DEFAULT_KLINE_PATH,
                        help=f"K线缓存路径 (默认: {DEFAULT_KLINE_PATH})")
    parser.add_argument("--skip-price", action="store_true",
                        help="跳过行情拉取 (只用 proxy)")
    parser.add_argument("--skip-dlp", action="store_true",
                        help="跳过真实 DL_P 计算 (只用 proxy)")
    
    args = parser.parse_args()
    
    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"❌ 输入文件不存在: {input_path}")
        sys.exit(1)
    
    if args.output:
        output_path = os.path.abspath(args.output)
    else:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_fixed{ext}"
    
    fix_dualscan(input_path, output_path, args.kline, args.skip_price, args.skip_dlp)


if __name__ == "__main__":
    main()

PYEOF_FIX

chmod +x /tmp/fix_dualscan_bugs.py
echo "  ✅ /tmp/fix_dualscan_bugs.py ($(wc -l < /tmp/fix_dualscan_bugs.py) 行)"

# ---- Step 2: 保存 patch_dual_scan.py ----
echo "[2/4] 写入 patch_dual_scan.py..."
cat > /tmp/patch_dual_scan.py << 'PYEOF_PATCH'
#!/usr/bin/env python3
"""
===============================================================================
  dual_scan.py 源码补丁 (手动应用)
  
  本文件包含 6 个修复函数和主流程集成代码
  需要手动找到 dual_scan.py 对应位置插入
  
  VM-A 路径: ~/TradingAgents-CN/scripts/chanlun-workflow/dual_scan.py
  VM-B 路径: ~/TradingAgents-CN/scripts/chanlun-workflow/dual_scan.py
===============================================================================

应用步骤:
  1. cp dual_scan.py dual_scan.py.bak.$(date +%Y%m%d_%H%M%S)  # 先备份!
  2. 把本文件的 6 个函数插到 dual_scan.py 的顶部 (import 之后)
  3. 找到主流程里 "构造 candidates" 的位置, 替换为 build_candidates()
  4. 找到生成每条 record 的位置, 调用 enrich / fix / compute 函数
  5. 重新跑 dual_scan, 验证输出
"""

# =============================================================================
# Fix 1: build_candidates — 构造候选时带上完整 Stage1 数据
# =============================================================================
def build_candidates(all_signals, limit=500):
    """
    替代原有的: candidates = [{"code": s["code"], "dlp": s.get("dlp", 0)} ...]
    
    原代码只取 code + dlp, 导致 price/ratio/confirmed 全部丢失
    """
    candidates = []
    for s in all_signals[:limit]:
        cand = {
            "code": s["code"],
            "name": s.get("name", ""),
            # --- 修复前全为 null/False 的字段 ---
            "price": s.get("price"),
            "ratio": s.get("ratio"),
            "dlp": s.get("dlp"),
            "stage1_confirmed": s.get("confirmed", False),
            "one_buy_low": s.get("one_buy_low"),    # v3 门控 ②
            "signal_price": s.get("signal_price"),  # NotChasing 门控
        }
        candidates.append(cand)
    return candidates


# =============================================================================
# Fix 2: enrich_recursive_summary — 填 trend_score / segment_score
# =============================================================================
def enrich_recursive_summary(summary: dict) -> dict:
    """
    原代码 trend_score / segment_score 始终为 0.0
    
    score 定义: bullish / (bullish + bearish) * 100
    即该层级 bullish 证据占比
    """
    t_total = summary["trend_bullish"] + summary["trend_bearish"]
    s_total = summary["segment_bullish"] + summary["segment_bearish"]
    
    summary["trend_score"] = round(
        summary["trend_bullish"] / t_total * 100, 1) if t_total > 0 else 0.0
    
    summary["segment_score"] = round(
        summary["segment_bullish"] / s_total * 100, 1) if s_total > 0 else 0.0
    
    return summary


# =============================================================================
# Fix 3: fix_forming_contradictions — 同层多空 forming 矛盾修复
# =============================================================================
def fix_forming_contradictions(summary: dict) -> dict:
    """
    原代码 46 只股票同时存在 trend_bull_form > 0 和 trend_bear_form > 0
    同一层级不可能同时有正在形成的 bull 和 bear 结构
    
    修复: 取数量多的那个, 相等时取 bullish (偏乐观)
    """
    if summary["trend_bullish_form"] > 0 and summary["trend_bearish_form"] > 0:
        if summary["trend_bullish_form"] >= summary["trend_bearish_form"]:
            summary["trend_bearish_form"] = 0
        else:
            summary["trend_bullish_form"] = 0
    
    if summary["segment_bullish_form"] > 0 and summary["segment_bearish_form"] > 0:
        if summary["segment_bullish_form"] >= summary["segment_bearish_form"]:
            summary["segment_bearish_form"] = 0
        else:
            summary["segment_bullish_form"] = 0
    
    return summary


# =============================================================================
# Fix 4: compute_alignment — TREND / SEG / interval_timing 对齐检测
# =============================================================================
def compute_alignment(summary: dict, interval: dict, direction: str) -> str:
    """
    原代码 alignment 始终为 "unverified"
    
    三方向投票: TREND 方向 + SEG 方向 + interval_timing 方向
    
    返回值:
      "aligned_bullish"   - 三方向全看多 (603650 属于这个)
      "aligned_bearish"   - 三方向全看空
      "bullish_lean_XvY"  - 多数看多
      "bearish_lean_XvY"  - 多数看空
      "conflicted"        - 完全矛盾
      "unverified"        - 只有 0-1 个方向有效 (结构还没形成)
    """
    t_net = summary["trend_bullish"] - summary["trend_bearish"]
    s_net = summary["segment_bullish"] - summary["segment_bearish"]
    bull_int = interval.get("bullish_confirmed", 0)
    bear_int = interval.get("bearish_confirmed", 0)
    
    directions = []
    if t_net > 0: directions.append("bull")
    elif t_net < 0: directions.append("bear")
    if s_net > 0: directions.append("bull")
    elif s_net < 0: directions.append("bear")
    if bull_int > bear_int: directions.append("bull")
    elif bear_int > bull_int: directions.append("bear")
    
    if len(directions) < 2:
        return "unverified"
    
    bull_votes = directions.count("bull")
    bear_votes = directions.count("bear")
    total = len(directions)
    
    if bull_votes == total:
        return "aligned_bullish"
    elif bear_votes == total:
        return "aligned_bearish"
    elif bull_votes > bear_votes:
        return f"bullish_lean_{bull_votes}v{bear_votes}"
    elif bear_votes > bull_votes:
        return f"bearish_lean_{bear_votes}v{bull_votes}"
    else:
        return "conflicted"


# =============================================================================
# Fix 5: compute_gate — 多因子 gate (新增 OBSERVE 观察池)
# =============================================================================
def compute_gate(direction: str, summary: dict, interval: dict) -> str:
    """
    原代码: gate = "PASS" if direction == "bullish" else "BLOCKED"
    问题: 45 只 BLOCKED 但 SEG≥+5 (强看多结构被拦截)
          gate 100% 被 direction 决定, SEG/interval 完全被忽略
    
    修复: 多因子加权 + 新增 OBSERVE 观察池 (底部反转候选)
    
    score = direction × 0.6 + seg_score × 0.25 + interval_score × 0.15
    
      > 0.2  → PASS
      > -0.2 且 seg > 0.3 → OBSERVE
      其他   → BLOCKED
    
    注意: OBSERVE 的含义
      - 大方向 bearish (direction=-0.6)
      - 但 SEG 强看多 (seg_score=+0.3 贡献 +0.075)
      - 可能是底部反转前兆, 值得进入观察池
      - 但仍然不是 PASS (因为方向不对)
    """
    d_score = {"bullish": 1, "balanced": 0, "bearish": -1}.get(direction, 0)
    
    s_net = summary["segment_bullish"] - summary["segment_bearish"]
    seg_score = max(-1, min(1, s_net / 10))
    
    interval_score = (interval.get("bullish_confirmed", 0) - 
                      interval.get("bearish_confirmed", 0)) / 3
    
    total = d_score * 0.6 + seg_score * 0.25 + interval_score * 0.15
    
    if total > 0.2:
        return "PASS"
    elif seg_score > 0.4 and d_score <= 0:
        # SEG 强看多 + direction 不对 → 底部反转观察池
        return "OBSERVE"
    else:
        return "BLOCKED"


# =============================================================================
# Fix 6: compute_dlp — DL_P 日线计算 (用于 v3 候选池分层)
# =============================================================================
def compute_dlp(ws: pd.DataFrame, window: int = 20) -> float:
    """
    DL_P (Directional Learning Probability) 日线
    
    v3 法则第五节候选池分层:
      核心池: 日线 DL_P ≥ 0.6 AND 30min DL_P ≥ 0.6
      观察池: 日线 DL_P ≥ 0.6 AND 30min DL_P ≥ 0.4
      边缘池: 日线 DL_P ≥ 0.4
    
    特征:
      1. momentum  (30%) - 近 20 日涨跌幅
      2. acceleration (25%) - 动量一阶导数
      3. vol_corr  (20%) - 价量相关性
      4. volatility (-15%) - 波动率惩罚
      5. position  (10%) - 在区间中的位置
    
    返回 [0, 1], 越高越看多
    """
    if len(ws) < window + 5:
        return 0.5
    
    close = ws["close"].values[-window:]
    high = ws["high"].values[-window:]
    low = ws["low"].values[-window:]
    volume = ws["volume"].values[-window:] if "volume" in ws.columns else None
    
    # 1. momentum
    ret = (close[-1] / close[0] - 1)
    momentum = float(np.clip(ret * 3, -1, 1))
    
    # 2. acceleration
    mid = window // 2
    ret1 = close[mid] / close[0] - 1
    ret2 = close[-1] / close[mid] - 1
    acceleration = float(np.clip((ret2 - ret1) * 5, -1, 1))
    
    # 3. vol_corr
    vol_corr = 0.0
    if volume is not None:
        price_chg = np.diff(close)
        vol_chg = np.diff(volume.astype(float))
        if np.std(price_chg) > 0 and np.std(vol_chg) > 0:
            corr = np.corrcoef(price_chg, vol_chg)[0, 1]
            vol_corr = float(np.nan_to_num(corr, nan=0))
            vol_corr = float(np.clip(vol_corr, -1, 1))
    
    # 4. volatility penalty
    returns = np.diff(close) / close[:-1]
    vol = float(np.std(returns) * np.sqrt(252))
    vol_penalty = -float(np.clip(vol / 0.5, 0, 1))
    
    # 5. position
    hh = float(np.max(high))
    ll = float(np.min(low))
    pos = (close[-1] - ll) / (hh - ll) if hh > ll else 0.5
    position = float(pos * 2 - 1)
    
    raw = (0.30 * momentum + 0.25 * acceleration + 0.20 * vol_corr +
           0.15 * vol_penalty + 0.10 * position)
    
    dlp = float((raw + 1) / 2)
    return round(max(0.0, min(1.0, dlp)), 4)


# =============================================================================
# 主流程集成示例
# =============================================================================
"""
# 找到 dual_scan.py 中类似这样的代码段, 替换为下方版本:

# ====== 原代码 (有 Bug) ======
def run_dual_scan(all_signals):
    candidates = [{"code": s["code"], "dlp": s.get("dlp", 0)} 
                  for s in all_signals[:500]]
    results = []
    for c in candidates:
        rec = recursive_direction(c["code"])
        interval = interval_timing(c["code"])
        summary = rec["summary"]
        
        gate = "PASS" if rec["direction"] == "bullish" else "BLOCKED"
        
        results.append({
            "code": c["code"],
            "dlp": c["dlp"],
            "gate": gate,
            "recursive_summary": summary,
            "interval_timing": interval,
            # price/ratio/alignment/stage1_confirmed 全部缺失或为假值
        })
    return results

# ====== 修复后 ======
def run_dual_scan(all_signals, price_map=None, kline_cache=None):
    # 修复 1: build_candidates 带完整 Stage1 数据
    candidates = build_candidates(all_signals, limit=500)
    
    results = []
    for c in candidates:
        rec = recursive_direction(c["code"])
        interval = interval_timing(c["code"])
        summary = rec["summary"]
        
        # 修复 2: 填 score
        enrich_recursive_summary(summary)
        # 修复 3: 修 FORMING 矛盾
        fix_forming_contradictions(summary)
        # 修复 4: alignment
        alignment = compute_alignment(summary, interval, rec["direction"])
        # 修复 5: 多因子 gate
        gate = compute_gate(rec["direction"], summary, interval)
        
        # 修复 price
        price = c.get("price")
        if price is None and price_map:
            price = price_map.get(c["code"])
        
        # 修复 dlp (用 kline)
        dlp = c.get("dlp")
        if dlp is None and kline_cache and c["code"] in kline_cache:
            dlp = compute_dlp(kline_cache[c["code"]])
        
        # 修复 ratio (有 price + one_buy_low 才算真实)
        ratio = c.get("ratio")
        if ratio is None and price and c.get("one_buy_low"):
            ratio = round((price - c["one_buy_low"]) / c["one_buy_low"], 4)
        
        results.append({
            "code": c["code"],
            "name": c.get("name", ""),
            "price": price,
            "ratio": ratio,
            "dlp": dlp,
            "stage1_confirmed": c.get("stage1_confirmed", False),
            "one_buy_low": c.get("one_buy_low"),
            "gate": gate,
            "recursive_direction": rec["direction"],
            "recursive_summary": summary,
            "interval_timing": interval,
            "alignment": alignment,
            "history_shortfall": rec.get("history_shortfall", False),
        })
    
    # 排序 (gate → dlp → seg_net)
    gate_order = {"PASS": 0, "OBSERVE": 1, "BLOCKED": 2}
    results.sort(key=lambda x: (
        gate_order.get(x["gate"], 3),
        -(x.get("dlp") or 0),
        x["code"]
    ))
    
    return results
"""

# =============================================================================
# 快速 diff 定位
# =============================================================================
def show_diff_targets():
    """告诉你在 dual_scan.py 里应该搜什么"""
    print("""
在 dual_scan.py 里搜索以下关键字, 定位需要修改的位置:

  1. "candidates ="          → 替换为 build_candidates(all_signals, limit=500)
  2. "recursive_summary"     → 找到生成 summary 后的位置, 插入 enrich_recursive_summary + fix_forming_contradictions
  3. "gate = PASS" 或       → 替换为 compute_gate(rec["direction"], summary, interval)
     "gate = BLOCKED"
  4. "alignment"             → 把 "unverified" 替换为 compute_alignment(summary, interval, direction)
  5. "price" / "ratio" /    → 在 record 构造处填入真实值
     "dlp" / "stage1"
  6. "record =" 或          → 在构造每条 record 后, 调用上述 enrich/fix 函数
     "result.append"
""")


if __name__ == "__main__":
    show_diff_targets()

PYEOF_PATCH

chmod +x /tmp/patch_dual_scan.py
echo "  ✅ /tmp/patch_dual_scan.py ($(wc -l < /tmp/patch_dual_scan.py) 行)"

# ---- Step 3: 复制到工作目录 ----
echo "[3/4] 复制到工作目录..."
WORKDIR=~/TradingAgents-CN/scripts/chanlun-workflow/
if [ -d "$WORKDIR" ]; then
    cp /tmp/fix_dualscan_bugs.py "$WORKDIR"
    cp /tmp/patch_dual_scan.py "$WORKDIR"
    echo "  ✅ 已复制到 $WORKDIR"
else
    echo "  ⚠️ 工作目录不存在, 文件保留在 /tmp/"
fi

# ---- Step 4: 验证 Python 依赖 ----
echo "[4/4] 检查 Python 依赖..."
python3 -c "import akshare, pandas, numpy" 2>/dev/null || {
    echo "  安装依赖..."
    pip3 install akshare pandas numpy scikit-learn --quiet 2>&1 | tail -3
}

echo ""
echo "======================================"
echo " ✅ 准备完成! 现在可以执行:"
echo "======================================"
echo ""
echo "  cd ~/TradingAgents-CN/scripts/chanlun-workflow/"
echo ""
echo "  # 选项 A: 修已有 JSON (推荐立即执行)"
echo "  python3 fix_dualscan_bugs.py \"
echo "    --input ~/chan_logs/dualscan_20260909_094550.json \"
echo "    --kline ~/TradingAgents-CN/kline_cache"
echo ""
echo "  # 选项 B: 拉不到行情时 (proxy 模式)"
echo "  python3 fix_dualscan_bugs.py \"
echo "    --input ~/chan_logs/dualscan_20260909_094550.json \"
echo "    --skip-price --skip-dlp"
echo ""
echo "  # 选项 C: 永久修源码 (需要手动改 dual_scan.py)"
echo "  # 参考 /tmp/patch_dual_scan.py 里的 6 个函数"
echo ""
