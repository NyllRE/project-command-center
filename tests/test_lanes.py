#!/usr/bin/env python3
"""Lane definitions are data: schema, migration, validation and the lane routes' store calls.

Plain Python like tests/test_session_status.py, no pytest. It builds a scratch home, opens the
store (which creates and seeds the ``lanes`` table), and exercises the seven system lanes, custom
lanes, reordering, deletion with reassignment, the system_lane refusal, the Unfiled fallback and
the done/canceled derivation from a linked item.

A second pass runs the same module in a subprocess against a database written with the OLD five
lane rows and no ``system`` column, which is the shape a live ``board.db`` has: the missing two
lanes are inserted, all seven are marked system, the order is rewritten once, and a later open
leaves a user reorder alone.

    python3 tests/test_lanes.py

No network, no real plugin data: ``PCC_LANES_SCRATCH`` (default
``~/.hermes/cache/scratch/pcc-lanes``) is wiped and rebuilt on every run.
"""

from __future__ import annotations

import sys as _sys

_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402,F401  finds the Hermes tree and a sandbox home

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN = Path(__file__).resolve().parent.parent
SCRATCH = Path(os.environ.get("PCC_LANES_SCRATCH",
                              Path.home() / ".hermes" / "cache" / "scratch" / "pcc-lanes"))
FAILURES: list[str] = []
DEFAULT_IDS = ["unfiled", "backlog", "active", "review", "blocked", "done", "canceled"]
DEFAULT_LABELS = ["Unfiled", "Backlog", "Active", "Review", "Blocked", "Done", "Canceled"]
# The shape a board.db written before the lanes table held system rows has.
OLD_IDS = ["backlog", "active", "review", "blocked", "done"]
OLD_LABELS = ["Backlog", "Active", "Review", "Blocked", "Done"]


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}{f' :: {detail}' if detail else ''}")
    if not condition:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ".ljust(78, "="))


def load(name: str, path: Path):
    """Import a plugin module the way the plugin loader does: as a package with a search path."""
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)])
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(path.parent)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def ids_of(rows: list[dict[str, Any]]) -> list[str]:
    return [row["id"] for row in rows]


def by_id(rows: list[dict[str, Any]], lane_id: str) -> dict[str, Any]:
    return next((row for row in rows if row["id"] == lane_id), {})


def positions(rows: list[dict[str, Any]]) -> list[int]:
    return [row["order"] for row in rows]


def write_old_board(db: Path) -> None:
    """The live shape before this change: five default lanes, no ``system`` column, one custom."""
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE lanes (id TEXT PRIMARY KEY, label TEXT NOT NULL, position INTEGER NOT NULL,"
            " custom INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)")
        now = time.time()
        conn.executemany(
            "INSERT INTO lanes (id, label, position, custom, created_at) VALUES (?, ?, ?, 0, ?)",
            [(lane_id, label, position, now)
             for position, (lane_id, label) in enumerate(zip(OLD_IDS, OLD_LABELS))])
        # A custom lane a user added before the upgrade: it must survive the migration and keep
        # its place behind the seven system lanes.
        conn.execute(
            "INSERT INTO lanes (id, label, position, custom, created_at) VALUES (?, ?, ?, 1, ?)",
            ("qa-pending", "QA Pending", len(OLD_IDS), now))
        conn.commit()
    finally:
        conn.close()


