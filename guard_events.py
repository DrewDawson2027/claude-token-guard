"""Structured event emitters for token management hooks."""

from __future__ import annotations

import json
from typing import Any, Dict

from hook_utils import locked_append


def append_jsonl(path: str, record: Dict[str, Any]) -> bool:
    try:
        return locked_append(path, json.dumps(record) + "\n")
    except Exception:
        return False
