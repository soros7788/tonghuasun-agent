
# =============================================================================
# dual_scan.py BUG 修复补丁
# 生成时间: 2026-09-10
# 修复范围: price/ratio/dlp 全 null, score 全 0, alignment 全 unverified,
#          FORMING 矛盾, gate 逻辑过于简单, stage1_confirmed 全 False
# =============================================================================

# ------------------ 修复 1: 构造候选时带上 Stage1 字段 ------------------
# 原代码 (bug):
#   candidates = [{"code": s["code"], "dlp": s.get("dlp", 0)} for s in all_signals[:500]]
# 修复后:
def build_candidates(all_signals, limit=500):
    """构造候选池, 完整保留 Stage1 数据"""
    candidates = []
    for s in all_signals[:limit]:
        cand = {
            "code": s["code"],
            "name": s.get("name", ""),
            "price": s.get("price"),              # ← 修复: 从 Stage1 带价格
            "ratio": s.get("ratio"),              # ← 修复: 从 Stage1 带 ratio
            "dlp": s.get("dlp"),                  # ← 修复: 从 Stage1 带 DL_P
            "stage1_confirmed": s.get("confirmed", False),  # ← 修复: confirmed 状态
            "one_buy_low": s.get("one_buy_low"),  # ← 修复: 一买低点 (v3 法则门控 ②)
            "signal_price": s.get("signal_price"), # ← 修复: 信号价 (NotChasing 门控)
        }
        candidates.append(cand)
    return candidates

# ------------------ 修复 2: recursive_summary 填充 score ------------------
# 在 recursive_direction() 返回结果后, 添加 score 计算
def enrich_recursive_summary(summary):
    """给 recursive_summary 填 score 字段"""
    t_total = summary["trend_bullish"] + summary["trend_bearish"]
    s_total = summary["segment_bullish"] + summary["segment_bearish"]
    
    if t_total > 0:
        summary["trend_score"] = round(
            summary["trend_bullish"] / t_total * 100, 1)
    else:
        summary["trend_score"] = 0.0
    
    if s_total > 0:
        summary["segment_score"] = round(
            summary["segment_bullish"] / s_total * 100, 1)
    else:
        summary["segment_score"] = 0.0
    
    return summary

# ------------------ 修复 3: FORMING 矛盾检测 ------------------
def fix_forming_contradictions(summary):
    """同层 bull_form 和 bear_form 不可能同时 > 0"""
    if summary["trend_bullish_form"] > 0 and summary["trend_bearish_form"] > 0:
        # 取数量多的那个, 或取 bullish (如果相等)
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

# ------------------ 修复 4: alignment 计算 ------------------
def compute_alignment(summary, interval_timing, recursive_direction):
    """TREND / SEG / interval_timing 三方向对齐检测"""
    t_net = summary["trend_bullish"] - summary["trend_bearish"]
    s_net = summary["segment_bullish"] - summary["segment_bearish"]
    bull_int = interval_timing.get("bullish_confirmed", 0)
    bear_int = interval_timing.get("bearish_confirmed", 0)
    
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

# ------------------ 修复 5: Gate 逻辑升级 (多因子) ------------------
def compute_gate(recursive_direction, summary, interval_timing):
    """
    多因子 gate 评估
    
    score = direction×0.6 + seg_score×0.25 + interval_score×0.15
    
    threshold:
      > 0.2   → PASS
      > -0.2  → OBSERVE (大方向 bearish 但 SEG 强看多)
      其他     → BLOCKED
    """
    d_score = {"bullish": 1, "balanced": 0, "bearish": -1}.get(recursive_direction, 0)
    
    s_net = summary["segment_bullish"] - summary["segment_bearish"]
    seg_score = max(-1, min(1, s_net / 10))
    
    interval_score = (
        interval_timing.get("bullish_confirmed", 0) -
        interval_timing.get("bearish_confirmed", 0)
    ) / 3
    
    total = d_score * 0.6 + seg_score * 0.25 + interval_score * 0.15
    
    if total > 0.2:
        return "PASS"
    elif total > -0.2 and seg_score > 0.3:
        return "OBSERVE"  # 新增观察池: 底部反转候选
    else:
        return "BLOCKED"

