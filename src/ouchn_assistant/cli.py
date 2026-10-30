import argparse
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from time import monotonic

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from ouchn_assistant import __version__
from ouchn_assistant.artifacts import append_jsonl, atomic_write_json, sanitize_error_message
from ouchn_assistant.browser import BrowserBoundaryError, BrowserSession
from ouchn_assistant.config import AppConfig, build_config
from ouchn_assistant.inspector import ActivityParseError, inspect_course
from ouchn_assistant.models import Activity, RunEvent, RunMode, SessionState
from ouchn_assistant.runner import AutomationPaused, CourseRunner
from ouchn_assistant.session import (
    ALLOWED_TRANSITIONS, LockOwnershipLost, ProfileInUse, ProfileLock, SessionMachine,
)


Clock = Callable[[], datetime]
RunIdFactory = Callable[[datetime], str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_cli_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ouchn-assistant",
        description="提供一个课程首页链接，通过页面点击播放该课程内的未完成视频。",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("url", nargs="?", help="国开 LMS 课程首页链接")
    parser.add_argument("--course-url", help="课程链接（兼容原参数）")
    parser.add_argument(
        "--mode", choices=[RunMode.ASSIST.value, RunMode.INSPECT.value],
        default=RunMode.ASSIST.value,
        help="默认 assist 自动播放；inspect 仅采集目录",
    )
    arguments = parser.parse_args(argv)
    if arguments.url and arguments.course_url:
        parser.error("课程链接只需提供一次")
    arguments.course_url = arguments.url or arguments.course_url
    if not arguments.course_url:
        parser.error("请提供一个课程首页链接")
    return arguments


def build_snapshot(
    *, run_id: str, config: AppConfig, observed_at: datetime,
    activities: Sequence[Activity],
) -> dict[str, Any]:
    return {
        "schema_version": 2, "run_id": run_id, "course_id": config.course_id,
        "observed_at": _format_utc(observed_at),
        "filter": "unfinished", "coverage": "expanded_dom_observed",
        "activities": [activity.to_dict() for activity in activities],
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_cli_args(argv)
    try:
        config = build_config(
            mode=RunMode(arguments.mode),
            course_url=arguments.course_url,
            project_root=Path(__file__).resolve().parents[2],
        )
    except ValueError as error:
        return _report_error(2, error)
    try:
        output = (
            run_inspect(config) if config.mode is RunMode.INSPECT
            else run_assist(config)
        )
    except KeyboardInterrupt:
        return _report_error(130, "已中断，浏览器已关闭；重新运行时会重新读取平台目录")
    except (ProfileInUse, LockOwnershipLost) as error:
        return _report_error(3, error)
    except AutomationPaused as error:
        return _report_error(7, f"自动化已暂停：{error}")
    except (BrowserBoundaryError, PlaywrightError) as error:
        return _report_error(4, error)
    except ActivityParseError as error:
        return _report_error(5, error)
    except OSError as error:
        return _report_error(6, error)
    print(f"运行结果：{output}", flush=True)
    return 0


def run_inspect(
    config: AppConfig, *, input_fn: Callable[[str], str] = input,
    clock: Clock = _utc_now, run_id_factory: RunIdFactory | None = None,
) -> Path:
    return _run_course(
        config, playback=False, input_fn=input_fn, clock=clock,
        run_id_factory=run_id_factory,
    )


def run_assist(
    config: AppConfig, *, input_fn: Callable[[str], str] = input,
    clock: Clock = _utc_now, run_id_factory: RunIdFactory | None = None,
) -> Path:
    return _run_course(
        config, playback=True, input_fn=input_fn, clock=clock,
        run_id_factory=run_id_factory,
    )


def _run_course(
    config: AppConfig, *, playback: bool, input_fn: Callable[[str], str],
    clock: Clock, run_id_factory: RunIdFactory | None,
) -> Path:
    started_at = clock().astimezone(timezone.utc)
    run_id = (run_id_factory or _new_run_id)(started_at)
    run_dir = config.runs_dir / run_id
    events_path = run_dir / "events.jsonl"
    machine = SessionMachine(run_id=run_id, clock=clock)

    def emit(state: SessionState, event: str, details: dict | None = None) -> None:
        details = details or {}
        if state is machine.state:
            run_event = RunEvent(
                timestamp=_format_utc(clock()), run_id=run_id,
                state=state, event=event,
            )
        else:
            run_event = machine.transition(state, event)
        payload = run_event.to_dict()
        payload["activity_id"] = details.get("activity_id")
        payload["details"] = details
        append_jsonl(events_path, payload)
        if event == "video_selected":
            print(
                f"剩余 {details['remaining_video_count']} 个视频，播放：{details['title']}",
                flush=True,
            )
        elif event == "resource_selected":
            print(f"进入外部资源探查：{details['title']}", flush=True)
        elif event == "resource_video_detected":
            print("资源内容识别为视频，开始正常播放。", flush=True)
        elif event == "resource_file_selected":
            print(f"打开引用文件：{details['file_name']}", flush=True)
        elif event == "resource_file_video_detected":
            print("引用文件识别为视频，开始播放至结束。", flush=True)
        elif event == "resource_non_video_detected":
            print("资源内容不是可识别视频，准备退出资源页面。", flush=True)
        elif event == "resource_inspected":
            print(
                "外部资源探查完成，平台完成状态："
                f"{'已确认' if details['platform_complete'] else '未确认'}。",
                flush=True,
            )
        elif event == "page_selected":
            print(f"进入文本页面：{details['title']}", flush=True)
        elif event == "page_reached_bottom":
            print("文本页面已滚动到底部，准备返回。", flush=True)
        elif event == "page_read_completed":
            print("文本页面阅读完成，平台已确认。", flush=True)
        elif event == "page_read_result":
            print(
                "文本页面已阅读，平台完成状态："
                f"{'已确认' if details['platform_complete'] else '未确认'}，继续读取后续活动。",
                flush=True,
            )
        elif event == "playback_progress":
            print(
                f"播放进度：{details['current_time']:.0f} / {details['duration']:.0f} 秒",
                flush=True,
            )
        elif event == "playback_started":
            print("已开始播放。", flush=True)
        elif event == "video_ended":
            print("视频自然结束，正在返回课程页。", flush=True)
        elif event == "returned_to_course":
            print("已返回课程页，重新筛选并核对完成状态。", flush=True)
        elif event == "platform_completion_confirmed":
            print("平台已明确记录完成，继续读取剩余视频。", flush=True)
        elif event == "no_unfinished_videos":
            print("当前课程目录没有剩余的未完成视频。", flush=True)

    with ProfileLock(config.profile_dir.parent / "chrome-profile.lock"):
        emit(SessionState.PREFLIGHT, "preflight_started")
        try:
            with sync_playwright() as playwright:
                browser = BrowserSession(playwright, config)
                try:
                    page = browser.open_course(
                        input_fn=input_fn,
                        on_login_required=lambda: emit(
                            SessionState.LOGIN_REQUIRED, "manual_login_required",
                        ),
                    )
                    emit(SessionState.READY, "course_ready")
                    print(f"运行目录：{run_dir}", flush=True)
                    if playback:
                        output = CourseRunner(page, config, run_dir, emit).run()
                    else:
                        emit(SessionState.INSPECTING, "inspection_started")
                        activities = inspect_course(page)
                        output = run_dir / "activities_snapshot.json"
                        atomic_write_json(output, build_snapshot(
                            run_id=run_id, config=config, observed_at=clock(),
                            activities=activities,
                        ))
                        emit(SessionState.READY, "inspection_completed")
                    emit(SessionState.STOPPED, "run_completed")
                except AutomationPaused as error:
                    if SessionState.PAUSED in ALLOWED_TRANSITIONS[machine.state]:
                        emit(SessionState.PAUSED, "run_paused", {"reason": str(error)})
                    _hold_browser_for_review(page)
                    if SessionState.STOPPED in ALLOWED_TRANSITIONS[machine.state]:
                        emit(SessionState.STOPPED, "run_stopped")
                    raise
                except KeyboardInterrupt as error:
                    if SessionState.PAUSED in ALLOWED_TRANSITIONS[machine.state]:
                        emit(SessionState.PAUSED, "run_paused", {"reason": str(error)})
                    if SessionState.STOPPED in ALLOWED_TRANSITIONS[machine.state]:
                        emit(SessionState.STOPPED, "run_stopped")
                    raise
                finally:
                    browser.close()
        except BaseException as error:
            _record_terminal_failure(machine, events_path, error)
            raise
    return output


def _new_run_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:6]}"


