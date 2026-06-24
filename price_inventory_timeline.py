#!/usr/bin/env python3
"""price_inventory_timeline.py — 价格+库存+素材时间线追踪引擎 v1.0

解决问题：判断质量 67/100 (C级) - 缺少价格历史、库存监控、素材变化追踪

核心功能：
1. 每日快照价格变化（识别促销/涨价）
2. 库存状态监控（sold out = 机会窗口）
3. 广告素材变化追踪（创意疲劳信号）
4. 价格/库存/素材三维时间线可视化
"""

import json
import sqlite3
import hashlib
import re
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta
import sys

sys.path.insert(0, str(Path(__file__).parent))
import shopify_ultimate as su

# ═══════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════

TIMELINE_DB = Path(__file__).parent / "data" / "timeline.db"
TIMELINE_DB.parent.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════
#  数据库层
# ═══════════════════════════════════════════════════════════

def _init_timeline_db():
    """初始化时间线数据库"""
    conn = sqlite3.connect(str(TIMELINE_DB))
    
    # 价格历史表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            product_handle TEXT NOT NULL,
            product_title TEXT,
            price REAL NOT NULL,
            compare_at_price REAL,
            on_sale INTEGER DEFAULT 0,
            discount_percent REAL DEFAULT 0,
            snapshot_date TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            UNIQUE(domain, product_handle, snapshot_date)
        )
    """)
    
    # 库存历史表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inventory_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            product_handle TEXT NOT NULL,
            product_title TEXT,
            available INTEGER DEFAULT 1,
            total_variants INTEGER DEFAULT 1,
            available_variants INTEGER DEFAULT 1,
            snapshot_date TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            UNIQUE(domain, product_handle, snapshot_date)
        )
    """)
    
    # 广告素材历史表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS creative_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            product_handle TEXT,
            ad_id TEXT NOT NULL,
            creative_hash TEXT NOT NULL,
            ad_text TEXT,
            media_url TEXT,
            media_type TEXT,
            snapshot_date TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            days_since_change INTEGER DEFAULT 0,
            UNIQUE(domain, ad_id, snapshot_date)
        )
    """)
    
    # 事件表（价格暴跌、断货、补货、换素材）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timeline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            product_handle TEXT,
            event_type TEXT NOT NULL,
            event_data TEXT,
            event_date TEXT NOT NULL,
            event_time TEXT NOT NULL,
            severity TEXT DEFAULT 'info'
        )
    """)
    
    conn.execute("CREATE INDEX IF NOT EXISTS idx_price_domain_handle ON price_history(domain, product_handle)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_domain_handle ON inventory_history(domain, product_handle)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_creative_domain ON creative_history(domain)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_domain ON timeline_events(domain, event_date DESC)")
    
    conn.commit()
    conn.close()

_init_timeline_db()


def _timeline_now() -> tuple[str, str]:
    """返回 (日期, 时间)"""
    now = datetime.now()
    return now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")


def _normalize_domain(domain: str) -> str:
    return domain.replace("www.", "").strip().lower()


# ═══════════════════════════════════════════════════════════
#  价格快照
# ═══════════════════════════════════════════════════════════

