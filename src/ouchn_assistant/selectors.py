"""Selectors shared by the directory reader and UI execution loop."""

ACTIVITY = ".learning-activity[id]"
CLICKABLE_ACTIVITY = f"{ACTIVITY} .clickable-area"
EXPAND_ALL = ".expand-collapse-all-button"
INCOMPLETE_FILTER = "input[ng-click='toggleIncompleteActivityFilter()']"
INCOMPLETE_FILTER_FALLBACK = "label:has-text('未完成') input"
MODULE = ".module"
RETURN_TO_COURSE = ".full-screen-mode-back"
RESOURCE_EXIT_CANDIDATES = (
    ".full-screen-mode-back:visible",
    ".reveal-modal.open .close:visible",
    ".reveal-modal.open [aria-label='关闭']:visible",
    ".reveal-modal.open button:visible:has-text('关闭')",
    "[role='dialog']:visible button:visible:has-text('关闭')",
    "button:visible:has-text('返回')",
    "a:visible:has-text('返回')",
)
PLAY_BUTTON = (
    ".mvp-toggle-play:visible, .vjs-play-control:visible, "
    "button[data-plyr='play']:visible"
)
BLOCKING_DIALOG_CANDIDATES = (
    "[role='dialog'], [role='alertdialog'], "
    ".ivu-modal-wrap, .modal, "
    ".reveal-modal.open, .prerequisites-confirmation-popup.open"
)
BLOCKING_DIALOG = ", ".join(
    f"{candidate.strip()}:visible"
    for candidate in BLOCKING_DIALOG_CANDIDATES.split(",")
)
