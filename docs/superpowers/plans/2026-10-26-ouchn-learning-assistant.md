# 国开 LMS 跨平台学习辅助器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个 Windows/macOS 通用、用户在环的国开 LMS 学习辅助器，提供只读课程探查、异常提醒、本地状态记录和离线报告，不代替用户完成学习动作。

**Architecture:** 使用 Python 3.12 和 Playwright 有头 Chrome。浏览器适配层只读取页面和守卫域名边界，状态机负责暂停与人工接管，产物层负责原子写入与脱敏；任何播放、确认、验证、提交和外链操作均由用户本人完成。

**Tech Stack:** Python 3.12、Playwright 1.64.1、pytest 8.4.2、JSON/JSONL、系统 Chrome

---

## 0. 执行规则

### 0.1 事实来源

- 方案基线：`国开LMS自动课时方案分析.md` v0.3。
- 当前环境：macOS Python 3.12.10、`.venv`、Playwright 1.64.1 已安装。
- 执行前仓库基线：尚未初始化 Git，没有业务代码。
- 每个平台单独创建 `.venv`，不得复制 macOS 虚拟环境到 Windows。

### 0.2 不可突破的边界

实现中不得出现以下能力：

- 自动播放、暂停、续播、拖拽、倍速或切换活动。
- 自动点击“继续学习”等确认。
- 自动答题、提交、发帖、回帖、验证或访问外链。
- 修改自动化标识、注入 stealth 脚本或伪造 OS 鼠标输入。
- 主动构造、重放或伪造学习进度接口请求。
- 记录 cookie、Authorization、密码、验证码、localStorage 或完整请求头。

### 0.3 阶段门控

1. 每个阶段独立提交、独立测试、独立评审。
2. 当前阶段未验收，不得开始下一阶段。
3. 阶段 0-1 只操作本地文件，不访问 LMS。
4. 阶段 2 先用脱敏 fixture，通过后才允许本人在场执行真实课程只读探查。
5. 阶段 3 不自动操作播放器，只观察用户操作后的页面状态。
6. 任何未知结果记录为 `unknown`，不得推断成功。

---

## 1. 目标文件结构

```text
.gitignore
pyproject.toml
requirements.txt
scripts/
  ouchn_assistant.py
src/ouchn_assistant/
  __init__.py
  artifacts.py
  browser.py
  cli.py
  config.py
  inspector.py
  models.py
  report.py
  session.py
tests/
  fixtures/
    course_en.html
    course_zh.html
  test_artifacts.py
  test_browser.py
  test_config.py
  test_inspector.py
  test_models.py
  test_report.py
  test_session.py
.runtime/                 # 运行生成，不提交
runs/                     # 运行生成，不提交
```

职责约束：

- `models.py` 只定义稳定数据模型和枚举。
- `config.py` 只负责参数解析、URL 校验和派生路径。
- `artifacts.py` 只负责脱敏、原子写入、事件追加和保留期清理。
- `session.py` 只负责状态转移和单实例锁。
- `browser.py` 只负责 Chrome 生命周期、页面读取和域名守卫。
- `inspector.py` 只把原始 DOM 信号转换成稳定活动模型。
- `report.py` 只读取本地产物，不启动浏览器。
- `cli.py` 只编排以上模块，不放业务细节。

---

## 2. 阶段总览

| 阶段 | 交付物 | 是否访问 LMS | 结束门槛 |
|---|---|---:|---|
| 0. 仓库与环境基线 | Git、忽略规则、依赖、包骨架 | 否 | 环境测试通过 |
| 1. 核心安全底座 | 配置、模型、原子写入、锁、状态机 | 否 | 核心单测全部通过 |
| 2. 只读探查 MVP | 浏览器适配、fixture 解析、快照 | fixture 后可只读访问 | 不进入活动页 |
| 3. 用户在环辅助 | 页面分类、通知、观察事件 | 是，本人在场 | 自动学习动作数为 0 |
| 4. 离线报告与清理 | report、保留期清理 | 否 | 报告不启动浏览器 |
| 5. 跨平台验收 | macOS/Windows 验证记录 | 只读烟测 | 两平台核心测试通过 |

---

## 3. 阶段 0：仓库与环境基线

### Task 0.1：初始化 Git 与敏感目录隔离

**Files:**
- Create: `.gitignore`
- Existing: `.DS_Store`

- [x] **Step 1：初始化仓库**

Run:

```bash
git init
```

Expected:

```text
Initialized empty Git repository
```

- [x] **Step 2：创建 `.gitignore`**

写入：

```gitignore
.DS_Store
.venv/
.runtime/
runs/
__pycache__/
*.py[cod]
.pytest_cache/
.coverage
htmlcov/
dist/
build/
*.egg-info/
```

- [x] **Step 3：验证敏感目录不会进入版本控制**

Run:

