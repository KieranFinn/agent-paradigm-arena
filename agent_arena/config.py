"""Zero-dependency configuration loader.

Parses a *subset* of YAML sufficient for this project's config files:
- two indentation levels of mappings only (``top:`` then ``  key: value``)
- scalars: int / float / bool / str
- blank lines and ``#`` comments ignored

If PyYAML happens to be installed we do NOT use it on purpose: the benchmark
must run in a bare stdlib environment (see docs/design.md, non-functional
constraints).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict

_SCALAR_RE = re.compile(r"^(true|false|null|-?\d+(?:\.\d+)?|'.*'|\".*\"|.+)$")


def _coerce(raw: str) -> Any:
    raw = raw.strip()
    if raw in ("true", "True"):
        return True
    if raw in ("false", "False"):
        return False
    if raw in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def load(path: str | Path) -> Dict[str, Dict[str, Any]]:
    """Load a two-level YAML-subset mapping into a nested dict."""
    cfg: Dict[str, Dict[str, Any]] = {}
    current: Dict[str, Any] | None = None
    text = Path(path).read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.split("#", 1)[0].rstrip() if not _in_quotes_hash(line) else line.rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()
        if indent == 0:
            if not content.endswith(":"):
                raise ValueError(f"{path}:{lineno}: top-level lines must be 'key:', got: {line!r}")
            key = content[:-1].strip()
            current = {}
            cfg[key] = current
        elif indent in (2, 4) and current is not None:
            if ":" not in content:
                raise ValueError(f"{path}:{lineno}: expected 'key: value', got: {line!r}")
            key, _, value = content.partition(":")
            current[key.strip()] = _coerce(value)
        else:
            raise ValueError(f"{path}:{lineno}: unsupported indentation/structure: {line!r}")
    return cfg


def _in_quotes_hash(line: str) -> bool:
    """True when the '#' in the line sits inside quotes (naive but adequate)."""
    in_s = in_d = False
    for ch in line:
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
    return in_s or in_d


def merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge ``override`` into a copy of ``base`` (dicts only)."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


DEFAULTS: Dict[str, Dict[str, Any]] = {
    "llm": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "temperature": 0,
        "max_tokens": 4096,
        "request_timeout_s": 180,
        "price_input_cached": 0.2,
        "price_input": 2.0,
        "price_output": 8.0,
    },
    "models": {"main": "deepseek-chat", "strong": "deepseek-reasoner"},
    "defaults": {"repeats": 10, "max_steps": 40, "max_llm_calls": 60},
    "plan": {
        "max_steps": 8,
        "max_step_turns": 8,
        "max_replans": 3,
        "replan_scope": "local",
    },
    "reflexion": {"max_epochs": 4, "memory_size": 3, "evaluator": "strict"},
    "budget": {"max_cny_per_run": 500},
    "cache": {"enabled": False, "path": ".cache/llm_audit.jsonl"},
}


def load_with_defaults(path: str | Path | None) -> Dict[str, Dict[str, Any]]:
    cfg = merge(DEFAULTS, load(path) if path else {})
    return cfg
