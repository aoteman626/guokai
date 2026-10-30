import re
from collections.abc import Mapping
from dataclasses import dataclass
from time import monotonic

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from ouchn_assistant.artifacts import sanitize_url
from ouchn_assistant.models import Activity, ActivityStatus
from ouchn_assistant import selectors
from ouchn_assistant.ui_actions import UiActionError, delayed_click


ACTIVITY_SELECTOR = selectors.ACTIVITY
CLICKABLE_ACTIVITY_SELECTOR = selectors.CLICKABLE_ACTIVITY
STABLE_SCAN_ROUNDS = 3
SCROLL_WAIT_MS = 750
DIRECTORY_TIMEOUT_MS = 30_000


class ActivityParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CourseDirectory:
    """Keep hidden states for confirmation; select targets only from visible nodes."""

    activities: tuple[Activity, ...]

    @property
    def visible_activities(self) -> list[Activity]:
        return [
            activity for activity in self.activities
            if activity.signals.get("visible") is True
        ]

    @property
    def unfinished_videos(self) -> list[Activity]:
        return [
            activity for activity in self.visible_activities
            if activity.type_hint == "online_video"
            and activity.status is ActivityStatus.INCOMPLETE
        ]

    @property
    def unfinished_resources(self) -> list[Activity]:
        return [
            activity for activity in self.visible_activities
            if activity.type_hint == "external_course_resources"
            and activity.status is ActivityStatus.INCOMPLETE
        ]

    @property
    def unfinished_pages(self) -> list[Activity]:
        return [
            activity for activity in self.visible_activities
            if activity.type_hint == "page"
            and activity.status is ActivityStatus.INCOMPLETE
        ]

    def find(self, activity_id: str) -> Activity | None:
        return next(
            (item for item in self.activities if item.activity_id == activity_id),
            None,
        )


# Only public DOM and visible UI fields are read. No framework state or actions.
READ_ACTIVITIES = """
(selector) => Array.from(document.querySelectorAll(selector)).map((node, index) => {
    const icon = node.querySelector('.activity-icon i');
    const title = node.querySelector('.activity-title .title, a.title');
    const module = node.closest('.module');
    const section = node.closest('.learning-activities')
        ?.parentElement?.closest('.syllabus-content')?.parentElement;
    const locked = node.classList.contains('locked')
        || !!node.querySelector('.locked');
    const prerequisitePopup = document.querySelector(
        `#prerequisites-confirmation-popup-${node.id.replace('learning-activity-', '')}, `
        + `#prerequisites-confirmation-popup-online_video-${node.id.replace('learning-activity-', '')}`
    );
    return {
        activity_id: node.id.startsWith('learning-activity-')
            ? node.id.slice('learning-activity-'.length) : '',
        element_id: node.id,
        index: node.getAttribute('data-index') || String(index),
        title: (title?.textContent || '').trim(),
        href: '',
        icon_class: icon?.getAttribute('class') || '',
        aria_label: icon?.getAttribute('aria-label')
            || icon?.getAttribute('original-title')
            || icon?.getAttribute('title') || '',
        raw_type: node.getAttribute('ng-switch') || '',
        completion_text: (node.querySelector('.ivu-tooltip-inner')?.textContent || '').trim(),
        completeness_class: node.querySelector('.completeness')?.className || '',
        locked: locked ? 'true' : 'false',
        prerequisite_text: prerequisitePopup
            ? (prerequisitePopup.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 300)
            : '',
        visible: node.getClientRects().length > 0
            && getComputedStyle(node).visibility !== 'hidden' ? 'true' : 'false',
        module_title: (module?.querySelector('.module-name')?.textContent || '').trim(),
        section_title: (section?.querySelector('.syllabus-header .title')?.textContent || '').trim()
    };
})
"""


def normalize_activity(raw: Mapping[str, str]) -> Activity:
    activity_id = raw.get("activity_id", "").strip()
    if not activity_id.isdigit():
        raise ActivityParseError("Activity is missing a valid stable id")
    complete = raw.get("complete", "").strip().lower()
    status = {
        "true": ActivityStatus.COMPLETE,
        "false": ActivityStatus.INCOMPLETE,
    }.get(complete, ActivityStatus.UNKNOWN)
    return Activity(
        activity_id=activity_id,
        title=raw.get("title", "").strip(),
        href=sanitize_url(raw.get("href", "").strip()),
        type_hint=_type_hint(raw),
        status=status,
        signals={
            "aria_label": raw.get("aria_label", "").strip(),
            "complete": complete,
            "completion_text": raw.get("completion_text", "").strip(),
            "element_id": raw.get("element_id", "").strip(),
            "index": raw.get("index", "").strip(),
            "locked": raw.get("locked", "") == "true",
            "prerequisite_text": raw.get("prerequisite_text", "").strip(),
            "raw_type": raw.get("raw_type", "").strip(),
            "visible": raw.get("visible", "") == "true",
            "module_title": raw.get("module_title", "").strip(),
            "section_title": raw.get("section_title", "").strip(),
        },
    )


def _read_raw_activities(page: Page) -> list[dict[str, str]]:
    raw = page.evaluate(READ_ACTIVITIES, ACTIVITY_SELECTOR)
    if not raw:
        raise ActivityParseError("目录节点尚未加载，不能判断视频已经全部完成")
    ids: set[str] = set()
    for activity in raw:
        activity_id = activity["activity_id"]
        if not activity_id.isdigit() or activity_id in ids:
            raise ActivityParseError("目录活动 ID 缺失或重复")
        ids.add(activity_id)
        if not activity["title"]:
            raise ActivityParseError(f"目录活动尚未加载标题：{activity_id}")
        activity["complete"] = _completion_value(
            activity["completion_text"], activity.pop("completeness_class"),
        )
    return raw


