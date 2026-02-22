#!/usr/bin/env python3
"""Agent Metrics — extracts real token usage from subagent transcripts.

Triggered by SubagentStop hook. Reads the agent's transcript JSONL,
sums actual input/output tokens from API responses, and logs precise
cost data. This solves the "no per-invocation token metering" limitation
by parsing what Claude Code already records.
"""
import json
import sys
import os
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from guard_contracts import build_metrics_usage_entry
from guard_normalize import normalize_subagent_type, normalize_text
from hook_utils import locked_append, read_jsonl_fault_tolerant

METRICS_DIR = os.path.expanduser("~/.claude/hooks/session-state")
METRICS_FILE = os.path.join(METRICS_DIR, "agent-metrics.jsonl")

# Sonnet 4.6 pricing (per 1K tokens)
COST_PER_1K_INPUT = 0.003    # $3/M input
COST_PER_1K_OUTPUT = 0.015   # $15/M output
COST_PER_1K_CACHE_READ = 0.0003  # $0.30/M cache read (90% discount)


def parse_transcript(transcript_path: str) -> Tuple[dict, Dict[str, Any]]:
    """Parse a subagent transcript JSONL and sum token usage."""
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "api_calls": 0,
    }
    quality: Dict[str, Any] = {
        "transcript_found": False,
        "usage_records_parsed": 0,
        "usage_records_skipped": 0,
    }

    if not transcript_path or not os.path.isfile(transcript_path):
        return totals, quality
    quality["transcript_found"] = True

    try:
        with open(transcript_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    quality["usage_records_skipped"] += 1
                    continue

                msg = entry.get("message", {})
                if not isinstance(msg, dict):
                    quality["usage_records_skipped"] += 1
                    continue

                usage = msg.get("usage")
                if not usage or not isinstance(usage, dict):
                    quality["usage_records_skipped"] += 1
                    continue

                totals["input_tokens"] += usage.get("input_tokens", 0)
                totals["output_tokens"] += usage.get("output_tokens", 0)
                totals["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
                totals["cache_creation_tokens"] += usage.get("cache_creation_input_tokens", 0)
                totals["api_calls"] += 1
                quality["usage_records_parsed"] += 1
    except (OSError, PermissionError):
        pass

    return totals, quality


def calculate_cost(totals: dict) -> float:
    """Calculate estimated cost from token counts."""
    # Input tokens that aren't cache reads
    fresh_input = totals["input_tokens"] - totals["cache_read_tokens"]
    if fresh_input < 0:
        fresh_input = 0

    cost = (
        (fresh_input / 1000) * COST_PER_1K_INPUT
        + (totals["cache_read_tokens"] / 1000) * COST_PER_1K_CACHE_READ
        + (totals["output_tokens"] / 1000) * COST_PER_1K_OUTPUT
    )
    return round(cost, 4)


def correlate_decision(agent_id: str) -> Tuple[str, bool]:
    """Best-effort correlation using lifecycle start records in agent-metrics.jsonl."""
    if not os.path.isfile(METRICS_FILE) or not agent_id:
        return "", False
    try:
        entries = read_jsonl_fault_tolerant(METRICS_FILE)
    except Exception:
        return "", False
    for entry in reversed(entries):
        if str(entry.get("agent_id", "")) != str(agent_id):
            continue
        if entry.get("event") != "start":
            continue
        decision_id = normalize_text(entry.get("decision_id", ""), max_len=32)
        if decision_id:
            return decision_id, True
        return "", False
    return "", False


def lookup_agent_type_from_start(agent_id: str) -> str:
    """Recover agent_type from lifecycle start record when SubagentStop payload is empty."""
    if not os.path.isfile(METRICS_FILE) or not agent_id:
        return ""
    try:
        entries = read_jsonl_fault_tolerant(METRICS_FILE)
    except Exception:
        return ""
    for entry in reversed(entries):
        if str(entry.get("agent_id", "")) != str(agent_id):
            continue
        if entry.get("event") != "start":
            continue
        at = normalize_subagent_type(entry.get("agent_type", ""))
        if at and at != "unknown":
            return at
    return ""


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    event = input_data.get("hook_event_name", "")
    if event != "SubagentStop":
        sys.exit(0)

    agent_type = normalize_subagent_type(input_data.get("agent_type", "unknown"))
    agent_id = normalize_text(input_data.get("agent_id", "unknown"), max_len=64) or "unknown"
    session_id = input_data.get("session_id", "unknown")
    transcript_path = input_data.get("agent_transcript_path", "")

    # Recover agent_type from lifecycle start record if SubagentStop payload is empty
    if not agent_type or agent_type == "unknown":
        agent_type = lookup_agent_type_from_start(agent_id) or "unknown"

    # Parse real token usage from transcript
    totals, quality = parse_transcript(transcript_path)
    cost = calculate_cost(totals)
    decision_id, correlated = correlate_decision(agent_id)

    os.makedirs(METRICS_DIR, exist_ok=True)

    # Log detailed metrics
    metric = build_metrics_usage_entry(
        agent_type=agent_type,
        agent_id=agent_id,
        session_id=session_id,
        totals=totals,
        cost_usd=cost,
        decision_id=decision_id,
        correlated=correlated if decision_id else False,
        transcript_found=bool(quality.get("transcript_found")),
        usage_records_parsed=int(quality.get("usage_records_parsed", 0)),
        usage_records_skipped=int(quality.get("usage_records_skipped", 0)),
    )
    # preserve explicit timestamp style used here previously
    metric["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    locked_append(METRICS_FILE, json.dumps(metric) + "\n")

    # Auto-truncate
    try:
        with open(METRICS_FILE, "r") as f:
            lines = f.readlines()
        if len(lines) > 500:
            with open(METRICS_FILE, "w") as f:
                f.writelines(lines[-400:])
    except OSError:
        pass


if __name__ == "__main__":
    main()