def snapshot_price(domain: str, product: dict) -> dict:
    """
    记录单个产品价格快照
    
    Args:
        domain: 域名
        product: 产品数据 {"handle": str, "title": str, "price": float, ...}
        
    Returns:
        {"ok": bool, "change": {...}}
    """
    domain = _normalize_domain(domain)
    handle = str(product.get("handle") or "")
    if not handle:
        return {"ok": False, "error": "missing_handle"}
    
    title = str(product.get("title") or "")
    price = float(product.get("price") or 0)
    compare_at = float(product.get("compare_at_price") or 0)
    
    if price <= 0:
        return {"ok": False, "error": "invalid_price"}
    
    on_sale = 1 if compare_at > price else 0
    discount = round((compare_at - price) / compare_at * 100, 1) if compare_at > price else 0
    
    date_str, time_str = _timeline_now()
    
    conn = sqlite3.connect(str(TIMELINE_DB))
    
    # 读取昨天的价格
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    cursor = conn.execute("""
        SELECT price, on_sale, discount_percent
        FROM price_history
        WHERE domain = ? AND product_handle = ? AND snapshot_date = ?
    """, (domain, handle, yesterday))
    
    last_row = cursor.fetchone()
    old_price = last_row[0] if last_row else None
    
    # 插入今天的快照
    try:
        conn.execute("""
            INSERT OR REPLACE INTO price_history
            (domain, product_handle, product_title, price, compare_at_price,
             on_sale, discount_percent, snapshot_date, snapshot_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (domain, handle, title, price, compare_at, on_sale, discount, date_str, time_str))
        conn.commit()
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}
    
    # 检测价格变化事件
    change = None
    if old_price and abs(price - old_price) > 0.01:
        change_percent = round((price - old_price) / old_price * 100, 1)
        
        # 价格暴跌 > 20% = 重要信号
        if change_percent < -20:
            _record_event(conn, domain, handle, "price_drop", {
                "old_price": old_price,
                "new_price": price,
                "change_percent": change_percent
            }, date_str, time_str, severity="high")
        
        # 涨价 > 10%
        elif change_percent > 10:
            _record_event(conn, domain, handle, "price_increase", {
                "old_price": old_price,
                "new_price": price,
                "change_percent": change_percent
            }, date_str, time_str, severity="medium")
        
        change = {
            "old_price": old_price,
            "new_price": price,
            "change_percent": change_percent,
            "change_type": "drop" if change_percent < 0 else "increase"
        }
    
    conn.close()
    
    return {
        "ok": True,
        "domain": domain,
        "handle": handle,
        "price": price,
        "on_sale": bool(on_sale),
        "discount": discount,
        "change": change
    }


def _record_event(conn: sqlite3.Connection, domain: str, handle: str, event_type: str, 
                  data: dict, date_str: str, time_str: str, severity: str = "info"):
    """记录时间线事件"""
    try:
        conn.execute("""
            INSERT INTO timeline_events
            (domain, product_handle, event_type, event_data, event_date, event_time, severity)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (domain, handle, event_type, json.dumps(data, ensure_ascii=False), 
              date_str, time_str, severity))
        conn.commit()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  库存快照
# ═══════════════════════════════════════════════════════════