```bash
git check-ignore -v .venv .runtime runs .DS_Store
```

Expected：四个路径均匹配 `.gitignore` 规则。

- [x] **Step 4：提交仓库基线**

Run:

```bash
git add .gitignore requirements.txt 国开LMS自动课时方案分析.md docs/superpowers/plans/2026-10-26-ouchn-learning-assistant.md
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "chore: initialize learning assistant repository"
```

Expected：提交作者为 `sunbo.giog <sunbo.giog@bytedance.com>`。

### Task 0.2：收敛依赖并建立可测试包骨架

**Files:**
- Create: `pyproject.toml`
- Create: `src/ouchn_assistant/__init__.py`
- Create: `src/ouchn_assistant/cli.py`
- Create: `scripts/run_ouchn_assistant.py`
- Create: `tests/test_package.py`
- Modify: `requirements.txt`

- [x] **Step 1：将 `requirements.txt` 收敛为项目入口**

替换为：

```text
-e .[dev]
```

- [x] **Step 2：创建 `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "ouchn-learning-assistant"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "playwright==1.64.1",
]

[project.optional-dependencies]
dev = [
  "pytest==8.4.2",
]

[project.scripts]
ouchn-assistant = "ouchn_assistant.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [x] **Step 3：创建包版本文件**

`src/ouchn_assistant/__init__.py`：

```python
"""Cross-platform, human-in-the-loop OUCHN learning assistant."""

__version__ = "0.1.0"
```

- [x] **Step 4：创建最小 CLI 与唯一脚本入口**

`src/ouchn_assistant/cli.py`：

```python
import argparse
from collections.abc import Sequence

from ouchn_assistant import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ouchn-assistant")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args(argv)
    return 0
```

`scripts/run_ouchn_assistant.py`：

```python
from ouchn_assistant.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **Step 5：安装项目与开发依赖**

macOS:

```bash
.venv/bin/python -m pip install -e ".[dev]"
```

Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Expected：`playwright==1.64.1`、`pytest==8.4.2` 安装成功。

- [x] **Step 6：创建并运行包骨架烟测**

`tests/test_package.py`：

```python
import ouchn_assistant
import pytest

from ouchn_assistant.cli import main


def test_package_version() -> None:
    assert ouchn_assistant.__version__ == "0.1.0"


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--version"])

    assert error.value.code == 0
    assert capsys.readouterr().out.strip() == "0.1.0"
```

Run:

```bash
.venv/bin/python -c "import ouchn_assistant; print(ouchn_assistant.__version__)"
.venv/bin/ouchn-assistant --version
.venv/bin/python scripts/run_ouchn_assistant.py --version
.venv/bin/python -m pytest -v
```

Expected:

```text
0.1.0
0.1.0
0.1.0
2 passed
```

- [x] **Step 7：提交工程骨架**

```bash
git add pyproject.toml requirements.txt scripts src tests/test_package.py
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "build: add python package skeleton"
```

### 阶段 0 验收门

- [x] `git status --short` 无意外的 `.venv/`、`.runtime/` 或 `runs/`。
- [x] `.venv/bin/python -m pytest` 输出 `2 passed`。
- [ ] 用户审查提交历史后明确批准进入阶段 1。

**停止点：不访问 LMS，不启动业务浏览器。**

---

## 4. 阶段 1：核心安全底座

### Task 1.1：定义稳定数据模型

**Files:**
- Create: `src/ouchn_assistant/models.py`
- Create: `tests/test_models.py`

- [ ] **Step 1：先写模型测试**

`tests/test_models.py`：

```python
from ouchn_assistant.models import Activity, ActivityStatus, RunMode, SessionState


def test_activity_defaults_to_unknown() -> None:
    activity = Activity(activity_id="a1", title="Lesson 1", href="/activity/a1")
    assert activity.status is ActivityStatus.UNKNOWN


def test_public_enums_are_stable() -> None:
    assert [mode.value for mode in RunMode] == ["inspect", "assist", "report"]
    assert SessionState.USER_ACTION_REQUIRED.value == "user_action_required"
```

- [ ] **Step 2：运行测试确认失败**

```bash
.venv/bin/python -m pytest tests/test_models.py -v
```

Expected：失败原因是 `ouchn_assistant.models` 不存在。

- [ ] **Step 3：实现最小模型**

`src/ouchn_assistant/models.py`：