def migration() -> int:
    """Child process: open a five-lane board and prove the one-time system migration."""
    db = SCRATCH / "mig-home" / "plugin-data" / "project-command-center" / "board.db"
    print(f"HERMES_HOME={db.parent.parent.parent}")
    store = load("pcc_lanes_mig_store", PLUGIN / "board_store.py")

    section("migration / a five-lane board gains unfiled and canceled, once")
    opened = store.board()
    check("the old board opens", opened.get("ok") is True, str(opened.get("code")))
    rows = store.lanes()
    print(f"  lanes: {json.dumps(rows, default=str)}")
    check("the seven system lanes are live, in the documented order",
          ids_of(rows)[:7] == DEFAULT_IDS, str(ids_of(rows)))
    check("the two inserted lanes carry the seed labels",
          [row["label"] for row in rows[:7]] == DEFAULT_LABELS,
          str([row["label"] for row in rows[:7]]))
    check("positions are 0..n-1", positions(rows) == list(range(len(rows))), str(positions(rows)))
    check("every seeded lane is marked system",
          all(by_id(rows, lane).get("system") == 1 for lane in DEFAULT_IDS),
          str([(row["id"], row["system"]) for row in rows]))
    check("the five pre-existing lanes are not marked custom",
          all(by_id(rows, lane).get("custom") == 0 for lane in DEFAULT_IDS),
          str([(row["id"], row["custom"]) for row in rows]))
    check("the custom lane survived, behind the system lanes",
          ids_of(rows)[7:] == ["qa-pending"], str(ids_of(rows)))

    section("migration / a second open is a no-op")
    store._schema_ready.discard(store._db_path())
    conn = store._connect()
    try:
        count = conn.execute("SELECT count(*) FROM lanes").fetchone()[0]
    finally:
        conn.close()
    check("no lane was inserted twice", count == 8, str(count))
    check("the ids did not move", ids_of(store.lanes()) == DEFAULT_IDS + ["qa-pending"],
          str(store.lane_ids()))

    section("migration / a user reorder after the upgrade survives the next open")
    reordered = store.reorder_lanes(list(reversed(store.lane_ids())))
    check("the reorder is accepted", reordered.get("ok") is True, str(reordered.get("code")))
    store._schema_ready.discard(store._db_path())
    store._connect().close()
    check("the reopened board keeps the user's order",
          ids_of(store.lanes()) == list(reversed(DEFAULT_IDS + ["qa-pending"])),
          str(store.lane_ids()))

    section("migration / the new lanes accept writes and refuse deletion")
    store.set_projects([{"id": "pcc-mig", "label": "Migrated board"}])
    wrote = store.set_status("pcc-mig", "canceled", "dropped", "user", scope="project")
    check("a project lane write to canceled succeeds", wrote.get("ok") is True,
          str(wrote.get("code")))
    refused = store.delete_lane("unfiled")
    check("deleting unfiled is system_lane", refused.get("code") == "system_lane",
          str(refused.get("code")))
    check("the custom lane is still deletable",
          store.delete_lane("qa-pending").get("ok") is True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + ", ".join(FAILURES))
        return 1
    print("migration checks ok")
    return 0


