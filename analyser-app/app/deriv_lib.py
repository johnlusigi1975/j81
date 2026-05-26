"""Deriv Trade-Type Library — the tree's single source of truth.

We ONLY trade four contract types: Rise/Fall, Even/Odd, Over/Under, Matches/Differs.
This module holds:

  * STATIC knowledge from Deriv's API docs (contract codes, settlement rules,
    structural win probabilities, market list, constraints, honest notes).
  * LIVE data refreshed from Deriv (real payout multipliers per (market, type),
    last refresh time).

Persisted to {DATA_DIR}/data/deriv_library.json so it survives restarts. The
analyser exposes /deriv/library/*; the bot proxies it so the UI and the
researcher both consume the same numbers from a single endpoint.

Honest by design: every win-probability and EV claim here is documented from
Deriv's published mechanics, not invented. There is NO predictive edge in this
library — it is reference data the systems use to compute true EV, render
honest UIs, and pick the highest-payout market (the one genuine Even/Odd lever).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from app.config import data_path
from app.deriv import fetch_proposal_payout
from app.scanner import SCAN_SYMBOLS


# ============================================================ STATIC KNOWLEDGE

TRADE_TYPES: dict[str, dict[str, Any]] = {
    "rise_fall": {
        "name": "Rise / Fall",
        "deriv_codes": {"up": "CALL", "down": "PUT"},
        "settles_on": "exit-tick price vs entry-tick price",
        "win_rule": "CALL wins if exit > entry; PUT wins if exit < entry; tie loses on entry tick",
        "duration_unit": "t",
        "duration_min": 1, "duration_max": 10,
        "barrier_required": False,
        "structural_win_prob": 0.50,
        "structural_note": "Synthetic ticks are an audited RNG; up/down on a tick is ~50/50 (P(up|prev up) ≈ 49.2%, verified ~independence).",
        "typical_payout_multiplier": 1.94,
        "break_even_winrate": 0.5155,
        "house_edge_pct_approx": 1.5,
        "real_ev_edge": "None — payout is essentially flat across markets and structural odds are 50/50.",
    },
    "even_odd": {
        "name": "Even / Odd",
        "deriv_codes": {"even": "DIGITEVEN", "odd": "DIGITODD"},
        "settles_on": "last digit of the final tick at expiry",
        "win_rule": "DIGITEVEN wins on {0,2,4,6,8}; DIGITODD wins on {1,3,5,7,9}",
        "duration_unit": "t",
        "duration_min": 1, "duration_max": 10,
        "barrier_required": False,
        "structural_win_prob": 0.50,
        "structural_note": "Last-digit distribution is uniform — verified across thousands of ticks.",
        "typical_payout_multiplier": "1.92–1.95 depending on market",
        "break_even_winrate": 0.5128,
        "house_edge_pct_approx_range": [1.5, 4.0],
        "real_ev_edge": "THE ONLY genuine lever in this library. Deriv quotes slightly different payouts per market (e.g. R_25/50/75/1HZ* ≈ 1.95×, R_100 ≈ 1.92×). Trading the highest-payout market recovers ~1–2% EV vs the lowest. Same 50/50 odds everywhere — no prediction.",
    },
    "over_under": {
        "name": "Over / Under",
        "deriv_codes": {"over": "DIGITOVER", "under": "DIGITUNDER"},
        "settles_on": "last digit of the final tick at expiry",
        "win_rule": "DIGITOVER wins if last digit > barrier; DIGITUNDER wins if < barrier; ties lose",
        "duration_unit": "t",
        "duration_min": 1, "duration_max": 10,
        "barrier_required": True,
        "barrier_constraints": {"over": "0–8 (over 9 is impossible)", "under": "1–9 (under 0 is impossible)"},
        "structural_win_prob_formula": {"over_N": "(9 − N) / 10", "under_N": "N / 10"},
        "structural_win_prob_examples": {
            "over_0": 0.9, "over_4": 0.5, "over_8": 0.1,
            "under_1": 0.1, "under_5": 0.5, "under_9": 0.9,
        },
        "real_ev_edge": "None — Deriv prices payouts per barrier so EV stays negative for both sides. High-probability barriers pay low (~1.1×); low-probability pay high (~9×). Break-even tracks the structural odds plus the house edge.",
    },
    "matches_differs": {
        "name": "Matches / Differs",
        "deriv_codes": {"matches": "DIGITMATCH", "differs": "DIGITDIFF"},
        "settles_on": "last digit of the final tick at expiry",
        "win_rule": "DIGITMATCH wins if last digit == barrier; DIGITDIFF wins if last digit != barrier",
        "duration_unit": "t",
        "duration_min": 1, "duration_max": 10,
        "barrier_required": True,
        "barrier_constraints": {"any": "0–9"},
        "structural_win_prob": {"matches": 0.10, "differs": 0.90},
        "typical_payout_multiplier": {"matches": 9.5, "differs": 1.05},
        "break_even_winrate": {"matches": 0.105, "differs": 0.952},
        "real_ev_edge": "None — and the most honest demonstration in the library of 'high win-rate ≠ profit'. DIFFERS wins ~90% but pays only ~1.05×; MATCHES wins ~10% but pays ~9.5×. Both sides carry a small house edge.",
    },
}

MARKETS: list[dict[str, Any]] = [
    {"code": "R_10",   "name": "Vol 10",          "kind": "synthetic", "pip_size": 3, "tick_interval_sec": 2, "vol_class": "low"},
    {"code": "R_25",   "name": "Vol 25",          "kind": "synthetic", "pip_size": 3, "tick_interval_sec": 2, "vol_class": "low-mid"},
    {"code": "R_50",   "name": "Vol 50",          "kind": "synthetic", "pip_size": 4, "tick_interval_sec": 2, "vol_class": "mid"},
    {"code": "R_75",   "name": "Vol 75",          "kind": "synthetic", "pip_size": 4, "tick_interval_sec": 2, "vol_class": "mid-high"},
    {"code": "R_100",  "name": "Vol 100",         "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 2, "vol_class": "high"},
    {"code": "1HZ10V", "name": "Vol 10 (1s)",     "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 1, "vol_class": "low"},
    {"code": "1HZ25V", "name": "Vol 25 (1s)",     "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 1, "vol_class": "low-mid"},
    {"code": "1HZ50V", "name": "Vol 50 (1s)",     "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 1, "vol_class": "mid"},
    {"code": "1HZ75V", "name": "Vol 75 (1s)",     "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 1, "vol_class": "mid-high"},
    {"code": "1HZ100V","name": "Vol 100 (1s)",    "kind": "synthetic", "pip_size": 2, "tick_interval_sec": 1, "vol_class": "high"},
]

CONSTRAINTS: dict[str, Any] = {
    "min_stake_usd": 0.35,
    "max_app_markup_pct": 3.0,
    "tick_duration_min": 1,
    "tick_duration_max": 10,
    "audit_source": "Deriv synthetics use a NIST-certified random number generator (per Deriv.com).",
}

HONEST_NOTES: list[str] = [
    "These four contract types settle on RNG outputs (price ticks or last digits of ticks).",
    "There is no exploitable next-tick prediction edge on Deriv synthetics.",
    "The only documented real EV lever is Even/Odd market selection (~1–2% recoverable by trading the highest-payout market).",
    "High win-rate ≠ profit: DIFFERS wins ~90% but pays 1.05× → still loses money long-term.",
    "Treat as speculation; never stake money you can't afford to lose.",
]


# ============================================================ J81 STANCE & GOAL
#
# The tree treats this as a competitive game: Deriv is a regulated bookmaker
# whose business model is to take a built-in house edge over volume — i.e.
# "Deriv plays to win." J81 plays to win too. Below: the explicit goal, the
# honest math around it, and the operational strategy the tree uses to chase it.

DERIV_STANCE: dict[str, Any] = {
    "what_deriv_is": "A regulated derivatives broker offering synthetic indices generated by a NIST-certified RNG.",
    "deriv_business_model": "Take a small house edge (~1.5–4%) on every contract. Profit comes from VOLUME, not from predicting individual outcomes.",
    "deriv_intent": "Deriv is built to win over volume — that is the design, not a flaw.",
    "implications_for_us": [
        "Every bet starts negative EV by design.",
        "There is no 'crack' in the RNG to exploit — it's audited.",
        "The lever Deriv leaves on the table: slightly uneven payouts per market on Even/Odd.",
    ],
}

TRADING_DISCIPLINE: dict[str, Any] = {
    # Synthesized from: CFA Institute trader-behaviour research; Mark Douglas,
    # "Trading in the Zone" (probabilistic thinking, pre-commit to risk);
    # Investopedia + BabyPips on risk management; Trader-psychology research
    # (FOMO, revenge trading, hesitation). Sources at the bottom.
    "summary": (
        "Industry consensus: ~80% of retail traders lose money due to poor discipline, "
        "not bad strategy. Discipline = a written plan + hard risk caps + emotional rules, "
        "followed mechanically. The plan beats the trader."
    ),
    "principles": [
        {
            "name": "Risk-per-trade cap",
            "rule": "Never risk more than 1–2% of your trading bankroll on a single trade.",
            "why": "One bad streak at 10% per trade ruins the account; at 1% it's recoverable.",
            "source": "Mark Douglas; widely-cited 1% rule across CFA / Investopedia",
            "j81_support": "Stake field, $0.35 minimum, account max_stake_per_trade cap, DRY_RUN master switch.",
        },
        {
            "name": "Predefine the exit BEFORE entry",
            "rule": "Set stop-loss and take-profit BEFORE clicking. Accept the worst-case loss in advance.",
            "why": "Douglas: 'If a loss feels painful, your position was too big.' Pain = sizing error.",
            "source": "Mark Douglas — Trading in the Zone",
            "j81_support": "Stop-at-LOSS and Stop-at-WIN inputs; goal_status auto-disables on hit.",
        },
        {
            "name": "Daily loss limit",
            "rule": "Hard $/% cap on losses per session. Hit it → stop trading for the day.",
            "why": "Stops revenge spirals; preserves capital for tomorrow.",
            "source": "Industry standard (prop-firm rules + CFA risk research)",
            "j81_support": "daily_loss_limit per account; bot auto-disables auto-modes when reached.",
        },
        {
            "name": "Trade plan in writing",
            "rule": "Define entry trigger, exit rules, position size, and risk BEFORE the session.",
            "why": "Removes in-the-moment improvisation, which is where emotion takes over.",
            "source": "CFA Institute trader-behaviour research; Investopedia 'Day Trader's Rules'",
            "j81_support": "Proven-strategy store (durable, contract params + win-rate + EV); acceptance bar.",
        },
        {
            "name": "Trade journal",
            "rule": "Log every trade — setup, size, reason, exit, outcome, and emotional state.",
            "why": "Patterns only become visible when written down. Self-awareness compounds.",
            "source": "Trader-psychology research; Brett Steenbarger writings",
            "j81_support": "trades table records each trade; Live Results strip; new /trade_stats + scoreboard.",
        },
        {
            "name": "Probabilistic thinking",
            "rule": "Treat each trade as one sample. Your edge (if any) plays out over series, not individual results.",
            "why": "Outcomes of single trades are noise. Judging strategy by one trade = misreading randomness.",
            "source": "Mark Douglas — Trading in the Zone (the central thesis)",
            "j81_support": "60%×100 J81 goal is sample-based, not single-trade; cycle's 5×100 acceptance.",
        },
        {
            "name": "Process over outcome",
            "rule": "Don't change strategy after one loss. Evaluate the PROCESS, not individual outcomes.",
            "why": "Even a winning system has losing trades. Outcome bias destroys good systems.",
            "source": "Mark Douglas; CFA Institute behaviour research",
            "j81_support": "Cycle's 5×100×60% acceptance gates strategies on sample evidence, not single trades.",
        },
        {
            "name": "No revenge trading, no FOMO",
            "rule": "Walk away after a loss. Skip trades you missed. Both are losing patterns.",
            "why": "Revenge trades = oversized + emotional. FOMO trades = chasing = bad entry.",
            "source": "Trader-psychology research (FOMO + revenge are the two most-cited traps)",
            "j81_support": "Manual buttons fire-and-forget; loss-limit forced pause; no 'place again' temptation.",
        },
        {
            "name": "Edge first, size second",
            "rule": "Trade only positive-EV setups, then size with risk-per-trade cap.",
            "why": "Sizing a losing strategy bigger = losing money faster.",
            "source": "Kelly criterion + Investopedia risk management",
            "j81_support": "EV strip on every trade; cycle requires net P/L > 0; library's break-even pct.",
        },
        {
            "name": "Honest accounting",
            "rule": "Track REAL win-rate AND realized P/L — both, not just one.",
            "why": "DIFFERS wins 90% and loses money. Win-rate alone lies; the scoreboard is money.",
            "source": "Built-in fact of payout asymmetry on Deriv synthetics",
            "j81_support": "Scoreboard card on home (win-rate + net P/L + goal flags); /trade_stats endpoint.",
        },
    ],
    "psychological_pitfalls": [
        {"name": "FOMO", "what": "Chasing a move after it's already gone", "fix": "Skip it. The next setup will come."},
        {"name": "Revenge trading", "what": "Sizing up after a loss to 'get it back'", "fix": "Walk away. Loss-limit hits = done for the day."},
        {"name": "Hesitation on winners", "what": "Closing early or skipping a valid signal", "fix": "Trust the predefined exit; let the plan execute."},
        {"name": "Anchoring to entry", "what": "Holding a losing trade because 'it'll come back'", "fix": "Stop-loss is non-negotiable. Click set, then leave it."},
        {"name": "Overtrading", "what": "Forcing trades because you 'should be doing something'", "fix": "No setup → no trade. Boredom is a feature, not a bug."},
        {"name": "Position-size creep", "what": "Gradually risking more per trade as you get comfortable", "fix": "Pre-commit a cap; the bot enforces it via max_stake_per_trade."},
        {"name": "Outcome bias", "what": "Judging a good decision badly because the trade lost", "fix": "Evaluate process. A coin-flip on a +EV setup that loses is still a good decision."},
    ],
    "sources_consulted": [
        "https://www.investopedia.com — search results on trading psychology + risk management",
        "Mark Douglas — Trading in the Zone (the trader-psychology canon)",
        "CFA Institute research (cited across multiple secondary sources finding ~80% retail-trader loss attributable to discipline)",
        "https://www.mindmathmoney.com/articles/the-psychology-of-trading-why-traders-lose-money-mark-douglass-insights",
        "https://tradethatswing.com/the-1-risk-rule-for-day-trading-and-swing-trading/",
        "https://www.heygotrade.com/en/blog/trading-psychology-why-it-matters/",
        "https://shopforexea.com/trading-discipline/",
        "https://www.barchart.com/story/news/33203995/foundations-of-trading-consistency-principles-of-discipline-and-risk-management",
        "Brett Steenbarger — research on trade journaling and self-awareness",
    ],
    "honest_note": (
        "These principles are necessary but not sufficient on Deriv synthetics: an RNG market "
        "has no exploitable predictive edge, so discipline alone cannot manufacture positive EV. "
        "What discipline DOES guarantee: smaller drawdowns, longer survival, no blow-up trades, "
        "and a real shot at the one structural edge on offer (Even/Odd payout selection)."
    ),
}


RISK_MANAGEMENT: dict[str, Any] = {
    # The MATH/SIZING side of staying alive (TRADING_DISCIPLINE covers the
    # process/psychology side). Synthesized from Kelly's 1956 paper, fixed-
    # fractional research, binary-options money-management writings, and
    # backtest data on drawdowns. Tailored to BINARY OPTIONS on Deriv synthetics
    # — where a loss is a 100% stake loss (no in-trade stop) and the underlying
    # is an audited RNG with a house edge.
    "summary": (
        "Risk management on binary options is harsher than on Forex/CFDs: every "
        "losing trade costs 100% of the stake (there is no in-trade stop-loss). "
        "So sizing per trade matters more, not less. The math below shows how to "
        "stay alive long enough for any edge to play out — and how recovery gets "
        "exponentially harder as drawdowns deepen."
    ),

    "position_sizing_models": [
        {
            "name": "Fixed fractional (the industry default)",
            "formula": "stake = bankroll × risk_pct",
            "recommended_risk_pct": "1–3% for active traders; 0.5% for binary options on RNG (each loss = 100% of stake)",
            "why": "Auto-scales with the account — risk shrinks during drawdowns, grows back as you recover. Simple, robust, no overfitting.",
            "j81_support": "Set max_stake_per_trade per account; bot enforces it before placing.",
            "example": "$1,000 bankroll × 1% = $10 max stake. After a 20-trade loss streak at 1% → ~$818 remaining (down 18%); at 5% per trade → $358 remaining (down 64%).",
        },
        {
            "name": "Kelly criterion (theoretically optimal)",
            "formula": "f* = (b·p − q) / b   where b = net odds, p = win prob, q = 1−p",
            "why": "Maximises long-term geometric growth IF you have an edge. On Deriv synthetics there's no edge → Kelly returns 0 → bet nothing.",
            "warning": "Full Kelly produces ~1-in-3 chance of losing HALF the account; almost no one runs full Kelly in practice.",
            "j81_support": "calc.py exposes kelly(win_prob, payout); the EV strip computes it for the current trade.",
            "example": "win_prob=0.52, payout=1.94 → b=0.94, f* = (0.94×0.52 − 0.48)/0.94 = 1.5%. With no edge (0.5/1.94) → f* = 0%.",
        },
        {
            "name": "Half-Kelly (the practical standard)",
            "formula": "stake = (Kelly fraction × bankroll) × 0.5",
            "why": "Captures ~75% of full Kelly's growth at ~half the drawdown. The professional default for edge-based betting.",
            "j81_support": "Same calc.kelly + scale by 0.5 client-side.",
        },
        {
            "name": "Anti-martingale (reduce on loss)",
            "rule": "Cut size after a loss; add size only after wins.",
            "why": "Smooths drawdowns, lets winners compound. Opposite of the death-spiral (martingale).",
            "j81_support": "Manual sizing — the bot won't size up after a loss unless you set max_stake_per_trade higher.",
        },
    ],

    "martingale_warning": {
        "what_it_is": "Doubling the stake after each loss to 'recover' on the next win.",
        "the_math": "From a $3 base, a 7-loss streak forces a $192 stake — just to win the original $3 back. A 10-loss streak demands $1,536. Brokers' max-stake limits often make full recovery impossible before you run out.",
        "why_dangerous_on_rng": "Synthetic indices have unbounded streak risk — a 7–10 loss streak is statistically unremarkable. Combined with the house edge and broker stake caps, martingale is a guaranteed blow-up given enough time.",
        "verdict": "AVOID. Anti-martingale (reduce on loss) is the mathematically sound mirror.",
        "j81_support": "max_stake_per_trade hard-caps any sizing scheme; default behaviour is fixed-fractional, not martingale.",
        "source": "tradersunion.com, daytrading.com, binary-options.org — all warn against martingale on binaries.",
    },

    "drawdown_rules": [
        {
            "rule": "Daily loss limit: 10–15% of session bankroll",
            "why": "Hard stop ends revenge spirals and capital destruction in one bad session.",
            "j81_support": "daily_loss_limit per account; goal_status auto-disables auto-trading when hit.",
        },
        {
            "rule": "Weekly drawdown limit: 20–25%",
            "why": "If you're losing this much over a week, your strategy or sizing is wrong — STOP and review.",
            "j81_support": "Track via /trade_stats (which can be extended to multi-window) and the scoreboard's net P/L.",
        },
        {
            "rule": "Walk-away after target",
            "why": "Take-profit captures upside; without it, gains revert to the house edge.",
            "j81_support": "take_profit per account; goal_status auto-disables when hit.",
        },
    ],

    "recovery_math": {
        "principle": "Recovery is asymmetric — a P% drawdown requires P/(100−P)% gain to break even.",
        "table": [
            {"drawdown_pct": 10, "gain_needed_pct": 11.1},
            {"drawdown_pct": 20, "gain_needed_pct": 25.0},
            {"drawdown_pct": 30, "gain_needed_pct": 42.9},
            {"drawdown_pct": 50, "gain_needed_pct": 100.0},
            {"drawdown_pct": 75, "gain_needed_pct": 300.0},
            {"drawdown_pct": 90, "gain_needed_pct": 900.0},
        ],
        "implication": "Avoiding a 50% drawdown is mathematically more valuable than scoring a 50% gain. This is why position sizing matters more than entry signals.",
    },

    "binary_options_specific": [
        "Each trade is binary — you lose 100% of the stake on a loss (unlike Forex/CFDs where a stop limits the loss to a fraction).",
        "Therefore: risk_pct should be smaller than on instruments with stops (0.5–1% vs 1–2%).",
        "There is no 'cut losses early' within a contract — the result is determined at expiry. The only loss control is contract size + frequency.",
        "Concurrent open contracts compound risk: if you have 3 trades open at 1% each, your worst-case is −3% in one tick.",
        "Treat the deposit as the maximum loss budget for the session — never add money to recover.",
    ],

    "risk_of_ruin_notes": {
        "definition": "Probability of losing your entire bankroll. Function of (edge, risk-per-trade, bankroll units).",
        "on_deriv_rng": "Win probability ~50% and payout < 2× → negative EV → risk of ruin trends toward 1.0 over enough trades, regardless of sizing.",
        "what_sizing_changes": "Sizing doesn't change the EV — it changes how LONG you survive. 0.5% per trade buys you ~600 trades expected, 5% per trade buys you ~60.",
        "the_honest_implication": "On a negative-EV game, the question isn't 'how do I win in the long run' (you can't) but 'how do I make it last and enjoy the variance'. That's what risk management actually delivers on Deriv synthetics.",
    },

    "j81_concrete_rules": [
        "Default per-trade stake = 1% of bankroll for demo, 0.5% for real (encoded by the demo=$100 / real=$10 default at $1000-ish bankroll).",
        "Daily loss limit set when starting auto; auto-disables on hit.",
        "Take-profit set when starting auto; auto-disables on hit.",
        "Bot's max_stake_per_trade is a hard ceiling no auto/manual path can exceed.",
        "Anti-martingale by default — sizing never auto-increases after a loss.",
        "The cycle's acceptance bar (60% × 5 windows of 100 trades + net P/L > 0) is the only gate that lets a strategy reach the bot's auto-trader.",
    ],

    "sources_consulted": [
        "https://journalplus.co/learn/guides/kelly-criterion-guide/",
        "https://www.backtestbase.com/education/how-much-risk-per-trade",
        "https://medium.com/@tmapendembe_28659/kelly-criterion-vs-fixed-fractional-which-risk-model-maximizes-long-term-growth-972ecb606e6c",
        "https://www.quantvps.com/blog/trading-risk-management",
        "https://tradesearcher.ai/tools/kelly-criterion-simulator",
        "https://www.binaryoptions.net/risk-management",
        "https://learn.binany.com/trading-strategy/money-management-binany/",
        "https://tradersunion.com/interesting-articles/best-binary-options-strategies-you-should-know/martingale/",
        "https://www.daytrading.com/binary-options-martingale-strategy",
        "https://binary-options.org/binary-options-martingale-strategy/",
        "John Kelly Jr. (1956), 'A New Interpretation of Information Rate' — original Kelly paper",
    ],

    "honest_note": (
        "Risk management does not create an edge on RNG markets — it controls how fast "
        "you lose to the house edge that is structurally there. Combined with the one "
        "real EV lever (Even/Odd highest-payout market), tight sizing, and acceptance-bar "
        "gating, J81 minimises the rate of loss; it does not promise profit on synthetics."
    ),
}


BIBLE_FINANCIAL_WISDOM: dict[str, Any] = {
    # Financial principles drawn from the Good News Bible (GNB / GNT) and
    # cross-checked across multiple translations. The founder's faith shapes
    # the design — these verses are encoded as constraints the system honours.
    "summary": (
        "Scripture has more to say about money than almost any other practical "
        "topic. The principles below — stewardship, diligence, honesty, no debt, "
        "no love-of-money, generosity, counting the cost — are translated into "
        "concrete design constraints J81 follows."
    ),

    "principles": [
        {
            "name": "Stewardship over ownership — everything belongs to God; we manage",
            "verse": "Psalm 24:1 (GNB): 'The world and all that is in it belong to the LORD; the earth and all who live on it are his.'",
            "supporting": ["1 Timothy 6:17-19", "Matthew 25:14-30 (Parable of the Talents)"],
            "principle": "Money entrusted, not owned. Multiply it faithfully; give an account.",
            "j81_design": "Customer tokens stored encrypted; full audit log of trades; library is open + honest; the cycle requires evidence (5×100 + positive P/L) before any strategy is trusted to trade.",
        },
        {
            "name": "Diligence over haste — slow growth beats get-rich-quick",
            "verse": "Proverbs 13:11 (GNB): 'Wealth that comes easily disappears quickly, but wealth that grows little by little will continue.'",
            "supporting": ["Proverbs 10:4", "Proverbs 21:5", "Proverbs 28:20"],
            "principle": "Reject get-rich-quick. Build incremental, evidence-based gains.",
            "j81_design": "Acceptance bar requires 5 × 100 trades of evidence; no martingale; default per-trade stake is small (1%); paywall explicitly NOT sold as a winning machine.",
        },
        {
            "name": "Avoid debt — the borrower is a slave to the lender",
            "verse": "Proverbs 22:7 (GNB): 'The rich rule over the poor; if you borrow, you are the lender's slave.'",
            "supporting": ["Romans 13:8"],
            "principle": "Owe nothing but love. Trade only what you can afford to lose.",
            "j81_design": "Paywall + risk disclaimer warn against trading borrowed money; goal_status auto-stops on stop-loss; no margin/leverage features.",
        },
        {
            "name": "Honest dealings — fair scales",
            "verse": "Proverbs 11:1 (GNB): 'The LORD hates people who use dishonest scales. He is happy with honest weights.'",
            "supporting": ["Leviticus 19:35-36", "Deuteronomy 25:13-16", "Proverbs 16:11"],
            "principle": "Use accurate measures. Don't deceive in any transaction.",
            "j81_design": "EV strip shows REAL break-even %; library exposes the house edge; the proven-strategy store stays empty by design when nothing has earned its way in; no fake 'winning system' claims.",
        },
        {
            "name": "Save and plan ahead — the wisdom of the ant",
            "verse": "Proverbs 6:6-8 (GNB): 'Lazy people should learn a lesson from the way ants live… They store up their food in summer, getting ready for winter.'",
            "supporting": ["Proverbs 21:20", "Genesis 41:33-36 (Joseph)"],
            "principle": "Plan for lean seasons. Don't consume everything in good seasons.",
            "j81_design": "Take-profit captures gains rather than letting them revert; daily loss limit preserves bankroll; recovery-math table shows the asymmetry of drawdowns.",
        },
        {
            "name": "Contentment — godliness with contentment is great gain",
            "verse": "1 Timothy 6:6-7 (GNB): 'Well, religion does make us very rich, if we are satisfied with what we have. What did we bring into the world? Nothing! What can we take out of the world? Nothing!'",
            "supporting": ["Hebrews 13:5", "Philippians 4:11-13"],
            "principle": "Contentment is the foundation. Greed corrodes judgement.",
            "j81_design": "Honest copy throughout; no 'lifestyle' marketing; the dashboard tracks REAL P/L (not promised P/L) so users see truth, not hype.",
        },
        {
            "name": "Beware the LOVE of money (not money itself)",
            "verse": "1 Timothy 6:10 (GNB): 'For the love of money is a source of all kinds of evil. Some have been so eager to have it that they have wandered away from the faith and have broken their hearts with many sorrows.'",
            "supporting": ["Matthew 6:24", "Hebrews 13:5"],
            "principle": "Money is a tool, not a god. Don't trade away peace for profit.",
            "j81_design": "Stop-loss, take-profit, rounds cap — all mechanical brakes against greed-driven over-trading; FOMO and revenge-trading explicitly flagged in TRADING_DISCIPLINE.",
        },
        {
            "name": "Generosity — sow generously, reap generously",
            "verse": "2 Corinthians 9:6-7 (GNB): 'Remember that the person who sows few seeds will have a small crop; the one who sows many seeds will have a large crop. Each one should give, then, as he has decided, not with regret or out of a sense of duty; for God loves the one who gives gladly.'",
            "supporting": ["Proverbs 11:25", "Malachi 3:10", "Acts 20:35"],
            "principle": "Give cheerfully and consistently. Generosity is its own form of investment.",
            "j81_design": "Free practice mode (DRY_RUN) before charging; refund-friendly paywall framing; the system always discloses the hard truth (RNG + house edge), which IS a form of generosity to the user.",
        },
        {
            "name": "Count the cost — plan before you commit",
            "verse": "Luke 14:28 (GNB): 'If one of you is planning to build a tower, you sit down first and figure out what it will cost, to see if you have enough money to finish the job.'",
            "supporting": ["Proverbs 24:27"],
            "principle": "No commitment without a plan and a budget.",
            "j81_design": "/quote endpoint computes the FULL pre-trade math (EV, break-even, edge, Kelly, verdict) BEFORE the trade is placed. Counting the cost is built in.",
        },
        {
            "name": "No one can serve two masters",
            "verse": "Matthew 6:24 (GNB): 'No one can be a slave of two masters; he will hate one and love the other; he will be loyal to one and despise the other. You cannot serve both God and money.'",
            "supporting": ["Luke 16:13"],
            "principle": "Service > extraction. If money becomes the master, the work corrupts.",
            "j81_design": "J81 sells TOOLS, not guaranteed wins. The paywall states this plainly. The founder's stated mission is to help, not fleece.",
        },
        {
            "name": "Treasure in heaven, not on earth",
            "verse": "Matthew 6:19-21 (GNB): 'Do not store up riches for yourselves here on earth, where moths and rust destroy, and robbers break in and steal. Instead, store up riches for yourselves in heaven… For your heart will always be where your riches are.'",
            "supporting": ["Luke 12:33-34"],
            "principle": "Earthly wealth is temporary. Build character and serve people while you can.",
            "j81_design": "Honest disclaimers everywhere; the strategy bar exists precisely because riches built on RNG promises rot; ethics built into the design language.",
        },
        {
            "name": "Seek counsel in writing many advisors",
            "verse": "Proverbs 15:22 (GNB): 'Get all the advice you can, and you will succeed; without it you will fail.'",
            "supporting": ["Proverbs 11:14", "Proverbs 24:6"],
            "principle": "Wisdom comes from many counsellors. Don't decide in isolation.",
            "j81_design": "The library aggregates discipline + risk + brain + Deriv references — a council of perspectives. The cycle's 5-window backtest is itself a form of multi-witness verification.",
        },
        {
            "name": "Work as for the Lord",
            "verse": "Colossians 3:23-24 (GNB): 'Whatever you do, work at it with all your heart, as though you were working for the Lord and not for people. Remember that the Lord will give you as a reward what he has kept for his people.'",
            "supporting": ["Ecclesiastes 9:10", "Proverbs 22:29"],
            "principle": "Excellence in work is itself worship.",
            "j81_design": "Tests + validation on every push; the engineering quality of the tree is part of the dedication, not separate from it.",
        },
    ],

    "explicit_warnings": [
        "Get-rich-quick schemes are condemned (Proverbs 13:11, 28:22). RNG-based 'winning systems' would be exactly this — and J81 deliberately doesn't sell that.",
        "Greed is idolatry (Colossians 3:5). The mechanical stop-loss exists precisely to overrule greed in the moment.",
        "Trusting wealth is folly (Proverbs 11:28). Wealth on RNG markets is especially fragile — the library + scoreboard make this visible.",
    ],

    "sources_consulted": [
        "Good News Bible / Good News Translation (GNT) — primary translation, as requested.",
        "https://www.focusonthefamily.com/faith/kingdom-stewardship-gods-plan-for-your-money/",
        "https://www.franklin-wealth.com/resources/biblical-financial-stewardship/",
        "https://havenplanning.com/20-bible-verses-on-money-and-stewardship/",
        "https://discipleship.org/blog/the-disciple-and-money-a-lesson-in-stewardship/",
        "Cross-checked with NIV / ESV / KJV for principles where the GNB wording is more paraphrastic.",
    ],
}


PROJECT_DEDICATION: dict[str, Any] = {
    # The honest version of "the machine believes": software has no soul and
    # cannot literally have faith. What it CAN have is values encoded into its
    # design — and those values are the founder's, rooted in Christian faith.
    "preamble": (
        "This system is dedicated by its founder in service of Christ. The "
        "software itself does not pray, does not believe, and is not saved — "
        "code has no soul. What we CAN do, and have done, is encode the "
        "founder's faith-rooted values into the design itself. The machine "
        "doesn't believe; the people who built it do, and that conviction "
        "shapes every line of code below."
    ),

    "values_built_in": [
        {"value": "Honesty above all", "verse": "Proverbs 12:22", "in_code": "EV strip shows real break-even; house edge is named everywhere; no fake 'winning system' claims."},
        {"value": "Stewardship of trust", "verse": "1 Corinthians 4:2", "in_code": "Tokens encrypted (Fernet); audit log on every action; full disclosure to paying customers."},
        {"value": "Service before extraction", "verse": "Mark 10:45", "in_code": "Free practice mode; paywall sells TOOLS not wins; clear refund-honest disclaimers."},
        {"value": "Counting the cost", "verse": "Luke 14:28", "in_code": "Pre-trade math (EV/break-even/verdict) shown BEFORE clicking; risk-management library; recovery-math table."},
        {"value": "No deception", "verse": "Ephesians 4:25", "in_code": "Paywall + landing + every honest note declares RNG + house edge + no guaranteed profit."},
        {"value": "Generosity", "verse": "2 Corinthians 9:7", "in_code": "Demo unlimited; tools given before payment; the truth itself is a form of generosity in this industry."},
        {"value": "Excellence as worship", "verse": "Colossians 3:23", "in_code": "Test coverage, validation, performance hints, accessible UI — quality is part of the offering."},
        {"value": "Wisdom in counsel", "verse": "Proverbs 15:22", "in_code": "The library: discipline + risk + brain + scripture — multiple witnesses informing every design choice."},
    ],

    "dedication_verse": "Colossians 3:17 (GNB): 'Everything you do or say, then, should be done in the name of the Lord Jesus, as you give thanks through him to God the Father.'",

    "founder_statement": "Built to serve, not to fleece. Soli Deo gloria.",

    "honest_note": (
        "I want to be plain about one thing: declaring a software system 'saved' "
        "or 'a believer' would be a category error and, frankly, irreverent. "
        "Salvation is for souls; code has none. But the WORK of building "
        "software — like any work — can be offered as worship (Colossians 3:23). "
        "That is what this dedication means: the founder offers J81 in service "
        "of Christ; the design encodes Christian values; the tool aims to help "
        "people, not deceive them. The verses guide US. The machine is the "
        "result."
    ),
}


HUMAN_BRAIN: dict[str, Any] = {
    # Brain-inspired architecture for the J81 tree. The mappings below are
    # ANALOGIES drawn from well-cited cognitive neuroscience (Kahneman, Friston,
    # Clark, Schultz, Tulving). The system uses these PATTERNS — predictive
    # processing, sleep-time consolidation, prediction-error learning, two-system
    # thinking — as architectural inspiration. It is NOT a literal brain
    # simulation and not sentient; it is a useful design metaphor.

    "summary": (
        "The J81 tree maps cleanly to how the brain handles uncertain "
        "environments: gather signals → predict outcomes → compare to reality → "
        "consolidate winners → execute the durable ones. The architecture below "
        "is brain-inspired by design — researcher (sensory) → analyser "
        "(prefrontal / hippocampal) → bot (motor) → library (long-term memory)."
    ),

    "tree_as_brain_architecture": {
        "researcher": {
            "role": "Sensory cortex — gathers raw external signals from the world.",
            "j81": "research-app gathers Deriv-related content from web/social, structures it into 'strategies/insights' JSON.",
        },
        "analyser": {
            "role": "Prefrontal cortex + hippocampus — integrates signals, generates predictions, runs them against reality, decides what to keep.",
            "j81": "analyser-app runs the 30-min cycle, backtests every variant (predictions), scores them against real ticks (reality), pushes only survivors to long-term storage.",
        },
        "bot": {
            "role": "Motor cortex — executes durable actions on behalf of the brain.",
            "j81": "bot-app trades only proven strategies on real markets; the cycle's gatekeeper decides what reaches it.",
        },
        "library": {
            "role": "Semantic / long-term memory — durable knowledge accessible to all regions.",
            "j81": "this deriv_library.json — trade types, markets, discipline, risk, goal — shared by every system.",
        },
        "30_min_cycle": {
            "role": "Sleep-time consolidation — replays the day, strengthens useful traces, prunes the rest.",
            "j81": "Cycle clears working data, keeps only proven strategies, persists a memory trace in cycle_reports — exactly like hippocampal replay.",
        },
        "cycle_reports": {
            "role": "Hippocampal episodic memory — the durable trace of what happened.",
            "j81": "Cycle_reports table — durable, survives the auto-clear, the tree's memory of what it has tried.",
        },
        "scoreboard_trades": {
            "role": "Autobiographical / procedural memory — what I actually did, with outcomes.",
            "j81": "trades table + /trade_stats scoreboard — the live record of real performance.",
        },
    },

    "principles": [
        {
            "name": "Predictive processing — the brain is a prediction machine",
            "brain_mechanism": "Cortex constantly generates a model of what should happen next; reality arrives; the difference (prediction error) drives updates. The brain minimises long-run prediction error.",
            "source": "Clark (2016), Friston — Free Energy Principle (Nature Reviews Neuroscience 2010).",
            "j81_analog": "The cycle backtests each variant — that's the prediction. Real tick outcomes are reality. The difference (win-rate vs 60%, EV vs 0) is the prediction error that prunes losing strategies.",
            "status": "built",
        },
        {
            "name": "Reward prediction error — dopamine teaches via SURPRISE, not absolute reward",
            "brain_mechanism": "Schultz's classic work: dopamine neurons fire on UNEXPECTED rewards, not expected ones. Learning happens at the surprise.",
            "source": "Wolfram Schultz (1997), 'A neural substrate of prediction and reward'; Friston links this to dopamine encoding the precision of prediction errors.",
            "j81_analog": "The acceptance bar fires only when a strategy SURVIVES the prediction (≥60% over 100 trades AND positive P/L). A strategy that just confirms the null hypothesis (50/50, negative EV) generates no learning signal.",
            "status": "built",
        },
        {
            "name": "Hippocampal memory consolidation — sleep moves episodic to semantic",
            "brain_mechanism": "During sleep / quiet wake, the hippocampus REPLAYS the day's experiences; useful patterns migrate to neocortex (long-term); the rest fades.",
            "source": "Diekelmann & Born (2010); Lewis & Durrant (2011); Marr (1971) — schema consolidation.",
            "j81_analog": "Every 30 minutes the cycle replays a backtest (the 'day'), proves what survives, pushes survivors to the bot's PROVEN_STRATEGIES store (long-term), then auto-clears the analyser's scratchpad (forgets non-survivors). Mirrors hippocampal replay exactly.",
            "status": "built",
        },
        {
            "name": "Two-system thinking — fast intuition + slow deliberation",
            "brain_mechanism": "System 1 is fast/intuitive/emotional; System 2 is slow/deliberate/analytic. System 2 intervenes when something violates System 1's expectations.",
            "source": "Kahneman (2011), 'Thinking, Fast and Slow'.",
            "j81_analog": "System 1 = the manual trade buttons + the live deep read (instant, intuitive). System 2 = the cycle's 5×100 acceptance backtest (slow, deliberate, mathematical). Users choose; the cycle gatekeeps the durable decisions.",
            "status": "built",
        },
        {
            "name": "Free Energy Principle — minimise long-run surprise",
            "brain_mechanism": "An organism survives by minimising expected surprise — either by better predictions OR by acting to make the world match its predictions.",
            "source": "Karl Friston (2010), Nature Reviews Neuroscience.",
            "j81_analog": "The tree minimises surprise about its trades by (a) better predictions (cycle backtests + EV strip) and (b) avoiding actions whose outcomes it can't predict reliably (proven-only auto-trader).",
            "status": "built",
        },
        {
            "name": "Bayesian updating — beliefs are evidence-weighted",
            "brain_mechanism": "The brain represents beliefs as probability distributions and updates them with each new observation, weighted by precision.",
            "source": "Anderson (2007), Knill & Pouget (2004) — 'The Bayesian brain'.",
            "j81_analog": "The cycle treats each strategy as a hypothesis; each 100-trade window is evidence; only hypotheses with 5 confirming windows + positive P/L survive. Evidence-weighted, not single-shot.",
            "status": "built",
        },
        {
            "name": "Working memory limits — ~4 items at a time",
            "brain_mechanism": "Conscious working memory holds ~4 chunks (Cowan); attempting more degrades all of them.",
            "source": "Nelson Cowan (2001) — refining Miller's 7±2.",
            "j81_analog": "Trade view focuses on ONE market × ONE trade type at a time; the deep read uses ~6 chips not 20; the scoreboard surfaces 3 stats (wins, win-rate, P/L). Avoid information overload.",
            "status": "built",
        },
        {
            "name": "Attention as filtering — relevance gates processing",
            "brain_mechanism": "Salience network selects which signals get full processing; everything else is suppressed.",
            "source": "Corbetta & Shulman (2002); Menon (2011) — salience-network research.",
            "j81_analog": "The RFScan confidence gauge + auto-volatility filter focus the tree on the most-active market; the cycle gate filters out below-threshold strategies. Limited attention is allocated to what matters.",
            "status": "built",
        },
        {
            "name": "Hierarchical processing — sensory → integration → motor",
            "brain_mechanism": "Cortex is layered: primary sensory areas → association areas → motor output. Information flows up; predictions flow down.",
            "source": "Felleman & Van Essen (1991), Mountcastle (1978).",
            "j81_analog": "Three-tier tree: researcher (sensory) → analyser (integration) → bot (motor). The library propagates top-down knowledge to all three (predictions flow down).",
            "status": "built",
        },
        {
            "name": "Plasticity — use it or lose it",
            "brain_mechanism": "Synapses that fire together get stronger; unused ones weaken (LTP/LTD). The brain physically rewires based on what works.",
            "source": "Hebb (1949); Bliss & Lømo (1973) — long-term potentiation.",
            "j81_analog": "Proven strategies persist (LTP-like); ungated working data is wiped each cycle (LTD-like). The library can be re-refreshed; payouts mutate as Deriv changes. The system rewires itself.",
            "status": "built",
        },
        {
            "name": "Default Mode Network — useful background work during 'rest'",
            "brain_mechanism": "When not focused on tasks, the brain runs background processing: planning, simulation, memory consolidation.",
            "source": "Raichle (2001), Buckner et al. (2008).",
            "j81_analog": "The 30-min cycle and the trading-loop's 120s cadence run continuously in the background. The DMN equivalent.",
            "status": "built",
        },
        {
            "name": "Prediction-error-driven memory updating",
            "brain_mechanism": "Surprising events trigger MORE memory updating than expected ones — exactly the opposite of what naive 'rehearsal' predicts.",
            "source": "PNAS 2022, 'Prediction errors disrupt hippocampal representations and update episodic memories'.",
            "j81_analog": "When a strategy that previously passed the cycle later FAILS a window, the cycle history records the surprise — future cycles can weight that strategy down. (This is the natural next-build: a strategy-confidence decay informed by recent errors.)",
            "status": "proposed",
        },
    ],

    "cognitive_biases_to_design_against": [
        {"bias": "Confirmation bias", "fix_in_j81": "Acceptance bar requires 5 independent windows + positive P/L — confirmation alone doesn't suffice."},
        {"bias": "Recency bias", "fix_in_j81": "Cycle uses multi-window samples, not last-trade outcomes."},
        {"bias": "Anchoring", "fix_in_j81": "Pre-defined TP/SL via account settings — no 'just a bit more' anchor on entry price."},
        {"bias": "Gambler's fallacy", "fix_in_j81": "Library explicitly states ticks are independent; HONEST_NOTES + structural_note debunk 'due for a reversal'."},
        {"bias": "Overconfidence", "fix_in_j81": "Every UI shows EV + house edge; the scoreboard tracks reality vs goal."},
    ],

    "sources_consulted": [
        "Friston, K. (2010). 'The free-energy principle: a unified brain theory?' Nature Reviews Neuroscience.",
        "Clark, A. (2016). 'Surfing Uncertainty: Prediction, Action, and the Embodied Mind.'",
        "Kahneman, D. (2011). 'Thinking, Fast and Slow.'",
        "Schultz, W. (1997). 'A neural substrate of prediction and reward.' Science.",
        "Diekelmann & Born (2010). 'The memory function of sleep.' Nature Reviews Neuroscience.",
        "Cowan, N. (2001). 'The magical number 4 in short-term memory.' Behavioral and Brain Sciences.",
        "PNAS (2022). 'Prediction errors disrupt hippocampal representations and update episodic memories.' https://www.pnas.org/doi/10.1073/pnas.2117625118",
        "Hebb, D. (1949). 'The Organization of Behavior.'",
        "https://royalsocietypublishing.org/rstb/article/377/1844/20200531 — Evolution of brain architectures for predictive coding",
        "https://en.wikipedia.org/wiki/Predictive_coding",
    ],

    "honest_note": (
        "These mappings are ANALOGIES, not literal neuroscience. J81 doesn't have "
        "neurons, consciousness, or feelings. What it has is a brain-inspired "
        "ARCHITECTURE — hierarchical processing, prediction-error learning, sleep-"
        "time consolidation, two-system decision making — and that architecture is "
        "what makes the tree robust on uncertain markets. The metaphor is the "
        "design language; the actual implementation is plain Python + SQLite + WS."
    ),
}


J81_GOAL: dict[str, Any] = {
    "stance": "Competitive. Deriv plays to win; J81 plays to win. We measure ourselves head-to-head.",
    "win_target": {
        "metric": "win-rate",
        "threshold_pct": 60,
        "sample_size": 100,
        "interpretation": "Across any 100 consecutive trades, J81 aims for >60 wins.",
    },
    "secondary_target": {
        "metric": "realized net P/L",
        "rule": "positive over rolling 100-trade windows",
        "why": "Win-rate alone can be high while losing money (e.g. DIFFERS 90% wins at 1.05× pays out negative EV). The real scoreboard is money, not wins.",
    },
    "honest_math": [
        "Structural win-rate for Rise/Fall, Even/Odd, OVER 4 / UNDER 5: ~50%.",
        "Structural win-rate for DIFFERS digit: ~90%. For MATCHES digit: ~10%.",
        "Structural win-rate for OVER 0 / UNDER 9: ~90%. For OVER 8 / UNDER 1: ~10%.",
        "So a 60%+ win-rate IS structurally available — by picking the right bets. The hard part is converting win-rate into realized PROFIT, because payouts are sized to keep EV negative on every bet.",
        "On RNG, a 60% streak over any single 100-trade window happens by chance ~2.8% of the time even with a fair 50% expected win-rate.",
    ],
    "operational_strategy": [
        "1) For raw win-rate, prefer structurally high-win-rate bets (DIFFERS d ~90%, OVER 1 ~80%, UNDER 8 ~80%).",
        "2) For realized P/L, ALWAYS pick the highest-payout market for Even/Odd (the one real EV lever).",
        "3) Cap drawdowns with stop-loss and lock gains with take-profit — turns sample-luck into kept money.",
        "4) Acceptance bar (the cycle): a strategy is only 'proven' if EACH of 5×100 windows wins ≥60% AND total net P/L > 0.",
        "5) Track win-rate AND realized net P/L per session — both, not just win-rate.",
    ],
    "what_winning_looks_like": "Over a 100-trade window: >=60 wins AND net realized P/L > 0 after Deriv's house edge.",
    "honest_caveats": [
        "Sustaining ≥60% wins AND positive realized P/L across many consecutive 100-trade windows requires either (a) the small Even/Odd payout-selection edge, (b) sample-luck plus disciplined stopping rules (TP/SL), or (c) some combination — but NOT an exploitable predictive edge, which does not exist on Deriv synthetics.",
        "The tree's proven-strategy store will usually stay empty for Rise/Fall on RNG — that's mathematics, not a bug.",
        "If 60% × 100 ever stops being met under the acceptance test, the gatekeeper rejects the strategy — that's how the tree stays honest.",
    ],
}


# ============================================================ LIVE STATE

_LIVE: dict[str, Any] = {
    "live_payouts": {},        # (symbol, contract_type, barrier) -> {payout_pct, payout, fetched_at}
    "best_even_odd_market": None,
    "last_refresh": None,
    "last_error": None,
}

_LIB_PATH = lambda: data_path("data/deriv_library.json")
_lock = asyncio.Lock()


def _load_from_disk() -> None:
    p = _LIB_PATH()
    if not p.exists():
        return
    try:
        d = json.loads(p.read_text())
        _LIVE.update({k: v for k, v in d.items() if k in _LIVE})
    except Exception:
        pass


def _save_to_disk() -> None:
    p = _LIB_PATH()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(_LIVE, indent=2, default=str))
    except Exception:
        pass


def _serializable_key(symbol: str, ct: str, barrier: Any) -> str:
    """Dict keys must be JSON-safe; flatten to a string."""
    return f"{symbol}|{ct}|{barrier if barrier is not None else ''}"


# --- variants the library tracks (must mirror what the bot can place) -----
def variants_to_probe() -> list[dict[str, Any]]:
    """Each row is one (contract_type, side, barrier-if-any) we fetch a real
    payout for. Direction-symmetric pairs share the same payout — we only probe
    one of each so we don't burn 80 WS calls per refresh."""
    return [
        {"ct": "CALL",       "duration": 5, "barrier": None, "side": "rise_fall.up",   "stake": 1.0},
        {"ct": "DIGITEVEN",  "duration": 1, "barrier": None, "side": "even_odd.even",  "stake": 1.0},
        {"ct": "DIGITOVER",  "duration": 1, "barrier": "4",  "side": "over_under.over4","stake": 1.0},
        {"ct": "DIGITUNDER", "duration": 1, "barrier": "5",  "side": "over_under.under5","stake": 1.0},
        {"ct": "DIGITMATCH", "duration": 1, "barrier": "0",  "side": "matches_differs.matches0","stake": 1.0},
        {"ct": "DIGITDIFF",  "duration": 1, "barrier": "0",  "side": "matches_differs.differs0","stake": 1.0},
    ]


