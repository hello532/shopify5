"""SQLite database layer. Pure sqlite3, no ORM."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT UNIQUE NOT NULL,
    source TEXT,
    added_at TEXT NOT NULL,
    last_scanned_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_domain TEXT,
    handle TEXT,
    title TEXT,
    price_usd REAL,
    image_url TEXT,
    product_url TEXT,
    landing_url TEXT,
    category TEXT,
    keyword_id INTEGER,
    advertiser_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    shopify_competitor_count INTEGER,
    awareness_level_detected TEXT,
    raw_json TEXT,
    UNIQUE(shop_domain, handle),
    FOREIGN KEY (keyword_id) REFERENCES keywords(id)
);
CREATE INDEX IF NOT EXISTS idx_products_keyword ON products(keyword_id);
CREATE INDEX IF NOT EXISTS idx_products_last_seen ON products(last_seen_at);

CREATE TABLE IF NOT EXISTS ad_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    days_active INTEGER,
    impressions_total INTEGER,
    distinct_entity_ids INTEGER,
    creative_count_raw INTEGER,
    countries_running INTEGER,
    advertiser_id TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    homogeneity_flag TEXT,
    raw_json TEXT,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_ad_signals_product ON ad_signals(product_id);

CREATE TABLE IF NOT EXISTS trend_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    keyword TEXT,
    score_7d REAL,
    score_30d REAL,
    score_90d REAL,
    yoy_growth REAL,
    slope_90d REAL,
    seasonality_phase TEXT,
    related_queries_json TEXT,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_trend_signals_product ON trend_signals(product_id);

CREATE TABLE IF NOT EXISTS lp_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    has_shopify INTEGER,
    has_klaviyo INTEGER,
    has_reviews_app INTEGER,
    has_pixel INTEGER,
    has_capi INTEGER,
    payment_methods_count INTEGER,
    has_video_hero INTEGER,
    has_comparison_chart INTEGER,
    has_ugc_block INTEGER,
    awareness_signals_json TEXT,
    awareness_match_layer TEXT,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_lp_signals_product ON lp_signals(product_id);

CREATE TABLE IF NOT EXISTS profit_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    selling_price_usd REAL,
    source_cost_usd REAL,
    cost_method TEXT,
    payment_fee_pct REAL,
    refund_rate_pct REAL,
    shipping_cost_usd REAL,
    gross_margin_pct REAL,
    markup_multiplier REAL,
    beroas REAL,
    target_roas REAL,
    price_band TEXT,
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_profit_signals_product ON profit_signals(product_id);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    fb_score REAL,
    profit_score REAL,
    trend_score REAL,
    lp_score REAL,
    composite_score REAL,
    weights_json TEXT,
    scored_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_scores_product ON scores(product_id);
CREATE INDEX IF NOT EXISTS idx_scores_scored_at ON scores(scored_at);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    decision TEXT NOT NULL,        -- GO_TEST | WATCH | KILL
    kill_reason TEXT,
    watch_reason TEXT,
    watch_recheck_at TEXT,
    test_plan_json TEXT,
    composite_score REAL,
    decided_at TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_product ON decisions(product_id);
CREATE INDEX IF NOT EXISTS idx_decisions_decision ON decisions(decision);
CREATE INDEX IF NOT EXISTS idx_decisions_decided_at ON decisions(decided_at);

CREATE TABLE IF NOT EXISTS explain_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    scored_at TEXT NOT NULL,
    signal TEXT NOT NULL,           -- fb_ads | profit | trends | lp | composite | decision
    rule TEXT NOT NULL,             -- e.g. "min_days_active"
    threshold TEXT,
    observed TEXT,
    passed INTEGER NOT NULL,        -- 0 or 1
    note TEXT,
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_explain_product ON explain_log(product_id);
CREATE INDEX IF NOT EXISTS idx_explain_scored_at ON explain_log(scored_at);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    keyword_count INTEGER,
    products_found INTEGER,
    products_scored INTEGER,
    go_test_count INTEGER,
    watch_count INTEGER,
    kill_count INTEGER,
    error_log TEXT
);

-- ============================================================
-- AI-loop tables (PR6-PR9: performance feedback, creative, learn, radar)
-- ============================================================

CREATE TABLE IF NOT EXISTS ad_performance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER,
    product_id INTEGER NOT NULL,
    campaign_id TEXT,
    ad_set_id TEXT,
    ad_id TEXT,
    creative_id TEXT,
    day_index INTEGER,
    date_iso TEXT,
    spend_usd REAL,
    impressions INTEGER,
    clicks INTEGER,
    add_to_cart INTEGER,
    purchases INTEGER,
    revenue_usd REAL,
    roas REAL,
    cpa REAL,
    ctr REAL,
    cpc REAL,
    cpm REAL,
    captured_at TEXT NOT NULL,
    raw_json TEXT,
    FOREIGN KEY (decision_id) REFERENCES decisions(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_perf_product ON ad_performance(product_id);
CREATE INDEX IF NOT EXISTS idx_perf_ad ON ad_performance(ad_id);
CREATE INDEX IF NOT EXISTS idx_perf_date ON ad_performance(date_iso);

CREATE TABLE IF NOT EXISTS auto_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER,
    product_id INTEGER NOT NULL,
    ad_id TEXT,
    action TEXT NOT NULL,           -- KILL | SCALE | HOLD | INCREASE_BUDGET | PAUSE
    reason TEXT,
    metric_observed TEXT,
    metric_threshold TEXT,
    suggested_value REAL,
    triggered_at TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT,
    FOREIGN KEY (decision_id) REFERENCES decisions(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_actions_product ON auto_actions(product_id);
CREATE INDEX IF NOT EXISTS idx_actions_applied ON auto_actions(applied);

CREATE TABLE IF NOT EXISTS creative_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    variant_type TEXT,              -- hook_problem | hook_solution | hook_product | ugc | demo
    hook TEXT,
    body TEXT,
    cta TEXT,
    visual_brief TEXT,
    entity_signature TEXT,          -- visual hash for Andromeda Entity ID diversity
    generated_at TEXT NOT NULL,
    generator TEXT,                  -- claude | gemini | manual
    used_in_ad_id TEXT,
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_variants_product ON creative_variants(product_id);

CREATE TABLE IF NOT EXISTS winning_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_key TEXT UNIQUE NOT NULL,
    dimensions TEXT,                -- JSON: {price_band, category, hook_type, awareness_layer}
    win_count INTEGER DEFAULT 0,
    total_count INTEGER DEFAULT 0,
    win_rate REAL,
    avg_roas REAL,
    avg_cpa REAL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patterns_winrate ON winning_patterns(win_rate);

CREATE TABLE IF NOT EXISTS competitor_watch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_domain TEXT UNIQUE NOT NULL,
    tags TEXT,
    added_at TEXT NOT NULL,
    last_checked_at TEXT,
    products_seen_count INTEGER DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS competitor_new_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL,
    product_handle TEXT NOT NULL,
    title TEXT,
    price_usd REAL,
    detected_at TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    product_id INTEGER,
    FOREIGN KEY (competitor_id) REFERENCES competitor_watch(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_new_prods_processed ON competitor_new_products(processed);

CREATE TABLE IF NOT EXISTS threshold_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    threshold_key TEXT NOT NULL,
    old_value REAL,
    new_value REAL,
    backtested_win_rate REAL,
    sample_size INTEGER,
    applied INTEGER NOT NULL DEFAULT 0,
    suggested_at TEXT NOT NULL,
    applied_at TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS attribution_survey (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER,
    order_id TEXT,
    customer_email TEXT,
    reported_source TEXT,           -- self-reported channel
    matched_pixel_source TEXT,
    revenue_usd REAL,
    submitted_at TEXT NOT NULL,
    raw_json TEXT,
    FOREIGN KEY (product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_survey_product ON attribution_survey(product_id);

-- PR10: Shopify product clones — one row per push to user's own shop
CREATE TABLE IF NOT EXISTS shopify_clones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_product_id INTEGER NOT NULL,        -- v3.products.id
    shop_destination TEXT NOT NULL,             -- e.g. mystore.myshopify.com
    shopify_product_gid TEXT,                   -- e.g. gid://shopify/Product/123
    shopify_product_handle TEXT,
    shopify_admin_url TEXT,                     -- direct link to admin product page
    shopify_storefront_url TEXT,
    status TEXT NOT NULL,                       -- DRAFT | ACTIVE | FAILED
    pushed_at TEXT NOT NULL,
    error TEXT,
    payload_json TEXT,                          -- what we sent
    response_json TEXT,                         -- what shopify returned
    FOREIGN KEY (source_product_id) REFERENCES products(id)
);
CREATE INDEX IF NOT EXISTS idx_clones_source ON shopify_clones(source_product_id);
CREATE INDEX IF NOT EXISTS idx_clones_dest ON shopify_clones(shop_destination);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clones_unique ON shopify_clones(source_product_id, shop_destination);
"""


