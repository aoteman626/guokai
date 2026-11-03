from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class RunMode(StrEnum):
    INSPECT = "inspect"
    ASSIST = "assist"
    REPORT = "report"


class SessionState(StrEnum):
    INIT = "init"
    PREFLIGHT = "preflight"
    LOGIN_REQUIRED = "login_required"
    READY = "ready"
    INSPECTING = "inspecting"
    OBSERVING = "observing"
    PLAYING = "playing"
    RETURNING = "returning"
    USER_ACTION_REQUIRED = "user_action_required"
    PAUSED = "paused"
    FAILED = "failed"
    STOPPED = "stopped"


class ActivityStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True, slots=True)
class Activity:
    activity_id: str
    title: str
    href: str
    type_hint: str = "unknown"
    status: ActivityStatus = ActivityStatus.UNKNOWN
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VideoState:
    paused: bool
    ended: bool
    current_time: float
    duration: float
    playback_rate: float
    media_kind: str = "video"
    error_code: int | None = None
    ready_state: int = 0
    network_state: int = 0
    buffered_end: float = 0.0


@dataclass(frozen=True, slots=True)
class RunEvent:
    timestamp: str
    run_id: str
    state: SessionState
    event: str
    activity_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