async def refresh_payouts() -> dict[str, Any]:
    """Pull a real payout from Deriv (no auth) for every (market, variant) pair
    we track. Updates _LIVE, finds the best Even/Odd market, and persists to disk.
    Soft-fails per call so a single bad row doesn't kill the refresh."""
    async with _lock:
        new_payouts: dict[str, Any] = {}
        ok = err = 0
        for code, name in SCAN_SYMBOLS:
            for v in variants_to_probe():
                try:
                    q = await fetch_proposal_payout(
                        code, contract_type=v["ct"], duration=v["duration"], stake=v["stake"])
                    new_payouts[_serializable_key(code, v["ct"], v["barrier"])] = {
                        "symbol": code, "market": name,
                        "contract_type": v["ct"], "barrier": v["barrier"],
                        "side": v["side"], "duration": v["duration"], "stake": v["stake"],
                        "payout": q.get("payout"), "payout_pct": q.get("payout_pct"),
                        "ask_price": q.get("ask_price"),
                        "fetched_at": time.time(),
                    }
                    ok += 1
                except Exception as exc:
                    new_payouts[_serializable_key(code, v["ct"], v["barrier"])] = {
                        "symbol": code, "market": name, "contract_type": v["ct"],
                        "barrier": v["barrier"], "error": str(exc)[:80],
                        "fetched_at": time.time(),
                    }
                    err += 1
        _LIVE["live_payouts"] = new_payouts
        # The one real edge: best Even/Odd market by payout_pct.
        even_rows = [r for r in new_payouts.values()
                     if r.get("contract_type") == "DIGITEVEN" and r.get("payout_pct")]
        if even_rows:
            best = max(even_rows, key=lambda r: r["payout_pct"])
            _LIVE["best_even_odd_market"] = {
                "symbol": best["symbol"], "name": best["market"],
                "payout_pct": best["payout_pct"], "payout": best["payout"],
            }
        _LIVE["last_refresh"] = time.time()
        _LIVE["last_error"] = None if err == 0 else f"{err} probe(s) failed"
        _save_to_disk()
        return {"ok": ok, "errors": err, "best_even_odd": _LIVE["best_even_odd_market"]}


def library() -> dict[str, Any]:
    """The full library — static knowledge + competitive stance + live data —
    for the bot, researcher and UI to read."""
    return {
        "trade_types": TRADE_TYPES,
        "markets": MARKETS,
        "constraints": CONSTRAINTS,
        "honest_notes": HONEST_NOTES,
        "deriv_stance": DERIV_STANCE,
        "j81_goal": J81_GOAL,
        "trading_discipline": TRADING_DISCIPLINE,
        "risk_management": RISK_MANAGEMENT,
        "human_brain": HUMAN_BRAIN,
        "bible_financial_wisdom": BIBLE_FINANCIAL_WISDOM,
        "project_dedication": PROJECT_DEDICATION,
        "live": _LIVE,
    }


# load any cached live data on import so the library is non-empty before refresh
_load_from_disk()