def db_path() -> Path:
    return config.resolve_path("paths.db")


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> Path:
    """Create all tables. Idempotent."""
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
    return p


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ---------- keyword helpers ----------

def upsert_keyword(c: sqlite3.Connection, keyword: str, source: str = "manual") -> int:
    row = c.execute("SELECT id FROM keywords WHERE keyword = ?", (keyword,)).fetchone()
    if row:
        return row["id"]
    cur = c.execute(
        "INSERT INTO keywords(keyword, source, added_at, status) VALUES(?,?,?,?)",
        (keyword, source, now_iso(), "pending"),
    )
    return cur.lastrowid


def mark_keyword_scanned(c: sqlite3.Connection, keyword_id: int) -> None:
    c.execute(
        "UPDATE keywords SET last_scanned_at = ?, status = 'scanned' WHERE id = ?",
        (now_iso(), keyword_id),
    )


# ---------- product helpers ----------

def upsert_product(c: sqlite3.Connection, p: dict[str, Any]) -> int:
    """Upsert product by (shop_domain, handle). Returns product id."""
    now = now_iso()
    row = c.execute(
        "SELECT id FROM products WHERE shop_domain = ? AND handle = ?",
        (p.get("shop_domain"), p.get("handle")),
    ).fetchone()
    if row:
        c.execute(
            """UPDATE products SET
                 title = COALESCE(?, title),
                 price_usd = COALESCE(?, price_usd),
                 image_url = COALESCE(?, image_url),
                 product_url = COALESCE(?, product_url),
                 landing_url = COALESCE(?, landing_url),
                 category = COALESCE(?, category),
                 advertiser_id = COALESCE(?, advertiser_id),
                 keyword_id = COALESCE(?, keyword_id),
                 last_seen_at = ?,
                 raw_json = COALESCE(?, raw_json)
               WHERE id = ?""",
            (
                p.get("title"),
                p.get("price_usd"),
                p.get("image_url"),
                p.get("product_url"),
                p.get("landing_url"),
                p.get("category"),
                p.get("advertiser_id"),
                p.get("keyword_id"),
                now,
                json.dumps(p.get("raw"), ensure_ascii=False) if p.get("raw") else None,
                row["id"],
            ),
        )
        return row["id"]
    cur = c.execute(
        """INSERT INTO products
             (shop_domain, handle, title, price_usd, image_url, product_url, landing_url,
              category, keyword_id, advertiser_id, first_seen_at, last_seen_at, raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            p.get("shop_domain"),
            p.get("handle"),
            p.get("title"),
            p.get("price_usd"),
            p.get("image_url"),
            p.get("product_url"),
            p.get("landing_url"),
            p.get("category"),
            p.get("keyword_id"),
            p.get("advertiser_id"),
            now,
            now,
            json.dumps(p.get("raw"), ensure_ascii=False) if p.get("raw") else None,
        ),
    )
    return cur.lastrowid


# ---------- explain helpers ----------

def log_explain(
    c: sqlite3.Connection,
    product_id: int,
    scored_at: str,
    signal: str,
    rule: str,
    threshold: Any,
    observed: Any,
    passed: bool,
    note: str | None = None,
) -> None:
    c.execute(
        """INSERT INTO explain_log
             (product_id, scored_at, signal, rule, threshold, observed, passed, note)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            product_id,
            scored_at,
            signal,
            rule,
            None if threshold is None else str(threshold),
            None if observed is None else str(observed),
            1 if passed else 0,
            note,
        ),
    )
