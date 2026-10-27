"""Output-size control: keep tool results under a character budget."""

from __future__ import annotations

import copy
import json
from typing import Any

_MIN_STR = 200


def _size(obj: Any) -> int:
    return len(json.dumps(obj, default=str, ensure_ascii=False))


def _candidates(obj: Any, path: str, out: list[tuple[int, Any, Any, str]], parent: Any = None, key: Any = None) -> None:
    if isinstance(obj, list):
        if len(obj) > 1:
            out.append((_size(obj), parent, key, path))
        for i, v in enumerate(obj):
            _candidates(v, f"{path}[{i}]", out, obj, i)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _candidates(v, f"{path}.{k}" if path else str(k), out, obj, k)
    elif isinstance(obj, str) and len(obj) > _MIN_STR and parent is not None:
        out.append((len(obj), parent, key, path))


def fit(obj: Any, max_chars: int) -> Any:
    """Return ``obj`` unchanged if it fits, else a copy with the largest lists and
    strings shortened and a ``_truncated`` note saying what was cut."""
    if max_chars <= 0 or _size(obj) <= max_chars:
        return obj
    root: Any = {"value": copy.deepcopy(obj)}
    notes: dict[str, str] = {}
    original_len: dict[str, int] = {}
    for _ in range(200):
        if _size(root["value"]) <= max_chars - 400:  # room for the notes
            break
        found: list[tuple[int, Any, Any, str]] = []
        _candidates(root["value"], "", found, root, "value")
        if not found:
            break
        _, parent, key, path = max(found, key=lambda c: c[0])
        value = parent[key]
        label = path or "result"
        if isinstance(value, list):
            original_len.setdefault(label, len(value))
            parent[key] = value[: max(1, len(value) // 2)]
            notes[label] = f"kept {len(parent[key])} of {original_len[label]} items"
        else:
            original_len.setdefault(label, len(value))
            keep = max(_MIN_STR, len(value) // 2)
            parent[key] = value[-keep:] if label.endswith(("stdout", "stderr", "lines")) else value[:keep]
            notes[label] = f"kept {keep} of {original_len[label]} chars"
    result = root["value"]
    if _size(result) > max_chars:
        text = json.dumps(result, default=str, ensure_ascii=False)
        return {"_truncated": f"output was {len(text)} chars; showing the first {max_chars}", "preview": text[:max_chars]}
    note = "; ".join(f"{k}: {v}" for k, v in notes.items()) + " (output limit reached; narrow the request to see more)"
    if isinstance(result, dict):
        return {**result, "_truncated": note}
    return {"value": result, "_truncated": note}
