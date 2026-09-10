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