```python
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class RunMode(StrEnum):
    INSPECT = "inspect"
    ASSIST = "assist"
    REPORT = "report"


class SessionState(StrEnum):
    INIT = "init"
    PREFLIGHT = "preflight"
    LOGIN_REQUIRED = "login_required"
    READY = "ready"
    INSPECTING = "inspecting"
    OBSERVING = "observing"
    USER_ACTION_REQUIRED = "user_action_required"
    PAUSED = "paused"
    FAILED = "failed"
    STOPPED = "stopped"


class ActivityStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True, slots=True)
class Activity:
    activity_id: str
    title: str
    href: str
    type_hint: str = "unknown"
    status: ActivityStatus = ActivityStatus.UNKNOWN
    signals: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RunEvent:
    timestamp: str
    run_id: str
    state: SessionState
    event: str
    activity_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

- [ ] **Step 4：运行模型测试**

```bash
.venv/bin/python -m pytest tests/test_models.py -v
```

Expected：2 passed。

- [ ] **Step 5：提交**

```bash
git add src/ouchn_assistant/models.py tests/test_models.py
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "feat: define assistant domain models"
```

### Task 1.2：实现最小配置与课程 URL 校验

**Files:**
- Create: `src/ouchn_assistant/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1：先写配置测试**

`tests/test_config.py`：

```python
from pathlib import Path

import pytest

from ouchn_assistant.config import build_config, parse_course_url
from ouchn_assistant.models import RunMode


def test_parse_valid_course_url() -> None:
    parsed = parse_course_url("https://lms.ouchn.cn/course/10000118045/ng?locale=en")
    assert parsed.course_id == "10000118045"
    assert parsed.sanitized_url == "https://lms.ouchn.cn/course/10000118045/ng"


@pytest.mark.parametrize(
    "url",
    [
        "http://lms.ouchn.cn/course/100/ng",
        "https://evil.example/course/100/ng",
        "https://lms.ouchn.cn/user/courses",
    ],
)
def test_rejects_non_course_url(url: str) -> None:
    with pytest.raises(ValueError):
        parse_course_url(url)


def test_build_config_uses_project_local_paths(tmp_path: Path) -> None:
    config = build_config(
        mode=RunMode.INSPECT,
        course_url="https://lms.ouchn.cn/course/100/ng",
        project_root=tmp_path,
    )
    assert config.profile_dir == tmp_path / ".runtime" / "chrome-profile"
    assert config.runs_dir == tmp_path / "runs"
```

- [ ] **Step 2：确认测试失败**

```bash
.venv/bin/python -m pytest tests/test_config.py -v
```

Expected：失败原因是配置模块不存在。

- [ ] **Step 3：实现配置**

实现以下公开接口：

```python
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
    """只接受 https://lms.ouchn.cn/course/<数字>/ng，并删除 query/fragment。"""


def build_config(
    mode: RunMode,
    course_url: str,
    project_root: Path,
    screenshot_enabled: bool = False,
) -> AppConfig:
    """使用固定允许域名和项目内派生路径构建配置。"""
```

实现要求：

- 使用 `urllib.parse.urlsplit`，不得用字符串前缀判断主机。
- `scheme` 必须为 `https`。
- `hostname` 必须精确等于 `lms.ouchn.cn`。
- 路径必须匹配 `^/course/([0-9]+)/ng/?$`。
- `sanitized_url` 不含 query 和 fragment。

- [ ] **Step 4：运行配置测试**

```bash
.venv/bin/python -m pytest tests/test_config.py -v
```

Expected：5 passed。

- [ ] **Step 5：提交**

```bash
git add src/ouchn_assistant/config.py tests/test_config.py
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "feat: validate minimal assistant configuration"
```

### Task 1.3：实现脱敏与原子产物写入

**Files:**
- Create: `src/ouchn_assistant/artifacts.py`
- Create: `tests/test_artifacts.py`

- [ ] **Step 1：先写产物测试**

测试必须覆盖：

```python
def test_sanitize_url_drops_query_and_fragment() -> None:
    assert sanitize_url(
        "https://lms.ouchn.cn/course/100/ng?token=secret#part"
    ) == "https://lms.ouchn.cn/course/100/ng"


def test_redact_mapping_masks_secrets() -> None:
    value = redact_mapping(
        {"cookie": "abc", "Authorization": "Bearer x", "course_id": "100"}
    )
    assert value == {
        "cookie": "[REDACTED]",
        "Authorization": "[REDACTED]",
        "course_id": "100",
    }


def test_atomic_write_json_replaces_complete_document(tmp_path: Path) -> None:
    target = tmp_path / "snapshot.json"
    atomic_write_json(target, {"schema_version": 1, "activities": []})
    assert json.loads(target.read_text())["schema_version"] == 1
    assert not list(tmp_path.glob("*.tmp"))
```

- [ ] **Step 2：确认测试失败**

```bash
.venv/bin/python -m pytest tests/test_artifacts.py -v
```

- [ ] **Step 3：实现公开接口**

```python
SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "password",
    "token",
    "localstorage",
}


def sanitize_url(raw_url: str) -> str:
    """删除 query、fragment、用户名和密码部分。"""


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """按 key 大小写不敏感地遮蔽敏感值。"""


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """同目录临时文件写入、flush、fsync，再用 os.replace 原子替换。"""


def append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    """追加一行 UTF-8 JSON，flush 并 fsync。"""
```

