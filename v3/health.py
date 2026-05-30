"""Health check: probe external dependencies. Used by `v3 init`."""
from __future__ import annotations

import socket
import urllib.error
import urllib.request
from typing import Any

from . import config


def _check(label: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"label": label, "ok": ok, "detail": detail}


def check_http(url: str, timeout: int = 5) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "v3-healthcheck"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return e.code in (200, 404), f"HTTP {e.code}"
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        return False, f"unreachable: {type(e).__name__}"


def check_python_pkg(name: str) -> tuple[bool, str]:
    try:
        import importlib
        importlib.import_module(name)
        return True, "ok"
    except ImportError as e:
        return False, str(e)


def run_all() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    # Config
    try:
        config.load()
        checks.append(_check("config.yaml", True, "loaded"))
    except Exception as e:
        checks.append(_check("config.yaml", False, str(e)))
        return checks

    # DB path writable
    try:
        from . import db
        p = db.db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        checks.append(_check(f"db path {p}", True, "writable"))
    except Exception as e:
        checks.append(_check("db path", False, str(e)))

    # FB Ad Library service
    fb_url = config.get("fb_ads.service_url", "http://127.0.0.1:8000")
    ok, detail = check_http(fb_url, timeout=3)
    checks.append(_check(f"fb_ads service {fb_url}", ok, detail))

    # Python deps
    for pkg, label in [
        ("yaml", "PyYAML"),
        ("requests", "requests"),
        ("openpyxl", "openpyxl (Excel)"),
        ("pytrends", "pytrends (optional, real trends)"),
        ("apscheduler", "APScheduler (optional, watch daemon)"),
    ]:
        ok, detail = check_python_pkg(pkg)
        checks.append(_check(label, ok, detail))

    # Shopify snapshots dir
    try:
        snap = config.resolve_path("paths.shopify_snapshots")
        if snap.exists():
            n = len(list(snap.glob("*_snapshot.json")))
            checks.append(_check(f"shopify snapshots {snap}", True, f"{n} files"))
        else:
            checks.append(_check(f"shopify snapshots {snap}", False, "dir missing"))
    except Exception as e:
        checks.append(_check("shopify snapshots", False, str(e)))

    return checks


def format_report(checks: list[dict[str, Any]]) -> str:
    lines = ["v3 health check", "=" * 40]
    for c in checks:
        mark = "✅" if c["ok"] else "❌"
        lines.append(f"{mark}  {c['label']:<35} {c['detail']}")
    failed = [c for c in checks if not c["ok"]]
    lines.append("=" * 40)
    lines.append(f"{len(checks) - len(failed)}/{len(checks)} passed")
    if failed:
        lines.append("missing deps may be installed via: pip install pyyaml requests openpyxl pytrends apscheduler jinja2")
    return "\n".join(lines)
