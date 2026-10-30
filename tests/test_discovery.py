import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from ouchn_assistant.config import build_config
from ouchn_assistant.discovery import collect_dom_structure, run_discovery
from ouchn_assistant.models import RunMode


COURSE_URL = "https://lms.ouchn.cn/course/100/ng"
FIXTURE = Path(__file__).parent / "fixtures" / "discovery_course.html"


@pytest.fixture(scope="module")
def browser_page() -> Page:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(service_workers="block")
        page = context.new_page()
        page.route("**/*", lambda route: route.abort())
        yield page
        context.close()
        browser.close()


def test_collect_dom_structure_captures_bounded_structural_signals(
    browser_page: Page,
) -> None:
    browser_page.set_content(FIXTURE.read_text(encoding="utf-8"))

    result = collect_dom_structure(browser_page)

    main_frame = result["frames"][0]
    assert main_frame["read_allowed"] is True
    assert main_frame["selector_counts"]["subject"] == 2
    assert main_frame["selector_counts"]["sco"] == 1
    assert ["subject-item", 1] in main_frame["class_frequency"]
    assert ["data-activity-id", 1] in main_frame["data_attribute_frequency"]

    activity = next(
        node
        for node in main_frame["candidate_nodes"]
        if node["data"].get("activity_id") == "video-1"
    )
    assert activity["tag"] == "A"
    assert activity["text"] == "第一讲"
    assert activity["data"] == {
        "activity_id": "video-1",
        "type": "video",
        "complete": "true",
    }
    assert main_frame["course_links"] == [
        "/course/100/learning-activity/video-1"
    ]
    assert main_frame["outline_controls"][0]["text"] == "展开目录"
    framework_node = next(
        node
        for node in main_frame["framework_nodes"]
        if "ng-repeat" in node["attributes"]
    )
    assert framework_node["classes"] == ["item"]
    assert framework_node["visible"] is True
    assert framework_node["text"] == "第二讲"
    assert framework_node["attributes"] == {
        "ng-click": "openActivity(learningActivity)",
        "ng-repeat": "learningActivity in section.activities",
    }
    assert "html" not in json.dumps(result).lower()


def test_run_discovery_uses_one_browser_session_and_writes_snapshot(
    tmp_path: Path,
) -> None:
    config = build_config(
        mode=RunMode.INSPECT,
        course_url=COURSE_URL,
        project_root=tmp_path,
    )
    calls = {
        "playwright_enter": 0,
        "browser_open": 0,
        "collector": 0,
        "browser_close": 0,
    }

    class FakePage:
        url = COURSE_URL

        def goto(self, url: str, **_: object) -> None:
            assert url == COURSE_URL

        def wait_for_function(self, _expression: str, **_: object) -> None:
            return None

    class FakeBrowser:
        def __init__(self, _playwright: object, _config: object) -> None:
            pass

        def open(self) -> FakePage:
            calls["browser_open"] += 1
            return FakePage()

        def assert_allowed_page(self, _page: FakePage) -> None:
            return None

        def find_course_page(self) -> FakePage | None:
            return None

        def close(self) -> None:
            calls["browser_close"] += 1

    class FakePlaywrightManager:
        def __enter__(self) -> object:
            calls["playwright_enter"] += 1
            return object()

        def __exit__(self, *_: object) -> None:
            return None

    def collect_once(_page: FakePage) -> dict[str, object]:
        calls["collector"] += 1
        return {
            "page_url": COURSE_URL + "?private=value",
            "page_title": "Course",
            "frames": [],
        }

    output_path = run_discovery(
        config,
        clock=lambda: datetime(2026, 10, 26, 12, tzinfo=timezone.utc),
        run_id_factory=lambda _now: "discovery-run",
        playwright_factory=lambda: FakePlaywrightManager(),
        browser_factory=FakeBrowser,
        collector=collect_once,
    )

    assert calls == {
        "playwright_enter": 1,
        "browser_open": 1,
        "collector": 1,
        "browser_close": 1,
    }
    assert output_path == (
        tmp_path / "runs" / "discovery-run" / "dom_structure.json"
    )
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["course_id"] == "100"
    assert payload["discovery"]["page_url"] == COURSE_URL
    assert not (tmp_path / ".runtime" / "chrome-profile.lock").exists()
