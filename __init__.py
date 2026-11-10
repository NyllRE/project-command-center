"""Project Command Center, agent half: three tools over the board store, plus two board hooks.

The Desktop half renders the board; this half lets the agent read it and move it. Both write to the
same local SQLite file through ``board_store.py`` at the plugin root, so a status the agent sets is
the status the board shows after the next ``board.changed`` event.

  * ``pcc_board``       read the board, optionally one project's row, links and status.
  * ``pcc_set_status``  move this session's card, or a project's, to backlog / active / review /
    blocked / done.
  * ``pcc_work_item``   attach or detach the issue or PR the current session works on.

``hooks.py`` holds the two agent hooks registered alongside the tools: ``pre_llm_call`` injects a
<= 400 character card summary into the turn, and ``post_tool_call`` records the PR a terminal
``gh pr create`` opened (unless ``auto_link.off`` sits in the plugin's data directory). Neither
hook reaches the network or starts a process; both read and write the same local store.

Every handler takes ``(args, **kwargs)`` (the dispatcher signature-inspects context kwargs such as
``session_id``), never raises, and returns a JSON string. The project is resolved with
``board_store.resolve_project_detail``: explicit id, unique label, this session's link row (following
the compression lineage when the runtime id is newer than the stored one), then the longest path
prefix of the agent's working directory. A tool may create a bare project row only while the project
table is still empty; after the Desktop has synced anything, an id it does not know is
``unknown_project``. Writes record ``source: "agent"`` and broadcast ``board.changed`` so the Desktop
refreshes without polling.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

PLUGIN_NAME = "project-command-center"
PLUGIN_VERSION = "0.4.0"
TOOLSET = "project_command_center"

_STORE_LOCK = threading.Lock()
_STORE: Any = None


def _store() -> Any:
    """``board_store.py``, loaded by path once. The handle is code, not state."""
    global _STORE
    if _STORE is not None:
        return _STORE
    with _STORE_LOCK:
        if _STORE is None:
            path = Path(__file__).resolve().parent / "board_store.py"
            spec = importlib.util.spec_from_file_location("pcc_board_store", path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _STORE = module
    return _STORE


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False)


def _broadcast(kind: str, project_id: Optional[str], **fields: Any) -> None:
    payload = {"project_id": project_id, "kind": kind,
               **{k: v for k, v in fields.items() if v is not None}}
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN_NAME, "board.changed", payload)
    except Exception:  # no gateway in a CLI-only process; the row is already written
        log.debug("%s: board.changed broadcast skipped", PLUGIN_NAME, exc_info=True)


def _session_id(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("session_id") or "").strip()


def _guard(name: str) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """A handler that cannot raise: every failure becomes ``{ok: false, code, error}``."""
    def wrap(fn: Callable[..., str]) -> Callable[..., str]:
        def handler(args: Any, **kwargs: Any) -> str:
            try:
                return fn(args if isinstance(args, dict) else {}, **kwargs)
            except Exception as exc:
                log.warning("%s: %s failed: %s", PLUGIN_NAME, name, exc, exc_info=True)
                return _json({"ok": False, "code": "internal_error",
                              "error": f"{type(exc).__name__}: {exc}", "tool": name})
        handler.__name__ = fn.__name__
        handler.__doc__ = fn.__doc__
        return handler
    return wrap


# --- Handlers ----------------------------------------------------------------


@_guard("pcc_board")
def _pcc_board(args: dict[str, Any], **kwargs: Any) -> str:
    store = _store()
    board = store.board()
    if not board.get("ok"):
        return _json(board)
    session_id = _session_id(kwargs)
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        cwd = str(resolve_agent_cwd())
    except Exception:
        cwd = ""
    wanted = str(args.get("project") or "").strip()
    project_id = store.resolve_project(session_id=session_id, explicit=wanted)
    payload: dict[str, Any] = {
        "ok": True,
        "code": "ok",
        "resolved": project_id,
        "resolution": ("explicit" if wanted and project_id == wanted
                       else "label" if wanted else "context" if project_id else None),
        "session_id": session_id,
        "cwd": cwd,
        "projects": [{"project_id": p["project_id"], "label": p["label"], "paths": p["paths"]}
                     for p in board.get("projects", [])],
        "statuses": board.get("statuses", []),
        "links": board.get("links", []),
        "projects_synced": board.get("projects_synced", 0),
    }
    if wanted and not store.project_exists(project_id):
        return _json({"ok": False, "code": "unknown_project",
                      "error": f"no project matches {wanted!r}",
                      "projects": payload["projects"],
                      "projects_synced": payload["projects_synced"]})
    if project_id:
        _, session_link = store.resolve_link(session_id) if session_id else ("", None)
        payload["project"] = {"project_id": project_id,
                              "status": store.get_status(project_id),
                              "link": session_link}
    return _json(payload)


@_guard("pcc_set_status")
def _pcc_set_status(args: dict[str, Any], **kwargs: Any) -> str:
    store = _store()
    status = str(args.get("status") or "").strip().lower()
    if not status:
        return _json({"ok": False, "code": "missing_status", "error": "status is required"})
    calling = _session_id(kwargs)
    wanted_session = str(args.get("session_id") or "").strip()
    wanted_project = str(args.get("project") or "").strip()
    # An explicit session id always means that session's card. Without one, the calling session's
    # card is the default, unless the caller named a project: then the project lane moves, which is
    # what this tool did before sessions had lanes.
    session_id = wanted_session or (calling if not wanted_project else "")
    if session_id:
        project_id = store.resolve_project(session_id=session_id, explicit=wanted_project)
        if not project_id:
            return _json({"ok": False, "code": "unknown_project",
                          "error": "no project matched: pass project, or attach this session first"})
        result = store.set_status(project_id, status, args.get("note"), "agent",
                                  auto_project=True, session_id=session_id)
        if not result.get("ok"):
            return _json(result)
        link = result["link"]
        _broadcast("status", link.get("project_id") or project_id, status=link.get("status"),
                   session_id=link.get("session_id"), source="agent")
        return _json({"ok": True, "code": "ok", "scope": "session",
                      "session_id": link.get("session_id"), "project_id": project_id,
                      "status": link.get("status"), "note": link.get("note"),
                      "source": link.get("status_source"),
                      "updated_at": link.get("status_updated_at")})
    project_id = store.resolve_project(session_id=calling, explicit=wanted_project)
    if not project_id:
        return _json({"ok": False, "code": "unknown_project",
                      "error": "no project matched: pass project, or attach this session first"})
    result = store.set_status(project_id, status, args.get("note"), "agent", auto_project=True)
    if not result.get("ok"):
        return _json(result)
    row = result["status"]
    _broadcast("status", project_id, status=row["status"], source="agent")
    return _json({"ok": True, "code": "ok", "scope": "project", "project_id": project_id,
                  "status": row["status"], "note": row["note"], "source": row["source"],
                  "updated_at": row["updated_at"]})


@_guard("pcc_work_item")
def _pcc_work_item(args: dict[str, Any], **kwargs: Any) -> str:
    store = _store()
    action = str(args.get("action") or "").strip().lower()
    kind = str(args.get("kind") or "").strip().lower()
    if action not in ("attach", "detach"):
        return _json({"ok": False, "code": "bad_action", "error": "action must be attach or detach"})
    if kind not in ("issue", "pr"):
        return _json({"ok": False, "code": "bad_kind", "error": "kind must be issue or pr"})
    if args.get("number") in (None, ""):
        return _json({"ok": False, "code": "bad_number", "error": "number is required"})
    session_id = _session_id(kwargs)
    if not session_id:
        return _json({"ok": False, "code": "no_session",
                      "error": "this tool needs a session id; it attaches the current session"})
    if action == "detach":
        key, _ = store.resolve_link(session_id)
        result = store.detach_item(key or session_id, kind, args.get("number"))
        if not result.get("ok"):
            return _json(result)
        link = result["link"]
        _broadcast("link", link.get("project_id"), issue=link.get("issue"), pr=link.get("pr"),
                   source="agent")
        return _json({"ok": True, "code": "ok", "action": "detach", "kind": kind,
                      "link": link})

    project_id, resolved = store.resolve_project_detail(session_id=session_id,
                                                        explicit=args.get("project"))
    if not project_id:
        return _json({"ok": False, "code": "unknown_project",
                      "error": "no project matched: pass project, or attach this session first"})
    # Write back to the row's own key: after a compression the runtime id differs from the stored one.
    key, existing = store.resolve_link(session_id)
    existing = existing or {}
    result = store.set_link(
        key or session_id, project_id, existing.get("repo"),
        issue=args.get("number") if kind == "issue" else None,
        pr=args.get("number") if kind == "pr" else None, auto_project=True,
        # Keep the row's provenance when the project came from the row itself; only a cwd match or
        # an explicit id is new evidence about how this session was placed.
        resolved_by={"cwd": "cwd", "explicit": "agent", "label": "agent"}.get(resolved or ""))
    if not result.get("ok"):
        return _json(result)
    link = result["link"]
    _broadcast("link", project_id, issue=link.get("issue"), pr=link.get("pr"), source="agent")
    return _json({"ok": True, "code": "ok", "action": "attach", "kind": kind,
                  "project_id": project_id, "link": link})


# --- Schemas -----------------------------------------------------------------

_PROJECT_ARG = {
    "type": "string",
    "description": ("Project id, or its exact label. Omit to use the project this session is attached "
                    "to, else the project whose folder contains the current working directory."),
}

_BOARD_SCHEMA = {
    "description": ("Read the Project Command Center board: every project's lane (backlog, active, "
                    "review, blocked, done), every session's own lane and the issue or PR it is "
                    "attached to, and which project the current session resolves to. Read only."),
    "parameters": {
        "type": "object",
        "properties": {"project": _PROJECT_ARG},
        "additionalProperties": False,
    },
}

_SESSION_ARG = {
    "type": "string",
    "description": ("The session whose card this moves: this session's card by default when the tool "
                    "runs inside a session. Omit it (and pass project) to move the project's lane "
                    "instead."),
}

_STATUS_SCHEMA = {
    "description": ("Move a card on the Project Command Center board. By default this session's card "
                    "moves, so the session shows its own lane; pass project and no session_id to "
                    "move the project's card. Writes the status and an optional note with source "
                    "'agent', and refreshes the Desktop board."),
    "parameters": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["backlog", "active", "review", "blocked", "done"],
                       "description": "The lane the card belongs in now."},
            "project": _PROJECT_ARG,
            "note": {"type": "string", "description": "One short line shown on the card."},
            "session_id": _SESSION_ARG,
        },
        "required": ["status"],
        "additionalProperties": False,
    },
}

_WORK_ITEM_SCHEMA = {
    "description": ("Attach or detach the GitHub issue or PR this session is working on, so the "
                    "Desktop board shows the session under that work item. Writes a link row for the "
                    "current session and refreshes the board."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["attach", "detach"]},
            "kind": {"type": "string", "enum": ["issue", "pr"]},
            "number": {"type": "integer", "minimum": 1, "description": "Issue or PR number."},
            "project": _PROJECT_ARG,
        },
        "required": ["action", "kind", "number"],
        "additionalProperties": False,
    },
}


def register(ctx) -> None:  # noqa: ANN001 - the PluginContext type lives in core
    """Register the three board tools and the two board hooks.

    ``register()`` must stay cheap: it only binds handlers and callbacks, and neither hook opens
    the database here — they read the board when they fire.
    """
    for name, schema, handler, emoji in (
        ("pcc_board", _BOARD_SCHEMA, _pcc_board, "🗂"),
        ("pcc_set_status", _STATUS_SCHEMA, _pcc_set_status, "📍"),
        ("pcc_work_item", _WORK_ITEM_SCHEMA, _pcc_work_item, "🔗"),
    ):
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          description=schema["description"], emoji=emoji)
    try:
        from .hooks import register as register_hooks
        register_hooks(ctx)
    except Exception:  # the tools above still work; the hooks are the only thing lost
        log.warning("%s: agent hooks were not registered", PLUGIN_NAME, exc_info=True)
    log.debug("%s %s: registered 3 tools in toolset %s and 2 hooks",
              PLUGIN_NAME, PLUGIN_VERSION, TOOLSET)