def _hold_browser_for_review(page: Any) -> None:
    print(
        "自动化已停止点击并保留浏览器现场。请保持窗口打开以便分析；"
        "关闭浏览器或按 Ctrl+C 后程序才会退出。",
        flush=True,
    )
    next_report = monotonic() + 60
    while not page.is_closed():
        try:
            page.wait_for_timeout(1_000)
        except PlaywrightError:
            return
        if monotonic() >= next_report:
            print("浏览器现场仍在保留，自动化没有继续点击。", flush=True)
            next_report = monotonic() + 60


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_terminal_failure(
    machine: SessionMachine, events_path: Path, error: BaseException,
) -> None:
    failed_state = machine.state
    details = {
        "reason": sanitize_error_message(error), "error_type": type(error).__name__,
        "failed_state": failed_state.value,
    }
    try:
        if SessionState.FAILED in ALLOWED_TRANSITIONS[machine.state]:
            event = machine.transition(SessionState.FAILED, "run_failed")
            payload = event.to_dict()
            payload["details"] = details
            append_jsonl(events_path, payload)
        if SessionState.STOPPED in ALLOWED_TRANSITIONS[machine.state]:
            event = machine.transition(SessionState.STOPPED, "run_stopped_after_failure")
            append_jsonl(events_path, event.to_dict())
    except OSError:
        pass


def _report_error(exit_code: int, error: object) -> int:
    import sys
    print(sanitize_error_message(error), file=sys.stderr, flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
