from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from ouchn_assistant.models import SessionState
from ouchn_assistant.session import (
    InvalidTransition,
    LockOwnershipLost,
    ProfileInUse,
    ProfileLock,
    SessionMachine,
)


FIXED_TIME = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)


def test_valid_transition_records_deterministic_event() -> None:
    machine = SessionMachine(run_id="run-1", clock=lambda: FIXED_TIME)

    event = machine.transition(SessionState.PREFLIGHT, "start")

    assert machine.state is SessionState.PREFLIGHT
    assert machine.events == [event]
    assert event.timestamp == "2026-10-26T10:00:00Z"
    assert event.run_id == "run-1"
    assert event.state is SessionState.PREFLIGHT
    assert event.event == "start"


def test_invalid_transition_is_rejected_without_mutating_state() -> None:
    machine = SessionMachine(run_id="run-1")

    with pytest.raises(InvalidTransition, match="init -> observing"):
        machine.transition(SessionState.OBSERVING, "skip_preflight")

    assert machine.state is SessionState.INIT
    assert machine.events == []


def test_stopped_is_terminal() -> None:
    machine = SessionMachine(run_id="run-1")
    machine.transition(SessionState.PREFLIGHT, "start")
    machine.transition(SessionState.READY, "preflight_complete")
    machine.transition(SessionState.STOPPED, "user_exit")

    with pytest.raises(InvalidTransition, match="stopped -> preflight"):
        machine.transition(SessionState.PREFLIGHT, "restart")


def test_profile_lock_rejects_second_owner_and_allows_reacquire(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "runtime" / "profile.lock"
    first = ProfileLock(lock_path)
    second = ProfileLock(lock_path)

    first.acquire()
    with pytest.raises(ProfileInUse):
        second.acquire()

    first.release()
    second.acquire()
    second.release()

    assert not lock_path.exists()


def test_profile_lock_does_not_delete_replaced_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "profile.lock"
    lock = ProfileLock(lock_path)
    lock.acquire()
    lock_path.write_text("another-owner", encoding="utf-8")

    with pytest.raises(LockOwnershipLost):
        lock.release()

    assert lock_path.read_text(encoding="utf-8") == "another-owner"
    lock_path.unlink()


def test_profile_lock_context_manager_releases_on_error(tmp_path: Path) -> None:
    lock_path = tmp_path / "profile.lock"

    with pytest.raises(RuntimeError, match="boom"):
        with ProfileLock(lock_path):
            raise RuntimeError("boom")

    assert not lock_path.exists()


def test_profile_lock_removes_partial_lock_when_token_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "profile.lock"

    def fail_write(descriptor: int, data: bytes) -> int:
        raise OSError("disk write failed")

    monkeypatch.setattr(os, "write", fail_write)

    with pytest.raises(OSError, match="disk write failed"):
        ProfileLock(lock_path).acquire()

    assert not lock_path.exists()


def test_profile_lock_handles_partial_token_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "profile.lock"
    real_write = os.write

    def write_one_byte(descriptor: int, data: bytes) -> int:
        return real_write(descriptor, data[:1])

    monkeypatch.setattr(os, "write", write_one_byte)

    lock = ProfileLock(lock_path)
    lock.acquire()
    lock.release()

    assert not lock_path.exists()
