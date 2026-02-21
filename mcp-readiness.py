#!/usr/bin/env python3
"""
MCP readiness validator.

Outputs:
- ~/.claude/terminals/mcp-readiness.json
- ~/.claude/terminals/mcp-capability-matrix.md
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CATALOG_PATH = HOME / ".claude" / "mcp.json"
ACTIVE_PATH = HOME / ".claude" / "settings.local.json"
OUTPUT_DIR = HOME / ".claude" / "terminals"
OUT_JSON = OUTPUT_DIR / "mcp-readiness.json"
OUT_MD = OUTPUT_DIR / "mcp-capability-matrix.md"
ENV_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def load_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def extract_env_vars(server_cfg: dict) -> list[str]:
    env = server_cfg.get("env", {})
    if not isinstance(env, dict):
        return []
    refs: list[str] = []
    for value in env.values():
        if isinstance(value, str):
            refs.extend(ENV_VAR_RE.findall(value))
    # stable unique
    seen = set()
    ordered = []
    for var in refs:
        if var not in seen:
            seen.add(var)
            ordered.append(var)
    return ordered


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    catalog_doc = load_json(CATALOG_PATH)
    active_doc = load_json(ACTIVE_PATH)
    catalog_servers = {
        k: v for k, v in catalog_doc.get("mcpServers", {}).items()
        if isinstance(v, dict) and not str(k).strip().startswith("//")
    }
    active_servers = {
        k: v for k, v in active_doc.get("mcpServers", {}).items()
        if isinstance(v, dict) and not str(k).strip().startswith("//")
    }

    all_names = sorted(set(catalog_servers) | set(active_servers))
    rows = []
    for name in all_names:
        catalog_cfg = catalog_servers.get(name, {})
        active_cfg = active_servers.get(name)
        enabled = active_cfg is not None
        effective_cfg = active_cfg if enabled else catalog_cfg
        required_env = extract_env_vars(effective_cfg)
        missing_env = [var for var in required_env if not os.environ.get(var)]
        configured = len(missing_env) == 0
        ready = enabled and configured
        rows.append({
            "name": name,
            "enabled": enabled,
            "configured": configured,
            "ready": ready,
            "required_env": required_env,
            "missing_env": missing_env,
            "source": "active" if enabled else "catalog-only",
        })

    enabled_count = sum(1 for row in rows if row["enabled"])
    ready_count = sum(1 for row in rows if row["ready"])
    configured_count = sum(1 for row in rows if row["configured"])
    missing_env_total = sum(len(row["missing_env"]) for row in rows if row["enabled"])
    catalog_missing_env_vars = sorted({var for row in rows for var in row["missing_env"]})
    catalog_missing_env_total = len(catalog_missing_env_vars)
    ts = datetime.now(timezone.utc).isoformat()

    report = {
        "generated_at": ts,
        "catalog_total": len(catalog_servers),
        "active_total": enabled_count,
        "configured_total": configured_count,
        "ready_total": ready_count,
        "enabled_missing_env_vars_total": missing_env_total,
        "catalog_missing_env_vars_total": catalog_missing_env_total,
        "catalog_missing_env_vars": catalog_missing_env_vars,
        "servers": rows,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    md_lines = [
        f"# MCP Capability Matrix ({ts})",
        "",
        f"- Cataloged servers: **{len(catalog_servers)}**",
        f"- Active servers: **{enabled_count}**",
        f"- Ready servers: **{ready_count}**",
        f"- Missing env vars (active only): **{missing_env_total}**",
        f"- Missing env vars (catalog unique): **{catalog_missing_env_total}**",
        "",
        "| Server | Enabled | Configured | Ready | Missing Env Vars |",
        "|--------|---------|------------|-------|------------------|",
    ]
    for row in rows:
        missing = ", ".join(row["missing_env"]) if row["missing_env"] else "—"
        md_lines.append(
            f"| {row['name']} | {'yes' if row['enabled'] else 'no'} | "
            f"{'yes' if row['configured'] else 'no'} | {'yes' if row['ready'] else 'no'} | {missing} |"
        )
    OUT_MD.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(
        f"MCP readiness: catalog={len(catalog_servers)} active={enabled_count} "
        f"ready={ready_count} missing_env_active={missing_env_total} "
        f"missing_env_catalog_unique={catalog_missing_env_total}"
    )
    if missing_env_total > 0:
        print(f"MCP readiness details: {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
