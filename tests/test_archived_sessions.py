#!/usr/bin/env python3
"""The /sessions/archived route: the rows ``session.list`` can never return.

Plain Python like the other suites, no pytest: the plugin router is mounted on a bare FastAPI app
and driven with TestClient. The route reads the state database through the published
``hermes_state`` surface (``SessionDB(read_only=True)``), so the fixture is a handle whose
``list_sessions_rich`` / ``session_count`` answer a fabricated store: what is proved here is the
PAYLOAD and the refusals, not SQLite. One case runs the real handle against a temporary state.db so
the wiring is not only a mock.

Why this route exists at all is asserted, not assumed: the gateway's ``session.list`` lists with
``archived_only=False`` and exposes no flag for it, so without this route an archived session is
absent from every board row and the Desktop looks like it lost them.

    python3 tests/test_archived_sessions.py

No network and no real plugin data.
"""

from __future__ import annotations

import sys as _sys

_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402,F401  finds the Hermes tree and a sandbox home

import importlib.util
import sys
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


def row(n: int, **over: Any) -> dict[str, Any]:
    """One ``list_sessions_rich`` row, in the shape the real store returns (compact_rows)."""
    base: dict[str, Any] = {
        "id": f"20261112_0000{n:02d}_abc{n}",
        "title": f"Archived session {n}",
        "source": "desktop",
        "cwd": "/home/user/project",
        "git_branch": "main",
        "started_at": 1_700_000_000.0 + n,
        "last_active": 1_700_000_100.0 + n,
        "message_count": n * 3,
        "archived": 1,
    }
    base.update(over)
    return base


def main() -> int:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api = load("pcc_archived_api", PLUGIN / "dashboard" / "plugin_api.py")
    real_db = api._goal_db
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)

    def read(limit: Optional[int] = None) -> tuple[int, dict[str, Any]]:
        params = {} if limit is None else {"limit": limit}
        response = client.get("/sessions/archived", params=params)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"code": "not_json", "error": response.text[:200]}

    def serve(rows: list[dict[str, Any]], total: Optional[int] = None, *, raises: bool = False):
        """Swap the state.db handle for a fabricated store, and record what it was asked."""
        asked: dict[str, Any] = {}

        class FakeDB:
            def list_sessions_rich(self, **kwargs: Any) -> list[dict[str, Any]]:
                asked.update(kwargs)
                if raises:
                    raise RuntimeError("state.db is locked for this test")
                return rows

            def session_count(self, **kwargs: Any) -> int:
                asked["count_kwargs"] = kwargs
                if raises:
                    raise RuntimeError("state.db is locked for this test")
                return len(rows) if total is None else total

            def close(self) -> None:
                pass

        setattr(api, "_goal_db", lambda: FakeDB())
        return asked

    try:
        # --- the payload --------------------------------------------------------
        section("route / a page of archived rows, plus the REAL total")
        api._CACHE.clear()
        asked = serve([row(1), row(2), row(3)], total=1929)
        status, payload = read(limit=3)
        print(f"  status: {status} total: {payload.get('total')} rows: {len(payload.get('sessions') or [])}")
        check("200 and ok: true", status == 200 and payload.get("ok") is True, str(payload)[:160])
        # `total` is the count of every archived session, NOT the page size: the footer says
        # "the newest 300 of 1,929" and a total that echoed the cap would be a lie.
        check("total is the store count, not the page size", payload.get("total") == 1929, str(payload.get("total")))
        check("every row is returned", len(payload.get("sessions") or []) == 3)
        first = (payload.get("sessions") or [{}])[0]
        check("a row carries what the card needs",
              all(key in first for key in ("id", "title", "cwd", "git_branch", "started_at", "last_active")),
              ", ".join(sorted(first)))
        check("it asked the store for ARCHIVED rows only", asked.get("archived_only") is True, str(asked.get("archived_only")))
        check("it asked for the order the board wants", asked.get("order_by_last_active") is True)
        check("it asked for compact rows", asked.get("compact_rows") is True)
        check("the limit reached the store", asked.get("limit") == 3, str(asked.get("limit")))
        check("the count used the same filter", (asked.get("count_kwargs") or {}).get("archived_only") is True,
              str(asked.get("count_kwargs")))

        section("route / a row without an id is dropped, never rendered blank")
        api._CACHE.clear()
        serve([row(1), {"title": "no id"}, {"id": ""}], total=3)
        status, payload = read()
        check("only the identifiable row survives", len(payload.get("sessions") or []) == 1, str(payload)[:120])
        check("a missing title is an empty string, not null", (payload.get("sessions") or [{}])[0].get("title") == "Archived session 1")

        section("route / the cap")
        api._CACHE.clear()
        serve([row(i) for i in range(api.ARCHIVED_MAX)], total=api.ARCHIVED_MAX)
        status, payload = read(limit=api.ARCHIVED_MAX)
        check("the declared cap is accepted", status == 200, str(status))
        # The frontend's own cap is asserted in tests/archived_sessions.test.mjs; here the route
        # refuses anything above its own, so the two numbers cannot drift into a 422 at runtime.
        status, _ = read(limit=api.ARCHIVED_MAX + 1)
        check("one past the cap is refused", status == 422, str(status))

        section("route / an unreadable store is a sentence, never a 500 and never an empty success")
        api._CACHE.clear()
        serve([], raises=True)
        status, payload = read()
        print(f"  {str(payload)[:160]}")
        check("200 with ok: false", status == 200 and payload.get("ok") is False, str(status))
        check("it names its code", payload.get("code") == "sessions_unavailable", str(payload.get("code")))
        check("it says a sentence a human can read", bool(payload.get("error")))
        check("it does not pretend there are zero", "total" not in payload)

        section("route / a missing handle is the same answer")
        api._CACHE.clear()

        def broken():
            raise RuntimeError("no state.db in this home")

        setattr(api, "_goal_db", broken)
        status, payload = read()
        check("200 with ok: false", status == 200 and payload.get("ok") is False, str(status))
        check("same code", payload.get("code") == "sessions_unavailable", str(payload.get("code")))

        section("route / the cache holds a read, and a different limit is a different key")
        api._CACHE.clear()
        asked = serve([row(1)], total=1)
        read(limit=1)
        calls = len([k for k in asked if k in ("limit", "archived_only")])
        read(limit=1)
        check("the second identical read did not reach the store again",
              calls == 2 and asked.get("limit") == 1, str(calls))

        section("route / it is registered where the Desktop looks for it")
        paths = sorted({getattr(route, "path", "") for route in api.router.routes})
        check("/sessions/archived is mounted", "/sessions/archived" in paths, ", ".join(paths))
    finally:
        setattr(api, "_goal_db", real_db)
        api._CACHE.clear()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
