"""Researcher disk auto-clear.

The Researcher accumulates files forever:
  * out/_archive/<timestamp>/…   — sharing.py MOVES sent files here and nothing
                                   ever deletes them. This is the real disk hog.
  * out/strategies, out/insights — the live library (cleared only on a deep run,
                                   since the cycle's winners already live in the
                                   bot's strategy store).

This module reclaims that space. It's the researcher half of the tree's 30-min
auto-clear (the analyser half lives in analyser/app/store.reset_working_data).
"""

from __future__ import annotations

import shutil

from app.config import data_path


def _dir_bytes(path) -> int:
    total = 0
    if path.exists():
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    return total


def _wipe_children(path) -> int:
    """Delete everything inside `path` (but keep the dir). Returns items removed."""
    removed = 0
    if not path.exists():
        return 0
    for child in path.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed


def cleanup(deep: bool = False) -> dict:
    """Always clear out/_archive (pure dead weight). With deep=True also clear the
    live out/strategies + out/insights (used by the 30-min cycle: winners are
    already in the bot). Returns a small reclaimed-space report."""
    archive = data_path("out/_archive")
    strat = data_path("out/strategies")
    ins = data_path("out/insights")
    before = _dir_bytes(archive) + (_dir_bytes(strat) + _dir_bytes(ins) if deep else 0)
    report = {"archive_removed": _wipe_children(archive)}
    if deep:
        report["strategies_removed"] = _wipe_children(strat)
        report["insights_removed"] = _wipe_children(ins)
    after = _dir_bytes(archive) + (_dir_bytes(strat) + _dir_bytes(ins) if deep else 0)
    report["reclaimed_mb"] = round((before - after) / 1e6, 2)
    return report
