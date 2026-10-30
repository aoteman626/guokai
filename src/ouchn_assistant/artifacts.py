import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


REDACTED = "[REDACTED]"
SENSITIVE_KEY_PARTS = {
    "authorization",
    "cookie",
    "headers",
    "localstorage",
    "password",
    "token",
}


def sanitize_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    hostname = parsed.hostname

    if hostname is None:
        return urlunsplit((parsed.scheme, "", parsed.path, "", ""))

    netloc = hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        netloc = f"{hostname}:{port}"

    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def sanitize_error_message(error: object) -> str:
    """Strip credentials, queries and fragments from URLs embedded in errors."""
    def clean_url(match: re.Match[str]) -> str:
        try:
            return sanitize_url(match.group())
        except ValueError:
            return REDACTED

    return re.sub(
        r"https?://[^\s'\"<>]+",
        clean_url,
        str(error),
    )


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _redact_item(key, item) for key, item in value.items()}


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(
                redact_mapping(payload),
                temporary_file,
                ensure_ascii=False,
                indent=2,
            )
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, path)
        temporary_path = None
        os.chmod(path, 0o600)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(redact_mapping(payload), ensure_ascii=False)

    with path.open("a", encoding="utf-8") as output:
        output.write(serialized)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())

    os.chmod(path, 0o600)


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _redact_item(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return REDACTED

    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized.endswith("url") and isinstance(value, str):
        return sanitize_url(value)

    return _redact_value(value)


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    return value