def main() -> int:
    if "--migration" in sys.argv:
        return migration()

    home = (SCRATCH / "home").resolve()
    if home == (Path.home() / ".hermes").resolve():
        sys.exit("refusing to run against the real ~/.hermes; point PCC_LANES_SCRATCH elsewhere")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    home.mkdir(parents=True)
    data = home / "plugin-data" / "project-command-center"
    data.mkdir(parents=True)
    outside = SCRATCH / "outside"
    outside.mkdir()
    os.environ["HERMES_HOME"] = str(home)
    os.environ["TERMINAL_CWD"] = str(outside)
    print(f"HERMES_HOME={home}")

    store = load("pcc_lanes_store", PLUGIN / "board_store.py")
    db = data / "board.db"

    # --- seed ----------------------------------------------------------------
    section("seed / a fresh database starts with the seven system lanes")
    opened = store.board()
    check("a fresh board opens", opened.get("ok") is True, str(opened.get("code")))
    rows = store.lanes()
    print(f"  lanes: {json.dumps(rows, default=str)}")
    check("the seed ids are the seven lanes, in order", ids_of(rows) == DEFAULT_IDS,
          str(ids_of(rows)))
    check("the seed labels are the seven defaults", [row["label"] for row in rows] == DEFAULT_LABELS,
          str([row["label"] for row in rows]))
    check("positions are 0..6", positions(rows) == [0, 1, 2, 3, 4, 5, 6], str(positions(rows)))
    check("seeded lanes are not custom", all(row["custom"] == 0 for row in rows),
          str([row["custom"] for row in rows]))
    check("seeded lanes are system", all(row["system"] == 1 for row in rows),
          str([row["system"] for row in rows]))
    check("lane_ids() reads the same order", store.lane_ids() == DEFAULT_IDS, str(store.lane_ids()))

    section("seed / a second open does not duplicate the seed")
    store._schema_ready.discard(store._db_path())
    conn = store._connect()
    try:
        count = conn.execute("SELECT count(*) FROM lanes").fetchone()[0]
    finally:
        conn.close()
    check("the lanes table still holds exactly seven rows", count == 7, str(count))
    check("the ids did not move on the second open", store.lane_ids() == DEFAULT_IDS,
          str(store.lane_ids()))

    synced = store.set_projects([{"id": "pcc", "label": "Project Command Center",
                                  "paths": [str(PLUGIN)]}])
    check("a project is synced for the write cases", synced.get("ok") is True, str(synced.get("code")))

    section("writes / every system id keeps working, including unfiled and canceled")
    for lane in DEFAULT_IDS:
        wrote = store.set_status("pcc", lane, None, "user", scope="project")
        read_back = (store.get_status("pcc") or {}).get("status")
        check(f"a project write to {lane!r} succeeds and reads back",
              wrote.get("ok") is True and read_back == lane,
              f"{wrote.get('code')} / {read_back!r}")

    # --- the Unfiled fallback and the derivation ------------------------------
    section("reads / nothing set anywhere is Unfiled, never Backlog")
    empty = store.resolve_lane({}, {})
    check("no status, no item and no project lane answers unfiled",
          empty == {"status": "unfiled", "scope": "", "source": "unset",
                    "note": "", "updated_at": None}, str(empty))
    check("the pure call without a lane table answers unfiled too",
          store.resolve_lane(None, None)["status"] == "unfiled",
          str(store.resolve_lane(None, None)))
    check("an explicit session lane still wins over the fallback",
          store.resolve_lane({"status": "active", "status_source": "user"}, {})["status"] == "active")
    check("a project lane still wins over unfiled",
          store.resolve_lane({}, {"status": "review"})["status"] == "review")

    section("reads / done and canceled derive from a linked PR or issue")
    merged = store.resolve_lane({"pr_state": {"state": "closed", "merged": True}}, {})
    check("a merged PR derives done",
          merged["status"] == "done" and merged["source"] == "auto", str(merged))
    closed = store.resolve_lane({"pr_state": {"state": "closed", "merged": False}}, {})
    check("a closed-unmerged PR derives canceled", closed["status"] == "canceled", str(closed))
    open_pr = store.resolve_lane({"pr_state": {"state": "open", "merged": False}}, {})
    check("an open PR derives nothing and falls to unfiled", open_pr["status"] == "unfiled",
          str(open_pr))
    done_issue = store.resolve_lane({"issue_state": "closed", "issue_state_reason": "completed"}, {})
    check("an issue closed as completed derives done", done_issue["status"] == "done",
          str(done_issue))
    no_reason = store.resolve_lane({"issue_state": "closed", "issue_state_reason": ""}, {})
    check("an issue closed with no reason derives done", no_reason["status"] == "done",
          str(no_reason))
    dropped = store.resolve_lane({"issue_state": "closed",
                                  "issue_state_reason": "not_planned"}, {})
    check("an issue closed as not planned derives canceled", dropped["status"] == "canceled",
          str(dropped))
    dupe = store.resolve_lane({"issue_state": "closed", "issue_state_reason": "duplicate"}, {})
    check("an issue closed as duplicate derives canceled", dupe["status"] == "canceled",
          str(dupe))
    open_issue = store.resolve_lane({"issue_state": "open", "issue_state_reason": ""},
                                    {"status": "active"})
    check("an open issue leaves the project lane alone", open_issue["status"] == "active",
          str(open_issue))
    explicit = store.resolve_lane({"status": "review", "status_source": "user",
                                   "pr_state": {"state": "closed", "merged": True}}, {})
    check("an explicit lane wins over the derivation",
          explicit["status"] == "review" and explicit["source"] == "user", str(explicit))

    # --- create, rename, reorder ---------------------------------------------
    section("lanes / create, rename and reorder")
    made = store.save_lane(None, "QA Pending")
    print(f"  create: {json.dumps(made, default=str)}")
    check("a created lane is ok and slugs its label",
          made.get("ok") is True and made.get("lane") == "qa-pending", str(made.get("code")))
    entry = by_id(made.get("lanes") or [], "qa-pending")
    check("the new lane is custom, not system, labelled and appended",
          entry.get("custom") == 1 and entry.get("system") == 0
          and entry.get("label") == "QA Pending" and entry.get("order") == 7, str(entry))
    check("the live ids now end with the custom lane",
          store.lane_ids() == DEFAULT_IDS + ["qa-pending"], str(store.lane_ids()))

    renamed = store.save_lane("qa-pending", "QA Queue")
    check("a rename keeps the id and changes the label",
          renamed.get("ok") is True and by_id(renamed.get("lanes") or [], "qa-pending")
          .get("label") == "QA Queue", str(renamed.get("code")))
    check("a system lane can be renamed",
          by_id(store.save_lane("review", "In review").get("lanes") or [], "review").get("label")
          == "In review")

    moved = store.save_lane("qa-pending", None, 1)
    check("a reorder moves the lane to the requested position",
          moved.get("ok") is True and by_id(moved.get("lanes") or [], "qa-pending")
          .get("order") == 1, str(moved.get("code")))
    check("a move renumbers every position 0..n-1",
          ids_of(moved.get("lanes") or []) == ["unfiled", "qa-pending", "backlog", "active",
                                               "review", "blocked", "done", "canceled"],
          str(ids_of(moved.get("lanes") or [])))
    restored = store.save_lane("qa-pending", None, 7)
    check("moving it back restores the seed order",
          ids_of(restored.get("lanes") or []) == DEFAULT_IDS + ["qa-pending"],
          str(ids_of(restored.get("lanes") or [])))

    duplicate = store.save_lane(None, "QA Pending")
    check("a duplicate slug is refused with lane_exists",
          duplicate.get("ok") is False and duplicate.get("code") == "lane_exists",
          str(duplicate.get("code")))
    dup_label = store.save_lane(None, "QA   pending!")
    check("a differently spelled label with the same slug is the same duplicate",
          dup_label.get("code") == "lane_exists", str(dup_label.get("code")))
    empty_label = store.save_lane(None, "!!!")
    check("a label that slugs to nothing is bad_label", empty_label.get("code") == "bad_label",
          str(empty_label.get("code")))

    bad_order = store.reorder_lanes(["backlog", "active"])
    check("an order that is not a permutation is refused",
          bad_order.get("ok") is False and bad_order.get("code") == "bad_order",
          str(bad_order.get("code")))
    extra = store.reorder_lanes(store.lane_ids() + ["ghost"])
    check("an order with an unknown id is refused", extra.get("code") == "bad_order",
          str(extra.get("code")))
    check("a refused order left the lanes where they were",
          store.lane_ids() == DEFAULT_IDS + ["qa-pending"], str(store.lane_ids()))

    # --- custom id writes ----------------------------------------------------
    section("writes / a custom lane id works on projects and sessions")
    project_write = store.set_status("pcc", "qa-pending", "waiting on QA", "user", scope="project")
    print(f"  project: {json.dumps(project_write, default=str)}")
    check("a project status write to a custom lane succeeds",
          project_write.get("ok") is True
          and (store.get_status("pcc") or {}).get("status") == "qa-pending",
          str(project_write.get("code")))
    session_write = store.set_status("pcc", "QA-PENDING ", "session waiting", "agent",
                                     session_id="s-qa")
    check("a session status write to a custom lane trims and lowercases",
          session_write.get("ok") is True
          and (store.get_link("s-qa") or {}).get("status") == "qa-pending",
          str(session_write.get("code")))
    card = store.session_card("s-qa")
    check("the session card resolves the custom lane",
          (card.get("lane") or {}).get("status") == "qa-pending",
          str((card.get("lane") or {}).get("status")))

    # --- delete --------------------------------------------------------------
    section("lanes / delete in use is refused, then reassign moves the rows")
    refused = store.delete_lane("qa-pending")
    print(f"  refused: {json.dumps(refused, default=str)}")
    check("a lane rows still hold is lane_in_use with the counts",
          refused.get("ok") is False and refused.get("code") == "lane_in_use"
          and refused.get("project_status") == 1 and refused.get("links") == 1,
          json.dumps(refused, default=str))
    check("the refused delete kept the lane", "qa-pending" in store.lane_ids(),
          str(store.lane_ids()))

    deleted = store.delete_lane("qa-pending", "active")
    print(f"  deleted: {json.dumps(deleted, default=str)}")
    check("a delete with reassign succeeds and reports what it moved",
          deleted.get("ok") is True and deleted.get("project_status") == 1
          and deleted.get("links") == 1, str(deleted.get("code")))
    check("the project row actually moved", (store.get_status("pcc") or {}).get("status") == "active",
          str((store.get_status("pcc") or {}).get("status")))
    check("the session row actually moved", (store.get_link("s-qa") or {}).get("status") == "active",
          str((store.get_link("s-qa") or {}).get("status")))
    check("the deleted lane is gone", "qa-pending" not in store.lane_ids(), str(store.lane_ids()))
    check("the survivors are renumbered 0..6", positions(store.lanes()) == [0, 1, 2, 3, 4, 5, 6],
          str(positions(store.lanes())))

    section("lanes / a system lane cannot be deleted, renamed or moved only")
    for lane in DEFAULT_IDS:
        gone = store.delete_lane(lane)
        check(f"deleting system lane {lane!r} is system_lane",
              gone.get("ok") is False and gone.get("code") == "system_lane", str(gone.get("code")))
    check("every system lane survived", store.lane_ids() == DEFAULT_IDS, str(store.lane_ids()))
    check("a system lane can still be reordered",
          store.reorder_lanes(["review", "unfiled", "backlog", "active", "blocked", "done",
                               "canceled"]).get("ok") is True)
    store.reorder_lanes(DEFAULT_IDS)

    section("lanes / unknown ids and bad shapes")
    unknown_rename = store.save_lane("ghost", "Ghost")
    check("renaming an unknown lane is unknown_lane", unknown_rename.get("code") == "unknown_lane",
          str(unknown_rename.get("code")))
    unknown_delete = store.delete_lane("ghost")
    check("deleting an unknown lane is unknown_lane", unknown_delete.get("code") == "unknown_lane",
          str(unknown_delete.get("code")))
    bad_write = store.set_status("pcc", "shipped", None, "user", scope="project")
    check("a status write naming an unknown lane is bad_status",
          bad_write.get("ok") is False and bad_write.get("code") == "bad_status",
          str(bad_write.get("code")))
    check("the refusal message names the live lanes",
          all(lane in str(bad_write.get("error")) for lane in store.lane_ids()), str(bad_write))
    bad_shape = store.set_status("pcc", "no spaces!", None, "user", scope="project")
    check("a status that cannot be an id is refused before the lookup",
          bad_shape.get("code") == "bad_status" and (store.get_status("pcc") or {})
          .get("status") == "active", str(bad_shape.get("code")))

    # --- a stored id that is no longer live ---------------------------------
    section("reads / a stored lane id that no longer exists degrades to the first lane")
    raw = sqlite3.connect(db)
    try:
        raw.execute("UPDATE link SET status = 'ghost-lane' WHERE session_id = 's-qa'")
        raw.commit()
    finally:
        raw.close()
    first = store.lane_ids()[0]
    ghost_card = store.session_card("s-qa")
    print(f"  ghost session lane: {json.dumps(ghost_card.get('lane'), default=str)}")
    check("the session card shows the first lane instead of blank",
          (ghost_card.get("lane") or {}).get("status") == first
          and (ghost_card.get("lane") or {}).get("scope") == "session",
          str(ghost_card.get("lane")))
    store.set_status("pcc", "active", "back to live", "user", session_id="s-qa")

    raw = sqlite3.connect(db)
    try:
        raw.execute("UPDATE status SET status = 'ghost-lane' WHERE project_id = 'pcc'")
        raw.commit()
    finally:
        raw.close()
    ghost_project = store.card_for_project("pcc")
    check("a project card with a stale lane also degrades to the first lane",
          (ghost_project.get("lane") or {}).get("status") == first
          and (ghost_project.get("lane") or {}).get("scope") == "project",
          str(ghost_project.get("lane")))
    store.set_status("pcc", "active", None, "user", scope="project")

    # --- migration -----------------------------------------------------------
    section("migration / a five-lane board upgrades in a clean process")
    write_old_board(SCRATCH / "mig-home" / "plugin-data" / "project-command-center" / "board.db")
    env = dict(os.environ, HERMES_HOME=str(SCRATCH / "mig-home"),
               PCC_LANES_SCRATCH=str(SCRATCH), TERMINAL_CWD=str(outside))
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--migration"],
                            capture_output=True, text=True, env=env)
    print(result.stdout.rstrip())
    if result.returncode != 0:
        print(result.stderr.rstrip())
    check("the migration pass is green", result.returncode == 0, f"exit {result.returncode}")

    # --- routes --------------------------------------------------------------
    section("routes / lanes routes keep the board conventions: HTTP 200 with a code")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Capture broadcasts instead of reaching a gateway that is not running here.
    events: list[tuple[str, str, Any]] = []
    try:
        import hermes_cli.plugin_events as plugin_events
        plugin_events.broadcast_plugin_event = (
            lambda name, event, payload: events.append((name, event, payload)) or 1)
    except Exception:
        pass

    api = load("pcc_lanes_api", PLUGIN / "dashboard" / "plugin_api.py")
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)

    def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        response = getattr(client, method)(path, json=body) if body is not None else \
            getattr(client, method)(path)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"code": "not_json", "error": response.text[:200]}

    code, listed = call("get", "/board/lanes")
    print(f"  GET:  HTTP {code} {json.dumps(listed, default=str)}")
    check("GET /board/lanes answers the seven system lanes",
          code == 200 and listed.get("ok") is True and ids_of(listed.get("lanes") or []) == DEFAULT_IDS,
          f"HTTP {code} {listed.get('code')}")
    check("every row carries system: 0|1",
          all(row.get("system") == 1 for row in listed.get("lanes") or []),
          str([(row.get("id"), row.get("system")) for row in listed.get("lanes") or []]))

    code, created = call("post", "/board/lanes", {"label": "Route Lane"})
    check("POST /board/lanes creates a slugged custom lane",
          code == 200 and created.get("ok") is True and created.get("lane") == "route-lane"
          and by_id(created.get("lanes") or [], "route-lane").get("custom") == 1
          and by_id(created.get("lanes") or [], "route-lane").get("system") == 0,
          f"HTTP {code} {created.get('code')}")

    code, dupe = call("post", "/board/lanes", {"label": "Route Lane"})
    check("a duplicate create is HTTP 200 lane_exists",
          code == 200 and dupe.get("ok") is False and dupe.get("code") == "lane_exists",
          f"HTTP {code} {dupe.get('code')}")

    code, renamed = call("post", "/board/lanes", {"id": "route-lane", "label": "Route Renamed"})
    check("a rename by id works through the route",
          code == 200 and renamed.get("ok") is True
          and by_id(renamed.get("lanes") or [], "route-lane").get("label") == "Route Renamed",
          f"HTTP {code} {renamed.get('code')}")

    code, ordered = call("post", "/board/lanes/order",
                         {"ids": ["route-lane"] + DEFAULT_IDS})
    check("an order permutation is accepted",
          code == 200 and ordered.get("ok") is True
          and ids_of(ordered.get("lanes") or []) == ["route-lane"] + DEFAULT_IDS,
          f"HTTP {code} {ordered.get('code')}")

    code, bad = call("post", "/board/lanes/order", {"ids": ["route-lane"]})
    check("a non permutation is HTTP 200 bad_order",
          code == 200 and bad.get("ok") is False and bad.get("code") == "bad_order",
          f"HTTP {code} {bad.get('code')}")
    code, back = call("post", "/board/lanes/order", {"ids": DEFAULT_IDS + ["route-lane"]})
    check("the order can be restored", code == 200 and back.get("ok") is True,
          f"HTTP {code} {back.get('code')}")

    code, system_gone = call("post", "/board/lanes/delete", {"id": "review"})
    check("deleting a system lane through the route is system_lane",
          code == 200 and system_gone.get("ok") is False
          and system_gone.get("code") == "system_lane", f"HTTP {code} {system_gone.get('code')}")

    code, gone = call("post", "/board/lanes/delete", {"id": "route-lane"})
    check("an unused custom lane deletes through the route",
          code == 200 and gone.get("ok") is True and store.lane_ids() == DEFAULT_IDS,
          f"HTTP {code} {gone.get('code')}")
    code, unknown = call("post", "/board/lanes/delete", {"id": "route-lane"})
    check("deleting an unknown lane is HTTP 200 unknown_lane",
          code == 200 and unknown.get("code") == "unknown_lane",
          f"HTTP {code} {unknown.get('code')}")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + ", ".join(FAILURES))
        return 1
    print("all checks ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
