#!/usr/bin/env python3
"""The /goals route: one batched read of each session's ACTIVE /goal.

Plain Python like the other suites, no pytest: the plugin router is mounted on a bare FastAPI app
and driven with TestClient. The route reads the persisted ``state_meta`` row through the published
``hermes_state`` surface (``SessionDB(read_only=True)``), so the fixture is the real database shape:
each case swaps ``_goal_db`` for a handle whose ``get_meta`` answers a map of goal JSON, and one
case runs the real ``SessionDB`` against a temporary state.db. No case touches this machine's goal
rows, and the real handle factory is restored before the module-unavailable case and at the end.

    python3 tests/test_goals.py

No network and no real plugin data.
"""

from __future__ import annotations

import sys as _sys

_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402,F401  finds the Hermes tree and a sandbox home

import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

PLUGIN = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


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


def main() -> int:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api = load("pcc_goals_api", PLUGIN / "dashboard" / "plugin_api.py")
    real_goal_db = api._goal_db
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)

    def read(ids: str) -> tuple[int, dict[str, Any]]:
        response = client.get("/goals", params={"session_ids": ids})
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"code": "not_json", "error": response.text[:200]}

    def serve(rows: dict[str, Any], *, raises: set[str] | None = None):
        """Replace the state.db handle with a map lookup, and record every id it was asked for."""

        asked: list[str] = []

        class FakeGoalDB:
            def get_meta(self, key: str) -> Optional[str]:
                session_id = key[len("goal:"):] if key.startswith("goal:") else key
                asked.append(session_id)
                if raises and session_id in raises:
                    raise RuntimeError("state.db is locked for this test")
                value = rows.get(session_id)
                return None if value is None else json.dumps(value)

            def close(self) -> None:
                pass

        setattr(api, "_goal_db", lambda: FakeGoalDB())
        return asked

    def goal(title: str, *, status: str = "active", turns: int = 3, budget: int = 12,
             subgoals: list[str] | None = None, gates: list[Any] | None = None,
             last_turn_at: float = 1_700_000_000.0) -> dict[str, Any]:
        """The JSON `hermes_cli.goals` persists: GoalState.to_json() without the wait machinery."""
        return {"goal": title, "status": status, "turns_used": turns, "max_turns": budget,
                "subgoals": list(subgoals or []), "gates": list(gates or []),
                "contract": {}, "created_at": 1_699_000_000.0, "last_turn_at": last_turn_at}

    try:
        # --- the answer ---------------------------------------------------------
        section("route / an active goal is the whole row the card renders")
        read("")  # warm the client, no ids
        api._CACHE.clear()
        gate = {"command": "pytest -q", "attempts": 2, "last_exit_code": 0}
        active = goal("Fix the login redirect loop", turns=4, budget=20,
                      subgoals=["sign in as a fresh user", "land on /dashboard"],
                      gates=[gate], last_turn_at=1_700_000_500.0)
        asked = serve({"20261113_212605_66fa65": active})
        code, body = read("20261113_212605_66fa65")
        row = (body.get("goals") or {}).get("20261113_212605_66fa65") or {}

        print(f"  GET /goals: HTTP {code} {json.dumps(body, default=str)}")
        check("an active goal answers HTTP 200 ok:true", code == 200 and body.get("ok") is True,
              f"HTTP {code} {body.get('code')}")
        check("the row carries exactly the line's fields",
              set(row) == {"title", "status", "turns_used", "max_turns", "subgoals",
                           "subgoals_done", "gates", "updated_at"}, str(sorted(row)))
        check("the title is the goal text",
              row.get("title") == "Fix the login redirect loop", str(row.get("title")))
        check("turns and budget are the persisted numbers",
              row.get("turns_used") == 4 and row.get("max_turns") == 20, str(row))
        check("the criteria list is intact",
              row.get("subgoals") == ["sign in as a fresh user", "land on /dashboard"],
              str(row.get("subgoals")))
        check("subgoals_done is 0 while the goal is active (Hermes records no per-criterion state)",
              row.get("subgoals_done") == 0, str(row.get("subgoals_done")))
        check("a gate's command, attempts and exit code travel; the output tail does not",
              row.get("gates") == [{"command": "pytest -q", "attempts": 2, "last_exit_code": 0}],
              str(row.get("gates")))
        check("updated_at is the last turn, not the read time",
              row.get("updated_at") == 1_700_000_500.0, str(row.get("updated_at")))
        check("the reader was asked for that one id", asked == ["20261113_212605_66fa65"], str(asked))

        # --- which goals qualify ------------------------------------------------
        section("route / only an active goal is included")
        api._CACHE.clear()
        serve({
            "s-active": goal("still running", status="active"),
            "s-paused": goal("paused one", status="paused"),
            "s-done": goal("finished one", status="done"),
            "s-cleared": goal("archived one", status="cleared")
        })
        code, body = read("s-active,s-paused,s-done,s-cleared,s-unknown")
        found = body.get("goals") or {}
        print(f"  goals: {sorted(found)}")
        check("the active goal is included, every other status is absent",
              list(found) == ["s-active"], str(sorted(found)))
        check("a session with no goal and an unknown id are simply absent",
              "s-unknown" not in found and code == 200 and body.get("ok") is True, str(sorted(found)))

        code, empty = read("")
        check("no ids is ok:true and no goals, not an error",
              code == 200 and empty == {"ok": True, "goals": {}}, str(empty))

        # --- rows that are not the shape we expect -------------------------------
        section("route / a persisted row is type-checked, never trusted")
        api._CACHE.clear()
        serve({
            "s-bad-json": "{not json",
            "s-not-object": ["a", "list"],
            "s-no-status": {"goal": "old row", "turns_used": 1, "max_turns": 5},
            "s-bad-fields": {"goal": "  padded  ", "status": "active", "turns_used": "7",
                             "max_turns": None, "subgoals": [" a ", "", 3, None],
                             "gates": [{"command": ""}, {"command": "npm test",
                                                        "last_exit_code": None}, "junk"]}
        })
        code, body = read("s-bad-json,s-not-object,s-no-status,s-bad-fields")
        found = body.get("goals") or {}
        print(f"  goals: {json.dumps(found, default=str)}")
        check("unparseable JSON and a non-object row are no goal, not a failed view",
              code == 200 and body.get("ok") is True and "s-bad-json" not in found
              and "s-not-object" not in found, str(sorted(found)))
        check("an old row with no status keeps reading as active, as GoalState.from_json defaulted it",
              (found.get("s-no-status") or {}).get("max_turns") == 5, str(found.get("s-no-status")))
        row = found.get("s-bad-fields") or {}
        check("fields are coerced to the card's types",
              row.get("title") == "padded" and row.get("turns_used") == 7
              and row.get("max_turns") == 20 and row.get("subgoals") == ["a"],
              str(row))
        check("a gate without a command is dropped; a pending one keeps last_exit_code null",
              row.get("gates") == [{"command": "npm test", "attempts": 0, "last_exit_code": None}],
              str(row.get("gates")))

        # --- ids ---------------------------------------------------------------
        section("route / the id list is de-duplicated, capped and shape checked")
        api._CACHE.clear()
        rows = {f"s{i:03d}": goal(f"goal {i}") for i in range(70)}
        asked = serve(rows)
        ids = ",".join([f"s{i:03d}" for i in range(70)] + ["s000", "s000"])
        code, body = read(ids)
        found = body.get("goals") or {}
        print(f"  asked for {len(asked)} ids, answered {len(found)} goals")
        check("the cap is 60 ids per read", len(asked) == 60 and len(found) == 60,
              f"asked {len(asked)} answered {len(found)}")
        check("the cap keeps the newest first ids in the caller's order",
              asked[0] == "s000" and asked[-1] == "s059", f"{asked[:2]} ... {asked[-2:]}")
        check("a duplicate id is one lookup, not two", asked.count("s000") == 1, str(asked.count("s000")))

        api._CACHE.clear()
        asked = serve({"20261113_212605_66fa65": goal("real one")})
        junk = "../../etc/passwd,a b,http://example.com,20261113_212605_66fa65,,..,ok_id.2:x-1"
        code, body = read(junk)
        check("ids that are not plain session-id shaped are dropped before any lookup",
              asked == ["20261113_212605_66fa65", "ok_id.2:x-1"], str(asked))
        check("the well formed ids still answer", len((body.get("goals") or {})) == 1, str(body))

        # --- failures -----------------------------------------------------------
        section("route / one unreadable session never blanks the rest")
        api._CACHE.clear()
        serve({"s-good": goal("readable")}, raises={"s-bad"})
        code, body = read("s-bad,s-good,s-none")
        print(f"  HTTP {code} {json.dumps(body, default=str)}")
        check("a per-session exception is skipped and the good goal still answers",
              code == 200 and body.get("ok") is True
              and list(body.get("goals") or {}) == ["s-good"], str(body))

        api._CACHE.clear()
        serve({}, raises={"s-a", "s-b"})
        code, body = read("s-a,s-b")
        print(f"  all failed: HTTP {code} {json.dumps(body, default=str)}")
        check("every read failing is goals_unavailable, never a lie that there are no goals",
              code == 200 and body.get("ok") is False
              and body.get("code") == "goals_unavailable", str(body))

        section("route / an unreadable state database answers goals_unavailable, not a 500")
        api._CACHE.clear()
        setattr(api, "_goal_db", real_goal_db)
        saved = sys.modules.get("hermes_state")
        sys.modules["hermes_state"] = None  # type: ignore[assignment]  # `from ... import` raises
        try:
            code, body = read("20261113_212605_66fa65")
        finally:
            if saved is None:
                sys.modules.pop("hermes_state", None)
            else:
                sys.modules["hermes_state"] = saved
        print(f"  HTTP {code} {json.dumps(body, default=str)}")
        check("a missing state module is HTTP 200 with code goals_unavailable",
              code == 200 and body.get("ok") is False
              and body.get("code") == "goals_unavailable", f"HTTP {code} {body.get('code')}")
        check("the error is one sentence, not a traceback",
              isinstance(body.get("error"), str) and body["error"].strip().endswith("."),
              str(body.get("error")))

        # --- the real read path --------------------------------------------------
        section("route / the real persisted row, through the published hermes_state surface")
        api._CACHE.clear()
        from hermes_state import SessionDB

        tmp = Path(tempfile.mkdtemp()) / "state.db"
        conn = sqlite3.connect(tmp)
        conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO state_meta VALUES (?, ?)",
                     ("goal:real-1", json.dumps(goal("A real persisted goal", turns=6, budget=30))))
        conn.execute("INSERT INTO state_meta VALUES (?, ?)",
                     ("goal:real-done", json.dumps(goal("done", status="done"))))
        conn.commit()
        conn.close()
        setattr(api, "_goal_db", lambda: SessionDB(tmp, read_only=True))
        code, body = read("real-1,real-done,real-missing")
        found = body.get("goals") or {}
        print(f"  HTTP {code} {json.dumps(found, default=str)}")
        check("a goal written to state_meta is read back as the card's row",
              code == 200 and body.get("ok") is True
              and (found.get("real-1") or {}).get("title") == "A real persisted goal"
              and (found.get("real-1") or {}).get("turns_used") == 6
              and (found.get("real-1") or {}).get("max_turns") == 30, str(found))
        check("a done row and a session with no row stay absent",
              "real-done" not in found and "real-missing" not in found, str(sorted(found)))

        section("route / the answer is cached 30 s under the sorted id list")
        api._CACHE.clear()
        asked = serve({"s1": goal("cached one"), "s2": goal("cached two")})
        first_code, first = read("s2,s1")
        second_code, second = read("s1,s2")
        print(f"  first: {first.get('ok')} cached={first.get('cached')} "
              f"second: {second.get('ok')} cached={second.get('cached')} asked={len(asked)}")
        check("the first read fills the cache and the second answers from it",
              first_code == second_code == 200 and second.get("cached") is True,
              f"cached={second.get('cached')}")
        check("a reordered id list is the same key, so the meta read ran once per id",
              len(asked) == 2 and sorted(second.get("goals") or {}) == ["s1", "s2"], str(asked))
        check("the cache drops the row shape down to the same keys",
              set((second.get("goals") or {}).get("s2") or {}) == set((first.get("goals") or {}).get("s2") or {}))
    finally:
        # The patch swapped the handle factory in this process: put the real one back so a later
        # suite imported into the same interpreter never inherits the fake.
        setattr(api, "_goal_db", real_goal_db)

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("\nall goals route checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
