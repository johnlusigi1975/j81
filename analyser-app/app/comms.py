"""Inter-app comms — the Analyser is the hub.

Message types:
  command  — "do this" (e.g. balance search toward even_odd)
  advice   — "you might be doing X wrong, consider Y"
  grade    — a 0-10 mark on the other app's recent work
  report   — status / what I just did
  request  — please send me more of Z

The brain (this app) emits grades + balance commands after each backtest,
and advice when it spots a structural problem in what it's receiving.
"""

from __future__ import annotations

from app.store import get_store

RESEARCHER = "researcher"
ANALYSER = "analyser"
BOT = "bot"


def emit(
    *,
    to_app: str,
    type: str,
    subject: str,
    body: str = "",
    grade: float | None = None,
    data: dict | None = None,
    from_app: str = ANALYSER,
) -> str:
    return get_store().add_comms(
        {
            "from_app": from_app,
            "to_app": to_app,
            "type": type,
            "subject": subject,
            "body": body,
            "grade": grade,
            "data": data or {},
        }
    )


def grade_research_batch(backtest_results: list[dict]) -> None:
    """After a backtest run, grade the Researcher on the quality of what it
    sent, and command it to balance toward the trade type the brain most
    needs. Also drop advice if there's a structural problem."""
    tested = [r for r in backtest_results if "error" not in r]
    if not tested:
        emit(
            to_app=RESEARCHER,
            type="advice",
            subject="nothing testable",
            body="Your last batch produced no backtestable strategies. "
                 "Prioritise sources with explicit entry/exit rules and "
                 "concrete indicator thresholds.",
            grade=None,
        )
        return

    survived = [r for r in tested if r.get("status") == "survived"]
    rejected = [r for r in tested if r.get("status") == "rejected"]
    inconclusive = [r for r in tested if r.get("status") == "inconclusive"]

    # Grade: survivors are good; lots of inconclusive (too few trades) is meh;
    # all-rejected is bad. Scale 0-10.
    n = len(tested)
    score = (
        10.0 * (len(survived) / n)
        + 4.0 * (len(inconclusive) / n)   # partial credit — testable but thin
        + 0.0 * (len(rejected) / n)
    )
    score = round(min(10.0, score), 1)

    emit(
        to_app=RESEARCHER,
        type="grade",
        subject="strategy batch quality",
        grade=score,
        body=(
            f"Backtested {n}: {len(survived)} survived, {len(rejected)} rejected, "
            f"{len(inconclusive)} inconclusive (too few trades to judge). "
            + (
                "Solid work." if score >= 7
                else "Mixed — more strategies with clear, frequently-triggering rules, please."
                if score >= 4
                else "Weak batch — the rules rarely fire or lose. Find better-specified strategies."
            )
        ),
        data={"survived": len(survived), "rejected": len(rejected),
              "inconclusive": len(inconclusive)},
    )

    # Balance command: which trade type does the brain most lack survivors for?
    store = get_store()
    by_tt = store.stats()["strategies"]["by_trade_type"]
    have_survivors_tt = {r.get("trade_type") for r in survived}
    wanted = None
    for tt in ("rise_fall", "even_odd", "over_under", "matches_differs", "higher_lower"):
        if tt not in by_tt or tt not in have_survivors_tt:
            wanted = tt
            break
    if wanted:
        emit(
            to_app=RESEARCHER,
            type="command",
            subject="balance search",
            body=f"Brain needs more {wanted} strategies — weight your search toward it.",
            data={"trade_type": wanted, "weight": 3.0},
        )
