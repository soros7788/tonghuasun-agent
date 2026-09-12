#!/usr/bin/env python3
"""v3_position_manager — 动态仓位资金管理法则 CLI (独立版)

从 chan_merge 输出的 JSON (或 dualscan JSON) 读候选池, 按 v3 法则批量计算仓位建议,
输出结构化决策 JSON.

与 deploy/position_manager_v3.py (下划线版) 的关系:
  position_manager_v3.py  = 纯函数库 (CAP_TABLE / decide_position / RiskGates 等)
  v3_position_manager.py  = 独立 CLI (import 上面的库 + 批量计算 + JSON 输出)

用法:
  # 从 chan_merge 输出 (推荐)
  python3 v3_position_manager.py \
      --candidates merged_candidates.json \
      --regime strong --grade A --asset 50000 \
      --output positions.json

  # 从 dualscan JSON
  python3 v3_position_manager.py \
      --candidates dualscan_fixed.json --source dualscan \
      --regime neutral --grade B --asset 30000 \
      --output positions.json

  # 风控参数覆盖
  python3 v3_position_manager.py -c merged.json \
      --regime weak --grade C --asset 10000 \
      --max-loss 3 --peak-asset 35000 \
      --profit-effect 2.5 --losing-effect 1.0 \
      -o positions.json
"""

from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 导入同目录下的纯函数库
sys.path.insert(0, str(Path(__file__).resolve().parent))
from position_manager_v3 import (  # noqa: E402
    decide_position,
    BuyPointLevel, PnlBucket, MarketRegime, SignalGrade,
    INFANT_TOTAL_CAP_HARD, INFANT_SINGLE_CAP_HARD,
)

SH_TZ = timezone(timedelta(hours=8))


# ============================================================================
# 候选 → v3 输入映射
# ============================================================================

def detect_buy_point(record: dict) -> BuyPointLevel:
    """从 dualscan/merge 记录推断买点级别.

    推断策略 (dualscan 不直接给买点, 用 interval_timing + alignment 反推):
      - interval_timing.bullish_confirmed ≥ 3 → THREE (三买已确认)
      - alignment == 'aligned_bullish' AND interval ≥ 2 → TWO
      - 其他 → ONE (默认一买观察)
    """
    interval = record.get("interval_timing", {})
    bull_c = interval.get("bullish_confirmed", 0)
    alignment = record.get("alignment", "")

    if bull_c >= 3:
        return BuyPointLevel.THREE
    if alignment == "aligned_bullish" and bull_c >= 2:
        return BuyPointLevel.TWO
    return BuyPointLevel.ONE


def detect_signal_grade(record: dict) -> SignalGrade:
    """从 gate + tier + trend_score 推断信号等级."""
    gate = record.get("gate", "BLOCKED")
    tier = record.get("tier", "EDGE")
    trend = record.get("trend_score", 0)

    if gate == "PASS" and tier == "CORE" and trend >= 60:
        return SignalGrade.A
    if gate in ("PASS", "OBSERVE") and trend >= 45:
        return SignalGrade.B
    return SignalGrade.C


def load_candidates(path: str, source: str = "chan_merge") -> list[dict]:
    """加载候选池.

    source=chan_merge → 读 candidates[] 数组
    source=dualscan   → 读 dual[] 数组 + 套 detect_*
    source=raw        → 直接 list of dict (每条需含 decide_position 所有字段)
    """
    with open(path) as f:
        data = json.load(f)

    if source == "chan_merge":
        candidates = data.get("candidates", [])
    elif source == "dualscan":
        candidates = data.get("dual", [])
        # dualscan 字段名不同, 统一一下
        normalized = []
        for d in candidates:
            summary = d.get("recursive_summary", {})
            interval = d.get("interval_timing", {})
            normalized.append({
                "code": d["code"],
                "gate": d.get("gate", "UNKNOWN"),
                "tier": d.get("tier", "UNKNOWN"),
                "dlp": d.get("dlp", 0),
                "price": d.get("price"),
                "ratio": d.get("ratio"),
                "stage1_confirmed": d.get("stage1_confirmed", False),
                "trend_score": summary.get("trend_score", 0),
                "seg_score": summary.get("segment_score", 0),
                "alignment": d.get("alignment", "unverified"),
                "interval_timing": interval,
            })
        candidates = normalized
    elif source == "raw":
        candidates = data if isinstance(data, list) else data.get("candidates", [])
    else:
        print(f"❌ 未知 source: {source}")
        sys.exit(1)

    # 默认过滤: 只算 PASS + OBSERVE
    filtered = [c for c in candidates if c.get("gate") in ("PASS", "OBSERVE")]
    print(f"[load] {source}: {len(candidates)} 条 → 过滤后 {len(filtered)} 条 (PASS+OBSERVE)")
    return filtered


