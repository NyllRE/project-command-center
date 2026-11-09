"""Agent hooks for Project Command Center: card context in, PR link out.

Two hooks, both synchronous and local. Neither opens a socket, starts a process or reads a
credential: the only files either one touches are the SQLite board and (on a link-row miss) the
session database, both through ``board_store.py``.

  * ``pre_llm_call``   read only. The session's project card, one line per part, at most 400
    characters, injected into the turn's user message. No project, no injection. Three
    primary-key lookups, never a scan of the board.
  * ``post_tool_call`` after a ``terminal`` call that actually ran ``gh pr create`` and whose result
    printed a ``github.com/<owner>/<repo>/pull/<n>`` URL: records that PR on the session's link row,
    and moves the card to ``review`` when it was still ``backlog`` or ``active``.

Both fail open: every failure is a logged ``None``, because a hook that raises must never cost a
turn. ``post_tool_call`` additionally honours a kill file — ``auto_link.off`` in the plugin's data
directory — checked on every call so switching auto-link off needs no restart.

Resolution follows the session's ``link`` row (``use_cwd=False``), walking the compression lineage
when the runtime session id is newer than the id the Desktop stored the row under. The row's
``repo`` decides which PR a session may claim: a repository named on the row must match the URL, and
only a row whose project was resolved from the session's own working directory (``resolved_by =
"cwd"``) may claim a PR without naming one.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

PLUGIN_NAME = "project-command-center"
KILL_FILENAME = "auto_link.off"
MAX_CONTEXT = 400
LANES = "backlog, active, review, blocked, done"
AUTO_STATUS = "review"
AUTO_FROM = ("backlog", "active")

# The command, not a substring: an agent may `grep -rn 'gh pr create' .` or `gh pr view` and print a
# PR URL without ever creating one. A real invocation starts a command (start of text or a newline)
# or follows a shell separator: `;`, `&`, `|`, `(`. The quoted-grep case fails this on purpose.
_GH_PR_CREATE = re.compile(r"(?:^|[;&|(]\s*)gh\s+pr\s+create\b", re.MULTILINE)

# Scoped to output the caller has already proven came from ``gh pr create``; ``\d+`` stops at the
# first non-digit so a trailing slash or query never widens the number.
_PR_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)/pull/(\d+)"
)

# Result text that says the create failed even when no exit code reached us (plain-string results).
_FAILURE_TEXT = ("already exists", "failed to create", "could not create", "http 422",
                 "422 unprocessable", "authentication failed", "not logged in")

_STORE_LOCK = threading.Lock()
_STORE: Any = None


# --- Plumbing ----------------------------------------------------------------


def _store() -> Any:
    """``board_store.py`` from the plugin root, loaded by path once. The handle is code, not state."""
    global _STORE
    if _STORE is not None:
        return _STORE
    with _STORE_LOCK:
        if _STORE is None:
            path = Path(__file__).resolve().parent / "board_store.py"
            spec = importlib.util.spec_from_file_location("pcc_hook_board_store", path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _STORE = module
    return _STORE


def kill_file() -> Path:
    """``<hermes home>/plugin-data/project-command-center/auto_link.off`` for the active profile."""
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME) / KILL_FILENAME


def _killed() -> bool:
    """True when auto-link is switched off. Checked on every call: no cache, no restart.

    A data directory we cannot even resolve counts as off — a write whose kill switch cannot be
    proven absent must not happen.
    """
    try:
        return kill_file().exists()
    except Exception:
        log.debug("%s: kill file unreadable, treating auto-link as off", PLUGIN_NAME, exc_info=True)
        return True


def _broadcast(project_id: str, pr: int) -> None:
    payload: dict[str, Any] = {"project_id": project_id, "kind": "link", "pr": pr, "source": "auto"}
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(PLUGIN_NAME, "board.changed", payload)
    except Exception:  # no gateway in a CLI-only or subagent process; the row is already written
        log.debug("%s: board.changed broadcast skipped", PLUGIN_NAME, exc_info=True)


def _context(label: str, lane: str, issue: Any, title: str, pr: Any) -> str:
    """At most ``MAX_CONTEXT`` characters naming the card, its work item and what to do next."""
    head = f"Board: {label} — {lane}"
    pr_line = f"PR: #{pr}" if pr else "PR: no PR yet"
    tail = (f'Put "Closes #<issue>" in the PR body. Move the card with pcc_set_status '
            f"(lanes: {LANES}).")
    if issue:
        prefix = f"Issue: #{issue} "
        room = MAX_CONTEXT - len(head) - len(pr_line) - len(tail) - 3 - len(prefix)
        body = title if len(title) <= room else title[: max(room - 1, 0)] + "…"
        issue_line = f"{prefix}{body}".rstrip()
    else:
        issue_line = "Issue: no issue linked"
    text = f"{head}\n{issue_line}\n{pr_line}\n{tail}"
    return text if len(text) <= MAX_CONTEXT else text[: MAX_CONTEXT - 1] + "…"


# --- Hooks -------------------------------------------------------------------


_CWD_PROJECT_CACHE: dict[str, tuple[float, str]] = {}
_CWD_PROJECT_TTL = 10.0


def _project_from_session_cwd(store: Any, session_id: str) -> str:
    """The project this session's cwd belongs to, cached briefly.

    Resolving costs a config read plus a project scan, and a chat asks on every turn, so the
    answer is reused for a few seconds per session. Keyed by session because the cwd is the
    session's own while its turn runs.
    """
    now = time.monotonic()
    hit = _CWD_PROJECT_CACHE.get(session_id)
    if hit and now - hit[0] < _CWD_PROJECT_TTL:
        return hit[1]
    pid = str(store.resolve_project(session_id=session_id) or "").strip()
    if len(_CWD_PROJECT_CACHE) > 256:
        _CWD_PROJECT_CACHE.clear()
    _CWD_PROJECT_CACHE[session_id] = (now, pid)
    return pid


def _is_home_bucket(project: dict[str, Any]) -> bool:
    """True for the catch-all project that owns only the home directory.

    Its card is noise on every turn: a chat sitting in ``~`` is not working on a card.
    """
    paths = [str(p) for p in (project.get("paths") or [])]
    return bool(paths) and all(p == str(Path.home()) for p in paths)


def pre_llm_call(**kwargs: Any) -> Optional[dict[str, str]]:
    """The card this session works in, or ``None``. Read only: one project, one turn."""
    try:
        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return None
        store = _store()
        card = store.session_card(session_id)
        link = card.get("link") or {}
        project_id = str(link.get("project_id") or "").strip()
        if not project_id:
            # No link row yet: a chat opened from the board has one only after its first tool
            # call. Fall back to the session's own cwd, which is the project folder the draft
            # was anchored to, so turn one already knows its card.
            project_id = _project_from_session_cwd(store, session_id)
            if not project_id:
                return None
            card = store.card_for_project(project_id, session_id=session_id)
        status = card.get("status") or {}
        project = card.get("project") or {}
        if _is_home_bucket(project):
            return None
        lane = status.get("status") or "backlog (unset)"
        label = str(project.get("label") or project_id)
        return {"context": _context(label, lane, link.get("issue"), link.get("title") or "",
                                    link.get("pr"))}
    except Exception:
        log.debug("%s: pre_llm_call skipped", PLUGIN_NAME, exc_info=True)
        return None


def post_tool_call(**kwargs: Any) -> None:
    """Auto-link a PR that a terminal ``gh pr create`` just opened. Never raises, returns None."""
    try:
        _auto_link(kwargs)
    except Exception:
        log.debug("%s: post_tool_call skipped", PLUGIN_NAME, exc_info=True)
    return None


def _tool_failed(payload: dict[str, Any], text: str) -> bool:
    """True when the call itself reports failure. Absent evidence is not a failure.

    ``post_tool_call`` carries ``status`` (``ok`` / ``error`` / ``cancelled`` …) and the error
    fields; the terminal result is JSON with ``exit_code``, ``error`` and ``output``. A plain-string
    result (older callers, tests) has none of that, so only unambiguous failure text counts.
    """
    status = str(payload.get("status") or "").strip().lower()
    if status in ("error", "failed", "failure", "cancelled", "timeout", "blocked"):
        return True
    if payload.get("error_type") or payload.get("error_message"):
        return True
    data: Any = None
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if isinstance(data, dict):
        if data.get("error"):
            return True
        code = data.get("exit_code")
        if isinstance(code, int) and code != 0:
            return True
        if str(data.get("status") or "").strip().lower() in ("error", "cancelled", "timeout", "failed"):
            return True
        body = data.get("output")
        if isinstance(body, str):
            text = f"{text}\n{body}"
    lowered = text.lower()
    return any(needle in lowered for needle in _FAILURE_TEXT)


def _auto_link(payload: dict[str, Any]) -> bool:
    """The write, gated in this order: tool, command, result, kill file, session, project, repo."""
    if payload.get("tool_name") != "terminal":
        return False
    args = payload.get("args")
    command = args.get("command") if isinstance(args, dict) else ""
    if not isinstance(command, str) or _GH_PR_CREATE.search(command) is None:
        return False
    result = payload.get("result")
    text = result if isinstance(result, str) else str(result or "")
    match = _PR_URL.search(text)
    if match is None:
        return False
    if _tool_failed(payload, text):
        return False
    if _killed():
        return False
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return False
    store = _store()
    # The row may live under an older compression ancestor; write back to the key the Desktop reads.
    key, row = store.resolve_link(session_id)
    if row is None:
        return False
    project_id = str(row.get("project_id") or "").strip()
    if not project_id:
        return False
    repo = str(row.get("repo") or "").strip()
    wanted = f"{match.group(1)}/{match.group(2)}"
    if repo:
        if repo.lower() != wanted.lower():
            return False
    elif str(row.get("resolved_by") or "") != "cwd":
        # An empty repo is only trustworthy when the project came from the session's own folder.
        # A Desktop-attached or explicitly named project must match the URL's repo.
        return False
    pr = int(match.group(3))
    if not store.set_link(key, project_id, repo=repo or None, pr=pr,
                          pr_url=match.group(0)).get("ok"):
        return False
    status = store.get_status(project_id) or {}
    if status.get("status") in AUTO_FROM:
        # Compare-and-set: a user's move to another lane between this read and the write wins.
        store.move_status_if_current(project_id, status["status"], AUTO_STATUS,
                                     status.get("note") or None, "auto")
    _broadcast(project_id, pr)
    return True


# --- Entry point -------------------------------------------------------------


def register(ctx) -> None:  # noqa: ANN001 - the PluginContext type lives in core
    """Bind both callbacks. No I/O here: the hooks read the board only when they fire."""
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    log.debug("%s: registered hooks pre_llm_call, post_tool_call", PLUGIN_NAME)