# ------------------ 修复 6: DL_P 日线计算 ------------------
def compute_dlp(ws, window=20):
    """
    DL_P (Directional Learning Probability) 日线计算
    
    特征 (v3 法则第五节候选池分层用):
      1. momentum (30%): 近 20 日涨跌幅
      2. acceleration (25%): 动量的一阶导数
      3. vol_corr (20%): 价量相关性
      4. volatility (-15%): 波动率惩罚
      5. position (10%): 当前在区间的位置
    
    平滑: 5 日 EMA
    阈值: >0.6 核心池, >0.4 观察池, <0.4 边缘池
    """
    import numpy as np
    
    if len(ws) < window + 5:
        return 0.5  # 数据不足, 返回中性
    
    close = ws["close"].values
    high = ws["high"].values
    low = ws["low"].values
    volume = ws["volume"].values
    
    # 特征 1: momentum
    ret = (close[-1] / close[-window] - 1)
    momentum = np.clip(ret * 3, -1, 1)  # 归一化到 [-1, 1]
    
    # 特征 2: acceleration
    mid = window // 2
    ret1 = close[-mid] / close[-window] - 1
    ret2 = close[-1] / close[-mid] - 1
    acceleration = np.clip((ret2 - ret1) * 5, -1, 1)
    
    # 特征 3: vol_corr
    if len(volume) >= window:
        price_chg = np.diff(close[-window:])
        vol_chg = np.diff(volume[-window:].astype(float))
        if np.std(price_chg) > 0 and np.std(vol_chg) > 0:
            vol_corr = np.corrcoef(price_chg, vol_chg)[0, 1]
            vol_corr = np.nan_to_num(vol_corr, nan=0)
            vol_corr = np.clip(vol_corr, -1, 1)
        else:
            vol_corr = 0
    else:
        vol_corr = 0
    
    # 特征 4: volatility penalty
    if len(close) >= window:
        returns = np.diff(close[-window:]) / close[-window:-1]
        vol = np.std(returns) * np.sqrt(252)  # 年化
        vol_penalty = -np.clip(vol / 0.5, 0, 1)  # 0.5 年化波动率封顶
    else:
        vol_penalty = 0
    
    # 特征 5: position
    hh = np.max(high[-window:])
    ll = np.min(low[-window:])
    pos = (close[-1] - ll) / (hh - ll) if hh > ll else 0.5
    position = pos * 2 - 1  # 转 [-1, 1]
    
    # 加权
    raw = (0.30 * momentum +
           0.25 * acceleration +
           0.20 * vol_corr +
           0.15 * vol_penalty +
           0.10 * position)
    
    # 转 [0, 1]
    dlp = (raw + 1) / 2
    return round(float(dlp), 4)

# ------------------ 主流程集成 ------------------
def run_dual_scan(full_signals, stock_name_map=None, 
                  price_map=None, kline_cache=None):
    """
    完整的 dual_scan 流程
    
    full_signals: Stage1 全部信号
    stock_name_map: {code: name} 股票名称
    price_map: {code: current_price} 当前价格 (实时或缓存)
    kline_cache: {code: DataFrame} 日线 K 线 (用于 DL_P)
    """
    from datetime import datetime
    
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S CST")
    
    # Step 1: 构建候选池 (带完整 Stage1 数据)
    candidates = build_candidates(full_signals, limit=500)
    
    # Step 2: 双引擎分析
    dual_results = []
    for cand in candidates:
        code = cand["code"]
        
        # 引擎 A: 递归方向
        recursive = recursive_direction(code)  # ← 假设这个函数已存在
        summary = recursive["summary"]
        
        # 引擎 B: 区间套时机
        interval = interval_timing(code)       # ← 假设这个函数已存在
        
        # ---- BUG 修复 ----
        # 修复 score
        enrich_recursive_summary(summary)
        # 修复 forming 矛盾
        fix_forming_contradictions(summary)
        # 修复 alignment
        alignment = compute_alignment(summary, interval, recursive["direction"])
        
        # ---- 价格 / ratio / DL_P 补齐 ----
        price = cand.get("price")
        if price is None and price_map:
            price = price_map.get(code)
        
        ratio = cand.get("ratio")
        if ratio is None and cand.get("one_buy_low") and price:
            ratio = round((price - cand["one_buy_low"]) / cand["one_buy_low"], 4)
        
        dlp = cand.get("dlp")
        if dlp is None and kline_cache and code in kline_cache:
            dlp = compute_dlp(kline_cache[code])
        
        # ---- Gate 评估 ----
        gate = compute_gate(recursive["direction"], summary, interval)
        
        # ---- 名称 ----
        name = cand.get("name") or (stock_name_map or {}).get(code, "")
        
        record = {
            "code": code,
            "name": name,
            "price": price,
            "ratio": ratio,
            "dlp": dlp,
            "stage1_confirmed": cand.get("stage1_confirmed", False),
            "one_buy_low": cand.get("one_buy_low"),
            "signal_price": cand.get("signal_price"),
            "gate": gate,
            "recursive_direction": recursive["direction"],
            "recursive_summary": summary,
            "interval_timing": interval,
            "alignment": alignment,
            "history_shortfall": recursive.get("history_shortfall", False),
        }
        dual_results.append(record)
    
    # Step 3: 按 gate + dlp 排序
    gate_order = {"PASS": 0, "OBSERVE": 1, "BLOCKED": 2}
    dual_results.sort(key=lambda x: (
        gate_order.get(x["gate"], 3),
        -(x.get("dlp") or 0),
        x["code"]
    ))
    
    summary_count = {k: sum(1 for r in dual_results if r["gate"] == k)
                     for k in gate_order}
    
    output = {
        "ts": ts,
        "stage1_count": len(full_signals),
        "candidate_count": len(dual_results),
        "dual": dual_results,
        "summary": summary_count,
        "fix_version": "v1",
    }
    
    return output