# ============================================================================
# 批量仓位计算
# ============================================================================

def compute_all_positions(
    candidates: list[dict],
    market_regime: MarketRegime,
    signal_grade: SignalGrade,
    total_asset: float,
    max_consecutive_loss: int = 0,
    peak_asset: float = 0.0,
    profit_effect: float = 0.0,
    losing_effect: float = 0.0,
) -> list[dict]:
    """对每只候选调用 decide_position, 返回结构化决策."""
    decisions = []

    for rec in candidates:
        code = rec.get("code", "?")
        price = rec.get("price") or 0
        ratio = rec.get("ratio") or 0
        pnl_pct = max(0, ratio)  # 浮盈比例 (ratio = (close - one_buy_low) / one_buy_low)
        is_zhongyin = rec.get("is_zhongyin", False) or rec.get("alignment", "") == "conflicted"
        action = rec.get("action", "")

        buy_point = detect_buy_point(rec)
        # signal_grade 允许覆盖 (用户 CLI 指定则全局覆盖, 否则按记录推断)
        sig_grade = signal_grade  # CLI 指定的全局等级

        decision = decide_position(
            code=code,
            buy_point=buy_point,
            pnl_pct=pnl_pct,
            close=price,
            one_buy_low=price / (1 + ratio) if ratio > 0 else 0,  # 反推 one_buy_low
            is_zhongyin=is_zhongyin,
            action=action,
            signal_grade=sig_grade,
            market_regime=market_regime,
            total_asset=total_asset,
            max_consecutive_loss=max_consecutive_loss,
            peak_asset=peak_asset or total_asset,  # 默认 peak = 当前 (新账户)
            profit_effect=profit_effect,
            losing_effect=losing_effect,
        )

        d = decision.to_dict()
        # 附加候选池原始字段方便回溯
        d["source_gate"] = rec.get("gate")
        d["source_tier"] = rec.get("tier")
        d["source_dlp"] = rec.get("dlp")
        d["source_trend_score"] = rec.get("trend_score")
        decisions.append(d)

    return decisions


# ============================================================================
# 汇总输出
# ============================================================================

def summarize(decisions: list[dict], total_asset: float,
              market_regime: MarketRegime, signal_grade: SignalGrade) -> dict:
    """仓位决策汇总."""
    from collections import Counter

    # blocked / ok
    blocked = [d for d in decisions if d["blocked"]]
    ok = [d for d in decisions if not d["blocked"]]

    # 仓位合计 (每只单股上限理论值, 不考虑相关性)
    total_single_cap = sum(d["single_cap_final"] for d in ok)

    # gate / tier 分布
    gate_dist = Counter(d["source_gate"] for d in decisions)
    tier_dist = Counter(d["source_tier"] for d in decisions)

    ts = datetime.now(SH_TZ).isoformat(timespec="seconds")

    summary = {
        "ts": ts,
        "market_context": {
            "regime": market_regime.value,
            "signal_grade": signal_grade.value,
            "total_asset": total_asset,
            "infant_total_cap_hard": INFANT_TOTAL_CAP_HARD,
            "infant_single_cap_hard": INFANT_SINGLE_CAP_HARD,
        },
        "summary": {
            "candidates_total": len(decisions),
            "ok": len(ok),
            "blocked": len(blocked),
            "theoretical_total_single_cap": round(total_single_cap, 4),
            "effective_total_cap": min(
                total_single_cap,
                _get_total_cap_for(market_regime, signal_grade),
                INFANT_TOTAL_CAP_HARD,
            ),
            "gate_distribution": dict(gate_dist),
            "tier_distribution": dict(tier_dist),
        },
        "decisions": decisions,
    }
    return summary


def _get_total_cap_for(regime: MarketRegime, grade: SignalGrade) -> float:
    """查 TOTAL_CAP_TABLE (避免 import 大表)."""
    from position_manager_v3 import TOTAL_CAP_TABLE
    if regime in (MarketRegime.DRAWDOWN_15, MarketRegime.DRAWDOWN_10,
                  MarketRegime.PROFIT_EFFECT_GE4, MarketRegime.PROFIT_EFFECT_GE3):
        return TOTAL_CAP_TABLE.get((regime, None), 0.25)
    return TOTAL_CAP_TABLE.get((regime, grade), 0.25)