约束：

- JSON 使用 `ensure_ascii=False`。
- 临时文件名必须带 `.tmp`，异常时清理。
- 不序列化 Playwright 对象、cookie 或 request headers。

- [ ] **Step 4：运行测试**

```bash
.venv/bin/python -m pytest tests/test_artifacts.py -v
```

Expected：全部通过。

- [ ] **Step 5：提交**

```bash
git add src/ouchn_assistant/artifacts.py tests/test_artifacts.py
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "feat: add private atomic artifact storage"
```

### Task 1.4：实现单实例锁和显式状态机

**Files:**
- Create: `src/ouchn_assistant/session.py`
- Create: `tests/test_session.py`

- [ ] **Step 1：先写状态机与锁测试**

测试必须覆盖：

```python
def test_valid_transition() -> None:
    machine = SessionMachine(run_id="run-1")
    machine.transition(SessionState.PREFLIGHT, "start")
    assert machine.state is SessionState.PREFLIGHT


def test_invalid_transition_is_rejected() -> None:
    machine = SessionMachine(run_id="run-1")
    with pytest.raises(InvalidTransition):
        machine.transition(SessionState.OBSERVING, "skip_preflight")


def test_profile_lock_rejects_second_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "profile.lock"
    first = ProfileLock(lock_path)
    first.acquire()
    with pytest.raises(ProfileInUse):
        ProfileLock(lock_path).acquire()
    first.release()
```

- [ ] **Step 2：确认测试失败**

```bash
.venv/bin/python -m pytest tests/test_session.py -v
```

- [ ] **Step 3：实现状态机**

公开接口：

```python
class InvalidTransition(RuntimeError):
    pass


class ProfileInUse(RuntimeError):
    pass


class SessionMachine:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.state = SessionState.INIT
        self.events: list[RunEvent] = []

    def transition(self, target: SessionState, event: str) -> RunEvent:
        """只允许方案 v0.3 第 6 节定义的转移，并记录 UTC 事件。"""


class ProfileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        """使用 O_CREAT | O_EXCL 原子创建锁，内容为当前 PID。"""

    def release(self) -> None:
        """仅持有者释放锁；不自动删除不明来源的旧锁。"""
```

状态转移表必须在模块内写成不可变映射；`STOPPED` 没有后继状态。

- [ ] **Step 4：运行测试**

```bash
.venv/bin/python -m pytest tests/test_session.py -v
```

Expected：全部通过。

- [ ] **Step 5：运行阶段 1 全量测试**

```bash
.venv/bin/python -m pytest -v
```

Expected：模型、配置、产物和状态机测试全部通过。

- [ ] **Step 6：提交**

```bash
git add src/ouchn_assistant/session.py tests/test_session.py
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "feat: enforce session state and profile locking"
```

### 阶段 1 验收门

- [ ] 非 LMS URL 全部被拒绝。
- [ ] 日志脱敏测试中 secret/cookie/token 命中数为 0。
- [ ] 同一 profile 第二实例无法启动。
- [ ] 非法状态转移必然抛错。
- [ ] 原子写入后不存在残留 `.tmp`。
- [ ] 用户审查测试结果和提交后明确批准进入阶段 2。

**停止点：仍不访问 LMS。**

---

## 5. 阶段 2：只读探查 MVP

### Task 2.1：实现浏览器生命周期与域名守卫

**Files:**
- Create: `src/ouchn_assistant/browser.py`
- Create: `tests/test_browser.py`

- [ ] **Step 1：先写纯函数边界测试**

```python
@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://lms.ouchn.cn/course/100/ng", True),
        ("https://lms.ouchn.cn.evil.example/course/100/ng", False),
        ("http://lms.ouchn.cn/course/100/ng", False),
        ("https://example.com/", False),
    ],
)
def test_is_allowed_lms_url(url: str, allowed: bool) -> None:
    assert is_allowed_lms_url(url) is allowed
```

- [ ] **Step 2：确认失败并实现纯函数**

```python
def is_allowed_lms_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname == "lms.ouchn.cn"
```

- [ ] **Step 3：实现 `BrowserSession`**

公开接口：

```python
class BrowserBoundaryError(RuntimeError):
    pass


class BrowserSession:
    def __init__(self, playwright: Playwright, config: AppConfig) -> None:
        self.playwright = playwright
        self.config = config
        self.context: BrowserContext | None = None

    def open(self) -> Page:
        """用 channel='chrome'、headless=False 和独立 profile 启动。"""

    def assert_allowed_page(self, page: Page) -> None:
        """非允许域名立即抛出 BrowserBoundaryError。"""

    def close(self) -> None:
        """关闭 context；不修改系统音量、电源设置或浏览器标识。"""
```

启动参数只允许：

