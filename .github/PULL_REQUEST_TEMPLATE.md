## What does this PR do?

Brief description of the change.

## Checklist

- [ ] All 96+ tests pass (`python3 -m pytest tests/ -v`)
- [ ] New features include tests
- [ ] Hooks fail-open on errors (`exit 0`, never `exit 1`)
- [ ] Blocks use `exit 2` with a clear error message
- [ ] State writes use `save_json_state()` (atomic writes)
- [ ] Shared state access uses file locking
- [ ] No new dependencies added (stdlib only)

## Test output

```
Paste pytest output here
```