def snapshot_inventory(domain: str, product: dict) -> dict:
    """
    记录单个产品库存快照
    
    Args:
        domain: 域名
        product: 产品数据 {"handle": str, "available": bool, "variants": [...]}
        
    Returns:
        {"ok": bool, "change": {...}}
    """
    domain = _normalize_domain(domain)
    handle = str(product.get("handle") or "")
    if not handle:
        return {"ok": False, "error": "missing_handle"}
    
    title = str(product.get("title") or "")
    available = 1 if product.get("available") else 0
    
    variants = product.get("variants", []) if isinstance(product.get("variants"), list) else []
    total_variants = len(variants)
    available_variants = len([v for v in variants if v.get("available")])
    
    date_str, time_str = _timeline_now()
    
    conn = sqlite3.connect(str(TIMELINE_DB))
    
    # 读取昨天的库存状态
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    cursor = conn.execute("""
        SELECT available, available_variants
        FROM inventory_history
        WHERE domain = ? AND product_handle = ? AND snapshot_date = ?
    """, (domain, handle, yesterday))
    
    last_row = cursor.fetchone()
    old_available = last_row[0] if last_row else None
    old_available_variants = last_row[1] if last_row else None
    
    # 插入今天的快照
    try:
        conn.execute("""
            INSERT OR REPLACE INTO inventory_history
            (domain, product_handle, product_title, available, total_variants,
             available_variants, snapshot_date, snapshot_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (domain, handle, title, available, total_variants, available_variants,
              date_str, time_str))
        conn.commit()
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}
    
    # 检测库存变化事件
    change = None
    
    # 断货事件（昨天有货 → 今天无货）
    if old_available == 1 and available == 0:
        _record_event(conn, domain, handle, "sold_out", {
            "message": "产品断货，可能是爆款信号或补货机会"
        }, date_str, time_str, severity="high")
        change = {"type": "sold_out", "message": "产品断货"}
    
    # 补货事件（昨天无货 → 今天有货）
    elif old_available == 0 and available == 1:
        _record_event(conn, domain, handle, "restocked", {
            "message": "产品补货，卖家继续推广"
        }, date_str, time_str, severity="medium")
        change = {"type": "restocked", "message": "产品补货"}
    
    # 变体缺货（部分变体售罄）
    elif old_available_variants and available_variants < old_available_variants:
        _record_event(conn, domain, handle, "variants_depleting", {
            "old_count": old_available_variants,
            "new_count": available_variants,
            "message": f"{old_available_variants - available_variants} 个变体售罄"
        }, date_str, time_str, severity="medium")
        change = {
            "type": "variants_depleting",
            "old_count": old_available_variants,
            "new_count": available_variants
        }
    
    conn.close()
    
    return {
        "ok": True,
        "domain": domain,
        "handle": handle,
        "available": bool(available),
        "available_variants": available_variants,
        "total_variants": total_variants,
        "change": change
    }


# ═══════════════════════════════════════════════════════════
#  广告素材快照
# ═══════════════════════════════════════════════════════════

def snapshot_creative(domain: str, ad: dict) -> dict:
    """
    记录单个广告素材快照
    
    Args:
        domain: 域名
        ad: 广告数据 {"ad_id": str, "text": str, "media": str, ...}
        
    Returns:
        {"ok": bool, "change": {...}}
    """
    domain = _normalize_domain(domain)
    ad_id = str(ad.get("ad_id") or ad.get("id") or "")
    if not ad_id:
        return {"ok": False, "error": "missing_ad_id"}
    
    handle = str(ad.get("product_handle") or "")
    ad_text = str(ad.get("text") or ad.get("ad_creative_body") or "")[:500]
    media_url = str(ad.get("media") or ad.get("ad_creative_link_url") or "")
    media_type = "video" if "video" in media_url.lower() else "image"
    
    # 计算素材哈希（文案 + 媒体 URL）
    creative_hash = hashlib.md5(f"{ad_text}|{media_url}".encode()).hexdigest()[:16]
    
    date_str, time_str = _timeline_now()
    
    conn = sqlite3.connect(str(TIMELINE_DB))
    
    # 读取最近一次的素材哈希
    cursor = conn.execute("""
        SELECT creative_hash, snapshot_date
        FROM creative_history
        WHERE domain = ? AND ad_id = ?
        ORDER BY snapshot_date DESC
        LIMIT 1
    """, (domain, ad_id))
    
    last_row = cursor.fetchone()
    old_hash = last_row[0] if last_row else None
    old_date = last_row[1] if last_row else None
    
    # 计算距离上次更换素材的天数
    days_since_change = 0
    if old_date:
        try:
            old_dt = datetime.strptime(old_date, "%Y-%m-%d")
            days_since_change = (datetime.now() - old_dt).days
        except Exception:
            pass
    
    # 插入今天的快照
    try:
        conn.execute("""
            INSERT OR REPLACE INTO creative_history
            (domain, product_handle, ad_id, creative_hash, ad_text, media_url,
             media_type, snapshot_date, snapshot_time, days_since_change)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (domain, handle, ad_id, creative_hash, ad_text, media_url,
              media_type, date_str, time_str, days_since_change))
        conn.commit()
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}
    
    # 检测素材变化事件
    change = None
    if old_hash and old_hash != creative_hash:
        _record_event(conn, domain, handle, "creative_changed", {
            "ad_id": ad_id,
            "days_since_last_change": days_since_change,
            "message": f"广告素材更换（距离上次 {days_since_change} 天）"
        }, date_str, time_str, severity="medium")
        change = {
            "type": "creative_changed",
            "days_since_last": days_since_change
        }
    
    # 创意疲劳预警（同一素材跑超过 30 天）
    elif days_since_change > 30:
        _record_event(conn, domain, handle, "creative_fatigue", {
            "ad_id": ad_id,
            "days_active": days_since_change,
            "message": f"同一素材已跑 {days_since_change} 天，可能创意疲劳"
        }, date_str, time_str, severity="low")
    
    conn.close()
    
    return {
        "ok": True,
        "domain": domain,
        "ad_id": ad_id,
        "creative_hash": creative_hash,
        "days_since_change": days_since_change,
        "change": change
    }


# ═══════════════════════════════════════════════════════════
#  批量快照
# ═══════════════════════════════════════════════════════════

