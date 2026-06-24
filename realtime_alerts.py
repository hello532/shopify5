#!/usr/bin/env python3
"""realtime_alerts.py — 实时告警系统 v1.0

解决问题：缺少实时告警（新品上架、价格暴跌、断货、广告量暴涨）

告警渠道：
1. Telegram 推送（高优先级）
2. 本地日志（所有告警）
3. Web Hook（可选）

告警类型：
- new_product: 新品上架 24h 内通知
- price_drop: 价格暴跌 > 20% 通知
- sold_out: 断货通知
- restocked: 补货通知
- ad_surge: 广告量暴涨 > 50% 通知
- creative_change: 素材更换通知
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional
from datetime import datetime
import requests

# ═══════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════

ALERTS_DB = Path(__file__).parent / "data" / "alerts.db"
ALERTS_DB.parent.mkdir(parents=True, exist_ok=True)

ALERTS_LOG = Path(__file__).parent / "data" / "alerts.log"

# Telegram 配置（从环境变量读取）
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 告警阈值
PRICE_DROP_THRESHOLD = 20  # 价格暴跌 > 20%
AD_SURGE_THRESHOLD = 50    # 广告量暴涨 > 50%

# ═══════════════════════════════════════════════════════════
#  数据库层
# ═══════════════════════════════════════════════════════════

def _init_alerts_db():
    """初始化告警数据库"""
    conn = sqlite3.connect(str(ALERTS_DB))
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            domain TEXT NOT NULL,
            product_handle TEXT,
            alert_data TEXT,
            severity TEXT DEFAULT 'medium',
            created_at TEXT NOT NULL,
            sent_telegram INTEGER DEFAULT 0,
            sent_email INTEGER DEFAULT 0,
            read INTEGER DEFAULT 0
        )
    """)
    
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_domain ON alerts(domain, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_read ON alerts(read, created_at DESC)")
    
    conn.commit()
    conn.close()

_init_alerts_db()


def _alerts_now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def _normalize_domain(domain: str) -> str:
    return domain.replace("www.", "").strip().lower()


# ═══════════════════════════════════════════════════════════
#  告警创建
# ═══════════════════════════════════════════════════════════

def create_alert(
    alert_type: str,
    domain: str,
    product_handle: str = "",
    data: dict = None,
    severity: str = "medium",
    send_telegram: bool = True
) -> dict:
    """
    创建告警
    
    Args:
        alert_type: 告警类型 (new_product, price_drop, sold_out, ad_surge)
        domain: 域名
        product_handle: 产品 handle
        data: 告警数据
        severity: 严重程度 (low, medium, high, critical)
        send_telegram: 是否推送 Telegram
        
    Returns:
        {"ok": bool, "alert_id": int}
    """
    domain = _normalize_domain(domain)
    data = data or {}
    
    conn = sqlite3.connect(str(ALERTS_DB))
    
    try:
        cursor = conn.execute("""
            INSERT INTO alerts
            (alert_type, domain, product_handle, alert_data, severity, created_at,
             sent_telegram, sent_email, read)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)
        """, (
            alert_type, domain, product_handle,
            json.dumps(data, ensure_ascii=False),
            severity, _alerts_now()
        ))
        
        alert_id = cursor.lastrowid
        conn.commit()
        
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}
    
    conn.close()
    
    # 写入日志
    _log_alert(alert_type, domain, product_handle, data, severity)
    
    # 推送 Telegram
    telegram_sent = False
    if send_telegram and severity in ["high", "critical"]:
        telegram_result = _send_telegram_alert(alert_id, alert_type, domain, product_handle, data, severity)
        telegram_sent = telegram_result.get("ok", False)
        
        if telegram_sent:
            conn = sqlite3.connect(str(ALERTS_DB))
            conn.execute("UPDATE alerts SET sent_telegram = 1 WHERE id = ?", (alert_id,))
            conn.commit()
            conn.close()
    
    return {
        "ok": True,
        "alert_id": alert_id,
        "type": alert_type,
        "severity": severity,
        "telegram_sent": telegram_sent
    }


