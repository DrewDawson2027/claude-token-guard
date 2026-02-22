"""Versioned record contracts for Claude token management hooks.

Writers emit schema v2 fields while preserving legacy fields during a
compatibility window. Readers/helpers support mixed v1/v2 logs.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from guard_normalize import normalize_session_key, normalize_subagent_type, normalize_text, short_hash

SCHEMA_VERSION = 2

_REASON_TO_RULE_ID = {
    "one_per_session limit": "one_per_session",
    "max_per_type limit": "max_per_type",
    "session_cap limit": "session_cap",
    "parallel_window limit": "parallel_window",
    "necessity_check": "necessity_check",
    "type_switching": "type_switching",
    "global_cooldown": "global_cooldown",
    "team_session_cap": "team_session_cap",
    "team_session_cap limit": "team_session_cap",
}


def reason_to_rule_id(reason: str, event_type: str = "") -> str:
    if event_type == "resume":
        return "resume"
    if event_type == "allow_team":
        return "allow_team"
    if event_type == "allow":
        return "none"
    if event_type == "warn":
        return "warn"
    if event_type == "fault":
        return "fault"
    return _REASON_TO_RULE_ID.get(reason, normalize_text(reason, 80) or "unknown")


def build_decision_id(event_type: str, subagent_type: str, session_id: Any, ts: Optional[float] = None) -> str:
    ts = ts if ts is not None else time.time()
    millis = int(ts * 1000)
    sk = normalize_session_key(session_id, max_len=12)
    st = normalize_subagent_type(subagent_type, max_len=40)
    return short_hash(f"{millis}|{event_type}|{st}|{sk}", length=16)


def build_audit_entry(
    *,
    event_type: str,
    subagent_type: Any,
    description: Any,
    session_id: Any,
    reason: str = "",
    matched_pattern: str = "",
    decision_id: str = "",
    latency_ms: Optional[int] = None,
    message: str = "",
    fault_class: str = "",
) -> Dict[str, Any]:
    ts_epoch = time.time()
    ts_str = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts_epoch))
    desc = normalize_text(description, max_len=512)
    sub_type = normalize_subagent_type(subagent_type)
    session_key = normalize_session_key(session_id, max_len=12)
    decision_id = decision_id or build_decision_id(event_type, sub_type, session_id, ts=ts_epoch)
    rule_id = reason_to_rule_id(reason, event_type=event_type)
    reason_code = normalize_text(reason, max_len=120) or rule_id

    entry: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "audit_decision",
        "ts": ts_str,
        "event": event_type,
        "rule_id": rule_id,
        "reason_code": reason_code,
        "session_key": session_key,
        "subagent_type": sub_type,
        "decision_id": decision_id,
        "desc_present": bool(desc),
        "desc_hash": short_hash(desc, 12) if desc else "",
        "message": normalize_text(message, max_len=240),
        # legacy compatibility fields (one-release window)
        "type": sub_type,
        "desc": desc[:80],
        "session": session_key,
    }
    if reason:
        entry["reason"] = reason_code
    if matched_pattern:
        entry["pattern"] = normalize_text(matched_pattern, 120)
    if latency_ms is not None:
        entry["latency_ms"] = int(latency_ms)
    if fault_class:
        entry["fault_class"] = normalize_text(fault_class, 80)
    return entry


def build_metrics_lifecycle_entry(
    *,
    event: str,
    agent_type: Any,
    agent_id: Any,
    session_id: Any,
    decision_id: str = "",
    duration_seconds: Any = None,
    duration_known: Optional[bool] = None,
) -> Dict[str, Any]:
    session_key = normalize_session_key(session_id, max_len=12)
    agent_type_n = normalize_subagent_type(agent_type)
    agent_id_n = normalize_text(agent_id, max_len=64) or "unknown"
    entry: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "lifecycle",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": normalize_text(event, 20) or "unknown",
        "agent_type": agent_type_n,
        "agent_id": agent_id_n,
        "session_key": session_key,
        "session": session_key,  # legacy
        "decision_id": normalize_text(decision_id, 32),
    }
    if duration_seconds is not None:
        entry["duration_seconds"] = duration_seconds
    if duration_known is not None:
        entry["duration_known"] = bool(duration_known)
    return entry


def build_metrics_usage_entry(
    *,
    agent_type: Any,
    agent_id: Any,
    session_id: Any,
    totals: Dict[str, Any],
    cost_usd: float,
    decision_id: str = "",
    correlated: Optional[bool] = None,
    transcript_found: Optional[bool] = None,
    usage_records_parsed: Optional[int] = None,
    usage_records_skipped: Optional[int] = None,
    pricing_model: str = "sonnet-4.6-heuristic",
) -> Dict[str, Any]:
    session_key = normalize_session_key(session_id, max_len=12)
    agent_type_n = normalize_subagent_type(agent_type)
    agent_id_n = normalize_text(agent_id, max_len=64) or "unknown"
    entry: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "usage",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "agent_completed",
        "agent_type": agent_type_n,
        "agent_id": agent_id_n,
        "session_key": session_key,
        "session": session_key,
        "decision_id": normalize_text(decision_id, 32),
        "input_tokens": int(totals.get("input_tokens", 0) or 0),
        "output_tokens": int(totals.get("output_tokens", 0) or 0),
        "cache_read_tokens": int(totals.get("cache_read_tokens", 0) or 0),
        "cache_creation_tokens": int(totals.get("cache_creation_tokens", 0) or 0),
        "api_calls": int(totals.get("api_calls", 0) or 0),
        "total_tokens": int((totals.get("input_tokens", 0) or 0) + (totals.get("output_tokens", 0) or 0)),
        "cost_usd": float(cost_usd),
        "pricing_model": normalize_text(pricing_model, 80),
    }
    if correlated is not None:
        entry["correlated"] = bool(correlated)
    if transcript_found is not None:
        entry["transcript_found"] = bool(transcript_found)
    if usage_records_parsed is not None:
        entry["usage_records_parsed"] = int(usage_records_parsed)
    if usage_records_skipped is not None:
        entry["usage_records_skipped"] = int(usage_records_skipped)
    return entry


def entry_session_key(entry: Dict[str, Any]) -> str:
    return normalize_session_key(entry.get("session_key") or entry.get("session") or "unknown", max_len=12)


def entry_reason(entry: Dict[str, Any]) -> str:
    return normalize_text(entry.get("reason_code") or entry.get("reason") or "", 120)


def entry_type(entry: Dict[str, Any]) -> str:
    return normalize_subagent_type(entry.get("subagent_type") or entry.get("type") or entry.get("agent_type") or "unknown")


def entry_schema_version(entry: Dict[str, Any]) -> int:
    try:
        return int(entry.get("schema_version", 1))
    except (TypeError, ValueError):
        return 1
