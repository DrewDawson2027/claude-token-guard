# Security Policy

## Scope

Claude Token Guard is a local enforcement tool. It runs on your machine, reads stdin, writes to local state files, and exits. It never makes network requests, never sends telemetry, and never touches your API keys.

## Design Principles

1. **Fail-open** — If anything goes wrong (corrupted state, missing files, unexpected input), the hook exits 0 and allows the tool call. A bug in the guard should never block legitimate work.

2. **No secrets** — The hook reads tool call metadata (tool name, agent type, description). It never reads file contents, API responses, or conversation history.

3. **Atomic writes** — All state persistence uses `tempfile.mkstemp()` + `os.replace()` to prevent corruption from crashes or concurrent access.

4. **Bounded state** — All arrays have TTLs (300s for reads, 24h for sessions). The audit log rotates at 10K lines. State cannot grow unbounded.

## Reporting a Vulnerability

If you find a security issue:

1. **Do NOT open a public issue**
2. Email the maintainer directly (see GitHub profile)
3. Include steps to reproduce
4. Allow 48 hours for initial response

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest main | Yes |
| Older commits | Best-effort |

## Known Limitations

- File locking uses `fcntl.flock()` on Unix and `msvcrt.locking()` on Windows. NFS/network filesystems may not honor these locks. Claude Token Guard is designed for local filesystems only.
- The necessity scoring uses regex pattern matching, which can produce false positives. This is by design — false positives are preferable to false negatives for cost control. Tune thresholds via config if needed.
