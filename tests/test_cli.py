import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ouchn_assistant.cli import build_snapshot, main, parse_cli_args, run_inspect
from ouchn_assistant.browser import BrowserBoundaryError, is_allowed_lms_url
from ouchn_assistant.config import build_config
from ouchn_assistant.inspector import ActivityParseError
from ouchn_assistant.models import Activity, RunMode


COURSE_URL = "https://lms.ouchn.cn/course/100/ng"


def test_parse_inspect_arguments() -> None:
    arguments = parse_cli_args(
        ["--mode", "inspect", "--course-url", COURSE_URL]
    )

    assert arguments.mode == "inspect"
    assert arguments.course_url == COURSE_URL
    assert arguments.screenshots is False
    assert arguments.smoke_browser is False


def test_missing_course_url_exits_with_code_2() -> None:
    with pytest.raises(SystemExit) as error:
        parse_cli_args(["--mode", "inspect"])

    assert error.value.code == 2


def test_invalid_course_domain_returns_code_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--mode",
            "inspect",
            "--course-url",
            "https://example.com/course/100/ng",
        ]
    )

    assert exit_code == 2
    assert "valid OUCHN course URL" in capsys.readouterr().err


def test_build_snapshot_has_stable_schema(tmp_path: Path) -> None:
    config = build_config(
        mode=RunMode.INSPECT,
        course_url=COURSE_URL,
        project_root=tmp_path,
    )
    observed_at = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)

    snapshot = build_snapshot(
        run_id="20261026T100000Z-ab12cd",
        config=config,
        observed_at=observed_at,
        activities=[
            Activity(
                activity_id="page-2",
                title="Reading",
                href="/course/100/activity/page-2",
            )
        ],
    )

    assert snapshot == {
        "schema_version": 1,
        "run_id": "20261026T100000Z-ab12cd",
        "course_id": "100",
        "observed_at": "2026-10-26T10:00:00Z",
        "activities": [
            {
                "activity_id": "page-2",
                "title": "Reading",
                "href": "/course/100/activity/page-2",
                "type_hint": "unknown",
                "status": "unknown",
                "signals": {},
            }
        ],
    }

    json.dumps(snapshot)


def test_run_inspect_only_navigates_to_course_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = build_config(
        mode=RunMode.INSPECT,
        course_url=COURSE_URL,
        project_root=tmp_path,
    )
    navigated_urls: list[str] = []
    lifecycle = {"playwright_active": False}

    class FakePage:
        url = COURSE_URL

        def goto(self, url: str, **_: object) -> None:
            navigated_urls.append(url)

    class FakeBrowserSession:
        def __init__(self, _playwright: object, _config: object) -> None:
            self.page = FakePage()
            self.closed = False

        def open(self) -> FakePage:
            return self.page

        def assert_allowed_page(self, _page: FakePage) -> None:
            return None

        def close(self) -> None:
            if not lifecycle["playwright_active"]:
                raise RuntimeError("browser closed after Playwright stopped")
            self.closed = True

    class FakePlaywrightManager:
        def __enter__(self) -> object:
            lifecycle["playwright_active"] = True
            return object()

        def __exit__(self, *_: object) -> None:
            lifecycle["playwright_active"] = False
            return None

    monkeypatch.setattr(
        "ouchn_assistant.cli.BrowserSession",
        FakeBrowserSession,
    )
    monkeypatch.setattr(
        "ouchn_assistant.cli.sync_playwright",
        lambda: FakePlaywrightManager(),
    )
    monkeypatch.setattr(
        "ouchn_assistant.cli.inspect_course",
        lambda _page: [
            Activity(
                activity_id="page-2",
                title="Reading",
                href="/course/100/activity/page-2",
            )
        ],
    )

    snapshot_path = run_inspect(
        config,
        clock=lambda: datetime(2026, 10, 26, 10, tzinfo=timezone.utc),
        run_id_factory=lambda _now: "20261026T100000Z-ab12cd",
    )

    assert navigated_urls == [COURSE_URL]
    assert snapshot_path == (
        tmp_path
        / "runs"
        / "20261026T100000Z-ab12cd"
        / "activities_snapshot.json"
    )
    assert json.loads(snapshot_path.read_text(encoding="utf-8"))[
        "activities"
    ][0]["href"] == "/course/100/activity/page-2"
    assert not (tmp_path / ".runtime" / "chrome-profile.lock").exists()


