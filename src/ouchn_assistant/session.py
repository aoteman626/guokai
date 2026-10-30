import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

from ouchn_assistant.models import RunEvent, SessionState


class InvalidTransition(RuntimeError):
    pass


class ProfileInUse(RuntimeError):
    pass


class LockOwnershipLost(RuntimeError):
    pass


ALLOWED_TRANSITIONS = MappingProxyType(
    {
        SessionState.INIT: frozenset(
            {SessionState.PREFLIGHT, SessionState.FAILED}
        ),
        SessionState.PREFLIGHT: frozenset(
            {
                SessionState.LOGIN_REQUIRED,
                SessionState.READY,
                SessionState.FAILED,
            }
        ),
        SessionState.LOGIN_REQUIRED: frozenset(
            {SessionState.READY, SessionState.PAUSED, SessionState.FAILED, SessionState.STOPPED}
        ),
        SessionState.READY: frozenset(
            {
                SessionState.INSPECTING,
                SessionState.OBSERVING,
                SessionState.PLAYING,
                SessionState.PAUSED,
                SessionState.FAILED,
                SessionState.STOPPED,
            }
        ),
        SessionState.INSPECTING: frozenset(
            {SessionState.READY, SessionState.PAUSED, SessionState.FAILED}
        ),
        SessionState.OBSERVING: frozenset(
            {
                SessionState.USER_ACTION_REQUIRED,
                SessionState.READY,
                SessionState.PAUSED,
                SessionState.FAILED,
            }
        ),
        SessionState.PLAYING: frozenset(
            {SessionState.RETURNING, SessionState.PAUSED, SessionState.FAILED}
        ),
        SessionState.RETURNING: frozenset(
            {SessionState.READY, SessionState.PAUSED, SessionState.FAILED}
        ),
        SessionState.USER_ACTION_REQUIRED: frozenset(
            {
                SessionState.OBSERVING,
                SessionState.READY,
                SessionState.STOPPED,
            }
        ),
        SessionState.PAUSED: frozenset(
            {SessionState.PREFLIGHT, SessionState.STOPPED}
        ),
        SessionState.FAILED: frozenset({SessionState.STOPPED}),
        SessionState.STOPPED: frozenset(),
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SessionMachine:
    def __init__(
        self,
        run_id: str,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.run_id = run_id
        self.state = SessionState.INIT
        self.events: list[RunEvent] = []
        self._clock = clock

    def transition(self, target: SessionState, event: str) -> RunEvent:
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTransition(f"{self.state.value} -> {target.value}")

        timestamp = self._clock().astimezone(timezone.utc)
        run_event = RunEvent(
            timestamp=timestamp.isoformat().replace("+00:00", "Z"),
            run_id=self.run_id,
            state=target,
            event=event,
        )
        self.state = target
        self.events.append(run_event)
        return run_event


class ProfileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._token: str | None = None

    def acquire(self) -> None:
        if self._token is not None:
            raise ProfileInUse(f"Profile lock already held: {self.path}")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = f"{os.getpid()}:{uuid4().hex}"

        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as error:
            raise ProfileInUse(f"Profile is already in use: {self.path}") from error

        lock_identity = os.fstat(descriptor)
        try:
            try:
                encoded_token = token.encode("utf-8")
                bytes_written = 0
                while bytes_written < len(encoded_token):
                    written = os.write(descriptor, encoded_token[bytes_written:])
                    if written <= 0:
                        raise OSError("Unable to write profile lock token")
                    bytes_written += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            try:
                current_identity = self.path.stat()
            except FileNotFoundError:
                pass
            else:
                if (
                    current_identity.st_dev == lock_identity.st_dev
                    and current_identity.st_ino == lock_identity.st_ino
                ):
                    self.path.unlink(missing_ok=True)
            raise

        self._token = token

    def release(self) -> None:
        if self._token is None:
            return

        try:
            current_token = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise LockOwnershipLost(f"Profile lock disappeared: {self.path}") from error

        if current_token != self._token:
            raise LockOwnershipLost(f"Profile lock ownership changed: {self.path}")

        self.path.unlink()
        self._token = None

    def __enter__(self) -> "ProfileLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        try:
            self.release()
        except (LockOwnershipLost, OSError) as release_error:
            if exc_value is None:
                raise
            message = f"释放 profile 锁失败：{release_error}"
            exc_value.add_note(message)
            print(message, file=sys.stderr, flush=True)
