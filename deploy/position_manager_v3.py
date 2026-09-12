"""动态仓位资金管理法则 v3 (执行版)

版本: v3 (2026-09-10) — 含公式错位修复 + 代码真值对账
账户阶段: 婴儿期 (总仓上限 40%、单股上限 35%、目标年化 30%)
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple


class BuyPointLevel(str, Enum):
    ONE = "one_buy"
    TWO = "two_buy"
    THREE = "three_buy"


class PnlBucket(str, Enum):
    LOW = "pnl_lt_0.05"
    MID = "pnl_ge_0.05"
    HIGH = "pnl_ge_0.10"


class MarketRegime(str, Enum):
    STRONG = "strong"
    NEUTRAL = "neutral"
    WEAK = "weak"
    DRAWDOWN_10 = "drawdown_le_10"
    DRAWDOWN_15 = "drawdown_le_15"
    PROFIT_EFFECT_GE4 = "profit_effect_ge_4"
    PROFIT_EFFECT_GE3 = "profit_effect_ge_3"


class SignalGrade(str, Enum):
    A = "A"
    B = "B"
    C = "C"


# ============================================================
# 表 1: cap_table — 买点级别 × 浮盈分档 → 单股仓位上限
# ============================================================

CAP_TABLE: Dict[Tuple[BuyPointLevel, PnlBucket], float] = {
    (BuyPointLevel.ONE,   PnlBucket.LOW):  0.35,
    (BuyPointLevel.ONE,   PnlBucket.MID):  0.35,
    (BuyPointLevel.ONE,   PnlBucket.HIGH): 0.35,

    (BuyPointLevel.TWO,   PnlBucket.LOW):  0.35,
    (BuyPointLevel.TWO,   PnlBucket.MID):  0.50,
    (BuyPointLevel.TWO,   PnlBucket.HIGH): 0.50,

    (BuyPointLevel.THREE, PnlBucket.LOW):  0.35,
    (BuyPointLevel.THREE, PnlBucket.MID):  0.50,
    (BuyPointLevel.THREE, PnlBucket.HIGH): 0.60,
}


# ============================================================
# 表 2: TotalPositionCap — 市场状态 × 信号等级 → 总仓上限
# ============================================================

TOTAL_CAP_TABLE: Dict[Tuple[MarketRegime, Optional[SignalGrade]], float] = {
    (MarketRegime.STRONG,  SignalGrade.A): 0.70,
    (MarketRegime.STRONG,  SignalGrade.B): 0.60,
    (MarketRegime.STRONG,  SignalGrade.C): 0.40,

    (MarketRegime.NEUTRAL, SignalGrade.A): 0.50,
    (MarketRegime.NEUTRAL, SignalGrade.B): 0.40,
    (MarketRegime.NEUTRAL, SignalGrade.C): 0.25,

    (MarketRegime.WEAK,    SignalGrade.A): 0.30,
    (MarketRegime.WEAK,    SignalGrade.B): 0.20,
    (MarketRegime.WEAK,    SignalGrade.C): 0.10,

    (MarketRegime.DRAWDOWN_10,      None): 0.30,
    (MarketRegime.DRAWDOWN_15,      None): 0.10,
    (MarketRegime.PROFIT_EFFECT_GE4, None): 0.10,
    (MarketRegime.PROFIT_EFFECT_GE3, None): 0.30,
}

# 婴儿阶段硬约束
INFANT_TOTAL_CAP_HARD = 0.40
INFANT_SINGLE_CAP_HARD = 0.35


def single_stock_cap_by_asset(total_asset: float) -> float:
    """总资产 → 单股上限"""
    if total_asset < 30_000: return 0.35
    if total_asset < 50_000: return 0.30
    if total_asset < 100_000: return 0.25
    return 0.20


@dataclass
class PreGateResult:
    pnl_padlock: bool = False
    one_buy_low_gap: bool = False
    is_zhongyin: bool = False
    multiplier: float = 1.0

    def explain(self) -> str:
        locks = []
        if self.pnl_padlock:      locks.append("浮盈护垫→35%")
        if self.one_buy_low_gap:  locks.append("一买低点距<3%→35%")
        if self.is_zhongyin:      locks.append("中阴/NotChasing→×0.5")
        return "; ".join(locks) if locks else "全部通过"


def check_pre_gates(
    buy_point: BuyPointLevel, pnl_pct: float,
    close: float, one_buy_low: float,
    is_zhongyin: bool, action: str,
) -> PreGateResult:
    pnl_padlock = pnl_pct < 0.05
    one_buy_low_gap = False
    if one_buy_low > 0 and close > 0:
        one_buy_low_gap = (close - one_buy_low) / one_buy_low < 0.03
    zhongyin_gate = is_zhongyin or (action == "NotChasing")

    mult = 1.0
    if zhongyin_gate:
        mult *= 0.5

    return PreGateResult(
        pnl_padlock=pnl_padlock, one_buy_low_gap=one_buy_low_gap,
        is_zhongyin=zhongyin_gate, multiplier=mult,
    )


@dataclass
class RiskGates:
    money_losing_score: int = 0
    max_drawdown: float = 0.0
    block_buy: bool = False
    risk_level: str = "low"
    profit_effect: float = 0.0
    losing_effect: float = 0.0

    def explain(self) -> str:
        return (f"MLS={self.money_losing_score} DD={self.max_drawdown:.1%} "
                f"PE={self.profit_effect:.1f} LE={self.losing_effect:.1f} "
                f"risk={self.risk_level} block={'Y' if self.block_buy else 'n'}")


def compute_risk_gates(
    max_consecutive_loss: int, peak_asset: float, current_asset: float,
    profit_effect: float, losing_effect: float,
) -> RiskGates:
    mls = min(5, max_consecutive_loss)
    dd = (current_asset - peak_asset) / peak_asset if peak_asset > 0 else 0.0

    block_buy = False
    level = "low"
    if dd < -0.15 or mls >= 5:
        block_buy = True
        level = "high"
    elif dd < -0.10 or mls >= 3:
        level = "medium"

    return RiskGates(mls, dd, block_buy, level, profit_effect, losing_effect)


@dataclass
class PositionDecision:
    code: str
    buy_point: BuyPointLevel
    signal_grade: SignalGrade
    market_regime: MarketRegime
    single_cap_table: float
    single_cap_final: float
    total_cap: float
    pre_gates: PreGateResult
    risk: RiskGates
    blocked: bool
    explain: str

    def to_dict(self) -> dict:
        return {
            "code": self.code, "buy_point": self.buy_point.value,
            "signal_grade": self.signal_grade.value,
            "market_regime": self.market_regime.value,
            "single_cap_table": round(self.single_cap_table, 4),
            "single_cap_final": round(self.single_cap_final, 4),
            "total_cap": round(self.total_cap, 4),
            "blocked": self.blocked,
            "explain": self.explain,
        }


def decide_position(
    code: str, buy_point: BuyPointLevel, pnl_pct: float,
    close: float, one_buy_low: float, is_zhongyin: bool, action: str,
    signal_grade: SignalGrade, market_regime: MarketRegime,
    total_asset: float,
    max_consecutive_loss: int = 0, peak_asset: float = 0.0,
    profit_effect: float = 0.0, losing_effect: float = 0.0,
) -> PositionDecision:
    """单股仓位决策 (v3 全链路)"""
    # 1. 浮盈档
    bucket = PnlBucket.HIGH if pnl_pct >= 0.10 else PnlBucket.MID if pnl_pct >= 0.05 else PnlBucket.LOW

    # 2. cap_table 原始
    single_table = CAP_TABLE.get((buy_point, bucket), 0.35)

    # 3. 前置门控
    gates = check_pre_gates(buy_point, pnl_pct, close, one_buy_low, is_zhongyin, action)
    if gates.pnl_padlock or gates.one_buy_low_gap:
        after_gate = min(single_table, 0.35)
    else:
        after_gate = single_table * gates.multiplier

    # 4. 单股上限 by 资产
    cap_by_asset = single_stock_cap_by_asset(total_asset)
    single_final = min(after_gate, cap_by_asset, INFANT_SINGLE_CAP_HARD)

    # 5. 总仓上限
    if market_regime in (MarketRegime.DRAWDOWN_15, MarketRegime.DRAWDOWN_10,
                         MarketRegime.PROFIT_EFFECT_GE4, MarketRegime.PROFIT_EFFECT_GE3):
        total_table = TOTAL_CAP_TABLE[(market_regime, None)]
    else:
        total_table = TOTAL_CAP_TABLE.get((market_regime, signal_grade), 0.25)
    total_final = min(total_table, INFANT_TOTAL_CAP_HARD, 0.70)

    # 6. 风控闸门
    risk = compute_risk_gates(max_consecutive_loss, peak_asset, total_asset, profit_effect, losing_effect)
    blocked = risk.block_buy or risk.risk_level == "high"

    explain = (f"cap_table({buy_point.value},{bucket.value})={single_table:.0%} "
               f"gates=[{gates.explain()}] "
               f"single=min({after_gate:.0%},{cap_by_asset:.0%},{INFANT_SINGLE_CAP_HARD:.0%})={single_final:.0%} "
               f"total=min({total_table:.0%},{INFANT_TOTAL_CAP_HARD:.0%})={total_final:.0%} "
               f"risk=[{risk.explain()}] "
               f"{'BLOCKED' if blocked else 'OK'}")

    return PositionDecision(code, buy_point, signal_grade, market_regime,
                            single_table, single_final, total_final, gates, risk, blocked, explain)