def test_run_inspect_records_terminal_states_on_parse_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = build_config(
        mode=RunMode.INSPECT,
        course_url=COURSE_URL,
        project_root=tmp_path,
    )

    class FakePage:
        url = COURSE_URL

        def goto(self, _url: str, **_: object) -> None:
            return None

    class FakeBrowserSession:
        def __init__(self, _playwright: object, _config: object) -> None:
            pass

        def open(self) -> FakePage:
            return FakePage()

        def assert_allowed_page(self, _page: FakePage) -> None:
            return None

        def close(self) -> None:
            return None

    class FakePlaywrightManager:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(
        "ouchn_assistant.cli.BrowserSession",
        FakeBrowserSession,
    )
    monkeypatch.setattr(
        "ouchn_assistant.cli.sync_playwright",
        lambda: FakePlaywrightManager(),
    )
    monkeypatch.setattr(
        "ouchn_assistant.cli.inspect_course",
        lambda _page: (_ for _ in ()).throw(
            ActivityParseError("missing structure")
        ),
    )

    with pytest.raises(ActivityParseError, match="missing structure"):
        run_inspect(
            config,
            clock=lambda: datetime(2026, 10, 26, 10, tzinfo=timezone.utc),
            run_id_factory=lambda _now: "failed-run",
        )

    event_lines = (
        tmp_path / "runs" / "failed-run" / "events.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    states = [json.loads(line)["state"] for line in event_lines]
    assert states[-2:] == ["failed", "stopped"]
    assert not (tmp_path / ".runtime" / "chrome-profile.lock").exists()


def test_run_inspect_pauses_for_known_manual_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = build_config(
        mode=RunMode.INSPECT,
        course_url=COURSE_URL,
        project_root=tmp_path,
    )

    class FakePage:
        url = "about:blank"

        def goto(self, _url: str, **_: object) -> None:
            self.url = "https://iam.pt.ouchn.cn/am/oauth2/authorize"

    page = FakePage()
    created_sessions: list[object] = []

    class FakeBrowserSession:
        def __init__(self, _playwright: object, _config: object) -> None:
            self.course_page: FakePage | None = None
            created_sessions.append(self)

        def open(self) -> FakePage:
            return page

        def assert_allowed_page(self, checked_page: FakePage) -> None:
            if not is_allowed_lms_url(checked_page.url):
                raise BrowserBoundaryError(checked_page.url)

        def close(self) -> None:
            return None

        def find_course_page(self) -> FakePage | None:
            return self.course_page

    class FakePlaywrightManager:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(
        "ouchn_assistant.cli.BrowserSession",
        FakeBrowserSession,
    )
    monkeypatch.setattr(
        "ouchn_assistant.cli.sync_playwright",
        lambda: FakePlaywrightManager(),
    )
    monkeypatch.setattr(
        "ouchn_assistant.cli.inspect_course",
        lambda _page: [
            Activity(
                activity_id="page-2",
                title="Reading",
                href="/course/100/activity/page-2",
            )
        ],
    )

    def complete_login(_prompt: str) -> str:
        course_page = FakePage()
        course_page.url = COURSE_URL
        # The identity provider can finish login in a separate tab.
        browser_session = created_sessions[0]
        browser_session.course_page = course_page  # type: ignore[attr-defined]
        return ""

    snapshot_path = run_inspect(
        config,
        input_fn=complete_login,
        clock=lambda: datetime(2026, 10, 26, 10, tzinfo=timezone.utc),
        run_id_factory=lambda _now: "manual-login-run",
    )

    states = [
        json.loads(line)["state"]
        for line in (
            tmp_path / "runs" / "manual-login-run" / "events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert "login_required" in states
    assert snapshot_path.exists()
