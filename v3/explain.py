"""Decision replay — `v3 explain <product_id>`."""
from __future__ import annotations

import json
from . import db


def explain_product(pid: int) -> str:
    with db.conn() as c:
        prow = c.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        if not prow:
            return f"product id {pid} not found"
        srow = c.execute(
            "SELECT * FROM scores WHERE product_id = ? ORDER BY scored_at DESC LIMIT 1",
            (pid,),
        ).fetchone()
        drow = c.execute(
            "SELECT * FROM decisions WHERE product_id = ? ORDER BY decided_at DESC LIMIT 1",
            (pid,),
        ).fetchone()
        scored_at = srow["scored_at"] if srow else None
        if scored_at:
            erows = c.execute(
                "SELECT * FROM explain_log WHERE product_id = ? AND scored_at = ? ORDER BY id",
                (pid, scored_at),
            ).fetchall()
        else:
            erows = []

    lines = []
    lines.append(f"=== Product #{pid}: {prow['title'] or '(no title)'}")
    lines.append(f"    shop:  {prow['shop_domain']}/{prow['handle']}")
    lines.append(f"    price: ${prow['price_usd']}")
    lines.append(f"    url:   {prow['product_url'] or prow['landing_url']}")
    lines.append("")
    if srow:
        lines.append("=== Scores")
        lines.append(f"    fb={srow['fb_score']}  profit={srow['profit_score']}  trend={srow['trend_score']}  lp={srow['lp_score']}")
        lines.append(f"    composite = {srow['composite_score']}  weights={srow['weights_json']}")
        lines.append("")
    if drow:
        lines.append("=== Decision")
        lines.append(f"    {drow['decision']}")
        if drow["kill_reason"]:
            lines.append(f"    kill_reason: {drow['kill_reason']}")
        if drow["watch_reason"]:
            lines.append(f"    watch_reason: {drow['watch_reason']}  recheck_at: {drow['watch_recheck_at']}")
        if drow["test_plan_json"]:
            tp = json.loads(drow["test_plan_json"])
            lines.append(f"    test_plan: budget=${tp.get('test_budget_usd')}  creatives={tp.get('test_creatives_required')}  target_roas={tp.get('target_roas')}")
            lines.append(f"               kill_d3<{tp.get('kill_rule_day3')}  kill_d7<{tp.get('kill_rule_day7')}  scale_d7≥{tp.get('scale_rule')}")
        lines.append("")
    if erows:
        lines.append("=== Rule trace")
        cur_sig = None
        for er in erows:
            if er["signal"] != cur_sig:
                lines.append(f"  [{er['signal']}]")
                cur_sig = er["signal"]
            mark = "✓" if er["passed"] else "✗"
            line = f"    {mark} {er['rule']}: observed={er['observed']} threshold={er['threshold']}"
            if er["note"]:
                line += f"  ({er['note']})"
            lines.append(line)
    return "\n".join(lines)
