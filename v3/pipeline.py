"""Pipeline orchestrator. PR1: skeleton only; real logic added in PR2-PR4."""
from __future__ import annotations

import json
import traceback
from datetime import datetime
from typing import Any

from . import config, db
from .signals import SignalResult


def _start_run(c, keyword_count: int) -> int:
    cur = c.execute(
        "INSERT INTO scan_runs(started_at, keyword_count) VALUES(?, ?)",
        (db.now_iso(), keyword_count),
    )
    return cur.lastrowid


def _finish_run(c, run_id: int, **stats: Any) -> None:
    c.execute(
        """UPDATE scan_runs SET
             finished_at = ?,
             products_found = ?,
             products_scored = ?,
             go_test_count = ?,
             watch_count = ?,
             kill_count = ?,
             error_log = ?
           WHERE id = ?""",
        (
            db.now_iso(),
            stats.get("products_found", 0),
            stats.get("products_scored", 0),
            stats.get("go_test_count", 0),
            stats.get("watch_count", 0),
            stats.get("kill_count", 0),
            stats.get("error_log", ""),
            run_id,
        ),
    )


def scan(keywords: list[str], top: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Top-level pipeline: discover → enrich → score → decide → output.

    Returns a summary dict. Wired up incrementally in subsequent PRs.
    """
    from .pipeline_impl import run_pipeline  # lazy import to keep PR1 light

    return run_pipeline(keywords=keywords, top=top, dry_run=dry_run)
