"""Single-course execution through UI clicks and read-only playback observation."""

import re
import sys
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

from playwright.sync_api import (
    Error as PlaywrightError, Frame, Page,
    TimeoutError as PlaywrightTimeoutError,
)

from ouchn_assistant import selectors
from ouchn_assistant.artifacts import atomic_write_json, sanitize_error_message, sanitize_url
from ouchn_assistant.browser import (
    is_activity_url, is_allowed_lms_url, is_course_home_url,
)
from ouchn_assistant.config import AppConfig
from ouchn_assistant.inspector import ActivityParseError, CourseDirectory, read_course_directory
from ouchn_assistant.models import Activity, ActivityStatus, SessionState, VideoState
from ouchn_assistant.ui_actions import UiActionError, delayed_click


class AutomationPaused(RuntimeError):
    """The page needs user attention; do not guess or force actions."""


EventSink = Callable[[SessionState, str, dict], None]

PAUSED_RECOVERY_TIMEOUT_SECONDS = 90
PAUSED_RECOVERY_POLL_MS = 500

READ_VIDEO = """
(media) => ({
    paused: media.paused,
    ended: media.ended,
    current_time: media.currentTime,
    duration: Number.isFinite(media.duration) ? media.duration : 0,
    playback_rate: media.playbackRate,
    media_kind: media.tagName.toLowerCase(),
    error_code: media.error?.code || null,
    ready_state: media.readyState,
    network_state: media.networkState,
    buffered_end: media.buffered.length
        ? media.buffered.end(media.buffered.length - 1) : 0
})
"""

READ_SCROLL_METRICS = """
() => {
    const root = document.scrollingElement || document.documentElement;
    const visible = (element) => element.getClientRects().length > 0
        && getComputedStyle(element).visibility !== 'hidden';
    const containers = Array.from(document.querySelectorAll('*'))
        .filter(element => element !== root
            && visible(element)
            && element.clientHeight > 0
            && element.scrollHeight > element.clientHeight + 8
            && ['auto', 'scroll'].includes(getComputedStyle(element).overflowY))
        .slice(0, 40)
        .map(element => {
            const rect = element.getBoundingClientRect();
            return {
                id: element.id || '',
                class_name: typeof element.className === 'string'
                    ? element.className.slice(0, 160) : '',
                left: rect.left,
                top: rect.top,
                width: rect.width,
                height: rect.height,
                scroll_top: element.scrollTop,
                client_height: element.clientHeight,
                scroll_height: element.scrollHeight
            };
        });
    return {
        root: {
            scroll_top: root.scrollTop,
            client_height: window.innerHeight,
            scroll_height: Math.max(
                root.scrollHeight, document.body?.scrollHeight || 0
            )
        },
        viewport_width: window.innerWidth,
        viewport_height: window.innerHeight,
        body_text_length: (document.body?.innerText || '').trim().length,
        iframe_count: [...document.querySelectorAll('iframe')]
            .filter(visible).length,
        containers
    };
}
"""

READ_RESOURCE_FILES = """
() => {
    const visible = (element) => element.getClientRects().length > 0
        && getComputedStyle(element).visibility !== 'hidden';
    const roots = [...document.querySelectorAll(
        '.reveal-modal.open, [role="dialog"], .popup-area, '
        + '[class*="file-list"], [class*="resource-list"]'
    )].filter(visible);
    const scopes = roots.length ? roots : [document.body];
    const nodes = [...new Set(scopes.flatMap(scope => [
        ...scope.querySelectorAll('a[href], button, [role="button"], '
            + '[data-file-id], [data-file-url]')
    ]))].filter(visible);
    return nodes.map((node, index) => {
        const text = (node.textContent || '').trim().replace(/\\s+/g, ' ');
        const href = node.getAttribute('href') || '';
        const classes = typeof node.className === 'string'
            ? node.className : '';
        const dataId = node.getAttribute('data-file-id') || '';
        const dataUrl = node.getAttribute('data-file-url') || '';
        const signal = `${text} ${href} ${classes} ${dataId} ${dataUrl}`;
        if (/关闭|返回|取消|确定|查看文件/.test(text)
            || !/文件|视频|资料|资源|\\.pdf\\b|\\.docx?\\b|\\.pptx?\\b|\\.xlsx?\\b|\\.mp4\\b|\\.m[ko]v\\b|\\.avi\\b/i.test(signal)) {
            return null;
        }
        let selector = '';
        if (node.id) {
            selector = `#${CSS.escape(node.id)}`;
        } else if (dataId) {
            selector = `[data-file-id="${CSS.escape(dataId)}"]`;
        }
        return {
            index,
            selector,
            text: text.slice(0, 300),
            href: href.slice(0, 500),
            classes: classes.slice(0, 300),
            data_id: dataId
        };
    }).filter(Boolean);
}
"""

READ_VISIBLE_DIALOGS = """
(selector) => [...document.querySelectorAll(selector)]
    .filter(node => node.getClientRects().length > 0
        && getComputedStyle(node).visibility !== 'hidden')
    .map(node => ({
        id: node.id || '',
        classes: typeof node.className === 'string' ? node.className : '',
        role: node.getAttribute('role') || '',
        aria_label: node.getAttribute('aria-label') || '',
        text: (node.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 1000)
    }))
"""