```python
self.playwright.chromium.launch_persistent_context(
    user_data_dir=self.config.profile_dir,
    channel="chrome",
    headless=False,
    accept_downloads=False,
)
```

禁止添加 `ignore_default_args`、`AutomationControlled` 或 stealth 参数。

- [ ] **Step 4：运行浏览器边界测试**

```bash
.venv/bin/python -m pytest tests/test_browser.py -v
```

Expected：纯函数测试全部通过；不启动真实浏览器。

- [ ] **Step 5：提交**

```bash
git add src/ouchn_assistant/browser.py tests/test_browser.py
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "feat: add bounded browser session"
```

### Task 2.2：创建中英文脱敏 fixture

**Files:**
- Create: `tests/fixtures/course_zh.html`
- Create: `tests/fixtures/course_en.html`

- [ ] **Step 1：创建中文 fixture**

```html
<!doctype html>
<html lang="zh-CN">
<body>
  <section class="learning-activity" data-activity-id="video-1">
    <a class="clickable-area" href="/course/100/learning-activity/full-screen/video-1">
      <i class="font font-video" aria-label="音视频教材"></i>
      <span class="activity-title">第一讲</span>
      <span class="completion-state" data-complete="true">已完成</span>
    </a>
  </section>
</body>
</html>
```

- [ ] **Step 2：创建英文 fixture**

```html
<!doctype html>
<html lang="en">
<body>
  <section class="learning-activity" data-activity-id="page-2">
    <a class="clickable-area" href="/course/100/learning-activity/full-screen/page-2">
      <i class="font font-page" aria-label="Page"></i>
      <span class="activity-title">Reading two</span>
      <span class="completion-state" data-complete="false">Incomplete</span>
    </a>
  </section>
</body>
</html>
```

- [ ] **Step 3：确认 fixture 不含个人数据**

```bash
grep -RniE 'cookie|authorization|password|token|身份证|手机号|邮箱' tests/fixtures || true
```

Expected：无输出。

### Task 2.3：实现活动信号归一化

**Files:**
- Create: `src/ouchn_assistant/inspector.py`
- Create: `tests/test_inspector.py`

- [ ] **Step 1：先写归一化测试**

```python
from ouchn_assistant.inspector import normalize_activity
from ouchn_assistant.models import ActivityStatus


def test_normalize_activity_uses_structural_signals() -> None:
    activity = normalize_activity(
        {
            "activity_id": "video-1",
            "title": "第一讲",
            "href": "/course/100/learning-activity/full-screen/video-1",
            "icon_class": "font font-video",
            "aria_label": "音视频教材",
            "complete": "true",
        }
    )
    assert activity.activity_id == "video-1"
    assert activity.type_hint == "video"
    assert activity.status is ActivityStatus.COMPLETE


def test_missing_id_is_rejected() -> None:
    with pytest.raises(ActivityParseError):
        normalize_activity({"title": "No id", "href": ""})
```

- [ ] **Step 2：确认失败**

```bash
.venv/bin/python -m pytest tests/test_inspector.py -v
```

- [ ] **Step 3：实现公开接口**

```python
class ActivityParseError(ValueError):
    pass


def normalize_activity(raw: Mapping[str, str]) -> Activity:
    """优先使用 activity_id、href 和 icon class；文案只作为 signals 留存。"""


def collect_raw_activities(page: Page) -> list[dict[str, str]]:
    """读取活动卡片，不点击卡片、不进入详情页。"""


def inspect_course(page: Page) -> list[Activity]:
    """分段滚动，直到连续三轮活动数不再增长，再执行归一化。"""
```

实现约束：

- DOM 查询可从 `.learning-activity` 开始，但找不到时必须失败，不猜测点击。
- `collect_raw_activities` 只能读取属性和文本。
- 活动详情链接只作为字符串保存，不调用 `page.goto`。
- 完成态无法确认时必须为 `UNKNOWN`。

- [ ] **Step 4：使用本地 fixture 做浏览器集成测试**

测试用 `page.set_content(fixture.read_text())`，分别验证中文和英文 fixture 各解析 1 条活动；测试期间不得联网。

- [ ] **Step 5：运行 inspector 测试**

```bash
.venv/bin/python -m pytest tests/test_inspector.py -v
```

Expected：纯函数和 fixture 集成测试全部通过。

- [ ] **Step 6：提交**

```bash
git add src/ouchn_assistant/inspector.py tests/test_inspector.py tests/fixtures
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "feat: parse course activities without opening them"
```

### Task 2.4：实现 `inspect` CLI 编排

**Files:**
- Create: `src/ouchn_assistant/cli.py`
- Modify: `scripts/run_ouchn_assistant.py`
- Test: `tests/test_config.py`

- [ ] **Step 1：增加 CLI 参数测试**

覆盖：

- `--mode inspect --course-url <合法地址>` 成功解析。
- 缺少 `--course-url` 返回退出码 2。
- 非法域名返回退出码 2。
- `--screenshots` 默认 false。

