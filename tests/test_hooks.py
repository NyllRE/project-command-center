#!/usr/bin/env python3
"""Hook harness for project-command-center: both callbacks, every positive and negative case.

Run against a scratch home so the real plugin data is never touched:

    HERMES_HOME=/home/nyll/.hermes/cache/scratch/pcc-p2/home python3 tests/test_hooks.py

Plain Python, no pytest: it prints one line per case plus the board row it left behind, and exits
non-zero when a case fails. The callbacks are called directly with the same kwargs the Hermes
dispatcher sends (``session_id``, ``tool_name``, ``args``, ``result``), so the harness proves the
hook bodies, not the dispatcher.
"""

from __future__ import annotations

import sys as _sys

_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402,F401  finds the Hermes tree and a sandbox home

import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PLUGIN = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}{f' — {detail}' if detail else ''}")
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


def main() -> int:
    home = os.environ.get("HERMES_HOME")
    if not home:
        sys.exit("set HERMES_HOME to a scratch directory; this harness must not touch real plugin data")
    home_path = Path(home).expanduser().resolve()
    if home_path == (Path.home() / ".hermes").resolve():
        sys.exit("refusing to run against the real ~/.hermes; point HERMES_HOME at a scratch dir")
    data = home_path / "plugin-data" / "project-command-center"
    data.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        (data / f"board.db{suffix}").unlink(missing_ok=True)
    print(f"HERMES_HOME={home_path}")
    print(f"board:       {data / 'board.db'}")

    hooks = load("pcc_hooks_under_test", PLUGIN / "hooks.py")
    store = hooks._store()

    # Record what the hook broadcasts instead of reaching a gateway that is not running here.
    import hermes_cli.plugin_events as plugin_events
    events: list[tuple[str, str, Any]] = []
    plugin_events.broadcast_plugin_event = lambda name, event, payload: events.append((name, event, payload)) or 1

    repo = "acme/widget"
    title = "Add the two agent hooks"
    store.set_projects([{"id": "pcc", "label": "Project Command Center", "paths": [str(PLUGIN)]}])
    for session in ("s-live", "s-create", "s-view", "s-mismatch", "s-tool", "s-done", "s-killed", "s-nourl"):
        store.set_link(session, "pcc", repo=repo, issue=7, title=title)
    store.set_status("pcc", "active", "in flight", "user")

    def lane() -> Any:
        return (store.get_status("pcc") or {}).get("status")
    def link(session: str = "s-live") -> dict:
        return store.get_link(session) or {}

    # --- registration -------------------------------------------------------
    section("registration (the real register() entry point)")
    plugin = load("pcc_plugin_under_test", PLUGIN / "__init__.py")
    recorded: dict[str, list] = {"tools": [], "hooks": []}

    class Ctx:
        def register_tool(self, *, name, toolset, schema, handler, description, emoji=None):
            recorded["tools"].append(name)
        def register_hook(self, name, callback, *, hold_stream=False):
            recorded["hooks"].append(name)

    plugin.register(Ctx())
    print(f"  tools: {recorded['tools']}")
    print(f"  hooks: {recorded['hooks']}")
    check("register() binds exactly pre_llm_call, post_tool_call",
          recorded["hooks"] == ["pre_llm_call", "post_tool_call"], str(recorded["hooks"]))

    # --- pre_llm_call -------------------------------------------------------
    section("pre_llm_call / linked session")
    got = hooks.pre_llm_call(session_id="s-live", user_message="open the PR", is_first_turn=False)
    print(f"  return: {got!r}")
    text = (got or {}).get("context", "")
    print(f"  len():  {len(text)}")
    check("returns {'context': str}", isinstance(got, dict) and isinstance(text, str))
    check("len(context) <= 400", len(text) <= 400, str(len(text)))
    for needle in ("Project Command Center", "active", "#7", title, "no PR yet",
                   'Closes #<issue>', "pcc_set_status", "backlog, active, review, blocked, done"):
        check(f"names {needle!r}", needle in text)

    section("pre_llm_call / PR already on the link row")
    store.set_link("s-create", "pcc", pr=42, pr_url=f"https://github.com/{repo}/pull/42")
    got = hooks.pre_llm_call(session_id="s-create")
    print(f"  return: {got!r}")
    check("names PR: #42", "PR: #42" in (got or {}).get("context", ""))

    section("pre_llm_call / long title stays inside the cap")
    store.set_link("s-live", "pcc", title="A very long issue title " * 40)
    got = hooks.pre_llm_call(session_id="s-live")
    print(f"  len():  {len((got or {}).get('context', ''))}")
    print(f"  return: {(got or {}).get('context')!r}")
    check("len(context) <= 400 with an 800 char title", len((got or {}).get("context", "")) <= 400,
          str(len((got or {}).get("context", ""))))
    store.set_link("s-live", "pcc", title=title)

    section("pre_llm_call / no injection, never raises")
    cases: list[tuple[str, dict]] = [
        ("session_id None", {"session_id": None}),
        ("empty payload {}", {}),
        ("missing session_id key", {"user_message": "hi", "model": "x"}),
        ("whitespace session_id", {"session_id": "   "}),
        ("payload None", {"payload": None}),
    ]
    for name, kwargs in cases:
        try:
            result = hooks.pre_llm_call(**kwargs)
            check(name, result is None, repr(result))
        except Exception as exc:  # the whole point of the hook is that this cannot happen
            check(name, False, f"raised {type(exc).__name__}: {exc}")

    section("pre_llm_call / no link row, resolved from the session cwd")
    ghost = hooks.pre_llm_call(session_id="s-ghost")
    print(f"  cwd match:      {bool(ghost)}")
    check("an unlinked session in a project folder still gets its card",
          bool(ghost) and "Project Command Center" in str((ghost or {}).get("context", "")))
    saved_cwd = os.environ.get("TERMINAL_CWD")
    outside = str(Path(__file__).resolve().parent / "_nowhere_")
    os.environ["TERMINAL_CWD"] = outside
    hooks._CWD_PROJECT_CACHE.clear()

    class _EmptyStore:
        """No project matches this session and no project row exists: nothing to inject."""

        def session_card(self, session_id=None):
            return {"session_id": session_id or "", "link": None, "status": None, "project": None}

        def resolve_project(self, session_id=None, explicit=None, use_cwd=True):
            return None

    saved_store = hooks._store
    hooks._store = lambda: _EmptyStore()
    try:
        check("a cwd outside every project injects nothing",
              hooks.pre_llm_call(session_id="s-ghost2") is None)
    finally:
        hooks._store = saved_store
        hooks._CWD_PROJECT_CACHE.clear()
        if saved_cwd is None:
            os.environ.pop("TERMINAL_CWD", None)
        else:
            os.environ["TERMINAL_CWD"] = saved_cwd

    # --- post_tool_call: positive ------------------------------------------
    section("post_tool_call / gh pr create that printed a PR URL")
    events.clear()
    url = f"https://github.com/{repo}/pull/42"
    hooks.post_tool_call(tool_name="terminal",
                         args={"command": "gh pr create --fill --base main"},
                         result=f"Creating pull request for feature/x into main\n{url}\n",
                         session_id="s-create")
    print(f"  link:    {link('s-create')}")
    print(f"  status:  {store.get_status('pcc')}")
    print(f"  events:  {events}")
    check("link.pr == 42", link("s-create").get("pr") == 42, str(link("s-create").get("pr")))
    check("link.pr_url == the printed URL", link("s-create").get("pr_url") == url,
          str(link("s-create").get("pr_url")))
    check("lane moved to review", lane() == "review", str(lane()))
    check("source is auto", (store.get_status('pcc') or {}).get("source") == "auto")
    check("note moved with the card", (store.get_status('pcc') or {}).get("note") == "in flight")
    check("broadcast board.changed kind=link pr=42",
          events == [("project-command-center", "board.changed",
                      {"project_id": "pcc", "kind": "link", "pr": 42, "source": "auto"})], str(events))
    check("the session's own lane moved to review",
          link("s-create").get("status") == "review", str(link("s-create").get("status")))
    check("the session lane records source auto",
          link("s-create").get("status_source") == "auto", str(link("s-create").get("status_source")))

    # --- session lanes: tool scope, hook text, a person's lane --------------
    section("session lanes / tool scope and hook text")
    store.set_link("s-lane", "pcc", repo=repo, issue=7, title=title)
    store.set_status("pcc", "active", "in flight", "user")
    moved = json.loads(plugin._pcc_set_status({"status": "blocked", "note": "waiting"},
                                              session_id="s-lane"))
    print(f"  tool:         {moved.get('code')} scope={moved.get('scope')} "
          f"session={link('s-lane').get('status')} project={lane()}")
    check("pcc_set_status with no project moves this session's card",
          moved.get("ok") and moved.get("scope") == "session"
          and link("s-lane").get("status") == "blocked", str(moved.get("code")))
    check("the project lane is left alone", lane() == "active", str(lane()))
    card = hooks.pre_llm_call(session_id="s-lane") or {}
    print(f"  hook text:    {card.get('context', '').splitlines()[:1]}")
    check("pre_llm_call names the session's lane",
          card.get("context", "").splitlines()[:1]
          == ["Board: Project Command Center \u2014 blocked"],
          repr(card.get("context", "").splitlines()[:1]))
    project_move = json.loads(plugin._pcc_set_status({"status": "review", "project": "pcc"},
                                                     session_id="s-lane"))
    print(f"  project move: {project_move.get('code')} scope={project_move.get('scope')} "
          f"session={link('s-lane').get('status')} project={lane()}")
    check("an explicit project moves the project lane", project_move.get("scope") == "project"
          and lane() == "review", str(project_move.get("code")))
    check("the session lane survives a project move", link("s-lane").get("status") == "blocked",
          str(link("s-lane").get("status")))
    picked = store.resolve_lane(store.get_link("s-lane"), store.get_status("pcc"))
    check("resolve_lane prefers the session lane", picked.get("status") == "blocked"
          and picked.get("scope") == "session", str(picked))

    store.set_link("s-lane-done", "pcc", repo=repo, issue=7, title=title)
    store.set_status("pcc", "done", "shipped", "user", session_id="s-lane-done")
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result=f"https://github.com/{repo}/pull/44\n", session_id="s-lane-done")
    print(f"  done lane:    session={link('s-lane-done').get('status')} "
          f"source={link('s-lane-done').get('status_source')} pr={link('s-lane-done').get('pr')}")
    check("auto-link never overwrites a lane a person set",
          link("s-lane-done").get("status") == "done"
          and link("s-lane-done").get("status_source") == "user"
          and link("s-lane-done").get("pr") == 44, str(link("s-lane-done")))

    # --- post_tool_call: negatives -----------------------------------------
    def reset(session: str, lane_name: str = "active") -> None:
        """Each negative case owns its own link row (fresh, no PR) and its own lane write."""
        store.set_status("pcc", lane_name, "in flight", "user")
        events.clear()

    def untouched(session: str, lane_name: str) -> bool:
        row = link(session)
        state = store.get_status("pcc") or {}
        return (row.get("pr") is None and row.get("pr_url") is None
                and state.get("status") == lane_name and state.get("source") == "user" and not events)

    section("post_tool_call / negatives that must not write")
    reset("s-view")
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr view 42 --json url"},
                         result=f'{{"url": "{url}"}}', session_id="s-view")
    print(f"  gh pr view      pr={link('s-view').get('pr')} lane={lane()} events={len(events)}")
    check("gh pr view result with a PR URL", untouched("s-view", "active"))

    reset("s-mismatch")
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result="https://github.com/other/thing/pull/43\n", session_id="s-mismatch")
    print(f"  repo mismatch   pr={link('s-mismatch').get('pr')} lane={lane()} events={len(events)}")
    check("URL repo differs from the link row", untouched("s-mismatch", "active"))

    reset("s-tool")
    hooks.post_tool_call(tool_name="execute_code",
                         args={"command": "gh pr create --fill", "code": "..."},
                         result=f"{url}\n", session_id="s-tool")
    print(f"  non-terminal    pr={link('s-tool').get('pr')} lane={lane()} events={len(events)}")
    check("tool_name is not terminal", untouched("s-tool", "active"))

    reset("s-nourl")
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result="a pull request template, no URL here\n", session_id="s-nourl")
    print(f"  no URL          pr={link('s-nourl').get('pr')} lane={lane()} events={len(events)}")
    check("gh pr create output without a PR URL", untouched("s-nourl", "active"))

    reset("s-done", "done")
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result=f"{url}\n", session_id="s-done")
    print(f"  lane done       pr={link('s-done').get('pr')} lane={lane()} source="
          f"{(store.get_status('pcc') or {}).get('source')} pr_url={link('s-done').get('pr_url')}")
    check("lane done is never moved", lane() == "done", str(lane()))
    check("lane done keeps its source", (store.get_status('pcc') or {}).get("source") == "user")
    check("lane done still records the PR the command printed", link("s-done").get("pr") == 42)

    section("post_tool_call / kill file present")
    reset("s-killed")
    kill = hooks.kill_file()
    kill.write_text("auto-link off\n", encoding="utf-8")
    print(f"  kill file:      {kill} (exists={kill.exists()})")
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result=f"{url}\n", session_id="s-killed")
    print(f"  with kill file  pr={link('s-killed').get('pr')} lane={lane()} events={len(events)}")
    check("kill file blocks every write", untouched("s-killed", "active"))
    kill.unlink(missing_ok=True)
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result=f"{url}\n", session_id="s-killed")
    print(f"  kill removed    pr={link('s-killed').get('pr')} lane={lane()} events={len(events)}")
    check("removing the kill file re-enables without restart", link("s-killed").get("pr") == 42
          and lane() == "review" and len(events) == 1)

    section("post_tool_call / unlinked session")
    events.clear()
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result=f"{url}\n", session_id="s-ghost")
    print(f"  unknown session pr={link('s-ghost')} events={len(events)}")
    check("session with no link row writes nothing", link("s-ghost") == {} and not events)

    # --- post_tool_call: the reviewer's probes --------------------------------
    section("post_tool_call / a grep that merely mentions the command")
    reset("s-view")
    grep_line = f"docs/setup.md:12:    gh pr create --fill   {url}"
    hooks.post_tool_call(tool_name="terminal",
                         args={"command": "grep -rn 'gh pr create' ."},
                         result=f"{grep_line}\n", session_id="s-view")
    print(f"  grep output     pr={link('s-view').get('pr')} lane={lane()} events={len(events)}")
    check("a grep line with 'gh pr create' and a PR URL does not write",
          untouched("s-view", "active"))

    section("post_tool_call / a create that failed")
    for session in ("s-failed", "s-json-ok", "s-compound"):
        store.set_link(session, "pcc", repo=repo, issue=7, title=title)
    reset("s-failed")
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result=f"{url}\n", session_id="s-failed",
                         status="error", error_type="tool_error",
                         error_message="a pull request for branch already exists")
    print(f"  status=error    pr={link('s-failed').get('pr')} lane={lane()} events={len(events)}")
    check("hook status error does not write", untouched("s-failed", "active"))

    reset("s-failed")
    failed_json = json.dumps({"output": "a pull request for branch \"x\" already exists\n",
                              "exit_code": 1, "error": "gh pr create failed"})
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result=failed_json, session_id="s-failed")
    print(f"  exit_code 1     pr={link('s-failed').get('pr')} lane={lane()} events={len(events)}")
    check("a failed gh pr create (exit 1) does not write", untouched("s-failed", "active"))

    reset("s-failed")
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result=f"Warning: a pull request for branch already exists\n{url}\n",
                         session_id="s-failed")
    print(f"  already exists  pr={link('s-failed').get('pr')} lane={lane()} events={len(events)}")
    check("'already exists' text on a plain result does not write", untouched("s-failed", "active"))

    reset("s-json-ok")
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result=json.dumps({"output": f"Creating pull request\n{url}\n",
                                            "exit_code": 0, "error": None}),
                         session_id="s-json-ok")
    print(f"  exit_code 0     pr={link('s-json-ok').get('pr')} lane={lane()} events={len(events)}")
    check("a successful JSON result still writes", link("s-json-ok").get("pr") == 42
          and lane() == "review")

    reset("s-compound")
    hooks.post_tool_call(tool_name="terminal",
                         args={"command": "cd /tmp/acme && gh pr create --fill"},
                         result=f"{url}\n", session_id="s-compound")
    print(f"  cd && gh        pr={link('s-compound').get('pr')} lane={lane()} events={len(events)}")
    check("a real command after && still writes", link("s-compound").get("pr") == 42)

    section("post_tool_call / a link row that names no repo")
    # An empty repo used to be refused unless the project came from the session's
    # own cwd. It is now checked against the project folder's own origin remote,
    # which is the shape a Desktop created link actually has. The folder is made
    # here so the case does not depend on how this checkout was cloned.
    mine = "NyllRE/project-command-center"
    mine_url = f"https://github.com/{mine}/pull/42"
    git_home = Path(tempfile.mkdtemp(prefix="pcc-hook-remote-"))
    subprocess.run(["git", "init", "-q", str(git_home)], check=True)
    subprocess.run(["git", "-C", str(git_home), "remote", "add", "origin",
                    f"https://github.com/{mine}.git"], check=True)
    subprocess.run(["git", "-C", str(git_home), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(git_home), "config", "user.name", "Test"], check=True)
    store.set_projects([{"id": "pcc-remote", "label": "Remote", "paths": [str(git_home)]}])
    store.set_status("pcc-remote", "active", "in flight", "user")
    for session, by in (("s-cwd", "cwd"), ("s-agent", "agent"), ("s-desktop", "desktop")):
        store.set_link(session, "pcc-remote", repo="", issue=7, title=title, resolved_by=by)
    for session in ("s-cwd", "s-agent", "s-desktop"):
        reset(session)
        hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                             result=f"{mine_url}\n", session_id=session)
        print(f"  {session:<11} pr={link(session).get('pr')} repo={link(session).get('repo')} lane={lane()}")
    check("empty repo + URL names the project remote writes",
          link("s-cwd").get("pr") == 42
          and (store.get_status("pcc-remote") or {}).get("status") == "review")
    check("empty repo + resolved_by=agent writes too, it was the real case", link("s-agent").get("pr") == 42)
    check("empty repo + resolved_by=desktop writes too", link("s-desktop").get("pr") == 42)
    check("the resolved repo is stored on the row", link("s-agent").get("repo") == mine)

    store.set_link("s-other-repo", "pcc", repo="", issue=7, title=title, resolved_by="agent")
    reset("s-other-repo")
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result="https://github.com/other/thing/pull/51\n", session_id="s-other-repo")
    print(f"  other repo     pr={link('s-other-repo').get('pr')} lane={lane()}")
    check("empty repo + a URL from another repo does not write", untouched("s-other-repo", "active"))

    store.set_projects([{"id": "pcc-nofolder", "label": "No folder", "paths": ["/nonexistent/pcc"]}])
    store.set_link("s-nofolder", "pcc-nofolder", repo="", issue=7, title=title, resolved_by="agent")
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result=f"{mine_url}\n", session_id="s-nofolder")
    print(f"  no folder      pr={(store.get_link('s-nofolder') or {}).get('pr')}")
    check("empty repo + an unreadable project folder does not write",
          not (store.get_link("s-nofolder") or {}).get("pr"))

    section("session lineage / runtime id newer than the stored row")
    from hermes_state import SessionDB

    root_sid, tip_sid = "s-compress-root", "s-compress-tip"
    with SessionDB(home_path / "state.db") as db:
        db.create_session(root_sid, "cli")
        db.end_session(root_sid, "compression")
        db.create_session(tip_sid, "cli", parent_session_id=root_sid)
    lineage = store._compression_lineage(tip_sid)
    print(f"  lineage:        {lineage}")
    check("hermes_state reports the compression chain", lineage[:2] == [root_sid, tip_sid], str(lineage))
    store.set_link(root_sid, "pcc", repo=repo, issue=7, title=title)
    store.set_status("pcc", "active", "in flight", "user")
    events.clear()
    got = hooks.pre_llm_call(session_id=tip_sid)
    print(f"  pre_llm_call:   {(got or {}).get('context', '')!r}")
    check("pre_llm_call finds the card through the lineage", "Project Command Center" in
          (got or {}).get("context", ""))
    hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create --fill"},
                         result="https://github.com/acme/widget/pull/47\n", session_id=tip_sid)
    print(f"  auto-link:      root pr={link(root_sid).get('pr')} tip row={store.get_link(tip_sid)}")
    check("auto-link writes the ancestor row, not an orphan",
          link(root_sid).get("pr") == 47 and store.get_link(tip_sid) is None)
    attached = json.loads(plugin._pcc_work_item(
        {"action": "attach", "kind": "pr", "number": 48}, session_id=tip_sid))
    print(f"  pcc_work_item:  {attached.get('code')} root pr={link(root_sid).get('pr')} "
          f"tip row={store.get_link(tip_sid)}")
    check("pcc_work_item attaches to the ancestor row too",
          attached.get("ok") and link(root_sid).get("pr") == 48
          and store.get_link(tip_sid) is None)

    section("tools / a typo'd project id on a synced board")
    before_projects = len(store.projects())
    unknown = json.loads(plugin._pcc_set_status(
        {"status": "active", "project": "pcc-typo"}, session_id="s-tool"))
    print(f"  pcc_set_status: {unknown.get('code')} projects {before_projects} -> "
          f"{len(store.projects())}")
    check("pcc_set_status returns unknown_project", unknown.get("code") == "unknown_project",
          str(unknown.get("code")))
    check("pcc_set_status creates no project row", len(store.projects()) == before_projects,
          f"{before_projects} -> {len(store.projects())}")
    unknown_item = json.loads(plugin._pcc_work_item(
        {"action": "attach", "kind": "pr", "number": 99, "project": "pcc-typo"},
        session_id="s-tool"))
    print(f"  pcc_work_item:  {unknown_item.get('code')} projects {before_projects} -> "
          f"{len(store.projects())} link={store.get_link('s-tool')}")
    check("pcc_work_item follows the same unknown_project rule",
          unknown_item.get("code") == "unknown_project"
          and len(store.projects()) == before_projects
          and (store.get_link("s-tool") or {}).get("pr") is None,
          str(unknown_item.get("code")))

    store.set_link("s-provenance", "pcc", repo="", issue=7, resolved_by="cwd")
    json.loads(plugin._pcc_work_item({"action": "attach", "kind": "issue", "number": 13},
                                     session_id="s-provenance"))
    print(f"  attach keeps resolved_by={link('s-provenance').get('resolved_by')!r}")
    check("an attach resolved from the session row keeps resolved_by=cwd",
          link("s-provenance").get("resolved_by") == "cwd",
          repr(link("s-provenance").get("resolved_by")))

    section("store / note and lane races")
    store.set_status("pcc", "active", "keep this note", "user")
    moved = store.set_status("pcc", "blocked", None, "agent")
    print(f"  no note:        lane={moved['status']['status']} note={moved['status']['note']!r}")
    check("a lane move with no note preserves the existing note",
          moved["status"]["note"] == "keep this note", repr(moved["status"]["note"]))
    cas = store.move_status_if_current("pcc", "blocked", "review", None, "auto")
    print(f"  cas win:        {cas.get('code')} lane={lane()} note={(store.get_status('pcc') or {}).get('note')!r}")
    check("compare-and-set moves the lane and keeps the note",
          cas.get("ok") and lane() == "review"
          and (store.get_status("pcc") or {}).get("note") == "keep this note")
    store.set_status("pcc", "active", "in flight", "user")
    store.set_status("pcc", "done", "shipped by the user", "user")  # the concurrent move
    lost = store.move_status_if_current("pcc", "active", "review", None, "auto")
    print(f"  cas loss:       {lost.get('code')} lane={lane()} source="
          f"{(store.get_status('pcc') or {}).get('source')}")
    check("compare-and-set loses to a concurrent done",
          lost.get("code") == "status_changed" and lane() == "done"
          and (store.get_status("pcc") or {}).get("source") == "user", str(lost.get("code")))

    section("fail open / a broken board never costs a turn")
    healthy = hooks._STORE
    setattr(hooks, "_STORE", object())  # every attribute the hook touches now raises AttributeError
    try:
        result = hooks.pre_llm_call(session_id="s-live")
        check("pre_llm_call returns None instead of raising", result is None, repr(result))
        returned = hooks.post_tool_call(tool_name="terminal", args={"command": "gh pr create"},
                                        result=f"{url}\n", session_id="s-create")
        check("post_tool_call returns None instead of raising", returned is None, repr(returned))
    except Exception as exc:
        check("a broken board does not escape the hook", False, f"raised {type(exc).__name__}: {exc}")
    finally:
        setattr(hooks, "_STORE", healthy)

    # --- timing -------------------------------------------------------------
    section("timing, 1000 pre_llm_call invocations")
    store.set_link("s-live", "pcc", pr=None, pr_url=None)
    store.set_status("pcc", "active", "in flight", "user")
    hooks.pre_llm_call(session_id="s-live")  # warm the module and the schema bookkeeping
    timings: list[float] = []
    for _ in range(1000):
        start = time.perf_counter()
        hooks.pre_llm_call(session_id="s-live")
        timings.append((time.perf_counter() - start) * 1000)
    csv = home_path / "hooks-timing.csv"
    csv.write_text("call,ms\n" + "\n".join(f"{i + 1},{ms:.3f}" for i, ms in enumerate(timings)),
                   encoding="utf-8")
    ordered = sorted(timings)
    print(f"  calls:  {len(timings)}   total: {sum(timings):.1f} ms   mean: {statistics.mean(timings):.3f} ms")
    print(f"  min:    {ordered[0]:.3f} ms   p50: {ordered[499]:.3f} ms   "
          f"p95: {ordered[949]:.3f} ms   p99: {ordered[989]:.3f} ms   max: {ordered[-1]:.3f} ms")
    print("  first 10 per-call: " + " ".join(f"{ms:.2f}" for ms in timings[:10]) + " ms")
    print(f"  every per-call time: {csv}")
    check("every call under 50 ms", ordered[-1] < 50, f"max {ordered[-1]:.2f} ms")
    # Generous ceilings on purpose: these hooks do two or three primary-key lookups, so a
    # real regression is an order of magnitude, while wall clock on a loaded desktop can
    # triple a healthy number. A tight budget here only teaches people to ignore the suite.
    check("p95 under 25 ms", ordered[949] < 25, f"p95 {ordered[949]:.2f} ms")

    section("timing, 200 pre_llm_call invocations for an unlinked session")
    hooks.pre_llm_call(session_id="s-nobody")  # warm the state.db lineage handle
    miss: list[float] = []
    for _ in range(200):
        start = time.perf_counter()
        hooks.pre_llm_call(session_id="s-nobody")
        miss.append((time.perf_counter() - start) * 1000)
    ordered_miss = sorted(miss)
    print(f"  calls:  {len(miss)}   mean: {statistics.mean(miss):.3f} ms   "
          f"p95: {ordered_miss[189]:.3f} ms   max: {ordered_miss[-1]:.3f} ms")
    check("an unlinked session (lineage miss) stays under 25 ms p95",
          ordered_miss[189] < 25, f"p95 {ordered_miss[189]:.3f} ms")

    # --- schema cache --------------------------------------------------------
    section("store / board.db deleted under a running process")
    before_ready = store._db_path() in hooks._store()._schema_ready
    for suffix in ("", "-wal", "-shm"):
        (data / f"board.db{suffix}").unlink(missing_ok=True)
    healed = store.projects()
    print(f"  cache was warm: {before_ready}; projects() after delete: {len(healed)}")
    check("a deleted board.db is re-created instead of 'no such table'",
          isinstance(healed, list) and store._db_path() in hooks._store()._schema_ready)


    print(f"\n{'PASS' if not FAILURES else 'FAIL'}: {len(FAILURES)} failure(s)")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
