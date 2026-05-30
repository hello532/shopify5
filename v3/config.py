"""Configuration loader. Single source of truth for all thresholds."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as e:
    raise SystemExit("PyYAML required. pip install pyyaml") from e

_CACHE: dict[str, Any] | None = None
ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent


def load(path: str | os.PathLike | None = None) -> dict[str, Any]:
    """Load config.yaml. Re-reads on each call only if path differs."""
    global _CACHE
    if _CACHE is not None and path is None:
        return _CACHE
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    _CACHE = cfg
    return cfg


def get(key_path: str, default: Any = None) -> Any:
    """Dotted-path getter: get('scoring.weights.fb', 0.35)."""
    cur: Any = load()
    for part in key_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def resolve_path(key_path: str) -> Path:
    """Resolve a config path key to an absolute Path under project root."""
    raw = get(key_path)
    if raw is None:
        raise KeyError(f"config key missing: {key_path}")
    p = Path(raw)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    return p
