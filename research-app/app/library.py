"""The arranged-by-trade-type view of everything gathered in local mode.

Reads the out/ tree that logging_client writes and returns it grouped by
trade type — the same concentrated shape that is shipped downstream for
testing. (When LOGGING_APP_URL is set, data goes straight to that app and
this local view stays empty.)
"""

from __future__ import annotations

import json

from app.logging_client import out_insights, out_strategies


def _read_group(base) -> dict[str, list]:
    out: dict[str, list] = {}
    if not base.exists():
        return out
    for tt_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        items = []
        for f in sorted(tt_dir.glob("*.json")):
            try:
                items.append(json.loads(f.read_text()))
            except json.JSONDecodeError:
                continue
        if items:
            out[tt_dir.name] = items
    return out


def build_library() -> dict:
    strategies = _read_group(out_strategies())
    insights = _read_group(out_insights())
    return {
        "strategies_by_trade_type": strategies,
        "insights_by_trade_type": insights,
        "counts": {
            "strategies": {k: len(v) for k, v in strategies.items()},
            "insights": {k: len(v) for k, v in insights.items()},
        },
    }
