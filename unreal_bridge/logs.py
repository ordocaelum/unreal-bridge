"""Tail the project's ``Saved/Logs/*.log`` from disk.

Runs in the MCP server process, not the editor, so it still works while the
editor is busy, hung, or crashed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_BLOCK = 64 * 1024
_MAX_SCAN = 64 * 1024 * 1024


def logs_dir(project_dir: str | Path) -> Path:
    p = Path(project_dir)
    if p.suffix.lower() == ".uproject":
        p = p.parent
    return p / "Saved" / "Logs"


def pick_log(directory: Path, name: str | None = None) -> Path:
    if name:
        candidate = directory / name
        if candidate.suffix == "":
            candidate = candidate.with_suffix(".log")
        if not candidate.is_file():
            raise FileNotFoundError(f"{candidate} does not exist")
        return candidate
    logs = [p for p in directory.glob("*.log") if p.is_file()]
    if not logs:
        raise FileNotFoundError(f"no .log files in {directory}")
    # The live log is the most recently written one; backups carry a timestamp suffix.
    return max(logs, key=lambda p: (p.stat().st_mtime, "-backup-" not in p.name))


def tail(path: Path, n: int = 100, pattern: str | None = None) -> dict[str, Any]:
    """Return the last ``n`` lines of ``path`` (matching ``pattern`` if given),
    reading backwards in blocks so large logs cost only what is needed."""
    regex = re.compile(pattern, re.IGNORECASE) if pattern else None
    n = max(1, n)
    matched: list[str] = []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        pos = end = fh.tell()
        carry = b""
        while pos > 0 and len(matched) < n and end - pos < _MAX_SCAN:
            step = min(_BLOCK, pos)
            pos -= step
            fh.seek(pos)
            chunk = fh.read(step) + carry
            lines = chunk.split(b"\n")
            carry = lines.pop(0) if pos > 0 else b""  # first piece may be a partial line
            for raw in reversed(lines):
                line = raw.decode("utf-8", errors="replace").rstrip("\r").lstrip("﻿")
                if not line and not matched:
                    continue  # trailing newline
                if regex is None or regex.search(line):
                    matched.append(line)
                    if len(matched) >= n:
                        break
        scanned_all = pos == 0
    matched.reverse()
    out: dict[str, Any] = {"file": str(path), "lines": matched, "count": len(matched)}
    if pattern:
        out["pattern"] = pattern
    if not scanned_all and len(matched) < n:
        out["note"] = f"stopped after scanning the last {_MAX_SCAN // (1024 * 1024)} MB"
    return out
