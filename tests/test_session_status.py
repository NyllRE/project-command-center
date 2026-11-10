#!/usr/bin/env python3
"""Per-session lane harness for project-command-center: migration, writes, tools and hooks.

Plain Python like tests/test_hooks.py: it builds a scratch home, puts a pre-migration ``board.db``
in it, copies it into place, and then proves the copy upgrades in place while the fixture stays
untouched. Every later case runs against that same upgraded store, so the lane columns, the tool
scope rules and both hooks are exercised on one real database.

    python3 tests/test_session_status.py

No pytest, no network, no real plugin data: ``PCC_SESSION_SCRATCH`` (default
``~/.hermes/cache/scratch/pcc-session``) is wiped and rebuilt on every run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

PLUGIN = Path(__file__).resolve().parent.parent
SCRATCH = Path(os.environ.get("PCC_SESSION_SCRATCH",
                              Path.home() / ".hermes" / "cache" / "scratch" / "pcc-session"))
FAILURES: list[str] = []
LANE_COLUMNS = ("status", "note", "status_source", "status_updated_at")

# The board.db exactly as the last release wrote it: project ids, session links (with resolved_by),
# and no lane columns on link. This is the fixture the upgrade path is measured against.
LEGACY_SCHEMA = """
CREATE TABLE status (
    project_id TEXT PRIMARY KEY,
    status TEXT CHECK (status IN ('backlog', 'active', 'review', 'blocked', 'done')),
    note TEXT,
    source TEXT CHECK (source IN ('user', 'agent', 'auto')),
    updated_at REAL
);
CREATE TABLE link (
    session_id TEXT PRIMARY KEY,
    project_id TEXT,
    repo TEXT,
    issue INTEGER,
    pr INTEGER,
    pr_url TEXT,
    title TEXT,
    resolved_by TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE project (
    project_id TEXT PRIMARY KEY,
    label TEXT,
    paths TEXT,
    updated_at REAL
);
CREATE INDEX status_updated_idx ON status (updated_at);
CREATE INDEX link_project_idx ON link (project_id);
"""


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}{f' :: {detail}' if detail else ''}")
    if not condition:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ".ljust(78, "="))


def load(name: str, path: Path):
    """Import a plugin module the way the plugin loader does: as a package with a search path."""
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)])
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(path.parent)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def columns(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def build_legacy(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO project (project_id, label, paths, updated_at) VALUES (?, ?, ?, ?)",
            ("pcc", "Project Command Center", json.dumps([str(PLUGIN)]), 1000.0))
        conn.execute(
            "INSERT INTO status (project_id, status, note, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?)", ("pcc", "active", "project note", "user", 1000.0))
        conn.execute(
            "INSERT INTO link (session_id, project_id, repo, issue, pr, pr_url, title, resolved_by, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("s-old", "pcc", "acme/widget", 7, 9,
             "https://github.com/acme/widget/pull/9", "Ship the lane", "desktop", 1000.0, 1000.0))
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    home = (SCRATCH / "home").resolve()
    if home == (Path.home() / ".hermes").resolve():
        sys.exit("refusing to run against the real ~/.hermes; point PCC_SESSION_SCRATCH elsewhere")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    home.mkdir(parents=True)
    data = home / "plugin-data" / "project-command-center"
    data.mkdir(parents=True)
    outside = SCRATCH / "outside"
    outside.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    os.environ["TERMINAL_CWD"] = str(outside)
    print(f"HERMES_HOME={home}")

    fixture = SCRATCH / "legacy" / "board.db"
    build_legacy(fixture)
    live = data / "board.db"
    shutil.copy2(fixture, live)

    plugin = load("pcc_session_plugin", PLUGIN / "__init__.py")

    recorded: dict[str, list] = {"tools": [], "hooks": []}

    class Ctx:
        def register_tool(self, *, name, toolset, schema, handler, description, emoji=None):
            recorded["tools"].append(name)

        def register_hook(self, name, callback, *, hold_stream=False):
            recorded["hooks"].append(name)

    plugin.register(Ctx())
    hooks = sys.modules["pcc_session_plugin.hooks"]
    store = hooks._store()

    # Capture the broadcasts instead of reaching a gateway that is not running here.
    events: list[tuple[str, str, Any]] = []
    try:
        import hermes_cli.plugin_events as plugin_events
        plugin_events.broadcast_plugin_event = (
            lambda name, event, payload: events.append((name, event, payload)) or 1)
    except Exception:
        pass

    # --- migration ----------------------------------------------------------
    section("migration / a copy of a pre-migration board.db upgrades in place")
    before_fixture = columns(fixture, "link")
    print(f"  fixture columns: {before_fixture}")
    check("the fixture has none of the lane columns",
          not any(name in before_fixture for name in LANE_COLUMNS), str(before_fixture))
    payload = store.board()
    print(f"  board() ok={payload.get('ok')} links={len(payload.get('links', []))}")
    check("board() reads the migrated database", payload.get("ok") is True, str(payload.get("code")))
    after_copy = columns(live, "link")
    after_fixture = columns(fixture, "link")
    print(f"  upgraded columns: {after_copy}")
    check("the copy gained all four lane columns",
          all(name in after_copy for name in LANE_COLUMNS), str(after_copy))
    check("the pre-migration fixture was not touched",
          after_fixture == before_fixture, str(after_fixture))
    check("the migration is additive: today's columns are still there",
          all(name in after_copy for name in
              ("session_id", "project_id", "repo", "issue", "pr", "pr_url", "title",
               "resolved_by", "created_at", "updated_at")), str(after_copy))

    old = next((row for row in payload.get("links", []) if row["session_id"] == "s-old"), {})
    print(f"  old row: {json.dumps(old, default=str)}")
    check("the existing link row survived the upgrade",
          old.get("project_id") == "pcc" and old.get("issue") == 7 and old.get("pr") == 9
          and old.get("title") == "Ship the lane", json.dumps(old, default=str))
    check("an upgraded row starts with no session lane",
          old.get("status") is None and old.get("note") == ""
          and old.get("status_source") is None and old.get("status_updated_at") is None,
          str(old))
    project_row = next((row for row in payload.get("statuses", []) if row["project_id"] == "pcc"), {})
    check("the project lane is unchanged by the migration",
          project_row.get("status") == "active" and project_row.get("note") == "project note",
          str(project_row))
    project_keys = {"project_id", "label", "paths", "updated_at"}
    check("the project table survived too",
          bool(payload.get("projects")) and set(payload["projects"][0]) == project_keys,
          str(payload.get("projects")))

    second = store.board()
    check("running the migration again is a no-op",
          second.get("ok") and columns(live, "link") == after_copy, str(second.get("code")))

    raw = sqlite3.connect(live)
    try:
        try:
            raw.execute("UPDATE link SET status = 'nope' WHERE session_id = 's-old'")
            raw.commit()
            refused = False
        except sqlite3.IntegrityError:
            refused = True
        check("the CHECK constraint refuses an unknown lane", refused)
        try:
            raw.execute("UPDATE link SET status_source = 'nope' WHERE session_id = 's-old'")
            raw.commit()
            source_refused = False
        except sqlite3.IntegrityError:
            source_refused = True
        check("the CHECK constraint refuses an unknown status_source", source_refused)
    finally:
        raw.close()

    # --- store writes and reads --------------------------------------------
    section("store / set and read one session's lane")
    wrote = store.set_status("pcc", "review", "PR is up", "user", session_id="s-old")
    print(f"  set_status: {json.dumps(wrote, default=str)}")
    check("a session write answers scope=session", wrote.get("ok") and wrote.get("scope") == "session",
          str(wrote.get("code")))
    row = store.get_link("s-old") or {}
    print(f"  get_link:   {json.dumps(row, default=str)}")
    check("the link row now carries the lane", row.get("status") == "review", str(row.get("status")))
    check("the note landed on the session card", row.get("note") == "PR is up", repr(row.get("note")))
    check("the source is user", row.get("status_source") == "user", str(row.get("status_source")))
    check("status_updated_at is a fresh timestamp",
          isinstance(row.get("status_updated_at"), float)
          and row["status_updated_at"] >= 1000.0, str(row.get("status_updated_at")))
    check("the project lane did not move", (store.get_status("pcc") or {}).get("status") == "active",
          str((store.get_status("pcc") or {}).get("status")))
    check("the project note did not move", (store.get_status("pcc") or {}).get("note") == "project note",
          repr((store.get_status("pcc") or {}).get("note")))
    listed = next((item for item in store.board()["links"] if item["session_id"] == "s-old"), {})
    check("board() link rows carry the four lane fields",
          all(key in listed for key in LANE_COLUMNS) and listed.get("status") == "review",
          str(sorted(listed)))
    check("a lane write with no note keeps the card's note",
          (store.set_status("pcc", "blocked", None, "agent", session_id="s-old")["link"]
           .get("note") == "PR is up"))
    check("bad status values stay values, not exceptions",
          store.set_status("pcc", "nope", None, "user", session_id="s-old").get("code") == "bad_status")
    check("scope=project keeps the project behaviour even with a session id",
          store.set_status("pcc", "active", "project note", "user",
                           session_id="s-old", scope="project").get("scope") == "project")
    check("scope=session without a session id is refused",
          store.set_status("pcc", "active", session_id=None,
                           scope="session").get("code") == "empty_session")

    section("store / a session id with no link row gets one")
    created = store.set_status("pcc", "active", "fresh session", "agent", session_id="s-new")
    print(f"  created:    {json.dumps(created, default=str)}")
    check("the write created the session's card", created.get("ok") and created.get("created") is True,
          str(created.get("code")))
    fresh = store.get_link("s-new") or {}
    check("the new row names the project and the lane",
          fresh.get("project_id") == "pcc" and fresh.get("status") == "active"
          and fresh.get("status_source") == "agent", str(fresh))
    missing = store.set_status(None, "active", session_id="s-nothing")
    print(f"  no project: {json.dumps(missing, default=str)}")
    check("a session card with no project to attach to is empty_project",
          missing.get("code") == "empty_project" and store.get_link("s-nothing") is None,
          str(missing.get("code")))
    typo = store.set_status("pcc-typo", "active", session_id="s-nothing")
    check("a session card naming an unsynced project is unknown_project",
          typo.get("code") == "unknown_project" and store.get_link("s-nothing") is None,
          str(typo.get("code")))

    # --- the shared lane rule ----------------------------------------------
    section("store / the resolvable lane rule (session first, project second)")
    session_link = {"status": "review", "status_source": "user", "note": "session note",
                    "status_updated_at": 2000.0}
    project_card = {"status": "active", "source": "user", "note": "project note", "updated_at": 1000.0}
    pick = store.resolve_lane(session_link, project_card)
    print(f"  both set:   {pick}")
    check("an explicit session lane wins", pick["status"] == "review" and pick["scope"] == "session")
    fallback = store.resolve_lane({"status": None}, project_card)
    print(f"  session off:{fallback}")
    check("a null session lane falls back to the project lane",
          fallback["status"] == "active" and fallback["scope"] == "project"
          and fallback["note"] == "project note")
    empty = store.resolve_lane({}, {})
    check("neither set answers an empty lane",
          empty == {"status": "", "scope": "", "source": "", "note": "", "updated_at": None},
          str(empty))
    card = store.session_card("s-old")
    check("session_card carries the resolved lane", card.get("lane", {}).get("status") == "blocked",
          str(card.get("lane")))

    # --- tool scope ---------------------------------------------------------
    section("tool / pcc_set_status defaults to this session's card")
    store.set_link("s-tool", "pcc", repo="acme/widget", issue=11, title="Tool scope")
    events.clear()
    moved = json.loads(plugin._pcc_set_status({"status": "active", "note": "on it"},
                                              session_id="s-tool"))
    print(f"  session move: {json.dumps(moved, default=str)}")
    check("no project argument moves the calling session's lane",
          moved.get("ok") and moved.get("scope") == "session" and moved.get("status") == "active",
          json.dumps(moved, default=str))
    check("the session row moved", (store.get_link("s-tool") or {}).get("status") == "active")
    check("the project row did not move",
          (store.get_status("pcc") or {}).get("status") == "active"
          and (store.get_status("pcc") or {}).get("note") == "project note")
    check("the broadcast names the session",
          bool(events) and events[-1][2].get("session_id") == "s-tool", str(events[-1:]))

    project_move = json.loads(plugin._pcc_set_status({"status": "done", "project": "pcc"},
                                                     session_id="s-tool"))
    print(f"  project move: {json.dumps(project_move, default=str)}")
    check("an explicit project moves the project lane", project_move.get("scope") == "project"
          and (store.get_status("pcc") or {}).get("status") == "done", str(project_move.get("scope")))
    check("the session lane is untouched by a project move",
          (store.get_link("s-tool") or {}).get("status") == "active")

    store.set_link("s-other", "pcc", repo="acme/widget", issue=12, title="Other session")
    other_move = json.loads(plugin._pcc_set_status({"status": "review", "session_id": "s-other"},
                                                   session_id="s-tool"))
    print(f"  other session: {json.dumps(other_move, default=str)}")
    check("an explicit session_id moves that session's lane",
          (store.get_link("s-other") or {}).get("status") == "review"
          and (store.get_link("s-tool") or {}).get("status") == "active", str(other_move.get("code")))

    no_session = json.loads(plugin._pcc_set_status({"status": "backlog", "project": "pcc"}))
    check("with no session at all the project lane still moves",
          no_session.get("scope") == "project"
          and (store.get_status("pcc") or {}).get("status") == "backlog", str(no_session.get("code")))
    typo_tool = json.loads(plugin._pcc_set_status({"status": "active", "project": "pcc-typo"},
                                                  session_id="s-tool"))
    check("an explicit project typo is still unknown_project", typo_tool.get("code") == "unknown_project",
          str(typo_tool.get("code")))
    board_reply = json.loads(plugin._pcc_board({}, session_id="s-tool"))
    tool_link = next((item for item in board_reply.get("links", [])
                      if item["session_id"] == "s-tool"), {})
    print(f"  pcc_board link: {json.dumps(tool_link, default=str)}")
    check("pcc_board returns the lane fields on link rows",
          all(key in tool_link for key in LANE_COLUMNS),
          str(sorted(tool_link)))
    check("pcc_board names the session lane on the project card",
          (board_reply.get("project") or {}).get("link", {}).get("status") == "active",
          str((board_reply.get("project") or {}).get("link")))

    # --- hooks: pre_llm_call ------------------------------------------------
    section("hook / pre_llm_call names the session's lane")
    store.set_status("pcc", "active", "project note", "user")
    store.set_status("pcc", "review", "session note", "user", session_id="s-hook")
    hooks._CWD_PROJECT_CACHE.clear()
    got = hooks.pre_llm_call(session_id="s-hook") or {}
    text = got.get("context", "")
    print(f"  with lane:    {text!r}")
    lines = text.splitlines()
    check("the first line is the session lane, not the project lane",
          bool(lines) and lines[0] == "Board: Project Command Center \u2014 review", repr(lines[:1]))
    check("the card keeps its shape",
          len(lines) == 4 and lines[1].startswith("Issue: ")
          and lines[2].startswith("PR: ") and "pcc_set_status" in lines[3], repr(lines))
    check("the project lane is still active underneath",
          (store.get_status("pcc") or {}).get("status") == "active")

    store.set_link("s-hook-nolane", "pcc")
    got_fallback = hooks.pre_llm_call(session_id="s-hook-nolane") or {}
    text_fallback = got_fallback.get("context", "")
    print(f"  without lane: {text_fallback!r}")
    check("a session with no lane shows the project lane",
          text_fallback.splitlines()[:1] == ["Board: Project Command Center \u2014 active"],
          repr(text_fallback.splitlines()[:1]))
    os.environ["TERMINAL_CWD"] = str(PLUGIN)
    hooks._CWD_PROJECT_CACHE.clear()
    try:
        got_cwd = hooks.pre_llm_call(session_id="s-hook-unlinked") or {}
        print(f"  from cwd:     {got_cwd.get('context', '').splitlines()[:1]}")
        check("a session resolved from its cwd still shows the project lane",
              got_cwd.get("context", "").splitlines()[:1]
              == ["Board: Project Command Center \u2014 active"],
              repr(got_cwd.get("context", "")))
    finally:
        os.environ["TERMINAL_CWD"] = str(outside)
        hooks._CWD_PROJECT_CACHE.clear()

    # --- hooks: auto-link ---------------------------------------------------
    section("hook / auto-link moves the session lane, never a person's")
    url = "https://github.com/acme/widget/pull/42"
    command = {"command": "gh pr create --fill"}
    for session in ("s-auto-unset", "s-auto-active", "s-auto-review", "s-auto-blocked", "s-auto-done"):
        store.set_link(session, "pcc", repo="acme/widget", issue=7, title="Auto lane")
    store.set_status("pcc", "active", "auto active", "agent", session_id="s-auto-active")
    store.set_status("pcc", "review", "person note", "user", session_id="s-auto-review")
    store.set_status("pcc", "blocked", "waiting on review", "user", session_id="s-auto-blocked")
    store.set_status("pcc", "done", "shipped", "user", session_id="s-auto-done")

    hooks.post_tool_call(tool_name="terminal", args=command, result=f"{url}\n",
                         session_id="s-auto-unset")
    unset = (store.get_link("s-auto-unset") or {})
    print(f"  unset:    lane={unset.get('status')} source={unset.get('status_source')} pr={unset.get('pr')}")
    check("an unset session lane becomes review with source auto",
          unset.get("status") == "review" and unset.get("status_source") == "auto"
          and unset.get("pr") == 42, str(unset))

    hooks.post_tool_call(tool_name="terminal", args=command, result=f"{url}\n",
                         session_id="s-auto-active")
    active = (store.get_link("s-auto-active") or {})
    print(f"  active:   lane={active.get('status')} source={active.get('status_source')}")
    check("active becomes review with source auto",
          active.get("status") == "review" and active.get("status_source") == "auto", str(active))

    hooks.post_tool_call(tool_name="terminal", args=command, result=f"{url}\n",
                         session_id="s-auto-review")
    reviewed = (store.get_link("s-auto-review") or {})
    print(f"  review:   lane={reviewed.get('status')} source={reviewed.get('status_source')} "
          f"note={reviewed.get('note')!r}")
    check("a person's review lane is not overwritten",
          reviewed.get("status") == "review" and reviewed.get("status_source") == "user"
          and reviewed.get("note") == "person note", str(reviewed))
    check("the PR is still recorded on that row", reviewed.get("pr") == 42, str(reviewed.get("pr")))

    for session, lane_name in (("s-auto-blocked", "blocked"), ("s-auto-done", "done")):
        hooks.post_tool_call(tool_name="terminal", args=command, result=f"{url}\n",
                             session_id=session)
        row_after = (store.get_link(session) or {})
        print(f"  {lane_name}:  lane={row_after.get('status')} source={row_after.get('status_source')}")
        check(f"a person's {lane_name} lane is not overwritten",
              row_after.get("status") == lane_name and row_after.get("status_source") == "user",
              str(row_after))

    loser = store.move_session_status_if_current("s-auto-review", ("", "backlog", "active"),
                                                 "review", None, "auto")
    print(f"  cas:      {json.dumps(loser, default=str)}")
    check("the compare-and-set loses to the lane already on the card",
          loser.get("code") == "status_changed"
          and (store.get_link("s-auto-review") or {}).get("status") == "review",
          str(loser.get("code")))

    # --- routes -------------------------------------------------------------
    section("routes / board/status takes a session scope, codes stay HTTP 200")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api = load("pcc_session_api", PLUGIN / "dashboard" / "plugin_api.py")
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)

    def post(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        response = client.post("/board/status", json=body)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"code": "not_json", "error": response.text[:200]}

    project_before = (store.get_status("pcc") or {}).get("status")
    code, body = post({"project_id": "pcc", "session_id": "s-route", "status": "review",
                       "note": "from the board"})
    print(f"  session:  HTTP {code} {json.dumps(body, default=str)}")
    check("a session lane write answers HTTP 200 with ok",
          code == 200 and body.get("ok") is True and body.get("scope") == "session",
          f"HTTP {code} {body.get('code')}")
    routed = store.get_link("s-route") or {}
    check("the route wrote the session card and its note",
          routed.get("status") == "review" and routed.get("note") == "from the board"
          and routed.get("status_source") == "user", str(routed))
    check("the route left the project lane alone",
          (store.get_status("pcc") or {}).get("status") == project_before,
          str((store.get_status("pcc") or {}).get("status")))

    code, body = post({"project_id": "pcc", "status": "active"})
    check("a project lane write without a session is unchanged behaviour",
          code == 200 and body.get("scope") == "project"
          and (store.get_status("pcc") or {}).get("status") == "active",
          f"HTTP {code} {body.get('code')}")

    code, bad = post({"project_id": "pcc", "status": "nope"})
    check("a bad lane is HTTP 200 with code bad_status",
          code == 200 and bad.get("ok") is False and bad.get("code") == "bad_status",
          f"HTTP {code} {bad.get('code')}")

    code, orphan = post({"session_id": "s-route-orphan", "status": "active"})
    check("a session card with no project anywhere is HTTP 200 empty_project",
          code == 200 and orphan.get("ok") is False and orphan.get("code") == "empty_project"
          and store.get_link("s-route-orphan") is None,
          f"HTTP {code} {orphan.get('code')}")

    code, mismatch = post({"project_id": "other", "session_id": "s-route", "status": "active"})
    check("a session carded under another project is HTTP 200 project_mismatch",
          code == 200 and mismatch.get("ok") is False
          and mismatch.get("code") == "project_mismatch",
          f"HTTP {code} {mismatch.get('code')}")

    response = client.get("/board", params={"session_id": "s-route"})
    board_body = response.json()
    route_link = next((item for item in board_body.get("links", [])
                       if item["session_id"] == "s-route"), {})
    print(f"  GET /board: HTTP {response.status_code} auto_link={board_body.get('auto_link')}")
    check("GET /board returns HTTP 200 and every link's lane fields",
          response.status_code == 200 and all(key in route_link for key in LANE_COLUMNS),
          str(sorted(route_link)))
    check("GET /board resolves the asking session's lane",
          (board_body.get("auto_link") or {}).get("lane", {}).get("status") == "review"
          and (board_body.get("auto_link") or {}).get("lane", {}).get("scope") == "session",
          str(board_body.get("auto_link")))

    print(f"\n{'PASS' if not FAILURES else 'FAIL'}: {len(FAILURES)} failure(s)")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
