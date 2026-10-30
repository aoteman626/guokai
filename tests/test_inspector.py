from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from ouchn_assistant.inspector import (
    ActivityParseError,
    collect_raw_activities,
    inspect_course,
    normalize_activity,
)
from ouchn_assistant.models import ActivityStatus


FIXTURES = Path(__file__).parent / "fixtures"


def test_normalize_activity_uses_structural_signals() -> None:
    activity = normalize_activity(
        {
            "activity_id": "video-1",
            "title": "第一讲",
            "href": "/course/100/learning-activity/full-screen/video-1",
            "icon_class": "font font-video",
            "aria_label": "音视频教材",
            "complete": "true",
        }
    )

    assert activity.activity_id == "video-1"
    assert activity.type_hint == "video"
    assert activity.status is ActivityStatus.COMPLETE
    assert activity.signals == {
        "aria_label": "音视频教材",
        "complete": "true",
    }


@pytest.mark.parametrize(
    ("complete", "expected"),
    [
        ("false", ActivityStatus.INCOMPLETE),
        ("", ActivityStatus.UNKNOWN),
        ("maybe", ActivityStatus.UNKNOWN),
    ],
)
def test_normalize_activity_does_not_guess_status(
    complete: str,
    expected: ActivityStatus,
) -> None:
    activity = normalize_activity(
        {
            "activity_id": "page-2",
            "title": "Reading",
            "href": "/course/100/activity/page-2?session=secret#part",
            "icon_class": "font font-page",
            "aria_label": "Page",
            "complete": complete,
        }
    )

    assert activity.status is expected
    assert activity.href == "/course/100/activity/page-2"


def test_missing_id_is_rejected() -> None:
    with pytest.raises(ActivityParseError):
        normalize_activity({"title": "No id", "href": ""})


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


@pytest.mark.parametrize(
    ("fixture_name", "activity_id", "expected_status"),
    [
        ("course_zh.html", "video-1", ActivityStatus.COMPLETE),
        ("course_en.html", "page-2", ActivityStatus.INCOMPLETE),
    ],
)
def test_inspect_course_reads_local_fixture_without_opening_activity(
    browser_page: Page,
    fixture_name: str,
    activity_id: str,
    expected_status: ActivityStatus,
) -> None:
    browser_page.set_content(
        (FIXTURES / fixture_name).read_text(encoding="utf-8")
    )

    activities = inspect_course(browser_page)

    assert len(activities) == 1
    assert activities[0].activity_id == activity_id
    assert activities[0].status is expected_status
    assert browser_page.url == "about:blank"


def test_collect_raw_activities_fails_closed_when_structure_is_missing(
    browser_page: Page,
) -> None:
    browser_page.set_content("<main>No course outline</main>")

    with pytest.raises(ActivityParseError):
        collect_raw_activities(browser_page)


def test_optional_signals_can_be_absent(browser_page: Page) -> None:
    browser_page.set_content(
        """
        <section class="learning-activity" data-activity-id="unknown-3">
          <a href="/course/100/activity/unknown-3">
            <span class="activity-title">Unknown activity</span>
          </a>
        </section>
        """
    )

    activities = inspect_course(browser_page)

    assert len(activities) == 1
    assert activities[0].type_hint == "unknown"
    assert activities[0].status is ActivityStatus.UNKNOWN
