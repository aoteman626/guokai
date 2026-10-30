import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ouchn_assistant.models import RunMode


COURSE_PATH = re.compile(r"^/course/([0-9]+)/ng/?$")
COURSE_HOST = "lms.ouchn.cn"


@dataclass(frozen=True, slots=True)
class ParsedCourseUrl:
    course_id: str
    sanitized_url: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    mode: RunMode
    course_id: str
    course_url: str
    project_root: Path
    profile_dir: Path
    runs_dir: Path
    screenshot_enabled: bool = False


def parse_course_url(raw_url: str) -> ParsedCourseUrl:
    parsed = urlsplit(raw_url.strip())
    path_match = COURSE_PATH.fullmatch(parsed.path)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Expected a valid OUCHN course URL") from error

    if (
        parsed.scheme != "https"
        or parsed.hostname != COURSE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or path_match is None
    ):
        raise ValueError("Expected a valid OUCHN course URL")

    course_id = path_match.group(1)
    sanitized_url = urlunsplit(
        ("https", COURSE_HOST, f"/course/{course_id}/ng", "", "")
    )
    return ParsedCourseUrl(course_id=course_id, sanitized_url=sanitized_url)


def build_config(
    mode: RunMode,
    course_url: str,
    project_root: Path,
    screenshot_enabled: bool = False,
) -> AppConfig:
    parsed_url = parse_course_url(course_url)
    resolved_root = project_root.resolve()

    return AppConfig(
        mode=mode,
        course_id=parsed_url.course_id,
        course_url=parsed_url.sanitized_url,
        project_root=resolved_root,
        profile_dir=resolved_root / ".runtime" / "chrome-profile",
        runs_dir=resolved_root / "runs",
        screenshot_enabled=screenshot_enabled,
    )
