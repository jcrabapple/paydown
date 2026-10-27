"""Minimal `.env` loader for run.py / sync_cli.py (stdlib only).

The README's quick start copies `.env.example` to `.env`; this makes that
file actually take effect. Variables already present in the process
environment always win, so systemd `EnvironmentFile=` and explicit
`BUDGET_PASS=... uv run python run.py` behave exactly as before.

Format: `KEY=value` per line; blank lines and `#` comments ignored; an
optional leading `export `; values may be wrapped in single or double
quotes. No interpolation.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def load_env_file(path: str | os.PathLike = ".env") -> list[str]:
    """Load KEY=value pairs from `path` into os.environ without overriding
    existing variables. Returns the names that were set (never values)."""
    p = Path(path)
    if not p.is_file():
        return []
    loaded = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # trailing comment on an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        if key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded
