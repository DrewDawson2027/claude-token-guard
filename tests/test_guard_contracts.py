import json

from guard_contracts import build_audit_entry, build_metrics_usage_entry
from guard_normalize import normalize_file_path, normalize_session_key


def test_session_key_sanitizes_traversal():
    sk = normalize_session_key('../../bad-ta')
    assert '/' not in sk
    assert '..' not in sk
    assert sk


def test_audit_entry_v2_includes_legacy_fields():
    entry = build_audit_entry(
        event_type='block',
        subagent_type='Explore',
        description='search the codebase',
        session_id='../../bad-ta',
        reason='necessity_check',
        matched_pattern='search_grep',
    )
    assert entry['schema_version'] == 2
    assert entry['record_type'] == 'audit_decision'
    assert entry['event'] == 'block'
    assert entry['session'] == entry['session_key']
    assert '/' not in entry['session_key']
    assert 'reason' in entry
    assert 'rule_id' in entry
    assert entry['pattern'] == 'search_grep'


def test_metrics_usage_entry_non_empty_agent_type():
    entry = build_metrics_usage_entry(
        agent_type='',
        agent_id='abc123',
        session_id='sess-123456789',
        totals={'input_tokens': 10, 'output_tokens': 5, 'cache_read_tokens': 0, 'cache_creation_tokens': 0, 'api_calls': 1},
        cost_usd=0.01,
        correlated=False,
    )
    assert entry['record_type'] == 'usage'
    assert entry['agent_type'] == 'unknown'
    assert entry['total_tokens'] == 15


def test_normalize_file_path_collapses_alias(tmp_path):
    real = tmp_path / 'a' / 'file.py'
    real.parent.mkdir()
    real.write_text('x')
    alias = real.parent / '..' / 'a' / 'file.py'
    assert normalize_file_path(str(real)) == normalize_file_path(str(alias))
