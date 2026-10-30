from pathlib import Path

import pytest

from ouchn_assistant.config import build_config, parse_course_url
from ouchn_assistant.models import RunMode


def test_parse_valid_course_url_removes_query_fragment_and_trailing_slash() -> None:
    parsed = parse_course_url(
        "https://lms.ouchn.cn/course/10000118045/ng/?locale=en#section"
    )

    assert parsed.course_id == "10000118045"
    assert parsed.sanitized_url == "https://lms.ouchn.cn/course/10000118045/ng"


@pytest.mark.parametrize(
    "url",
    [
        "http://lms.ouchn.cn/course/100/ng",
        "https://evil.example/course/100/ng",
        "https://lms.ouchn.cn.evil.example/course/100/ng",
        "https://user:pass@lms.ouchn.cn/course/100/ng",
        "https://lms.ouchn.cn:444/course/100/ng",
        "https://lms.ouchn.cn:not-a-port/course/100/ng",
        "https://lms.ouchn.cn/user/courses",
        "https://lms.ouchn.cn/course/not-a-number/ng",
        "https://lms.ouchn.cn/course/100/ng/extra",
    ],
)
def test_rejects_non_course_url(url: str) -> None:
    with pytest.raises(ValueError, match="valid OUCHN course URL"):
        parse_course_url(url)


def test_build_config_uses_resolved_project_local_paths(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    config = build_config(
        mode=RunMode.INSPECT,
        course_url="https://lms.ouchn.cn/course/100/ng",
        project_root=project_root,
        screenshot_enabled=True,
    )

    assert config.mode is RunMode.INSPECT
    assert config.course_id == "100"
    assert config.course_url == "https://lms.ouchn.cn/course/100/ng"
    assert config.project_root == project_root.resolve()
    assert config.profile_dir == project_root.resolve() / ".runtime" / "chrome-profile"
    assert config.runs_dir == project_root.resolve() / "runs"
    assert config.screenshot_enabled is True


def test_parse_default_https_port_is_canonicalized() -> None:
    parsed = parse_course_url("https://lms.ouchn.cn:443/course/100/ng")

    assert parsed.sanitized_url == "https://lms.ouchn.cn/course/100/ng"