def batch_snapshot_domain(domain: str) -> dict:
    """
    对一个域名的所有产品做价格+库存快照
    
    Args:
        domain: 域名
        
    Returns:
        {"ok": bool, "stats": {...}}
    """
    domain = _normalize_domain(domain)
    
    # 抓取最新产品数据
    try:
        session = su.get_session()
        products = su.fetch_all_products(domain, session, best_effort=True)
    except Exception as e:
        return {"ok": False, "error": f"fetch_failed: {e}"}
    
    if not products:
        return {"ok": False, "error": "no_products"}
    
    price_changes = 0
    inventory_changes = 0
    
    for product in products:
        parsed = su.parse_product(product)
        
        # 价格快照
        price_result = snapshot_price(domain, parsed)
        if price_result.get("ok") and price_result.get("change"):
            price_changes += 1
        
        # 库存快照
        inv_result = snapshot_inventory(domain, parsed)
        if inv_result.get("ok") and inv_result.get("change"):
            inventory_changes += 1
    
    return {
        "ok": True,
        "domain": domain,
        "products_tracked": len(products),
        "price_changes": price_changes,
        "inventory_changes": inventory_changes
    }


# ═══════════════════════════════════════════════════════════
#  时间线查询
# ═══════════════════════════════════════════════════════════

def get_price_timeline(domain: str, handle: str, days: int = 30) -> dict:
    """查询产品价格时间线"""
    domain = _normalize_domain(domain)
    since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(str(TIMELINE_DB))
    cursor = conn.execute("""
        SELECT snapshot_date, price, on_sale, discount_percent
        FROM price_history
        WHERE domain = ? AND product_handle = ? AND snapshot_date >= ?
        ORDER BY snapshot_date ASC
    """, (domain, handle, since_date))
    
    timeline = [
        {
            "date": row[0],
            "price": row[1],
            "on_sale": bool(row[2]),
            "discount": row[3]
        }
        for row in cursor.fetchall()
    ]
    
    conn.close()
    
    if not timeline:
        return {"ok": False, "error": "no_data"}
    
    prices = [t["price"] for t in timeline]
    min_price = min(prices)
    max_price = max(prices)
    current_price = timeline[-1]["price"]
    
    return {
        "ok": True,
        "domain": domain,
        "handle": handle,
        "timeline": timeline,
        "stats": {
            "days_tracked": len(timeline),
            "current_price": current_price,
            "min_price": min_price,
            "max_price": max_price,
            "price_volatility": round((max_price - min_price) / min_price * 100, 1)
        }
    }


def get_events(domain: str, days: int = 7, severity: str = None) -> dict:
    """查询时间线事件"""
    domain = _normalize_domain(domain)
    since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(str(TIMELINE_DB))
    
    query = """
        SELECT event_type, event_data, event_date, event_time, severity, product_handle
        FROM timeline_events
        WHERE domain = ? AND event_date >= ?
    """
    params = [domain, since_date]
    
    if severity:
        query += " AND severity = ?"
        params.append(severity)
    
    query += " ORDER BY event_date DESC, event_time DESC LIMIT 100"
    
    cursor = conn.execute(query, params)
    
    events = []
    for row in cursor.fetchall():
        try:
            data = json.loads(row[1]) if row[1] else {}
        except Exception:
            data = {}
        
        events.append({
            "type": row[0],
            "data": data,
            "date": row[2],
            "time": row[3],
            "severity": row[4],
            "handle": row[5]
        })
    
    conn.close()
    
    return {
        "ok": True,
        "domain": domain,
        "events": events,
        "total": len(events),
        "high_severity": len([e for e in events if e["severity"] == "high"])
    }


if __name__ == "__main__":
    # 测试
    print("🧪 测试价格+库存时间线...")
    
    # 模拟产品数据
    test_product = {
        "handle": "test-product",
        "title": "Test Product",
        "price": 29.99,
        "compare_at_price": 39.99,
        "available": True,
        "variants": [
            {"available": True},
            {"available": True}
        ]
    }
    
    price_result = snapshot_price("test.myshopify.com", test_product)
    print(json.dumps(price_result, indent=2))
    
    inv_result = snapshot_inventory("test.myshopify.com", test_product)
    print(json.dumps(inv_result, indent=2))
