"""APScheduler daemon — daily 9am scan + watch_pool recheck."""
from __future__ import annotations

import time
from pathlib import Path

from . import config, db


def run_watch(cron_hour: int = 9) -> None:
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        raise SystemExit("APScheduler not installed. pip install apscheduler")

    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(_daily_scan, CronTrigger(hour=cron_hour, minute=0))
    sched.add_job(_recheck_watch, CronTrigger(hour=cron_hour + 1, minute=0))
    print(f"v3 watch daemon started. Daily scan at {cron_hour:02d}:00, recheck at {cron_hour+1:02d}:00 (UTC).")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass


def _daily_scan() -> None:
    """Re-run scan on all keywords ever added (or a maintained watchlist file)."""
    from . import pipeline_impl
    with db.conn() as c:
        rows = c.execute("SELECT keyword FROM keywords").fetchall()
    keywords = [r["keyword"] for r in rows]
    if not keywords:
        print("[watch] no keywords to scan")
        return
    print(f"[watch] running daily scan on {len(keywords)} keywords")
    pipeline_impl.run_pipeline(keywords=keywords, top=None, dry_run=False)


def _recheck_watch() -> None:
    """Rescan products in WATCH status whose recheck_at has elapsed."""
    from . import pipeline_impl
    now = db.now_iso()
    with db.conn() as c:
        rows = c.execute(
            """SELECT DISTINCT p.id FROM products p
               JOIN decisions d ON d.product_id = p.id
               WHERE d.decision = 'WATCH'
                 AND d.watch_recheck_at IS NOT NULL
                 AND d.watch_recheck_at <= ?
                 AND d.id = (SELECT MAX(id) FROM decisions WHERE product_id = p.id)""",
            (now,),
        ).fetchall()
    pids = [r["id"] for r in rows]
    if not pids:
        print("[watch] no products to recheck")
        return
    print(f"[watch] rechecking {len(pids)} watched products")
    for pid in pids:
        signals = pipeline_impl._collect_signals_for_product(pid)
        pipeline_impl.stage_score_and_decide(pid, signals)
