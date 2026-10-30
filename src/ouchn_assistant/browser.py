from collections.abc import Callable
import sys
from time import monotonic
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, Playwright

from ouchn_assistant import selectors
from ouchn_assistant.artifacts import sanitize_error_message, sanitize_url
from ouchn_assistant.config import AppConfig, COURSE_HOST


MANUAL_LOGIN_HOST = "iam.pt.ouchn.cn"
MANUAL_LOGIN_TIMEOUT_SECONDS = 600


class BrowserBoundaryError(RuntimeError):
    pass


def is_allowed_lms_url(url: str) -> bool:
    return _is_bounded_https_url(url, COURSE_HOST)


def is_manual_login_url(url: str) -> bool:
    return _is_bounded_https_url(url, MANUAL_LOGIN_HOST)


def is_course_home_url(url: str, config: AppConfig) -> bool:
    parsed = urlsplit(url)
    return (
        is_allowed_lms_url(url)
        and parsed.path.rstrip("/") == f"/course/{config.course_id}/ng"
        and parsed.fragment in ("", "/")
    )


def is_activity_url(url: str, config: AppConfig, activity_id: str) -> bool:
    parsed = urlsplit(url)
    return (
        is_allowed_lms_url(url)
        and parsed.path.rstrip("/")
        == f"/course/{config.course_id}/learning-activity/full-screen"
        and parsed.fragment.strip("/") == activity_id
    )


def _is_bounded_https_url(url: str, expected_host: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False

    return (
        parsed.scheme == "https"
        and parsed.hostname == expected_host
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )


class BrowserSession:
    def __init__(self, playwright: Playwright, config: AppConfig) -> None:
        self.playwright = playwright
        self.config = config
        self.context: BrowserContext | None = None

    def open(self) -> Page:
        if self.context is not None:
            raise RuntimeError("Browser session is already open")

        self.config.profile_dir.parent.mkdir(parents=True, exist_ok=True)
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.config.profile_dir,
            channel="chrome",
            headless=False,
            accept_downloads=False,
        )
        if self.context.pages:
            return self.context.pages[0]
        return self.context.new_page()

    def assert_allowed_page(self, page: Page) -> None:
        if not is_allowed_lms_url(page.url):
            raise BrowserBoundaryError(
                f"Page left the allowed LMS origin: {sanitize_url(page.url)}"
            )

    def open_course(
        self,
        input_fn: Callable[[str], str] = input,
        on_login_required: Callable[[], None] | None = None,
    ) -> Page:
        page = self.open()
        page.goto(self.config.course_url, wait_until="domcontentloaded", timeout=30_000)
        # Observe all supported landing states in one loop: a fast redirect can
        # finish login before a one-shot IAM URL check gets a chance to run.
        # input_fn remains for diagnostic callers; no terminal confirmation is needed.
        page = self._wait_for_manual_login(page, on_login_required=on_login_required)
        self.assert_allowed_page(page)
        if not is_course_home_url(page.url, self.config):
            raise BrowserBoundaryError("当前页面不是指定课程首页")
        page.bring_to_front()
        return page

    def _wait_for_manual_login(
        self, page: Page, *, on_login_required: Callable[[], None] | None = None,
    ) -> Page:
        deadline = monotonic() + MANUAL_LOGIN_TIMEOUT_SECONDS
        next_report = monotonic() + 30
        navigated_to_course = False
        login_notified = False
        print(
            "等待目标课程加载；需要登录时请在 Chrome 中扫码或手动登录，最多等待 10 分钟；"
            "检测到课程目录后自动继续，无需按 Enter。",
            flush=True,
        )
        while monotonic() < deadline:
            if self.context is None:
                raise BrowserBoundaryError("浏览器会话已关闭")
            pages = [candidate for candidate in self.context.pages if not candidate.is_closed()]
            if not pages:
                raise BrowserBoundaryError("浏览器窗口已关闭，登录等待结束")
            if not login_notified and any(is_manual_login_url(candidate.url) for candidate in pages):
                login_notified = True
                if on_login_required is not None:
                    on_login_required()
            for candidate in pages:
                if not is_course_home_url(candidate.url, self.config):
                    continue
                try:
                    ready = candidate.locator(selectors.EXPAND_ALL).first.is_visible()
                except PlaywrightError:
                    continue  # A login redirect or tab close can replace this document.
                if ready:
                    print("已检测到登录成功且目标课程目录就绪，继续执行。", flush=True)
                    return candidate
            if page.is_closed():
                page = pages[0]
            # Some login flows land on the LMS home instead of the requested course.
            # Only navigate once, to the original user-provided course homepage.
            if not navigated_to_course:
                landing = next(
                    (candidate for candidate in pages
                     if is_allowed_lms_url(candidate.url)
                     and not is_course_home_url(candidate.url, self.config)),
                    None,
                )
                if landing is not None:
                    navigated_to_course = True
                    page = landing
                    try:
                        page.goto(
                            self.config.course_url, wait_until="domcontentloaded", timeout=30_000,
                        )
                    except PlaywrightError:
                        if page.is_closed():
                            continue
                        raise
            if monotonic() >= next_report:
                remaining = max(0, int(deadline - monotonic()))
                print(f"仍在等待扫码登录或课程加载，剩余 {remaining} 秒。", flush=True)
                next_report = monotonic() + 30
            try:
                page.wait_for_timeout(1_000)
            except PlaywrightError:
                if page.is_closed():
                    continue  # Re-select a live tab, or report that all tabs were closed.
                raise
        raise BrowserBoundaryError("等待扫码登录超过 10 分钟，请重新运行")

    def find_course_page(self) -> Page | None:
        if self.context is None:
            return None

        expected_url = self.config.course_url.rstrip("/")
        for page in self.context.pages:
            if sanitize_url(page.url).rstrip("/") == expected_url:
                return page
        return None

    def close(self) -> None:
        context, self.context = self.context, None
        if context is not None:
            primary_error = sys.exception()
            try:
                context.close()
            except (PlaywrightError, OSError) as close_error:
                if primary_error is None:
                    raise
                message = f"关闭浏览器失败：{sanitize_error_message(close_error)}"
                primary_error.add_note(message)
                print(message, file=sys.stderr, flush=True)