def print_summary(summary: dict):
    """控制台打印汇总."""
    ctx = summary["market_context"]
    s = summary["summary"]
    decisions = summary["decisions"]

    print(f"\n{'='*70}")
    print(f"📊 v3_position_manager 决策汇总")
    print(f"{'='*70}")
    print(f"  市场状态:  {ctx['regime']}  信号等级: {ctx['signal_grade']}")
    print(f"  总资产:    ¥{ctx['total_asset']:,.0f}")
    print(f"  硬约束:    总仓 ≤ {ctx['infant_total_cap_hard']:.0%}  单股 ≤ {ctx['infant_single_cap_hard']:.0%}")
    print(f"\n  候选: {s['candidates_total']}  → OK {s['ok']} / BLOCKED {s['blocked']}")
    print(f"  理论总单仓上限: {s['theoretical_total_single_cap']:.0%}")
    print(f"  有效总仓上限:   {s['effective_total_cap']:.0%}")

    # Top 10 OK 候选 (按 single_cap_final 排序)
    ok_sorted = sorted(
        [d for d in decisions if not d["blocked"]],
        key=lambda d: -d["single_cap_final"],
    )[:10]
    if ok_sorted:
        print(f"\n  Top {len(ok_sorted)} 可买候选 (按单股上限):")
        for d in ok_sorted:
            print(f"    {d['code']}  cap={d['single_cap_final']:.0%} "
                  f"gate={d['source_gate']} tier={d['source_tier']} "
                  f"dlp={d['source_dlp']:.3f} blocked={'Y' if d['blocked'] else 'n'}")

    blocked_list = [d for d in decisions if d["blocked"]]
    if blocked_list:
        print(f"\n  BLOCKED ({len(blocked_list)} 只):")
        for d in blocked_list[:5]:
            print(f"    {d['code']}  {d['explain'][:80]}")
        if len(blocked_list) > 5:
            print(f"    ... 还有 {len(blocked_list) - 5} 只")


# ============================================================================
# CLI 入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="v3_position_manager — 动态仓位资金管理法则 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
市场状态 (--regime):
  strong / neutral / weak              常规状态, 配 --grade
  drawdown_le_10 / drawdown_le_15      回撤状态, 忽略 grade
  profit_effect_ge3 / profit_effect_ge4 盈利效应, 忽略 grade

候选来源 (--source):
  chan_merge  合并器输出 (默认, 含 candidates[] 数组)
  dualscan    dualscan_*.json (含 dual[] 数组)
  raw         原始 list of dict (每条需含完整字段)
        """,
    )
    parser.add_argument("-c", "--candidates", required=True,
                        help="候选池 JSON 路径")
    parser.add_argument("--source", choices=["chan_merge", "dualscan", "raw"],
                        default="chan_merge",
                        help="候选池来源格式 (默认: chan_merge)")
    parser.add_argument("--regime", required=True,
                        choices=[r.value for r in MarketRegime],
                        help="市场状态")
    parser.add_argument("--grade", required=True,
                        choices=[g.value for g in SignalGrade],
                        help="信号等级")
    parser.add_argument("--asset", type=float, required=True,
                        help="当前总资产 (¥)")
    parser.add_argument("--peak-asset", type=float, default=0,
                        help="历史峰值资产 (默认 = 当前资产)")
    parser.add_argument("--max-loss", type=int, default=0,
                        help="最大连续亏损次数 (默认 0)")
    parser.add_argument("--profit-effect", type=float, default=0.0,
                        help="盈利效应系数 (默认 0)")
    parser.add_argument("--losing-effect", type=float, default=0.0,
                        help="亏损效应系数 (默认 0)")
    parser.add_argument("-o", "--output", default="v3_positions.json",
                        help="输出 JSON 路径 (默认: v3_positions.json)")
    args = parser.parse_args()

    # 1. 加载候选
    candidates = load_candidates(args.candidates, source=args.source)
    if not candidates:
        print("❌ 过滤后候选为空 — 没什么好算的")
        sys.exit(1)

    # 2. 批量计算
    regime = MarketRegime(args.regime)
    grade = SignalGrade(args.grade)

    decisions = compute_all_positions(
        candidates=candidates,
        market_regime=regime,
        signal_grade=grade,
        total_asset=args.asset,
        max_consecutive_loss=args.max_loss,
        peak_asset=args.peak_asset,
        profit_effect=args.profit_effect,
        losing_effect=args.losing_effect,
    )

    # 3. 汇总
    summary = summarize(decisions, args.asset, regime, grade)

    # 4. 打印
    print_summary(summary)

    # 5. 落盘
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n💾 {out} ({out.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
