"""Profit math tests (pytest-style, but runnable as plain python)."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Isolate tests in a temp DB so we never pollute output/v3/v3.sqlite
_TMPDIR = tempfile.mkdtemp(prefix="v3test_")
os.environ["V3_TEST_TMPDIR"] = _TMPDIR

from v3 import config as _config

# Override DB path before any other v3 module loads it
_orig_load = _config.load
def _patched_load(*args, **kwargs):
    cfg = _orig_load(*args, **kwargs)
    cfg = dict(cfg)
    cfg["paths"] = dict(cfg.get("paths", {}))
    cfg["paths"]["db"] = str(Path(_TMPDIR) / "v3test.sqlite")
    _config._CACHE = cfg
    return cfg
_config.load = _patched_load
_config._CACHE = None  # force reload

from v3 import db
from v3.signals import profit


def setup_module(module):
    db.init_db()


import time
_handle_counter = [0]

def _make_product(price: float, category: str = "home") -> dict:
    _handle_counter[0] += 1
    handle = f"h{_handle_counter[0]}_{int(time.time()*1000)}_{price}{category}"
    with db.conn() as c:
        cur = c.execute(
            """INSERT INTO products(shop_domain, handle, title, price_usd, category,
                                    first_seen_at, last_seen_at)
               VALUES(?,?,?,?,?,?,?)""",
            ("test.com", handle, f"Test {category}", price, category,
             db.now_iso(), db.now_iso()),
        )
        pid = cur.lastrowid
        row = c.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
        return dict(row)


def test_marcus_rule_triggers_when_margin_below_15():
    # cost=$10, ship=$4.5, fee=2.9%, refund=8% (home), price = $20 → margin ≈ ?
    p = _make_product(20.0, "home")
    raw = {"source_cost_usd": 10.0}
    res = profit.collect(p, raw)
    assert res.status == "ok", res.error
    margin = res.data["gross_margin_pct"]
    # 20 - 10 - 4.5 - 0.58 - 1.6 = 3.32 / 20 = 16.6%  → just above 15%
    # so should NOT trigger Marcus rule; check borderline
    assert margin > 15
    print(f"  borderline margin = {margin:.1f}% (expect just above 15)")

    # Now a clearly thin case
    p2 = _make_product(15.0, "home")
    res2 = profit.collect(p2, {"source_cost_usd": 10.0})
    assert res2.status == "ok"
    assert res2.score == 0.0, f"expected profit_score=0 for Marcus Rule, got {res2.score}"
    assert res2.data["gross_margin_pct"] < 15
    print(f"  thin margin = {res2.data['gross_margin_pct']:.1f}% → score=0 ✓")


def test_known_math_from_v2_prompt():
    # Spec: price=30, cost=10, fee=0.029, ship=4.5, refund=0.08 (home, default), cat=home
    # → margin ≈ 41%, BEROAS ≈ 2.44, target_roas ≈ 2.93
    p = _make_product(30.0, "home")
    res = profit.collect(p, {"source_cost_usd": 10.0})
    d = res.data
    print(f"  price=30 cost=10 → margin={d['gross_margin_pct']:.2f}% beroas={d['beroas']} target_roas={d['target_roas']}")
    assert abs(d["gross_margin_pct"] - 41.0) < 1.5, f"expected ~41%, got {d['gross_margin_pct']}"
    assert abs(d["beroas"] - 2.44) < 0.2, f"expected ~2.44, got {d['beroas']}"
    assert abs(d["target_roas"] - 2.93) < 0.2, f"expected ~2.93, got {d['target_roas']}"


def test_markup_cap_below_3x():
    # price=$40, cost=$20 → markup=2.0 (below 3x). Even with great margin,
    # profit_score should be capped at 40.
    p = _make_product(40.0, "home")
    res = profit.collect(p, {"source_cost_usd": 20.0})
    print(f"  price=40 cost=20 → markup={res.data['markup_multiplier']} score={res.score}")
    assert res.data["markup_multiplier"] < 3
    assert res.score <= 40, f"expected cap at 40, got {res.score}"


def test_healthy_5x_markup():
    # price=$50, cost=$10 → markup=5x, margin should be very healthy
    p = _make_product(50.0, "home")
    res = profit.collect(p, {"source_cost_usd": 10.0})
    print(f"  price=50 cost=10 → margin={res.data['gross_margin_pct']:.1f}% markup={res.data['markup_multiplier']} score={res.score}")
    assert res.data["markup_multiplier"] >= 4.5
    assert res.score >= 85


if __name__ == "__main__":
    setup_module(None)
    tests = [
        test_marcus_rule_triggers_when_margin_below_15,
        test_known_math_from_v2_prompt,
        test_markup_cap_below_3x,
        test_healthy_5x_markup,
    ]
    failed = []
    for t in tests:
        try:
            print(f"\n→ {t.__name__}")
            t()
            print(f"  ✅ pass")
        except AssertionError as e:
            print(f"  ❌ FAIL: {e}")
            failed.append(t.__name__)
        except Exception as e:
            print(f"  ❌ ERROR: {type(e).__name__}: {e}")
            failed.append(t.__name__)
    print()
    if failed:
        print(f"FAILED: {failed}")
        sys.exit(1)
    print("All profit math tests passed ✅")