- [ ] **Step 2：实现 `main(argv: Sequence[str] | None = None) -> int`**

流程固定为：

```text
解析参数
→ 构建 AppConfig
→ 创建 run_id 与运行目录
→ 获取 ProfileLock
→ INIT → PREFLIGHT
→ 启动有头 Chrome
→ 未登录时 LOGIN_REQUIRED，等待用户在终端按 Enter 后重新检查
→ READY → INSPECTING
→ 只读采集目录
→ 原子写入 activities_snapshot.json
→ READY → STOPPED
→ finally 中关闭浏览器并释放锁
```

退出码：

| 退出码 | 含义 |
|---:|---|
| 0 | 成功 |
| 2 | 参数或配置错误 |
| 3 | profile 正在使用 |
| 4 | 浏览器或页面边界错误 |
| 5 | 页面结构无法解析 |
| 6 | 本地产物写入失败 |

- [ ] **Step 3：写入快照 schema**

```json
{
  "schema_version": 1,
  "run_id": "20261026T100000Z-ab12cd",
  "course_id": "10000118045",
  "observed_at": "2026-10-26T10:00:00Z",
  "activities": []
}
```

- [ ] **Step 4：运行全量测试**

```bash
.venv/bin/python -m pytest -v
```

- [ ] **Step 5：执行本地空白页烟测**

增加隐藏测试参数 `--smoke-browser`，只启动 Chrome、打开 `about:blank`、记录版本并关闭；该参数不得访问课程。

```bash
.venv/bin/python scripts/run_ouchn_assistant.py --smoke-browser
```

Expected：退出码 0，浏览器正常关闭，profile 锁被释放。

- [ ] **Step 6：提交**

```bash
git add src/ouchn_assistant/cli.py scripts/run_ouchn_assistant.py tests
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "feat: add read-only course inspection command"
```

### 阶段 2 验收门

- [ ] 所有 fixture 测试通过。
- [ ] `inspect` 源码不存在活动详情 `goto`、播放器控制或提交操作。
- [ ] 空白页烟测通过并释放 profile 锁。
- [ ] 用户本人在场，首次真实课程探查仅停留课程目录页。
- [ ] 人工核对快照数量、标题和完成态；存在不一致则停止，不进入阶段 3。
- [ ] 用户批准进入阶段 3。

**停止点：不进入活动详情页。**

---

## 6. 阶段 3：用户在环辅助

### Task 3.1：实现页面状态分类器

**Files:**
- Modify: `src/ouchn_assistant/browser.py`
- Create: `tests/test_browser.py`

- [ ] **Step 1：定义只读分类结果**

在 `models.py` 增加：

```python
class PageKind(StrEnum):
    COURSE_INDEX = "course_index"
    ACTIVITY = "activity"
    LOGIN = "login"
    USER_CONFIRMATION = "user_confirmation"
    STRONG_VERIFICATION = "strong_verification"
    EXTERNAL = "external"
    UNKNOWN = "unknown"
```

- [ ] **Step 2：写分类器测试**

至少覆盖：

- LMS 课程 URL → `COURSE_INDEX`。
- LMS 活动 URL → `ACTIVITY`。
- 非 LMS URL → `EXTERNAL`。
- 检测到滑块、人脸、扫码结构信号 → `STRONG_VERIFICATION`。
- 同时存在多个冲突信号 → `UNKNOWN`，不得选择最方便的结果。

- [ ] **Step 3：实现 `classify_page(page: Page) -> PageKind`**

约束：

- 只读取 URL、可见 role、class、aria 属性和少量脱敏文本。
- 不执行 click、dispatch_event、evaluate 修改、表单填充或播放器控制。
- 分类不确定时返回 `UNKNOWN`。

- [ ] **Step 4：运行测试并提交**

```bash
.venv/bin/python -m pytest tests/test_browser.py -v
git add src/ouchn_assistant/models.py src/ouchn_assistant/browser.py tests/test_browser.py
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "feat: classify pages for human takeover"
```

### Task 3.2：实现人工接管通知与观察循环

**Files:**
- Modify: `src/ouchn_assistant/session.py`
- Modify: `src/ouchn_assistant/cli.py`
- Modify: `tests/test_session.py`

- [ ] **Step 1：写观察循环测试**

使用假的 `PageKind` 序列验证：

```text
READY → ACTIVITY → OBSERVING
OBSERVING → USER_CONFIRMATION → USER_ACTION_REQUIRED
USER_ACTION_REQUIRED → 用户确认恢复 → OBSERVING
OBSERVING → EXTERNAL → USER_ACTION_REQUIRED
OBSERVING → UNKNOWN → PAUSED
浏览器关闭 → PAUSED，当前活动记录 unknown
```

- [ ] **Step 2：实现通知接口**

