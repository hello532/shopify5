"""v3 CLI — five commands only: init, scan, watch, explain, report."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence


def _cmd_init(args: argparse.Namespace) -> int:
    from . import db, health
    print("→ Initializing SQLite database...")
    p = db.init_db()
    print(f"  ok: {p}")
    print()
    print(health.format_report(health.run_all()))
    return 0


def _cmd_scan_old(args: argparse.Namespace) -> int:
    pass


def _cmd_watch(args: argparse.Namespace) -> int:
    from . import scheduler
    scheduler.run_watch(cron_hour=args.hour)
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    from . import explain
    text = explain.explain_product(args.product_id)
    print(text)
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from .outputs import decision_table
    since = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"invalid --since (use YYYY-MM-DD): {args.since}", file=sys.stderr)
            return 2
    out = decision_table.generate(since=since, fmt=args.format)
    print(f"report written: {out}")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    from . import pipeline
    # PR9 hook: --include-radar pulls in new competitor products as keywords
    radar_kws: list[str] = []
    if getattr(args, "include_radar", False):
        from .radar import daily_diff as _radar
        new_prods = _radar.pending_new_products(limit=100)
        radar_kws = list({(p.get("title") or "").split()[:3][0] for p in new_prods if p.get("title")})
        if radar_kws:
            print(f"[radar] adding {len(radar_kws)} keywords from competitor watch")

    if args.subcmd == "sync-perf":
        return _scan_sync_perf(args)
    if args.subcmd == "actions":
        return _scan_actions(args)
    if args.subcmd == "tune":
        return _scan_tune(args)
    if args.subcmd == "radar":
        return _scan_radar(args)
    if args.subcmd == "patterns":
        return _scan_patterns(args)
    if args.subcmd == "clone":
        return _scan_clone(args)

    # subcmd == "run"
    kw_file = Path(args.keywords_file) if args.keywords_file else None
    if not kw_file or not kw_file.exists():
        if radar_kws:
            keywords = radar_kws
        else:
            print(f"keywords file not found: {kw_file}", file=sys.stderr)
            return 2
    else:
        keywords = [
            line.strip()
            for line in kw_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ] + radar_kws
    if not keywords:
        print("no keywords", file=sys.stderr)
        return 2
    summary = pipeline.scan(keywords, top=args.top, dry_run=args.dry_run)
    print()
    print("=" * 50)
    print(f"scan finished: run_id={summary['run_id']}")
    print(f"  keywords:       {summary['keyword_count']}")
    print(f"  products found: {summary['products_found']}")
    print(f"  products scored:{summary['products_scored']}")
    print(f"  🟢 GO_TEST:     {summary['go_test_count']}")
    print(f"  🟡 WATCH:       {summary['watch_count']}")
    print(f"  🔴 KILL:        {summary['kill_count']}")
    if summary.get("report_path"):
        print(f"  report:         {summary['report_path']}")
    return 0


# -------- scan sub-modes (PR6-PR9) --------

def _scan_sync_perf(args: argparse.Namespace) -> int:
    from datetime import datetime as _dt, timedelta as _td
    from .performance import meta_ads_api, feedback
    since = _dt.utcnow() - _td(days=args.days)
    try:
        rows = meta_ads_api.fetch_ad_insights(since=since)
    except Exception as e:
        print(f"meta API error: {e}", file=sys.stderr)
        return 3
    counters = feedback.ingest_rows(rows)
    print(f"sync-perf: fetched {len(rows)} rows · {counters}")
    return 0


def _scan_actions(args: argparse.Namespace) -> int:
    from .performance import auto_action
    if args.evaluate:
        c = auto_action.evaluate_all_active()
        print(f"emitted actions: {c}")
    if args.list:
        pending = auto_action.pending_actions()
        if not pending:
            print("(no pending actions)")
        for a in pending:
            print(f"  #{a['id']:<4} {a['action']:<7} {a['title'][:40]:<40} ad_id={a['ad_id'] or '-'} reason={a['reason']}")
    if args.apply:
        for aid in args.apply:
            res = auto_action.apply_action(int(aid), dry_run=not args.live)
            print(f"  apply #{aid}: {res}")
    return 0


def _scan_tune(args: argparse.Namespace) -> int:
    from .learn import patterns, threshold_tuner
    if args.patterns:
        c = patterns.extract_patterns()
        print(f"pattern extraction: {c}")
        for p in patterns.top_patterns(limit=10):
            print(f"  {p['pattern_key']:<40}  win_rate={p['win_rate']:.0%}  n={p['total_count']}  avg_roas={p['avg_roas']:.2f}")
    if args.thresholds:
        keys = [
            "scoring.go_test.composite_min",
            "scoring.go_test.fb_min",
            "scoring.go_test.profit_min",
        ]
        for k in keys:
            r = threshold_tuner.suggest_threshold(k)
            print(f"  {k}: {r}")
    return 0


def _scan_radar(args: argparse.Namespace) -> int:
    from .radar import daily_diff
    if args.add:
        cid = daily_diff.add_competitor(args.add)
        print(f"added competitor #{cid}: {args.add}")
    if args.list:
        for c in daily_diff.list_competitors():
            print(f"  #{c['id']:<4} {c['shop_domain']:<35} last={c['last_checked_at']}  products={c['products_seen_count']}")
    if args.check:
        summaries = daily_diff.check_all()
        for s in summaries:
            print(f"  {s}")
    if args.pending:
        for p in daily_diff.pending_new_products():
            print(f"  [{p['shop_domain']}] {p['title']}  ${p.get('price_usd')}")
    return 0


def _scan_patterns(args: argparse.Namespace) -> int:
    from .learn import patterns
    c = patterns.extract_patterns()
    print(f"extracted {c['patterns']} patterns from {c['samples']} samples")
    for p in patterns.top_patterns(limit=20):
        print(f"  {p['pattern_key']:<40}  win_rate={p['win_rate']:.0%}  n={p['total_count']}  avg_roas={p['avg_roas']:.2f}")
    return 0


def _scan_clone(args: argparse.Namespace) -> int:
    from .shopify import auto_create
    if args.list:
        clones = auto_create.list_clones(limit=200)
        if not clones:
            print("(no clones yet)")
        for c in clones:
            mark = {"ACTIVE":"🟢","DRAFT":"⚪","FAILED":"🔴"}.get(c["status"], "·")
            print(f"  #{c['id']:<4} {mark} {c['status']:<7} src_pid={c['source_product_id']:<5} "
                  f"→ {c.get('shopify_product_handle') or '(no handle)':<35} "
                  f"@ {c['shop_destination']}")
            if c.get("error"):
                print(f"        err: {c['error'][:200]}")
        return 0
    if args.pid:
        for pid in args.pid:
            print(f"→ clone pid={pid} dry_run={args.dry_run} force={args.force}")
            try:
                res = auto_create.clone(int(pid), dry_run=args.dry_run, force=args.force,
                                         status="ACTIVE" if args.active else "DRAFT")
            except RuntimeError as e:
                print(f"  ❌ {e}")
                continue
            _print_clone_result(res)
        return 0
    if args.batch:
        print(f"→ batch clone decision={args.batch} limit={args.limit} dry_run={args.dry_run}")
        try:
            res = auto_create.clone_batch(decision_filter=args.batch, limit=args.limit,
                                          dry_run=args.dry_run, force=args.force)
        except RuntimeError as e:
            print(f"❌ {e}")
            return 3
        print(f"  attempted={res['attempted']}  cloned={res['cloned']}  "
              f"skipped={res['skipped']}  failed={res['failed']}")
        for r in res["results"][:20]:
            mark = "✓" if r.get("ok") else "✗"
            print(f"    {mark} pid={r['pid']}  {r.get('error') or (r.get('clone') or {}).get('shopify_admin_url') or 'ok'}")
        return 0
    print("nothing to do — use --pid <id> [...], --batch GO_TEST, or --list", file=sys.stderr)
    return 2


def _print_clone_result(res: dict) -> None:
    if res.get("dry_run"):
        print(f"  [dry-run] would send to Shopify; payload keys: {list(res['would_send'].keys())}")
        wp = res["would_send"]
        print(f"            title='{wp.get('title','')[:60]}'  status={wp.get('status')}  "
              f"variants={len(wp.get('variants') or [])}  images={len(wp.get('files') or [])}")
        return
    if res.get("already_cloned"):
        cl = res["clone"]
        print(f"  ⚠ already cloned: #{cl['id']} status={cl['status']} → {cl.get('shopify_admin_url')}")
        return
    if not res.get("ok"):
        print(f"  ❌ {res.get('error')}")
        return
    cl = res["clone"]
    print(f"  ✅ status={cl['status']}  admin: {cl.get('shopify_admin_url')}")
    if cl.get("shopify_storefront_url"):
        print(f"     storefront: {cl['shopify_storefront_url']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="v3",
        description="FB Ads × Shopify auto-decision pipeline",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="initialize DB + health check")
    p_init.set_defaults(func=_cmd_init)

    p_scan = sub.add_parser("scan", help="run pipeline / sub-modes")
    scan_sub = p_scan.add_subparsers(dest="subcmd", required=True)

    # scan run <keywords_file>  (default workflow)
    p_run = scan_sub.add_parser("run", help="run discover→enrich→score→decide pipeline on keywords file")
    p_run.add_argument("keywords_file", help="path to file with one keyword per line")
    p_run.add_argument("--top", type=int, default=None, help="top N to include in report")
    p_run.add_argument("--dry-run", action="store_true", help="run without writing reports/kits")
    p_run.add_argument("--include-radar", action="store_true", help="prepend competitor-radar new products as extra keywords")
    p_run.set_defaults(func=_cmd_scan, subcmd="run")

    # scan sync-perf  →  pull Meta Ads insights into ad_performance
    p_perf = scan_sub.add_parser("sync-perf", help="ingest Meta Ads API insights")
    p_perf.add_argument("--days", type=int, default=7)
    p_perf.set_defaults(func=_cmd_scan, subcmd="sync-perf")

    # scan actions  →  evaluate / list / apply auto kill/scale
    p_act = scan_sub.add_parser("actions", help="auto kill/scale actions from performance loop")
    p_act.add_argument("--evaluate", action="store_true", help="run rule engine and emit new actions")
    p_act.add_argument("--list", action="store_true", help="list pending actions")
    p_act.add_argument("--apply", nargs="*", help="action IDs to apply (default: dry-run)")
    p_act.add_argument("--live", action="store_true", help="actually call Meta API (default dry-run)")
    p_act.set_defaults(func=_cmd_scan, subcmd="actions")

    # scan tune  →  patterns + thresholds
    p_tune = scan_sub.add_parser("tune", help="learn winning patterns + suggest threshold tweaks")
    p_tune.add_argument("--patterns", action="store_true")
    p_tune.add_argument("--thresholds", action="store_true")
    p_tune.set_defaults(func=_cmd_scan, subcmd="tune")

    # scan radar  →  competitor watch CRUD
    p_radar = scan_sub.add_parser("radar", help="competitor radar")
    p_radar.add_argument("--add", help="add shop domain to watch list")
    p_radar.add_argument("--list", action="store_true")
    p_radar.add_argument("--check", action="store_true", help="diff all watched stores now")
    p_radar.add_argument("--pending", action="store_true", help="show new products detected")
    p_radar.set_defaults(func=_cmd_scan, subcmd="radar")

    # scan patterns  →  shortcut to learn patterns
    p_pat = scan_sub.add_parser("patterns", help="extract & list winning patterns")
    p_pat.set_defaults(func=_cmd_scan, subcmd="patterns")

    # scan clone  →  push v3 products to Shopify store as DRAFT (PR10)
    p_clone = scan_sub.add_parser("clone", help="clone v3 product(s) to Shopify store")
    p_clone.add_argument("--pid", nargs="*", help="v3 product IDs to clone")
    p_clone.add_argument("--batch", help="batch-clone all of decision (e.g. GO_TEST)")
    p_clone.add_argument("--limit", type=int, default=None)
    p_clone.add_argument("--list", action="store_true", help="list past clones")
    p_clone.add_argument("--dry-run", action="store_true", help="build payload without POSTing")
    p_clone.add_argument("--force", action="store_true", help="re-clone even if already pushed")
    p_clone.add_argument("--active", action="store_true", help="set status=ACTIVE (default DRAFT)")
    p_clone.set_defaults(func=_cmd_scan, subcmd="clone")

    p_watch = sub.add_parser("watch", help="daemon — daily scan + watch-pool recheck")
    p_watch.add_argument("--hour", type=int, default=9, help="cron hour (local)")
    p_watch.set_defaults(func=_cmd_watch)

    p_explain = sub.add_parser("explain", help="replay decision trace for a product")
    p_explain.add_argument("product_id", type=int)
    p_explain.set_defaults(func=_cmd_explain)

    p_report = sub.add_parser("report", help="regenerate decision report")
    p_report.add_argument("--since", help="YYYY-MM-DD")
    p_report.add_argument("--format", choices=["xlsx", "html", "json"], default="xlsx")
    p_report.set_defaults(func=_cmd_report)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