def _log_alert(alert_type: str, domain: str, handle: str, data: dict, severity: str):
    """写入本地日志"""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = json.dumps({
            "timestamp": timestamp,
            "type": alert_type,
            "domain": domain,
            "handle": handle,
            "data": data,
            "severity": severity
        }, ensure_ascii=False)
        
        with open(ALERTS_LOG, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  Telegram 推送
# ═══════════════════════════════════════════════════════════

def _send_telegram_alert(alert_id: int, alert_type: str, domain: str, 
                         handle: str, data: dict, severity: str) -> dict:
    """
    推送 Telegram 告警
    
    Returns:
        {"ok": bool, "message_id": int}
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"ok": False, "error": "telegram_not_configured"}
    
    # 生成消息文本
    emoji_map = {
        "new_product": "🆕",
        "price_drop": "💰",
        "sold_out": "⚠️",
        "restocked": "✅",
        "ad_surge": "📈",
        "creative_change": "🎨"
    }
    
    severity_emoji = {
        "low": "ℹ️",
        "medium": "⚠️",
        "high": "🚨",
        "critical": "🔥"
    }
    
    emoji = emoji_map.get(alert_type, "📢")
    sev_emoji = severity_emoji.get(severity, "⚠️")
    
    # 构建消息
    lines = [
        f"{emoji} *{_alert_type_label(alert_type)}* {sev_emoji}",
        f"",
        f"🏪 域名: `{domain}`"
    ]
    
    if handle:
        lines.append(f"📦 产品: `{handle}`")
    
    # 添加具体数据
    if alert_type == "price_drop":
        old_price = data.get("old_price", 0)
        new_price = data.get("new_price", 0)
        change = data.get("change_percent", 0)
        lines.append(f"")
        lines.append(f"💸 价格: ${old_price:.2f} → ${new_price:.2f}")
        lines.append(f"📉 跌幅: {abs(change):.1f}%")
    
    elif alert_type == "ad_surge":
        old_count = data.get("old_count", 0)
        new_count = data.get("new_count", 0)
        surge = data.get("surge_percent", 0)
        lines.append(f"")
        lines.append(f"📊 广告数: {old_count} → {new_count}")
        lines.append(f"📈 增幅: +{surge:.0f}%")
    
    elif alert_type == "new_product":
        title = data.get("title", "")
        price = data.get("price", 0)
        if title:
            lines.append(f"📝 标题: {title[:60]}")
        if price:
            lines.append(f"💵 价格: ${price:.2f}")
    
    lines.append(f"")
    lines.append(f"🕐 时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    
    message = "\n".join(lines)
    
    # 发送
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }, timeout=10)
        
        if resp.status_code == 200:
            result = resp.json()
            if result.get("ok"):
                return {"ok": True, "message_id": result.get("result", {}).get("message_id")}
        
        return {"ok": False, "error": f"telegram_api_error: {resp.status_code}"}
    
    except Exception as e:
        return {"ok": False, "error": f"telegram_request_failed: {e}"}


def _alert_type_label(alert_type: str) -> str:
    """告警类型中文标签"""
    labels = {
        "new_product": "新品上架",
        "price_drop": "价格暴跌",
        "sold_out": "产品断货",
        "restocked": "产品补货",
        "ad_surge": "广告量暴涨",
        "creative_change": "素材更换"
    }
    return labels.get(alert_type, alert_type)


# ═══════════════════════════════════════════════════════════
#  告警查询
# ═══════════════════════════════════════════════════════════

def get_alerts(
    alert_type: str = None,
    domain: str = None,
    severity: str = None,
    unread_only: bool = False,
    limit: int = 50
) -> dict:
    """
    查询告警列表
    
    Args:
        alert_type: 告警类型过滤
        domain: 域名过滤
        severity: 严重程度过滤
        unread_only: 只返回未读告警
        limit: 最多返回数量
        
    Returns:
        {"ok": bool, "alerts": [...]}
    """
    conn = sqlite3.connect(str(ALERTS_DB))
    
    query = "SELECT id, alert_type, domain, product_handle, alert_data, severity, created_at, read FROM alerts WHERE 1=1"
    params = []
    
    if alert_type:
        query += " AND alert_type = ?"
        params.append(alert_type)
    
    if domain:
        query += " AND domain = ?"
        params.append(_normalize_domain(domain))
    
    if severity:
        query += " AND severity = ?"
        params.append(severity)
    
    if unread_only:
        query += " AND read = 0"
    
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    
    cursor = conn.execute(query, params)
    
    alerts = []
    for row in cursor.fetchall():
        try:
            data = json.loads(row[4]) if row[4] else {}
        except Exception:
            data = {}
        
        alerts.append({
            "id": row[0],
            "type": row[1],
            "domain": row[2],
            "handle": row[3],
            "data": data,
            "severity": row[5],
            "created_at": row[6],
            "read": bool(row[7])
        })
    
    conn.close()
    
    return {
        "ok": True,
        "alerts": alerts,
        "total": len(alerts),
        "unread": len([a for a in alerts if not a["read"]])
    }


def mark_alert_read(alert_id: int) -> dict:
    """标记告警为已读"""
    conn = sqlite3.connect(str(ALERTS_DB))
    
    try:
        conn.execute("UPDATE alerts SET read = 1 WHERE id = ?", (alert_id,))
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as e:
        conn.close()
        return {"ok": False, "error": str(e)}


def get_alert_stats(days: int = 7) -> dict:
    """获取告警统计"""
    since = (datetime.now() - __import__('datetime').timedelta(days=days)).isoformat()
    
    conn = sqlite3.connect(str(ALERTS_DB))
    
    cursor = conn.execute("""
        SELECT alert_type, severity, COUNT(*) as count
        FROM alerts
        WHERE created_at >= ?
        GROUP BY alert_type, severity
    """, (since,))
    
    stats_by_type = {}
    stats_by_severity = {}
    
    for row in cursor.fetchall():
        alert_type = row[0]
        severity = row[1]
        count = row[2]
        
        stats_by_type[alert_type] = stats_by_type.get(alert_type, 0) + count
        stats_by_severity[severity] = stats_by_severity.get(severity, 0) + count
    
    cursor = conn.execute("SELECT COUNT(*) FROM alerts WHERE created_at >= ?", (since,))
    total = cursor.fetchone()[0]
    
    cursor = conn.execute("SELECT COUNT(*) FROM alerts WHERE created_at >= ? AND read = 0", (since,))
    unread = cursor.fetchone()[0]
    
    conn.close()
    
    return {
        "ok": True,
        "days": days,
        "total_alerts": total,
        "unread_alerts": unread,
        "by_type": stats_by_type,
        "by_severity": stats_by_severity
    }


# ═══════════════════════════════════════════════════════════
#  便捷告警方法
# ═══════════════════════════════════════════════════════════

def alert_new_product(domain: str, product: dict) -> dict:
    """新品上架告警"""
    return create_alert(
        alert_type="new_product",
        domain=domain,
        product_handle=product.get("handle", ""),
        data={
            "title": product.get("title", ""),
            "price": product.get("price", 0),
            "created_at": product.get("created_at", "")
        },
        severity="medium",
        send_telegram=True
    )


def alert_price_drop(domain: str, handle: str, old_price: float, new_price: float) -> dict:
    """价格暴跌告警"""
    change_percent = (new_price - old_price) / old_price * 100
    
    return create_alert(
        alert_type="price_drop",
        domain=domain,
        product_handle=handle,
        data={
            "old_price": old_price,
            "new_price": new_price,
            "change_percent": change_percent
        },
        severity="high",
        send_telegram=True
    )


def alert_sold_out(domain: str, handle: str, title: str = "") -> dict:
    """断货告警"""
    return create_alert(
        alert_type="sold_out",
        domain=domain,
        product_handle=handle,
        data={"title": title},
        severity="high",
        send_telegram=True
    )


def alert_ad_surge(domain: str, old_count: int, new_count: int) -> dict:
    """广告量暴涨告警"""
    surge_percent = (new_count - old_count) / max(old_count, 1) * 100
    
    return create_alert(
        alert_type="ad_surge",
        domain=domain,
        data={
            "old_count": old_count,
            "new_count": new_count,
            "surge_percent": surge_percent
        },
        severity="high",
        send_telegram=True
    )


if __name__ == "__main__":
    # 测试
    print("🧪 测试告警系统...")
    
    # 创建测试告警
    result = create_alert(
        alert_type="price_drop",
        domain="test.myshopify.com",
        product_handle="test-product",
        data={
            "old_price": 49.99,
            "new_price": 29.99,
            "change_percent": -40.0
        },
        severity="high",
        send_telegram=False
    )
    
    print(json.dumps(result, indent=2))
    
    # 查询告警
    alerts = get_alerts(limit=10)
    print(json.dumps(alerts, indent=2, ensure_ascii=False))
    
    # 统计
    stats = get_alert_stats(days=7)
    print(json.dumps(stats, indent=2))
