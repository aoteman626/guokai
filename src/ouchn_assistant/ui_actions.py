"""Delayed UI clicks with a fresh state check immediately before acting."""

from collections.abc import Callable
from random import uniform

from playwright.sync_api import Locator, Page

from ouchn_assistant import selectors


CLICK_DELAY_MIN_SECONDS = 0.0
CLICK_DELAY_MAX_SECONDS = 3.0


class UiActionError(RuntimeError):
    """The page changed while a click was pending."""


def delayed_click(
    page: Page,
    target: Locator,
    *,
    action: str,
    timeout_ms: int,
    before_click: Callable[[], None] | None = None,
    should_click: Callable[[], bool] | None = None,
    allow_dialog: bool = False,
) -> bool:
    """Return False when the desired state was reached during the delay.

    Calls remain synchronous. Each call issues at most one click and never
    retries an uncertain click; callers must confirm the resulting UI state.
    """
    expected_url = page.url
    target.wait_for(state="visible", timeout=timeout_ms)
    delay = uniform(CLICK_DELAY_MIN_SECONDS, CLICK_DELAY_MAX_SECONDS)
    print(f"{action}：等待 {delay:.2f} 秒。", flush=True)
    page.wait_for_timeout(delay * 1000)
    if page.url != expected_url:
        raise UiActionError(f"{action}等待期间页面已切换，取消点击")
    if not allow_dialog and page.locator(selectors.BLOCKING_DIALOG).count():
        raise UiActionError(f"{action}等待期间出现弹窗，取消点击并请人工处理")
    if before_click is not None:
        before_click()
    if should_click is not None and not should_click():
        print(f"{action}：状态已满足，跳过点击。", flush=True)
        return False
    target.click(timeout=timeout_ms)
    return True
