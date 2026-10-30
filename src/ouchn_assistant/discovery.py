import argparse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from playwright.sync_api import (
    Error as PlaywrightError,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from ouchn_assistant.artifacts import atomic_write_json, sanitize_url
from ouchn_assistant.browser import (
    BrowserBoundaryError,
    BrowserSession,
    is_allowed_lms_url,
)
from ouchn_assistant.config import AppConfig, build_config
from ouchn_assistant.inspector import ActivityParseError, wait_for_stable_activities
from ouchn_assistant.models import RunMode
from ouchn_assistant.session import LockOwnershipLost, ProfileInUse, ProfileLock


MAX_CANDIDATE_NODES = 500
MAX_FRAMEWORK_NODES = 500
MAX_COURSE_LINKS = 500
MAX_OUTLINE_CONTROLS = 100
MAX_TEXT_LENGTH = 160

Clock = Callable[[], datetime]
RunIdFactory = Callable[[datetime], str]
Collector = Callable[[Page], dict[str, Any]]
PlaywrightFactory = Callable[[], Any]
BrowserFactory = Callable[[Any, AppConfig], BrowserSession]


FRAME_STRUCTURE_SCRIPT = """
() => {
  const selectorCounts = {
    activity: document.querySelectorAll('[class*="activity"]').length,
    catalog: document.querySelectorAll('[class*="catalog"]').length,
    chapter: document.querySelectorAll('[class*="chapter"]').length,
    courseware: document.querySelectorAll('[class*="courseware"]').length,
    data_activity_id: document.querySelectorAll('[data-activity-id]').length,
    data_id: document.querySelectorAll('[data-id]').length,
    outline: document.querySelectorAll('[class*="outline"]').length,
    sco: document.querySelectorAll('[class*="sco"]').length,
    section: document.querySelectorAll('[class*="section"]').length,
    subject: document.querySelectorAll('[class*="subject"]').length,
    treeitem: document.querySelectorAll('[role="treeitem"]').length,
    unit: document.querySelectorAll('[class*="unit"]').length
  };

  const classTokens = Array.from(document.querySelectorAll('[class]'))
    .flatMap((element) =>
      typeof element.className === 'string'
        ? element.className.split(/\\s+/).filter(Boolean)
        : []
    );
  const dataAttributeNames = Array.from(document.querySelectorAll('*'))
    .flatMap((element) =>
      element.getAttributeNames().filter((name) => name.startsWith('data-'))
    );
  const candidateSelector = [
    '[class*="activity"]',
    '[class*="catalog"]',
    '[class*="chapter"]',
    '[class*="course"]',
    '[class*="courseware"]',
    '[class*="folder"]',
    '[class*="learning"]',
    '[class*="lesson"]',
    '[class*="module"]',
    '[class*="node"]',
    '[class*="outline"]',
    '[class*="resource"]',
    '[class*="sco"]',
    '[class*="section"]',
    '[class*="subject"]',
    '[class*="syllabus"]',
    '[class*="task"]',
    '[class*="unit"]',
    '[data-activity-id]',
    '[data-id]',
    '[role="treeitem"]'
  ].join(',');
  const structuralPattern =
    /activity|catalog|chapter|course|folder|learning|lesson|module|node|outline|resource|sco|section|subject|syllabus|task|unit/i;

  const candidateNodes = Array.from(document.querySelectorAll(candidateSelector))
    .slice(0, 500)
    .map((element) => {
      const className =
        typeof element.className === 'string' ? element.className : '';
      const structural = structuralPattern.test(className);
      const parent = element.parentElement;
      return {
        tag: element.tagName,
        element_id: element.id || '',
        classes: className.split(/\\s+/).filter(Boolean).slice(0, 16),
        role: element.getAttribute('role') || '',
        aria_expanded: element.getAttribute('aria-expanded') || '',
        href: element.getAttribute('href') || '',
        visible: element.getClientRects().length > 0,
        data: {
          id: element.getAttribute('data-id') || '',
          activity_id: element.getAttribute('data-activity-id') || '',
          type: element.getAttribute('data-type') || '',
          complete: element.getAttribute('data-complete') || '',
          status: element.getAttribute('data-status') || ''
        },
        text: structural
          ? (element.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 160)
          : '',
        parent: parent
          ? {
              tag: parent.tagName,
              classes:
                typeof parent.className === 'string'
                  ? parent.className.split(/\\s+/).filter(Boolean).slice(0, 12)
                  : []
            }
          : null,
        children: Array.from(element.children)
          .slice(0, 12)
          .map((child) => ({
            tag: child.tagName,
            classes:
              typeof child.className === 'string'
                ? child.className.split(/\\s+/).filter(Boolean).slice(0, 12)
                : []
          }))
      };
    });

  const frameworkAttributeNames = [
    'data-ng-click',
    'data-ng-repeat',
    'ng-bind',
    'ng-class',
    'ng-click',
    'ng-if',
    'ng-repeat',
    'ng-show',
    'ui-sref',
    'x-ng-repeat'
  ];
  const frameworkSelector = frameworkAttributeNames
    .map((name) => `[${name}]`)
    .join(',');
  const frameworkNodes = Array.from(
    document.querySelectorAll(frameworkSelector)
  )
    .slice(0, 500)
    .map((element) => {
      const attributes = {};
      for (const name of frameworkAttributeNames) {
        const value = element.getAttribute(name);
        if (value !== null) {
          attributes[name] = value.slice(0, 300);
        }
      }
      return {
        tag: element.tagName,
        element_id: element.id || '',
        classes:
          typeof element.className === 'string'
            ? element.className.split(/\\s+/).filter(Boolean).slice(0, 16)
            : [],
        attributes,
        role: element.getAttribute('role') || '',
        href: element.getAttribute('href') || '',
        visible: element.getClientRects().length > 0,
        text:
          element.getClientRects().length > 0
            ? (element.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 160)
            : '',
        parent: element.parentElement
          ? {
              tag: element.parentElement.tagName,
              classes:
                typeof element.parentElement.className === 'string'
                  ? element.parentElement.className
                      .split(/\\s+/)
                      .filter(Boolean)
                      .slice(0, 12)
                  : []
            }
          : null
      };
    });

  const courseLinks = Array.from(document.querySelectorAll('a[href]'))
    .map((link) => link.getAttribute('href') || '')
    .filter((href) => /\\/course\\/|learning-activity|sco/i.test(href))
    .slice(0, 500);

  const outlineControls = Array.from(
    document.querySelectorAll('button,[role="button"]')
  )
    .filter((element) => {
      const className =
        typeof element.className === 'string' ? element.className : '';
      const signal = [
        className,
        element.getAttribute('aria-label') || '',
        element.textContent || ''
      ].join(' ');
      return /目录|展开|收起|outline|catalog/i.test(signal);
    })
    .slice(0, 100)
    .map((element) => ({
      tag: element.tagName,
      classes:
        typeof element.className === 'string'
          ? element.className.split(/\\s+/).filter(Boolean).slice(0, 16)
          : [],
      role: element.getAttribute('role') || '',
      aria_expanded: element.getAttribute('aria-expanded') || '',
      text: (element.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 160)
    }));

  return {
    element_count: document.querySelectorAll('*').length,
    selector_counts: selectorCounts,
    class_tokens: classTokens,
    data_attribute_names: dataAttributeNames,
    candidate_nodes: candidateNodes,
    framework_nodes: frameworkNodes,
    course_links: courseLinks,
    outline_controls: outlineControls
  };
}
"""


def collect_dom_structure(page: Page) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    for frame in page.frames:
        read_allowed = (
            frame is page.main_frame
            or frame.url in ("", "about:blank")
            or is_allowed_lms_url(frame.url)
        )
        if not read_allowed:
            frames.append(
                {
                    "url": sanitize_url(frame.url),
                    "read_allowed": False,
                }
            )
            continue

        frames.append(_collect_frame_structure(frame))

    return {
        "page_url": sanitize_url(page.url),
        "page_title": page.title(),
        "frames": frames,
    }


def run_discovery(
    config: AppConfig,
    *,
    input_fn: Callable[[str], str] = input,
    clock: Clock | None = None,
    run_id_factory: RunIdFactory | None = None,
    playwright_factory: PlaywrightFactory = sync_playwright,
    browser_factory: BrowserFactory = BrowserSession,
    collector: Collector = collect_dom_structure,
) -> Path:
    effective_clock = clock or _utc_now
    started_at = effective_clock().astimezone(timezone.utc)
    run_id = (run_id_factory or _new_run_id)(started_at)
    output_path = config.runs_dir / run_id / "dom_structure.json"
    lock = ProfileLock(config.profile_dir.parent / "chrome-profile.lock")

    with lock:
        with playwright_factory() as playwright:
            browser = browser_factory(playwright, config)
            try:
                page = _open_course_page(browser, config, input_fn)
                directory_ready = False
                directory_reason = ""
                try:
                    wait_for_stable_activities(page)
                    directory_ready = True
                except (ActivityParseError, PlaywrightTimeoutError) as error:
                    # Keep a diagnostic snapshot of unknown/unfinished layouts,
                    # but never label the directory as successfully loaded.
                    directory_reason = str(error)
                    print(f"目录未确认就绪，保留不完整探查快照：{error}", flush=True)
                browser.assert_allowed_page(page)
                discovery = collector(page)
                snapshot = {
                    "schema_version": 1,
                    "run_id": run_id,
                    "course_id": config.course_id,
                    "observed_at": _format_utc(effective_clock()),
                    "status": "completed" if directory_ready else "incomplete",
                    "directory_ready": directory_ready,
                    "directory_reason": directory_reason,
                    "coverage": "expanded_dom_observed" if directory_ready else "unconfirmed_dom",
                    "filter": "unfinished" if directory_ready else "unconfirmed",
                    "discovery": discovery,
                }
                atomic_write_json(output_path, snapshot)
            finally:
                browser.close()

    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="discover-ouchn-dom")
    parser.add_argument("--course-url", required=True)
    arguments = parser.parse_args(argv)

    try:
        config = build_config(
            mode=RunMode.INSPECT,
            course_url=arguments.course_url,
            project_root=Path(__file__).resolve().parents[2],
        )
        output_path = run_discovery(config)
    except ValueError as error:
        return _report_error(2, error)
    except (ProfileInUse, LockOwnershipLost) as error:
        return _report_error(3, error)
    except (BrowserBoundaryError, PlaywrightError) as error:
        return _report_error(4, error)
    except OSError as error:
        return _report_error(6, error)

    print(output_path)
    return 0


