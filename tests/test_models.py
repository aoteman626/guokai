from ouchn_assistant.models import (
    Activity,
    ActivityStatus,
    RunEvent,
    RunMode,
    SessionState,
)


def test_activity_defaults_to_unknown() -> None:
    activity = Activity(activity_id="a1", title="Lesson 1", href="/activity/a1")

    assert activity.status is ActivityStatus.UNKNOWN
    assert activity.signals == {}


def test_activity_to_dict_keeps_stable_fields() -> None:
    activity = Activity(
        activity_id="a1",
        title="Lesson 1",
        href="/activity/a1",
        type_hint="video",
        status=ActivityStatus.COMPLETE,
        signals={"icon_class": "font-video"},
    )

    assert activity.to_dict() == {
        "activity_id": "a1",
        "title": "Lesson 1",
        "href": "/activity/a1",
        "type_hint": "video",
        "status": ActivityStatus.COMPLETE,
        "signals": {"icon_class": "font-video"},
    }


def test_run_event_to_dict_keeps_state_and_details() -> None:
    event = RunEvent(
        timestamp="2026-10-26T10:00:00Z",
        run_id="run-1",
        state=SessionState.PREFLIGHT,
        event="start",
        details={"source": "test"},
    )

    assert event.to_dict()["state"] is SessionState.PREFLIGHT
    assert event.to_dict()["details"] == {"source": "test"}


def test_public_enums_are_stable() -> None:
    assert [mode.value for mode in RunMode] == ["inspect", "assist", "report"]
    assert SessionState.USER_ACTION_REQUIRED.value == "user_action_required"
    assert [status.value for status in ActivityStatus] == [
        "complete",
        "incomplete",
        "unknown",
        "review_required",
    ]