class CourseRunner:
    def __init__(
        self, page: Page, config: AppConfig, run_dir: Path, emit: EventSink,
    ) -> None:
        self.page = page
        self.config = config
        self.run_dir = run_dir
        self.emit = emit
        self.results: list[dict] = []
        self.current_activity: Activity | None = None
        self.remaining: list[str] = []
        self.inspected_resource_ids: set[str] = set()
        self.inspected_page_ids: set[str] = set()
        self.skipped_page_ids: set[str] = set()
        self.video_frame: Frame | None = None
        self.phase = "starting"
        self.video_state: VideoState | None = None

    def run(self) -> Path:
        status = "completed"
        reason = ""
        error_type: str | None = None
        active_error: BaseException | None = None
        self._checkpoint("starting")
        try:
            self._run_loop()
        except KeyboardInterrupt as error:
            status, reason = "interrupted", "用户中断"
            error_type, active_error = type(error).__name__, error
            raise
        except (AutomationPaused, ActivityParseError, PlaywrightTimeoutError, UiActionError) as error:
            status, reason = "paused", sanitize_error_message(error)
            error_type = type(error).__name__
            active_error = AutomationPaused(reason)
            self._capture_pause_diagnostics(reason, error_type)
            raise active_error from error
        except PlaywrightError as error:
            error_type = type(error).__name__
            if self.page.is_closed():
                status, reason = "paused", "页面或浏览器已关闭，自动化停止；重新运行时读取实时目录"
                active_error = AutomationPaused(reason)
                raise active_error from error
            status, reason, active_error = "failed", sanitize_error_message(error), error
            self._capture_pause_diagnostics(reason, error_type)
            raise
        except BaseException as error:
            status, reason = "failed", sanitize_error_message(error) or type(error).__name__
            error_type, active_error = type(error).__name__, error
            raise
        finally:
            # Keep the last execution phase, even when the terminal status changes.
            final_payload = {
                "schema_version": 1, "run_id": self.run_dir.name,
                "course_id": self.config.course_id,
                "status": status, "reason": reason, "finished_at": _now(),
                "last_phase": self.phase, "error_type": error_type,
                "current_activity_id": (
                    self.current_activity.activity_id if self.current_activity else None
                ),
                "last_video_state": asdict(self.video_state) if self.video_state else None,
                "inspected_resource_ids": sorted(self.inspected_resource_ids),
                "inspected_page_ids": sorted(self.inspected_page_ids),
                "skipped_page_ids": sorted(self.skipped_page_ids),
                "results": self.results,
            }
            # Try both outputs independently; an I/O error must not hide the original cause.
            for output in ("checkpoint.json", "assist_results.json"):
                try:
                    if output == "checkpoint.json":
                        self._checkpoint(self.phase, status=status, reason=reason)
                    else:
                        atomic_write_json(self.run_dir / output, final_payload)
                except OSError as write_error:
                    if active_error is None:
                        active_error = write_error
                        final_payload.update(
                            status="failed", reason=str(write_error),
                            error_type=type(write_error).__name__,
                        )
                    else:
                        active_error.add_note(f"保存 {output} 失败：{write_error}")
                    print(f"保存 {output} 失败：{write_error}", file=sys.stderr, flush=True)
            if active_error is not None and status == "completed":
                raise active_error
        return self.run_dir / "assist_results.json"

    def _run_loop(self) -> None:
        directory = self._scan_directory()
        while True:
            visible = directory.visible_activities
            hidden_unfinished = [
                activity for activity in directory.activities
                if activity.type_hint in {
                    "online_video", "external_course_resources",
                }
                and activity.status is ActivityStatus.INCOMPLETE
                and activity.signals.get("visible") is not True
            ]
            if hidden_unfinished:
                raise AutomationPaused(
                    "全部展开后仍有隐藏的未完成活动，可能存在未展开学习区或特殊目录结构"
                )
            uncertain = [
                activity for activity in visible
                if activity.type_hint == "unknown"
                or activity.status is ActivityStatus.UNKNOWN
            ]
            if uncertain:
                raise AutomationPaused("目录存在无法识别的活动或视频完成态，请人工核对")

            pending: list[tuple[str, Activity]] = []
            for activity in visible:
                if activity.status is not ActivityStatus.INCOMPLETE:
                    continue
                if (
                    activity.type_hint == "external_course_resources"
                    and activity.activity_id not in self.inspected_resource_ids
                ):
                    pending.append(("resource", activity))
                elif activity.type_hint == "page":
                    if (
                        activity.activity_id not in self.inspected_page_ids
                        and activity.activity_id not in self.skipped_page_ids
                    ):
                        pending.append(("page", activity))
                elif activity.type_hint == "online_video":
                    pending.append(("video", activity))

            if pending:
                kind, activity = pending[0]
                self.current_activity = activity
                self.video_state = None
                self.video_frame = None
                if activity.signals.get("locked") is True:
                    details = activity.signals.get("prerequisite_text", "")
                    reason = f"当前{kind}活动被锁定，可能需要先完成前置条件"
                    if details:
                        reason = f"{reason}：{details}"
                    raise AutomationPaused(reason)
                if kind == "resource":
                    directory = self._inspect_resource(activity)
                    continue
                if kind == "page":
                    directory = self._read_text_page(activity)
                    continue

                videos = directory.unfinished_videos
                self.remaining = [video.activity_id for video in videos]
                self._checkpoint("selected")
                self.emit(SessionState.PLAYING, "video_selected", {
                    "activity_id": activity.activity_id, "title": activity.title,
                    "module_title": activity.signals.get("module_title", ""),
                    "remaining_video_count": len(videos),
                })
                self._open_and_play(activity)
                final_state = self._wait_for_video_end(activity)
                result = {
                    "kind": "video",
                    "activity_id": activity.activity_id, "title": activity.title,
                    "playback_ended": True, "platform_complete": False,
                    "current_time": final_state.current_time,
                    "duration": final_state.duration, "ended_at": _now(),
                }
                self.results.append(result)
                self._checkpoint("playback_ended")
                self.emit(SessionState.RETURNING, "video_ended", {
                    "activity_id": activity.activity_id,
                    "current_time": final_state.current_time,
                    "duration": final_state.duration,
                })
                self._return_to_course(activity)
                directory = self._confirm_completion(activity)
                result["platform_complete"] = True
                result["confirmed_at"] = _now()
                self._checkpoint("platform_complete")
                self.emit(SessionState.READY, "platform_completion_confirmed", {
                    "activity_id": activity.activity_id,
                })
                continue

            pending_pages = [
                page for page in directory.unfinished_pages
                if page.activity_id not in self.skipped_page_ids
            ]
            if directory.unfinished_resources or pending_pages:
                raise AutomationPaused(
                    "活动已完成页面探查，但平台仍未明确记录全部完成；"
                    "不会将探查结果推断为平台完成"
                )
            self.current_activity = None
            self.emit(SessionState.READY, "no_unfinished_videos", {
                "observed_activity_count": len(directory.activities),
                "remaining_video_count": 0,
                "remaining_page_count": len(pending_pages),
                "skipped_page_count": len(self.skipped_page_ids),
            })
            return

    def _assert_home(self) -> None:
        if not is_course_home_url(self.page.url, self.config):
            raise AutomationPaused("页面已离开指定课程目录，可能登录失效或用户切换了页面")
        self._assert_no_dialog()

    def _assert_activity(self, activity: Activity) -> None:
        if not is_activity_url(self.page.url, self.config, activity.activity_id):
            raise AutomationPaused("页面已离开当前视频，暂停自动化")
        self._assert_no_dialog()

    def _assert_no_dialog(self) -> None:
        if self.page.locator(selectors.BLOCKING_DIALOG).count():
            raise AutomationPaused("页面出现确认或提示弹窗，请人工处理后重新运行")

    def _scan_directory(self) -> CourseDirectory:
        self._assert_home()
        self._checkpoint("scanning_directory")
        self.emit(SessionState.INSPECTING, "directory_scan_started", {})
        directory = read_course_directory(self.page)
        self._assert_home()
        self.remaining = [
            item.activity_id
            for item in (*directory.unfinished_resources, *directory.unfinished_videos)
        ]
        # Preserve hidden states as evidence, but expose the visible set separately.
        atomic_write_json(self.run_dir / "activities_snapshot.json", {
            "schema_version": 2, "run_id": self.run_dir.name,
            "course_id": self.config.course_id,
            "observed_at": _now(), "filter": "unfinished",
            "coverage": "expanded_dom_observed",
            "observed_activity_count": len(directory.activities),
            "visible_activity_count": len(directory.visible_activities),
            "unfinished_resource_count": len(directory.unfinished_resources),
            "unfinished_video_count": len(directory.unfinished_videos),
            "activities": [item.to_dict() for item in directory.visible_activities],
            "all_observed_activities": [item.to_dict() for item in directory.activities],
        })
        self.emit(SessionState.READY, "directory_scan_completed", {
            "remaining_resource_count": len(directory.unfinished_resources),
            "remaining_video_count": len(directory.unfinished_videos),
        })
        return directory

    def _inspect_resource(self, resource: Activity) -> CourseDirectory:
        self._checkpoint("resource_selected")
        self.emit(SessionState.PLAYING, "resource_selected", {
            "activity_id": resource.activity_id,
            "title": resource.title,
            "module_title": resource.signals.get("module_title", ""),
        })
        self._open_resource(resource)
        kind = self._wait_for_resource_kind(resource)
        if kind == "unknown":
            raise AutomationPaused(
                "资源页面包含无法安全判断的视频嵌入或外部内容，已停止并保留现场"
            )

        if kind == "file_list":
            self._process_resource_files(resource)
            result = {
                "kind": "external_resource_files",
                "activity_id": resource.activity_id,
                "title": resource.title,
                "inspected": True,
                "platform_complete": False,
                "inspected_at": _now(),
            }
            self.results.append(result)
        elif kind == "video":
            self.emit(SessionState.PLAYING, "resource_video_detected", {
                "activity_id": resource.activity_id,
            })
            self._start_visible_video(resource, resource_surface=True)
            final_state = self._wait_for_video_end(
                resource, resource_surface=True,
            )
            result = {
                "kind": "external_resource_video",
                "activity_id": resource.activity_id,
                "title": resource.title,
                "playback_ended": True,
                "platform_complete": False,
                "current_time": final_state.current_time,
                "duration": final_state.duration,
                "ended_at": _now(),
            }
            self.results.append(result)
            self._checkpoint("resource_playback_ended")
            self._exit_resource(resource)
        else:
            self.emit(SessionState.PLAYING, "resource_non_video_detected", {
                "activity_id": resource.activity_id,
            })
            self._scroll_page_to_bottom(resource, resource_surface=True)
            self._exit_resource(resource)
            result = {
                "kind": "external_resource_document",
                "activity_id": resource.activity_id,
                "title": resource.title,
                "read_to_bottom": True,
                "platform_complete": False,
                "read_at": _now(),
            }
            self.results.append(result)

        self.inspected_resource_ids.add(resource.activity_id)
        directory = self._scan_directory()
        observed = directory.find(resource.activity_id)
        if observed is not None and observed.status is ActivityStatus.COMPLETE:
            result["platform_complete"] = True
            result["confirmed_at"] = _now()
        self._checkpoint("resource_inspected")
        self.emit(SessionState.READY, "resource_inspected", {
            "activity_id": resource.activity_id,
            "content_kind": kind,
            "platform_complete": result["platform_complete"],
        })
        return directory

    def _read_text_page(self, activity: Activity) -> CourseDirectory:
        self._checkpoint("page_selected")
        self.emit(SessionState.PLAYING, "page_selected", {
            "activity_id": activity.activity_id,
            "title": activity.title,
            "module_title": activity.signals.get("module_title", ""),
        })
        self._open_activity_surface(activity, action="进入文本页面")
        if self.page.locator("video:visible, audio:visible").count() > 0:
            raise AutomationPaused(
                "文本页面包含可见视频，已停止自动判断；请人工确认页面内容"
            )
        try:
            self._scroll_page_to_bottom(activity)
        except AutomationPaused as error:
            if str(error) != "无法读取引用文件 iframe 的滚动状态":
                raise
            result = {
                "kind": "text_page_skipped",
                "activity_id": activity.activity_id,
                "title": activity.title,
                "read_to_bottom": False,
                "platform_complete": False,
                "skip_reason": str(error),
                "skipped_at": _now(),
            }
            self.results.append(result)
            self.skipped_page_ids.add(activity.activity_id)
            self._checkpoint("page_skipped")
            self.emit(SessionState.RETURNING, "page_skipped", {
                "activity_id": activity.activity_id,
                "reason": str(error),
            })
            self._return_to_course(activity)
            directory = self._scan_directory()
            self._checkpoint("page_skip_result")
            self.emit(SessionState.READY, "page_skip_result", {
                "activity_id": activity.activity_id,
                "platform_complete": False,
                "reason": str(error),
            })
            return directory
        result = {
            "kind": "text_page",
            "activity_id": activity.activity_id,
            "title": activity.title,
            "read_to_bottom": True,
            "platform_complete": False,
            "read_at": _now(),
        }
        self.results.append(result)
        self._checkpoint("page_read_to_bottom")
        self.emit(SessionState.RETURNING, "page_read_completed", {
            "activity_id": activity.activity_id,
        })
        self._return_to_course(activity)
        self.inspected_page_ids.add(activity.activity_id)
        directory = self._scan_directory()
        observed = directory.find(activity.activity_id)
        if observed is not None and observed.status is ActivityStatus.COMPLETE:
            result["platform_complete"] = True
            result["confirmed_at"] = _now()
        self._checkpoint("page_read_result")
        self.emit(SessionState.READY, "page_read_result", {
            "activity_id": activity.activity_id,
            "platform_complete": result["platform_complete"],
        })
        return directory

    def _scroll_page_to_bottom(
        self, activity: Activity, *, resource_surface: bool = False,
    ) -> None:
        deadline = monotonic() + 90
        previous_target: tuple[str, int, float] | None = None
        stagnant_rounds = 0
        stable_signature: tuple | None = None
        stable_rounds = 0
        wheel_performed = False
        while monotonic() < deadline:
            if resource_surface:
                self._assert_resource_surface()
            else:
                self._assert_activity(activity)
            if self.page.locator("video:visible, audio:visible").count() > 0:
                raise AutomationPaused(
                    "文本页面滚动过程中出现视频，停止自动判断并保留现场"
                )
            contexts = self._read_scroll_contexts()
            signature = tuple(
                (
                    context["frame_index"],
                    context["metrics"]["body_text_length"],
                    context["metrics"]["root"]["scroll_height"],
                    context["metrics"]["root"]["client_height"],
                    tuple(
                        (item["id"], item["class_name"], item["client_height"],
                         item["scroll_height"])
                        for item in context["metrics"]["containers"]
                    ),
                )
                for context in contexts
            )
            if signature == stable_signature:
                stable_rounds += 1
            else:
                stable_signature = signature
                stable_rounds = 0

            target = self._choose_scroll_target(contexts)
            scroll_top = float(target["scroll_top"])
            client_height = max(300, int(target["client_height"]))
            scroll_height = float(target["scroll_height"])
            at_bottom = scroll_top + client_height >= scroll_height - 8
            if at_bottom and stable_rounds >= 2 and wheel_performed:
                self.emit(SessionState.PLAYING, "page_reached_bottom", {
                    "activity_id": activity.activity_id,
                    "scroll_top": scroll_top,
                    "scroll_height": scroll_height,
                    "scroll_target": target["kind"],
                    "scroll_frame": target["frame_index"],
                })
                return

            target_key = (target["kind"], target["frame_index"], scroll_top)
            if target_key == previous_target:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            if stagnant_rounds >= 5:
                raise AutomationPaused(
                    "文本页面无法确认滚动到底部，可能存在未识别的内部滚动容器"
                )

            center_x = float(target["left"]) + float(target["width"]) / 2
            center_y = float(target["top"]) + float(target["height"]) / 2
            self.page.mouse.move(max(1, int(center_x)), max(1, int(center_y)))
            self.page.mouse.wheel(
                0, min(800, max(300, int(client_height * 0.8))),
            )
            self.page.wait_for_timeout(600)
            previous_target = target_key
            wheel_performed = True
        raise AutomationPaused("文本页面超过限定时间仍未滚动到底部")

    def _read_scroll_contexts(self) -> list[dict]:
        contexts: list[dict] = []
        main_metrics = self.page.evaluate(READ_SCROLL_METRICS)
        contexts.append({
            "frame_index": 0,
            "frame": None,
            "box": {
                "left": 0,
                "top": 0,
                "width": main_metrics["viewport_width"],
                "height": main_metrics["viewport_height"],
            },
            "metrics": main_metrics,
        })
        for frame_index, frame in enumerate(self.page.frames[1:], start=1):
            try:
                frame_element = frame.frame_element()
                raw_box = frame_element.bounding_box()
                if not raw_box or raw_box["width"] < 20 or raw_box["height"] < 20:
                    continue
                box = {
                    "left": raw_box["x"],
                    "top": raw_box["y"],
                    "width": raw_box["width"],
                    "height": raw_box["height"],
                }
                frame_metrics = frame.evaluate(READ_SCROLL_METRICS)
            except PlaywrightError as error:
                raise AutomationPaused(
                    "无法读取引用文件 iframe 的滚动状态"
                ) from error
            contexts.append({
                "frame_index": frame_index,
                "frame": frame,
                "box": box,
                "metrics": frame_metrics,
            })
        return contexts

    @staticmethod
    def _choose_scroll_target(contexts: list[dict]) -> dict:
        candidates: list[dict] = []
        for context in contexts:
            metrics = context["metrics"]
            box = context["box"]
            root = metrics["root"]
            if (
                root["scroll_height"] > root["client_height"] + 8
                or context["frame_index"] > 0
            ):
                candidates.append({
                    "kind": "frame_root" if context["frame_index"] else "root",
                    "frame_index": context["frame_index"],
                    "left": box["left"],
                    "top": box["top"],
                    "width": box["width"],
                    "height": box["height"],
                    "scroll_top": root["scroll_top"],
                    "client_height": root["client_height"],
                    "scroll_height": root["scroll_height"],
                    "priority": (
                        0 if context["frame_index"] > 0 else 1,
                        -(box["width"] * box["height"]),
                    ),
                })
            for container in metrics["containers"]:
                candidates.append({
                    "kind": (
                        "frame_container" if context["frame_index"] else "container"
                    ),
                    "frame_index": context["frame_index"],
                    "left": box["left"] + container["left"],
                    "top": box["top"] + container["top"],
                    "width": container["width"],
                    "height": container["height"],
                    "scroll_top": container["scroll_top"],
                    "client_height": container["client_height"],
                    "scroll_height": container["scroll_height"],
                    "priority": (
                        0 if context["frame_index"] > 0 else 1,
                        -(container["width"] * container["height"]),
                    ),
                })
        if not candidates:
            root = contexts[0]["metrics"]["root"]
            box = contexts[0]["box"]
            return {
                "kind": "root",
                "frame_index": 0,
                "left": box["left"],
                "top": box["top"],
                "width": box["width"],
                "height": box["height"],
                "scroll_top": root["scroll_top"],
                "client_height": root["client_height"],
                "scroll_height": root["scroll_height"],
                "priority": (1, 0),
            }
        return min(candidates, key=lambda item: item["priority"])

    def _open_activity_surface(self, activity: Activity, *, action: str) -> None:
        self._assert_home()
        self._checkpoint("opening_activity")
        target = self.page.locator(
            f"#learning-activity-{activity.activity_id} .clickable-area"
        )
        if target.count() != 1 or not target.is_visible():
            raise AutomationPaused("选中的活动已不可见，暂停以避免误点")
        delayed_click(
            self.page, target, action=action, timeout_ms=10_000,
            before_click=self._assert_home,
        )
        if not self._try_wait_for_activity_url(activity, timeout_ms=20_000):
            self._assert_no_dialog()
            self._open_activity_fallback(activity, action=action)
            if not self._try_wait_for_activity_url(activity, timeout_ms=20_000):
                raise AutomationPaused(
                    "点击活动后没有进入活动页，当前页面："
                    f"{sanitize_url(self.page.url)}"
                )

    def _open_and_play(self, activity: Activity) -> None:
        self._open_activity_surface(activity, action="进入视频")
        self._start_visible_video(activity, resource_surface=False)

    def _open_resource(self, resource: Activity) -> None:
        self._assert_home()
        self._checkpoint("opening_resource")
        target = self._find_resource_open_target(resource)
        if target is None:
            raise AutomationPaused("选中的外部资源不可见，暂停以避免误点")
        before_pages = list(self.page.context.pages)
        original_url = self.page.url
        delayed_click(
            self.page, target, action="进入外部资源", timeout_ms=10_000,
            before_click=self._assert_home,
        )
        try:
            self._wait_for_resource_surface(
                resource, original_url=original_url, before_pages=before_pages,
                timeout_seconds=10,
            )
        except AutomationPaused as first_error:
            if self.page.url != original_url:
                raise
            fallback = self._find_resource_open_fallback(resource)
            if fallback is None:
                raise first_error
            delayed_click(
                self.page, fallback, action="打开资源文件", timeout_ms=10_000,
                before_click=self._assert_home,
            )
            self._wait_for_resource_surface(
                resource, original_url=original_url, before_pages=before_pages,
                timeout_seconds=20,
            )

    def _wait_for_resource_surface(
        self,
        resource: Activity,
        *,
        original_url: str,
        before_pages: list[Page],
        timeout_seconds: float,
    ) -> None:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            if self.page.is_closed():
                raise AutomationPaused("外部资源页面已关闭，无法判断内容")
            for candidate in self.page.context.pages:
                if candidate.is_closed() or candidate in before_pages:
                    continue
                if candidate.url.startswith(("about:", "data:")):
                    continue
                if not is_allowed_lms_url(candidate.url):
                    raise AutomationPaused(
                        "外部资源打开了非国开页面，无法安全判断内容；已保留现场"
                    )
                self.page = candidate
                return
            if self.page.url != original_url:
                if not is_allowed_lms_url(self.page.url):
                    raise AutomationPaused(
                        "外部资源跳转到非国开页面，无法安全判断内容；已保留现场"
                    )
                return
            if self.page.locator("video:visible, audio:visible").count() > 0:
                return
            if (
                self._find_resource_exit() is not None
                or (
                    not is_course_home_url(self.page.url, self.config)
                    and self._read_resource_files()
                )
            ):
                return
            self.page.wait_for_timeout(500)
        raise AutomationPaused("外部资源点击后未出现可识别的资源页面")

    def _find_resource_open_target(self, resource: Activity):
        selectors_to_try = (
            f"#learning-activity-{resource.activity_id} button:visible:has-text('查看文件')",
            f"#learning-activity-{resource.activity_id} .clickable-area",
            f"#learning-activity-{resource.activity_id} .activity-title .title",
        )
        for selector in selectors_to_try:
            target = self.page.locator(selector)
            if target.count() == 1 and target.is_visible():
                return target
        return None

    def _find_resource_open_fallback(self, resource: Activity):
        candidates = (
            f"#learning-activity-{resource.activity_id} .activity-title .title",
            f"#learning-activity-{resource.activity_id} a.title",
            f"#learning-activity-{resource.activity_id} .clickable-area",
        )
        for selector in candidates:
            target = self.page.locator(selector)
            if target.count() == 1 and target.is_visible():
                return target
        return None

    def _read_resource_files(self) -> list[dict]:
        raw_files = self.page.evaluate(READ_RESOURCE_FILES)
        files: list[dict] = []
        seen: set[str] = set()
        for item in raw_files:
            key = (
                item.get("data_id")
                or item.get("href")
                or f"{item.get('text', '')}|{item.get('classes', '')}"
            )
            if not key or key in seen:
                continue
            seen.add(key)
            item["key"] = key
            files.append(item)
        return files

    def _find_resource_file(self, processed: set[str]):
        for item in self._read_resource_files():
            if item["key"] in processed:
                continue
            target = None
            selector = item.get("selector", "")
            if selector:
                candidate = self.page.locator(selector)
                if candidate.count() == 1 and candidate.is_visible():
                    target = candidate
            if target is None and item.get("text"):
                candidate = self.page.get_by_text(item["text"], exact=True)
                if candidate.count() == 1 and candidate.is_visible():
                    target = candidate
            if target is not None:
                return item, target
        return None

    def _process_resource_files(self, resource: Activity) -> None:
        processed: set[str] = set()
        for _ in range(20):
            found = self._find_resource_file(processed)
            if found is None:
                self._exit_resource(resource)
                return
            item, target = found
            processed.add(item["key"])
            self.video_frame = None
            self.emit(SessionState.PLAYING, "resource_file_selected", {
                "activity_id": resource.activity_id,
                "file_name": item.get("text", ""),
            })
            before_pages = list(self.page.context.pages)
            original_url = self.page.url
            delayed_click(
                self.page, target, action="打开引用文件", timeout_ms=10_000,
                before_click=self._assert_resource_surface,
                allow_dialog=True,
            )
            self._wait_for_resource_surface(
                resource, original_url=original_url, before_pages=before_pages,
                timeout_seconds=20,
            )
            kind = self._wait_for_resource_kind(
                resource, allow_file_list=False,
            )
            if kind == "video":
                self.emit(SessionState.PLAYING, "resource_file_video_detected", {
                    "activity_id": resource.activity_id,
                    "file_name": item.get("text", ""),
                })
                self._start_visible_video(resource, resource_surface=True)
                self._wait_for_video_end(resource, resource_surface=True)
            elif kind in {"document", "non_video"}:
                self._scroll_page_to_bottom(resource, resource_surface=True)
            else:
                raise AutomationPaused(
                    "引用文件内容无法判断为视频或可阅读文件，已保留现场"
                )
            returned_to_list = self._exit_resource(
                resource, allow_resource_surface=True,
            )
            if not returned_to_list:
                self._open_resource(resource)
        raise AutomationPaused("外部引用文件数量超过安全探查上限")

    def _inspect_visible_frame_content(self) -> str:
        for frame in self.page.frames[1:]:
            try:
                state = frame.evaluate(
                    """() => {
                        const visible = (element) => element.getClientRects().length > 0
                            && getComputedStyle(element).visibility !== 'hidden';
                        const videos = [...document.querySelectorAll('video, audio')]
                            .filter(visible);
                        const root = document.scrollingElement || document.documentElement;
                        return {
                            video_count: videos.length,
                            text_length: (document.body?.innerText || '').trim().length,
                            scroll_height: root.scrollHeight,
                            client_height: root.clientHeight
                        };
                    }"""
                )
            except PlaywrightError:
                return "unknown"
            if state["video_count"] > 1:
                return "unknown"
            if state["video_count"] == 1:
                self.video_frame = frame
                return "video"
            if (
                state["text_length"] > 50
                or state["scroll_height"] > state["client_height"] + 8
            ):
                return "document"
        return "pending"

    def _wait_for_resource_kind(
        self, resource: Activity, *, allow_file_list: bool = True,
    ) -> str:
        deadline = monotonic() + 30
        while monotonic() < deadline:
            self._assert_resource_surface()
            media = self._media_locator(self.page)
            media_count = media.count()
            if media_count > 1:
                raise AutomationPaused("外部资源包含多个可见视频，无法安全选择")
            if media_count == 1:
                state = self._read_video()
                if state.error_code:
                    raise AutomationPaused(f"外部资源视频报错：{state.error_code}")
                if state.duration > 0:
                    return "video"
                self.page.wait_for_timeout(500)
                continue
            if (
                allow_file_list
                and (
                    not is_course_home_url(self.page.url, self.config)
                    or self.page.locator(selectors.BLOCKING_DIALOG).count()
                )
                and self._read_resource_files()
            ):
                return "file_list"
            frame_kind = self._inspect_visible_frame_content()
            if frame_kind in {"video", "document", "unknown"}:
                return frame_kind
            if self.page.locator("iframe:visible").count() > 0:
                self.page.wait_for_timeout(500)
                continue
            if (
                self._find_resource_exit() is not None
                or is_activity_url(self.page.url, self.config, resource.activity_id)
                or not is_course_home_url(self.page.url, self.config)
            ):
                return "non_video"
            self.page.wait_for_timeout(500)
        raise AutomationPaused("外部资源内容在限定时间内未稳定，无法判断是否为视频")

    def _assert_resource_surface(self) -> None:
        if self.page.is_closed():
            raise AutomationPaused("外部资源页面已关闭")
        if not is_allowed_lms_url(self.page.url):
            raise AutomationPaused("外部资源离开允许的国开页面范围")
        if is_course_home_url(self.page.url, self.config):
            if (
                self.page.locator("video:visible, audio:visible").count() == 0
                and self._find_resource_exit() is None
            ):
                raise AutomationPaused("当前不是可识别的外部资源页面")

    def _find_resource_exit(self):
        for selector in selectors.RESOURCE_EXIT_CANDIDATES:
            target = self.page.locator(selector)
            if target.count() == 1 and target.is_visible():
                return target
        return None

    def _exit_resource(
        self, resource: Activity, *, allow_resource_surface: bool = False,
    ) -> bool:
        self.emit(SessionState.RETURNING, "resource_exiting", {
            "activity_id": resource.activity_id,
        })
        target = self._find_resource_exit()
        if target is None:
            raise AutomationPaused("资源内容没有唯一的返回或关闭控件")
        delayed_click(
            self.page, target, action="退出外部资源", timeout_ms=10_000,
            before_click=self._assert_resource_surface,
            allow_dialog=True,
        )
        deadline = monotonic() + 20
        while monotonic() < deadline:
            if self.page.is_closed():
                raise AutomationPaused("退出资源后页面已关闭")
            if (
                is_course_home_url(self.page.url, self.config)
                and self.page.locator(selectors.BLOCKING_DIALOG).count() == 0
            ):
                self.emit(SessionState.READY, "resource_exited", {
                    "activity_id": resource.activity_id,
                })
                return False
            if allow_resource_surface and self._read_resource_files():
                self.emit(SessionState.READY, "resource_file_exited", {
                    "activity_id": resource.activity_id,
                })
                return True
            self.page.wait_for_timeout(500)
        raise AutomationPaused("退出外部资源后未返回课程目录")

    def _start_visible_video(
        self, activity: Activity, *, resource_surface: bool,
    ) -> None:
        context = self._wait_for_visible_media(timeout_ms=30_000)
        if resource_surface:
            self._assert_resource_surface()
        else:
            self._assert_activity(activity)
        state = self._read_video()
        if state.duration <= 0:
            button = self._find_play_button(context)
            if button is None:
                self._validate_video(state)
            delayed_click(
                self.page, button, action="点击播放", timeout_ms=10_000,
                before_click=(
                    self._assert_resource_surface
                    if resource_surface else lambda: self._assert_activity(activity)
                ),
                allow_dialog=resource_surface,
            )
            try:
                context.wait_for_function(
                    """() => [...document.querySelectorAll('video, audio')]
                        .some(media => Number.isFinite(media.duration)
                            && media.duration > 0)""",
                    timeout=30_000,
                )
            except PlaywrightTimeoutError as error:
                raise AutomationPaused("播放后仍无法读取媒体时长") from error
            state = self._read_video()
        self._validate_video(state)
        if state.paused and not state.ended:
            button = self._find_play_button(context)
            if button is None:
                raise AutomationPaused("无法唯一定位视频播放按钮，当前播放器需要适配")
            def needs_playback() -> bool:
                current = self._read_video()
                self._validate_video(current)
                return current.paused and not current.ended

            delayed_click(
                self.page, button, action="点击播放", timeout_ms=10_000,
                before_click=(
                    self._assert_resource_surface
                    if resource_surface else lambda: self._assert_activity(activity)
                ),
                should_click=needs_playback,
                allow_dialog=resource_surface,
            )
            context.wait_for_function(
                "() => [...document.querySelectorAll('video, audio')].some(v => !v.paused || v.ended)",
                timeout=10_000,
            )
        self._checkpoint("playing")
        self.emit(SessionState.PLAYING, "playback_started", {
            "activity_id": activity.activity_id,
            "current_time": self._read_video().current_time,
            "resource_surface": resource_surface,
        })

    def _wait_for_visible_media(self, *, timeout_ms: int) -> Page | Frame:
        deadline = monotonic() + timeout_ms / 1000
        while monotonic() < deadline:
            contexts: list[Page | Frame] = [self.page, *self.page.frames[1:]]
            matches: list[tuple[Page | Frame, int]] = []
            for context in contexts:
                try:
                    count = self._media_locator(context).count()
                except PlaywrightError:
                    continue
                if count:
                    matches.append((context, count))
            total = sum(count for _, count in matches)
            if total == 1:
                context = matches[0][0]
                self.video_frame = context if isinstance(context, Frame) else None
                return context
            if total > 1:
                raise AutomationPaused(
                    "无法唯一识别当前视频或音频；不自动操作多个媒体播放器"
                )
            self.page.wait_for_timeout(250)
        raise PlaywrightTimeoutError(
            "等待可见视频或音频播放器超时"
        )

    def _read_video(self) -> VideoState:
        context = self.video_frame or self.page
        media = self._media_locator(context)
        if media.count() != 1:
            raise AutomationPaused("无法唯一识别当前视频或音频；不自动操作未知播放器或嵌入资源")
        self.video_state = VideoState(**media.evaluate(READ_VIDEO))
        return self.video_state

    @staticmethod
    def _find_play_button(context: Page | Frame):
        candidates = (
            context.locator(selectors.PLAY_BUTTON),
            context.get_by_role(
                "button", name=re.compile(r"^(播放|Play)(视频| Video)?$", re.I),
            ),
            context.locator(".audio-player-wrapper .play:visible"),
        )
        for candidate in candidates:
            if candidate.count() == 1 and candidate.is_visible():
                return candidate
        return None

    @staticmethod
    def _media_locator(context: Page | Frame):
        visible = context.locator("video:visible, audio:visible")
        if visible.count() == 1:
            return visible
        media = context.locator("video, audio")
        if media.count() == 1:
            return media
        return visible

    @staticmethod
    def _validate_video(state: VideoState) -> None:
        if state.error_code:
            raise AutomationPaused(f"播放器报错：{state.error_code}")
        if abs(state.playback_rate - 1) > 0.001:
            raise AutomationPaused("播放器不是 1 倍速，请人工恢复正常速度")
        if state.duration <= 0:
            raise AutomationPaused("无法读取视频时长")

    @staticmethod
    def _is_buffering(state: VideoState) -> bool:
        buffered_ahead = state.buffered_end - state.current_time
        return state.ready_state < 3 or (
            buffered_ahead >= 0 and buffered_ahead < 1
        )

    def _wait_for_video_resume(
        self,
        activity: Activity,
        state: VideoState,
        *,
        resource_surface: bool,
    ) -> VideoState:
        deadline = monotonic() + PAUSED_RECOVERY_TIMEOUT_SECONDS
        self.emit(SessionState.PLAYING, "playback_pause_wait_started", {
            "activity_id": activity.activity_id,
            "current_time": state.current_time,
            "duration": state.duration,
            "ready_state": state.ready_state,
            "network_state": state.network_state,
            "buffered_ahead": max(0.0, state.buffered_end - state.current_time),
            "likely_buffering": self._is_buffering(state),
            "timeout_seconds": PAUSED_RECOVERY_TIMEOUT_SECONDS,
        })
        while monotonic() < deadline:
            if resource_surface:
                self._assert_resource_surface()
            else:
                self._assert_activity(activity)
            state = self._read_video()
            self._validate_video(state)
            if state.ended or not state.paused:
                self.emit(SessionState.PLAYING, "playback_pause_recovered", {
                    "activity_id": activity.activity_id,
                    "current_time": state.current_time,
                    "duration": state.duration,
                    "ready_state": state.ready_state,
                    "buffered_ahead": max(
                        0.0, state.buffered_end - state.current_time
                    ),
                })
                return state
            remaining_ms = max(
                1, min(
                    PAUSED_RECOVERY_POLL_MS,
                    int((deadline - monotonic()) * 1000),
                )
            )
            context = self.video_frame or self.page
            try:
                context.wait_for_function(
                    """() => {
                        const media = [...document.querySelectorAll('video, audio')]
                            .find(v => v.getClientRects().length > 0);
                        return media && (
                            media.ended
                            || !media.paused
                            || media.error
                            || Math.abs(media.playbackRate - 1) > 0.001
                        );
                    }""",
                    polling=100,
                    timeout=remaining_ms,
                )
            except PlaywrightTimeoutError:
                pass
        raise AutomationPaused(
            "视频暂停或加载超过 "
            f"{PAUSED_RECOVERY_TIMEOUT_SECONDS} 秒，请人工核对"
        )

    def _wait_for_video_end(
        self, activity: Activity, *, resource_surface: bool = False,
    ) -> VideoState:
        state = self._read_video()
        self._validate_video(state)
        deadline = monotonic() + max(300, state.duration - state.current_time + 300)
        last_time = state.current_time
        last_progress = last_report = monotonic()
        while monotonic() < deadline:
            if resource_surface:
                self._assert_resource_surface()
            else:
                self._assert_activity(activity)
            state = self._read_video()
            self._validate_video(state)
            if state.ended:
                return state
            if state.paused:
                state = self._wait_for_video_resume(
                    activity, state, resource_surface=resource_surface,
                )
                if state.ended:
                    return state
            now = monotonic()
            if state.current_time > last_time + 0.1:
                last_progress = now
                last_time = state.current_time
            elif now - last_progress >= (
                PAUSED_RECOVERY_TIMEOUT_SECONDS
                if self._is_buffering(state) else 60
            ):
                if self._is_buffering(state):
                    raise AutomationPaused(
                        "视频加载超过 "
                        f"{PAUSED_RECOVERY_TIMEOUT_SECONDS} 秒没有播放进展"
                    )
                raise AutomationPaused("视频连续 60 秒没有播放进展")
            if now - last_report >= 60:
                self.emit(SessionState.PLAYING, "playback_progress", {
                    "activity_id": activity.activity_id,
                    "current_time": state.current_time, "duration": state.duration,
                })
                self._checkpoint("playing")
                last_report = now
            # Observe progress/ending without changing media or calling learning APIs.
            try:
                context = self.video_frame or self.page
                context.wait_for_function(
                    """(before) => {
                        const media = [...document.querySelectorAll('video, audio')]
                            .find(v => v.getClientRects().length > 0);
                        return !media || media.ended || media.paused || media.error
                            || Math.abs(media.playbackRate - 1) > 0.001
                            || media.currentTime >= before + 5;
                    }""",
                    arg=state.current_time, polling=500, timeout=10_000,
                )
            except PlaywrightTimeoutError:
                pass  # A timeout is followed by the bounded stall/deadline checks.
        raise AutomationPaused("超过视频自然播放的等待上限，暂停自动化")

    def _return_to_course(self, activity: Activity) -> None:
        self._assert_activity(activity)
        self._checkpoint("returning_to_course")
        back = self.page.locator(selectors.RETURN_TO_COURSE)
        if back.count() != 1:
            raise AutomationPaused("无法唯一定位返回课程按钮")
        delayed_click(
            self.page, back, action="返回课程", timeout_ms=10_000,
            before_click=lambda: self._assert_activity(activity),
        )
        self._wait_for_course_home_url()
        self.emit(SessionState.READY, "returned_to_course", {
            "activity_id": activity.activity_id,
        })
        self._checkpoint("returned")

    def _confirm_completion(self, activity: Activity) -> CourseDirectory:
        deadline = monotonic() + 30
        while True:
            directory = self._scan_directory()
            observed = directory.find(activity.activity_id)
            if observed is None:
                raise AutomationPaused("返回后找不到原活动，不能推断平台已经记录完成")
            if observed.status is ActivityStatus.COMPLETE:
                return directory
            if monotonic() >= deadline:
                raise AutomationPaused("视频已播放结束，但平台仍未明确记录完成；暂停避免重复播放")
            # Re-read UI status; never reload to force credit or send progress requests.
            self.page.wait_for_timeout(2_000)

    def _checkpoint(
        self, phase: str, *, status: str = "running", reason: str = "",
    ) -> None:
        self.phase = phase
        atomic_write_json(self.run_dir / "checkpoint.json", {
            "schema_version": 1, "run_id": self.run_dir.name,
            "course_id": self.config.course_id,
            "observed_at": _now(), "phase": phase,
            "status": status, "reason": reason,
            "current_activity_id": (
                self.current_activity.activity_id if self.current_activity else None
            ),
            "remaining_activity_ids": self.remaining,
            "inspected_resource_ids": sorted(self.inspected_resource_ids),
            "inspected_page_ids": sorted(self.inspected_page_ids),
            "skipped_page_ids": sorted(self.skipped_page_ids),
            "last_video_state": asdict(self.video_state) if self.video_state else None,
            "results": self.results,
        })

    def _capture_pause_diagnostics(self, reason: str, error_type: str) -> None:
        screenshot_path = self.run_dir / "pause.png"
        diagnostics_path = self.run_dir / "pause_diagnostics.json"
        dialogs: list[dict] = []
        title = ""
        current_url = ""

        try:
            if self.page.is_closed():
                return
            current_url = sanitize_url(self.page.url)
            title = self.page.title()
            self.page.screenshot(path=str(screenshot_path), full_page=False)
            os.chmod(screenshot_path, 0o600)
            dialogs = self.page.evaluate(
                READ_VISIBLE_DIALOGS, selectors.BLOCKING_DIALOG_CANDIDATES,
            )
            atomic_write_json(diagnostics_path, {
                "schema_version": 1,
                "observed_at": _now(),
                "course_id": self.config.course_id,
                "phase": self.phase,
                "reason": reason,
                "error_type": error_type,
                "current_activity_id": (
                    self.current_activity.activity_id if self.current_activity else None
                ),
                "page_url": current_url,
                "page_title": title,
                "visible_dialogs": dialogs,
                "screenshot": screenshot_path.name,
            })
            print(
                f"已保留暂停现场：{screenshot_path}；诊断信息：{diagnostics_path}",
                flush=True,
            )
        except (PlaywrightError, OSError, ValueError) as capture_error:
            print(
                f"保存暂停现场失败：{sanitize_error_message(capture_error)}",
                file=sys.stderr,
                flush=True,
            )

    def _wait_for_activity_url(self, activity: Activity) -> None:
        if not self._try_wait_for_activity_url(activity, timeout_ms=20_000):
            raise AutomationPaused(
                "等待活动页 URL 超时，当前页面："
                f"{sanitize_url(self.page.url)}"
            )

    def _try_wait_for_activity_url(
        self, activity: Activity, *, timeout_ms: int,
    ) -> bool:
        try:
            self.page.wait_for_function(
                """({coursePath, activityId}) => {
                    const url = new URL(location.href);
                    return url.protocol === 'https:'
                        && url.hostname === 'lms.ouchn.cn'
                        && url.pathname.replace(/\\/$/, '') === coursePath
                        && url.hash.replace(/^#\\/?/, '') === activityId;
                }""",
                arg={
                    "coursePath": f"/course/{self.config.course_id}/learning-activity/full-screen",
                    "activityId": activity.activity_id,
                },
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError:
            return False
        return True

    def _open_activity_fallback(self, activity: Activity, *, action: str) -> None:
        self._assert_home()
        fallbacks = [
            f"#learning-activity-{activity.activity_id} .activity-title .title",
            f"#learning-activity-{activity.activity_id} a.title",
            f"#learning-activity-{activity.activity_id}",
        ]
        for selector in fallbacks:
            candidate = self.page.locator(selector).first
            if candidate.count() == 0 or not candidate.is_visible():
                continue
            delayed_click(
                self.page, candidate, action=f"{action}后备点击",
                timeout_ms=10_000, before_click=self._assert_home,
            )
            return
        raise AutomationPaused("选中的活动没有可用的后备点击目标")

    def _wait_for_course_home_url(self) -> None:
        self.page.wait_for_function(
            """({coursePath}) => {
                const url = new URL(location.href);
                return url.protocol === 'https:'
                    && url.hostname === 'lms.ouchn.cn'
                    && url.pathname.replace(/\\/$/, '') === coursePath
                    && (url.hash === '' || url.hash === '#/');
            }""",
            arg={"coursePath": f"/course/{self.config.course_id}/ng"},
            timeout=20_000,
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