```python
def notify_user(message: str) -> None:
    print("\a", end="", flush=True)
    print(message, flush=True)
```

只使用终端铃声，不调用 macOS/Windows 专属通知 API。

- [ ] **Step 3：实现 `assist` 循环**

规则：

- 用户在浏览器中手动进入活动。
- 程序每 2 秒只读分类当前页面。
- `USER_CONFIRMATION`、`STRONG_VERIFICATION`、`EXTERNAL`、`UNKNOWN` 立即通知并暂停。
- 恢复必须由用户在终端输入 `resume`。
- 程序不点击任何弹窗，也不关闭 popup。
- 用户返回课程首页后才读取完成徽标并写 `events.jsonl`。

- [ ] **Step 4：增加 Ctrl-C 清理测试**

验证：

- 写入 `STOPPED` 事件。
- 浏览器 context 关闭。
- profile 锁释放。
- 正在观察的活动写为 `unknown`。

- [ ] **Step 5：运行全量测试**

```bash
.venv/bin/python -m pytest -v
```

- [ ] **Step 6：提交**

```bash
git add src/ouchn_assistant/session.py src/ouchn_assistant/cli.py tests/test_session.py
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "feat: add human-in-the-loop observation mode"
```

### 阶段 3 验收门

- [ ] 自动播放、确认、提交、论坛和验证调用数均为 0。
- [ ] 跨域、确认、验证和未知页面全部暂停。
- [ ] 恢复只能由终端显式输入触发。
- [ ] 断网、关闭浏览器或 Ctrl-C 后不自动重试活动。
- [ ] 用户批准进入阶段 4。

---

## 7. 阶段 4：离线报告与保留期清理

### Task 4.1：实现离线报告

**Files:**
- Create: `src/ouchn_assistant/report.py`
- Create: `tests/test_report.py`
- Modify: `src/ouchn_assistant/cli.py`

- [ ] **Step 1：写报告测试**

构造本地快照和事件，验证输出固定包含：

```text
课程：10000118045
观察完成：1
未完成：1
状态未知：1
需要复核：1
注意：本报告仅为本地观察结果，平台 UI 是最终事实来源。
```

测试必须断言 report 模式没有导入或启动 Playwright。

- [ ] **Step 2：实现报告接口**

```python
@dataclass(frozen=True, slots=True)
class ReportSummary:
    course_id: str
    complete: int
    incomplete: int
    unknown: int
    review_required: int


def build_report(run_dir: Path) -> ReportSummary:
    """只读取 snapshot 与 events；损坏文件抛出 ReportDataError。"""


def format_report(summary: ReportSummary) -> str:
    """输出固定中文摘要和事实来源声明。"""
```

- [ ] **Step 3：接入 `--mode report`**

report 模式：

- 需要 `--run-dir`。
- 不启动 Chrome。
- 不读取 profile。
- 损坏 JSON 返回退出码 7。

- [ ] **Step 4：运行测试并提交**

```bash
.venv/bin/python -m pytest tests/test_report.py -v
git add src/ouchn_assistant/report.py src/ouchn_assistant/cli.py tests/test_report.py
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "feat: add offline observation report"
```

### Task 4.2：实现保留期清理

**Files:**
- Modify: `src/ouchn_assistant/artifacts.py`
- Modify: `tests/test_artifacts.py`

- [ ] **Step 1：写清理测试**

验证：

- 8 天前截图被删除。
- 7 天内截图保留。
- 31 天前运行目录被删除。
- 30 天内运行目录保留。
- `.runtime/chrome-profile` 永远不被清理函数触碰。

- [ ] **Step 2：实现清理接口**

```python
def cleanup_artifacts(
    runs_dir: Path,
    now: datetime,
    run_retention_days: int = 30,
    screenshot_retention_days: int = 7,
) -> list[Path]:
    """只允许删除 runs_dir 解析后路径内的内容，返回已删除路径。"""
```

安全要求：

- 调用 `resolve()` 后确认目标仍位于 `runs_dir.resolve()` 内。
- 符号链接不跟随。
- 删除前写入清理日志。
- 任一边界检查失败时不删除任何内容。

- [ ] **Step 3：运行全量测试并提交**

```bash
.venv/bin/python -m pytest -v
git add src/ouchn_assistant/artifacts.py tests/test_artifacts.py
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "feat: enforce local artifact retention"
```

### 阶段 4 验收门

- [ ] report 模式不启动浏览器。
- [ ] 报告明确“本地观察结果”。
- [ ] 清理逻辑无法离开 `runs/`。
- [ ] 用户批准进入阶段 5。

---

## 8. 阶段 5：跨平台验收

### Task 5.1：macOS 验收

- [ ] **Step 1：从干净环境重建**

