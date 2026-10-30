from pathlib import Path

import pytest

from ouchn_assistant.browser import (
    BrowserBoundaryError,
    BrowserSession,
    is_allowed_lms_url,
    is_manual_login_url,
)
from ouchn_assistant.config import build_config
from ouchn_assistant.models import RunMode


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://lms.ouchn.cn/course/100/ng", True),
        ("https://lms.ouchn.cn:443/course/100/ng", True),
        ("https://lms.ouchn.cn.evil.example/course/100/ng", False),
        ("http://lms.ouchn.cn/course/100/ng", False),
        ("https://user@lms.ouchn.cn/course/100/ng", False),
        ("https://lms.ouchn.cn:444/course/100/ng", False),
        ("https://example.com/", False),
        ("not a url", False),
    ],
)
def test_is_allowed_lms_url(url: str, allowed: bool) -> None:
    assert is_allowed_lms_url(url) is allowed


@pytest.mark.parametrize(
    ("url", "is_login"),
    [
        ("https://iam.pt.ouchn.cn/am/oauth2/authorize", True),
        ("https://iam.pt.ouchn.cn.evil.example/login", False),
        ("http://iam.pt.ouchn.cn/login", False),
        ("https://lms.ouchn.cn/course/100/ng", False),
    ],
)
def test_is_manual_login_url(url: str, is_login: bool) -> None:
    assert is_manual_login_url(url) is is_login


class FakePage:
    def __init__(self, url: str = "about:blank") -> None:
        self.url = url


class FakeContext:
    def __init__(self) -> None:
        self.pages = [FakePage()]
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.launch_kwargs: dict[str, object] | None = None

    def launch_persistent_context(self, **kwargs: object) -> FakeContext:
        self.launch_kwargs = kwargs
        return self.context


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium


def test_browser_session_uses_bounded_visible_chrome(tmp_path: Path) -> None:
    config = build_config(
        mode=RunMode.INSPECT,
        course_url="https://lms.ouchn.cn/course/100/ng",
        project_root=tmp_path,
    )
    context = FakeContext()
    chromium = FakeChromium(context)
    session = BrowserSession(FakePlaywright(chromium), config)  # type: ignore[arg-type]

    page = session.open()

    assert page is context.pages[0]
    assert chromium.launch_kwargs == {
        "user_data_dir": config.profile_dir,
        "channel": "chrome",
        "headless": False,
        "accept_downloads": False,
    }
    session.close()
    assert context.closed is True


def test_browser_session_rejects_page_outside_lms(tmp_path: Path) -> None:
    config = build_config(
        mode=RunMode.INSPECT,
        course_url="https://lms.ouchn.cn/course/100/ng",
        project_root=tmp_path,
    )
    session = BrowserSession(
        FakePlaywright(FakeChromium(FakeContext())),  # type: ignore[arg-type]
        config,
    )

    session.assert_allowed_page(FakePage("https://lms.ouchn.cn/course/100/ng"))  # type: ignore[arg-type]
    with pytest.raises(BrowserBoundaryError) as error:
        session.assert_allowed_page(
            FakePage("https://example.com/path?token=secret#private")  # type: ignore[arg-type]
        )

    assert "https://example.com/path" in str(error.value)
    assert "secret" not in str(error.value)


def test_browser_session_finds_exact_course_tab(tmp_path: Path) -> None:
    config = build_config(
        mode=RunMode.INSPECT,
        course_url="https://lms.ouchn.cn/course/100/ng",
        project_root=tmp_path,
    )
    context = FakeContext()
    login_page = FakePage("https://iam.pt.ouchn.cn/am/oauth2/authorize")
    course_page = FakePage("https://lms.ouchn.cn/course/100/ng?locale=zh-CN")
    context.pages = [login_page, course_page]
    session = BrowserSession(
        FakePlaywright(FakeChromium(context)),  # type: ignore[arg-type]
        config,
    )
    session.context = context  # type: ignore[assignment]

    assert session.find_course_page() is course_page  # type: ignore[comparison-overlap]
