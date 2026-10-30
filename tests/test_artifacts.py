import json
from pathlib import Path

import pytest

from ouchn_assistant.artifacts import (
    append_jsonl,
    atomic_write_json,
    redact_mapping,
    sanitize_url,
)


def test_sanitize_url_drops_credentials_query_and_fragment() -> None:
    assert sanitize_url(
        "https://user:pass@lms.ouchn.cn/course/100/ng?token=secret#part"
    ) == "https://lms.ouchn.cn/course/100/ng"


def test_sanitize_url_tolerates_malformed_port() -> None:
    assert sanitize_url(
        "https://user:pass@lms.ouchn.cn:not-a-port/course/100/ng?token=secret"
    ) == "https://lms.ouchn.cn/course/100/ng"


def test_redact_mapping_masks_nested_secrets_without_mutating_input() -> None:
    original = {
        "cookie": "abc",
        "Authorization": "Bearer x",
        "course_id": "100",
        "nested": {
            "access_token": "secret",
            "items": [{"password": "hidden"}, {"status": "ok"}],
        },
    }

    redacted = redact_mapping(original)

    assert redacted == {
        "cookie": "[REDACTED]",
        "Authorization": "[REDACTED]",
        "course_id": "100",
        "nested": {
            "access_token": "[REDACTED]",
            "items": [{"password": "[REDACTED]"}, {"status": "ok"}],
        },
    }
    assert original["cookie"] == "abc"
    assert original["nested"]["access_token"] == "secret"


def test_redact_mapping_sanitizes_url_values() -> None:
    assert redact_mapping(
        {
            "current_url": "https://lms.ouchn.cn/course/100/ng?token=secret#part",
            "label": "course",
        }
    ) == {
        "current_url": "https://lms.ouchn.cn/course/100/ng",
        "label": "course",
    }


def test_atomic_write_json_replaces_complete_document(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "snapshot.json"

    atomic_write_json(target, {"schema_version": 1, "title": "第一讲"})
    atomic_write_json(target, {"schema_version": 2, "activities": []})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "schema_version": 2,
        "activities": [],
    }
    assert not list(target.parent.glob("*.tmp"))


def test_atomic_write_failure_preserves_previous_document(tmp_path: Path) -> None:
    target = tmp_path / "snapshot.json"
    atomic_write_json(target, {"schema_version": 1})

    with pytest.raises(TypeError):
        atomic_write_json(target, {"invalid": object()})

    assert json.loads(target.read_text(encoding="utf-8")) == {"schema_version": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_append_jsonl_redacts_each_event_and_preserves_unicode(tmp_path: Path) -> None:
    target = tmp_path / "events.jsonl"

    append_jsonl(target, {"event": "开始", "token": "secret"})
    append_jsonl(target, {"event": "停止", "status": "unknown"})

    lines = target.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"event": "开始", "token": "[REDACTED]"},
        {"event": "停止", "status": "unknown"},
    ]