def collect_raw_activities(page: Page) -> list[dict[str, str]]:
    filter_unfinished_activities(page)
    expand_all_activities(page)
    wait_for_activity_list(page)
    return _read_raw_activities(page)


def read_course_directory(page: Page) -> CourseDirectory:
    return CourseDirectory(tuple(
        normalize_activity(raw) for raw in wait_for_stable_activities(page)
    ))


def inspect_course(page: Page) -> list[Activity]:
    return read_course_directory(page).visible_activities


def expand_all_activities(page: Page) -> None:
    control = page.locator(selectors.EXPAND_ALL).first

    def needs_expansion() -> bool:
        label = control.inner_text().strip()
        if "全部收起" in label:
            return False
        if "全部展开" not in label:
            raise ActivityParseError("无法识别目录展开控件")
        return True

    try:
        control.wait_for(state="visible", timeout=DIRECTORY_TIMEOUT_MS)
        if not needs_expansion():
            return
        delayed_click(
            page, control, action="展开目录", timeout_ms=5_000,
            should_click=needs_expansion,
        )
        page.wait_for_function(
            "(selector) => document.querySelector(selector)?.textContent.includes('全部收起')",
            arg=selectors.EXPAND_ALL,
            timeout=10_000,
        )
    except PlaywrightTimeoutError as error:
        raise ActivityParseError("课程目录展开失败或未准备完成") from error
    except UiActionError as error:
        raise ActivityParseError(str(error)) from error


def filter_unfinished_activities(page: Page) -> None:
    # Wait for the directory before choosing between known checkbox layouts.
    page.locator(selectors.EXPAND_ALL).first.wait_for(
        state="visible", timeout=DIRECTORY_TIMEOUT_MS,
    )
    checkbox = page.locator(selectors.INCOMPLETE_FILTER).first
    if checkbox.count() == 0:
        checkbox = page.locator(selectors.INCOMPLETE_FILTER_FALLBACK).first
    try:
        checkbox.wait_for(state="visible", timeout=10_000)
        if not checkbox.is_checked():
            delayed_click(
                page, checkbox, action="勾选未完成", timeout_ms=5_000,
                should_click=lambda: not checkbox.is_checked(),
            )
        if not checkbox.is_checked():
            raise ActivityParseError("未完成筛选未成功勾选")
    except PlaywrightTimeoutError as error:
        raise ActivityParseError("无法操作未完成筛选") from error
    except UiActionError as error:
        raise ActivityParseError(str(error)) from error


def wait_for_activity_list(page: Page, timeout_ms: int = DIRECTORY_TIMEOUT_MS) -> None:
    try:
        # Completed nodes remain attached when filtered out. Do not require visibility.
        page.locator(ACTIVITY_SELECTOR).first.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeoutError as error:
        raise ActivityParseError("目录没有可识别活动，可能为空课程或页面结构不匹配") from error


def wait_for_stable_activities(page: Page) -> list[dict[str, str]]:
    filter_unfinished_activities(page)
    expand_all_activities(page)
    wait_for_activity_list(page)
    deadline = monotonic() + DIRECTORY_TIMEOUT_MS / 1000
    previous: list[dict[str, str]] | None = None
    stable_rounds = 0
    while monotonic() < deadline:
        try:
            current = _read_raw_activities(page)
        except ActivityParseError:
            previous = None
            stable_rounds = 0
        else:
            if current == previous:
                stable_rounds += 1
            else:
                stable_rounds = 0
            previous = current
            if stable_rounds >= STABLE_SCAN_ROUNDS:
                return current
        page.wait_for_timeout(SCROLL_WAIT_MS)
    raise ActivityParseError("课程目录在限定时间内未稳定加载")


def _activity_id_from_element_id(element_id: str) -> str:
    prefix = "learning-activity-"
    return element_id.removeprefix(prefix) if element_id.startswith(prefix) else ""


def _completion_value(completion_text: str, completeness_class: str) -> str:
    tokens = set(completeness_class.lower().split())
    if "full" in tokens or "已完成" in completion_text:
        return "true"
    if tokens.intersection({"part", "none"}) or "未完成" in completion_text or "部分完成" in completion_text:
        return "false"
    return ""


def _type_hint(raw: Mapping[str, str]) -> str:
    label_type = _type_hint_from_label(raw.get("aria_label", "").strip())
    if label_type != "unknown":
        return label_type
    raw_type = raw.get("raw_type", "").strip()
    if raw_type and "." not in raw_type:
        return raw_type
    icon_type = _type_hint_from_icon_class(raw.get("icon_class", ""))
    return "online_video" if icon_type in {"video", "online_video"} else icon_type


def _type_hint_from_label(label: str) -> str:
    return {
        "页面": "page", "音视频教材": "online_video",
        "测验": "exam", "测试": "exam", "参考资料": "material",
        "作业": "homework", "讨论": "forum", "线上链接": "web_link",
        "外部资源": "external_course_resources",
    }.get(label, "unknown")


def _type_hint_from_icon_class(icon_class: str) -> str:
    prefix = "font-syllabus-"
    for token in re.split(r"\s+", icon_class.strip()):
        if token.startswith(prefix) and len(token) > len(prefix):
            return token.removeprefix(prefix).replace("-", "_")
    return "unknown"