def _collect_frame_structure(frame: Frame) -> dict[str, Any]:
    raw = frame.evaluate(FRAME_STRUCTURE_SCRIPT)
    return {
        "url": sanitize_url(frame.url),
        "read_allowed": True,
        "element_count": raw["element_count"],
        "selector_counts": raw["selector_counts"],
        "class_frequency": _frequency(raw["class_tokens"], limit=500),
        "data_attribute_frequency": _frequency(
            raw["data_attribute_names"],
            limit=50,
        ),
        "candidate_nodes": [
            _sanitize_node(node)
            for node in raw["candidate_nodes"][:MAX_CANDIDATE_NODES]
        ],
        "framework_nodes": [
            _sanitize_node(node)
            for node in raw["framework_nodes"][:MAX_FRAMEWORK_NODES]
        ],
        "course_links": _sanitize_links(
            raw["course_links"][:MAX_COURSE_LINKS]
        ),
        "outline_controls": [
            _sanitize_node(control)
            for control in raw["outline_controls"][:MAX_OUTLINE_CONTROLS]
        ],
    }


def _open_course_page(
    browser: BrowserSession,
    config: AppConfig,
    input_fn: Callable[[str], str],
) -> Page:
    return browser.open_course(input_fn=input_fn)


def _frequency(values: Sequence[str], limit: int) -> list[list[Any]]:
    return [
        [value, count]
        for value, count in sorted(
            Counter(values).items(),
            key=lambda item: (-item[1], item[0]),
        )[:limit]
    ]


def _sanitize_links(links: Sequence[str]) -> list[str]:
    sanitized: list[str] = []
    for link in links:
        clean_link = sanitize_url(link)
        if clean_link and clean_link not in sanitized:
            sanitized.append(clean_link)
    return sanitized


def _sanitize_node(node: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = _remove_empty_values(node)
    if isinstance(sanitized, dict) and "href" in sanitized:
        sanitized["href"] = sanitize_url(sanitized["href"])
    return sanitized


def _remove_empty_values(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _remove_empty_values(item)
            for key, item in value.items()
            if item not in ("", None, [], {})
        }
    if isinstance(value, list):
        return [_remove_empty_values(item) for item in value]
    if isinstance(value, str):
        return value[:MAX_TEXT_LENGTH]
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_run_id(now: datetime) -> str:
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:6]}"


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _report_error(exit_code: int, error: BaseException) -> int:
    import sys

    print(str(error), file=sys.stderr)
    return exit_code