```bash
rm -rf .venv
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

- [ ] **Step 2：运行全部测试**

```bash
.venv/bin/python -m pytest -v
```

Expected：全部通过。

- [ ] **Step 3：执行空白页烟测**

```bash
.venv/bin/python scripts/run_ouchn_assistant.py --smoke-browser
```

- [ ] **Step 4：本人在场执行只读探查**

```bash
.venv/bin/python scripts/run_ouchn_assistant.py \
  --mode inspect \
  --course-url "https://lms.ouchn.cn/course/10000118045/ng"
```

验收：只停留课程首页；活动快照与人工目录数量一致。

### Task 5.2：Windows 10/11 验收

- [ ] **Step 1：单独创建 Windows 虚拟环境**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

- [ ] **Step 2：运行全部测试**

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

- [ ] **Step 3：执行空白页烟测**

```powershell
.\.venv\Scripts\python.exe scripts\run_ouchn_assistant.py --smoke-browser
```

- [ ] **Step 4：本人在场执行只读探查**

```powershell
.\.venv\Scripts\python.exe scripts\run_ouchn_assistant.py `
  --mode inspect `
  --course-url "https://lms.ouchn.cn/course/10000118045/ng"
```

验收：

- 中文标题写入 JSON 后无乱码。
- Ctrl-C 后浏览器关闭、锁释放。
- 快照 schema 与 macOS 完全相同。
- 不请求辅助功能、输入注入或管理员权限。

### Task 5.3：最终边界审计

- [ ] **Step 1：检索禁止能力**

```bash
grep -RniE \
  'playbackRate|currentTime|dispatch_event|navigator\.webdriver|AutomationControlled|SendInput|Quartz|pyautogui|course/activities-read|statistics/api/user-visits' \
  src scripts tests || true
```

Expected：除断言“禁止出现”的测试字符串外，无业务实现命中。

- [ ] **Step 2：检索敏感信息**

```bash
grep -RniE 'authorization|cookie|password|localstorage|bearer ' runs src tests || true
```

Expected：业务产物无真实敏感值；测试只包含人工构造的脱敏样例。

- [ ] **Step 3：核对提交作者**

```bash
git log --format='%h %an <%ae> %s'
```

Expected：所有提交作者统一为 `sunbo.giog <sunbo.giog@bytedance.com>`。

- [ ] **Step 4：执行最终测试**

```bash
.venv/bin/python -m pytest -v
git status --short
```

Expected：测试全部通过，工作区无未说明改动。

- [ ] **Step 5：提交跨平台记录**

只提交脱敏的版本与测试摘要，不提交 profile、截图或课程个人数据：

```bash
git add 国开LMS自动课时方案分析.md docs/superpowers/plans/2026-10-26-ouchn-learning-assistant.md
git -c user.name="sunbo.giog" -c user.email="sunbo.giog@bytedance.com" \
  commit -m "docs: record cross-platform acceptance"
```

---

## 9. 最终完成标准

只有以下条件全部满足，项目才可标记完成：

- [ ] macOS 与 Windows 使用 Python 3.12 独立虚拟环境。
- [ ] 两个平台全量 pytest 通过。
- [ ] `inspect` 不进入活动页。
- [ ] `assist` 不执行播放、确认、验证、提交或切换活动。
- [ ] 跨域和未知页面立即暂停。
- [ ] profile 单实例锁可靠释放。
- [ ] 快照原子写入且 schema 一致。
- [ ] report 只读取本地数据，不启动浏览器。
- [ ] 日志和产物无 cookie、token、密码和验证码。
- [ ] `.venv/`、`.runtime/`、`runs/` 均未进入 Git。
- [ ] 提交作者统一，工作区干净。
- [ ] 用户审查实际行为并确认符合 v0.3 方案边界。

## 10. 计划覆盖映射

| v0.3 方案要求 | 执行任务 |
|---|---|
| 最小输入与 URL 白名单 | Task 1.2 |
| 稳定模型与 `unknown` 默认值 | Task 1.1 |
| 原子写入与脱敏 | Task 1.3 |
| 单实例和状态机 | Task 1.4 |
| 独立 profile、有头 Chrome | Task 2.1 |
| 中英文页面兼容 | Task 2.2、2.3 |
| 只读目录探查 | Task 2.3、2.4 |
| 人工确认与强验证暂停 | Task 3.1、3.2 |
| 离线报告 | Task 4.1 |
| 保留期清理 | Task 4.2 |
| Windows/macOS 一致性 | Task 5.1、5.2 |
| 禁止反检测与自动学习动作 | Task 5.3 |

## 11. 执行方式

推荐按本文顺序进行内联执行：

1. 一次只执行一个 Task。
2. 每个 Task 坚持“失败测试 → 最小实现 → 通过测试 → 提交”。
3. 每个阶段结束后展示测试结果、提交记录和未解决问题。
4. 用户明确批准后再进入下一阶段。
5. 阶段 2 真实课程探查和阶段 3 用户在环观察必须由用户本人在场。
