#!/usr/bin/env python3
"""State harness for project-command-center: GET /github across open, closed and all.

Plain Python like tests/test_avatars.py: the plugin is path-loaded, ``_auth`` and
``_repo_context`` are replaced, and ``_GhReader`` is a fake that answers from one
canned board keyed by the requested state, so no request leaves the machine.

What is under test is the contract the Desktop filters rest on: ``state=all``
carries the open half and the closed half in one response, per-state limits hold,
every issue row names its ``state_reason``, every PR row names ``draft`` and
``merged``, ``counts`` still counts open rows only, and the 60 s cache answers a
repeat read without touching the reader.

    python3 tests/test_github_states.py
"""

from __future__ import annotations

import sys as _sys

_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402,F401  finds the Hermes tree and a sandbox home

import asyncio
import importlib.util
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN = Path(__file__).resolve().parent.parent
SCRATCH = Path(os.environ.get(
    "PCC_STATE_SCRATCH", Path.home() / ".hermes" / "cache" / "scratch" / "pcc-states"))
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: Any = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}{f' ({detail})' if detail else ''}")
    if not condition:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ".ljust(78, "="))


def load_api() -> Any:
    path = PLUGIN / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("pcc_api_state_test", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pcc_api_state_test"] = module
    spec.loader.exec_module(module)
    return module


OPEN_ISSUES = [
    {"number": 1, "title": "open one", "state": "open", "state_reason": None,
     "html_url": "https://github.com/o/r/issues/1", "user": {"login": "alice"},
     "labels": [], "assignees": [], "comments": 0,
     "updated_at": "2026-11-09T10:00:00Z", "created_at": "2026-11-01T10:00:00Z"},
    {"number": 2, "title": "open two", "state": "open", "state_reason": None,
     "html_url": "https://github.com/o/r/issues/2", "user": {"login": "alice"},
     "labels": [], "assignees": [], "comments": 0,
     "updated_at": "2026-11-09T09:00:00Z", "created_at": "2026-11-01T10:00:00Z"},
]
CLOSED_ISSUES = [
    {"number": 3, "title": "done", "state": "closed", "state_reason": "completed",
     "html_url": "https://github.com/o/r/issues/3", "user": {"login": "alice"},
     "labels": [], "assignees": [], "comments": 0,
     "updated_at": "2026-11-08T10:00:00Z", "created_at": "2026-11-01T10:00:00Z"},
    {"number": 4, "title": "not planned", "state": "closed", "state_reason": "not_planned",
     "html_url": "https://github.com/o/r/issues/4", "user": {"login": "alice"},
     "labels": [], "assignees": [], "comments": 0,
     "updated_at": "2026-11-07T10:00:00Z", "created_at": "2026-11-01T10:00:00Z"},
    # GitHub also returns pull requests from /issues: it must be dropped there.
    {"number": 70, "title": "a PR leaking in", "state": "closed", "state_reason": None,
     "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/70"},
     "html_url": "https://github.com/o/r/pull/70", "user": {"login": "alice"},
     "labels": [], "assignees": [], "comments": 0,
     "updated_at": "2026-11-10T10:00:00Z", "created_at": "2026-11-01T10:00:00Z"},
]
OPEN_PULLS = [
    {"number": 6, "title": "open pr", "state": "open", "draft": False, "merged_at": None,
     "html_url": "https://github.com/o/r/pull/6", "user": {"login": "alice"},
     "head": {"ref": "feat/open"}, "base": {"ref": "main"},
     "updated_at": "2026-11-09T12:00:00Z", "created_at": "2026-11-01T10:00:00Z"},
    {"number": 9, "title": "draft pr", "state": "open", "draft": True, "merged_at": None,
     "html_url": "https://github.com/o/r/pull/9", "user": {"login": "alice"},
     "head": {"ref": "feat/draft"}, "base": {"ref": "main"},
     "updated_at": "2026-11-09T11:00:00Z", "created_at": "2026-11-01T10:00:00Z"},
]
CLOSED_PULLS = [
    {"number": 7, "title": "merged pr", "state": "closed", "draft": False,
     "merged_at": "2026-11-08T12:00:00Z",
     "html_url": "https://github.com/o/r/pull/7", "user": {"login": "alice"},
     "head": {"ref": "feat/merged"}, "base": {"ref": "main"},
     "updated_at": "2026-11-08T12:00:00Z", "created_at": "2026-11-01T10:00:00Z"},
    {"number": 8, "title": "abandoned pr", "state": "closed", "draft": False, "merged_at": None,
     "html_url": "https://github.com/o/r/pull/8", "user": {"login": "alice"},
     "head": {"ref": "feat/abandoned"}, "base": {"ref": "main"},
     "updated_at": "2026-11-06T12:00:00Z", "created_at": "2026-11-01T10:00:00Z"},
]


class _FakeGh:
    """One canned board, sliced by the state the route asks for."""

    def __init__(self, api: Any, calls: list[str]) -> None:
        self.api = api
        self.calls = calls
        self.auth = "gh"
        self.auth_note = None
        self.credential = "none"
        self.owner, self.repo = "o", "r"

    async def rest(self, endpoint: str, params: Any = None, etag: Optional[str] = None) -> Any:
        wanted = str((params or {}).get("state") or "open")
        kind = "issues" if endpoint.endswith("/issues") else "pulls"
        self.calls.append(f"{kind}:{wanted}")
        per_page = int((params or {}).get("per_page") or 0)
        if kind == "issues":
            rows = list(OPEN_ISSUES if wanted in ("open", "all") else CLOSED_ISSUES)
            if wanted == "open":
                rows = list(OPEN_ISSUES)
            elif wanted == "closed":
                rows = list(CLOSED_ISSUES)
            else:
                rows = OPEN_ISSUES + CLOSED_ISSUES
        else:
            if wanted == "open":
                rows = list(OPEN_PULLS)
            elif wanted == "closed":
                rows = list(CLOSED_PULLS)
            else:
                rows = OPEN_PULLS + CLOSED_PULLS
            rows = rows[:per_page]
        return self.api._Response(200, rows, {"etag": 'W/"one"', "x-ratelimit-remaining": "4999"})


class _FakeRefs(_FakeGh):
    """A fake that also answers the refs GraphQL query."""

    def __init__(self, api: Any, calls: list[str]) -> None:
        super().__init__(api, calls)
        self.credential = "gh"

    async def graphql(self, query: str, variables: Any, partial: bool = False) -> Any:
        return {"repository": {
            "r0": {"__typename": "Issue", "number": 4, "title": "not planned",
                   "state": "CLOSED", "stateReason": "NOT_PLANNED",
                   "url": "https://github.com/o/r/issues/4", "updatedAt": "2026-11-07T10:00:00Z",
                   "author": {"login": "alice"}},
            "r1": {"__typename": "PullRequest", "number": 7, "title": "merged pr",
                   "state": "MERGED", "isDraft": False, "merged": True,
                   "url": "https://github.com/o/r/pull/7", "updatedAt": "2026-11-08T12:00:00Z",
                   "author": {"login": "alice"}},
            "r2": {"__typename": "PullRequest", "number": 9, "title": "draft pr",
                   "state": "OPEN", "isDraft": True, "merged": False,
                   "url": "https://github.com/o/r/pull/9", "updatedAt": "2026-11-09T11:00:00Z",
                   "author": {"login": "alice"}},
        }}


async def _fake_ctx(api: Any, path: Path) -> Any:
    return api._RepoContext("o", "r", str(path), "ok", None, None)


async def _fake_auth() -> tuple[str, None]:
    return "gh", None


def board(api: Any, folder: Path, **kwargs: Any) -> tuple[dict[str, Any], list[str]]:
    # The route's Query defaults are FastAPI markers, not values, when it is
    # called as a plain coroutine: spell every parameter out.
    params: dict[str, Any] = {"state": "all", "limit": 50, "refresh": 0}
    params.update(kwargs)
    calls: list[str] = []
    originals = (api._repo_context, api._auth, api._GhReader)
    api._repo_context = lambda path: _fake_ctx(api, path)
    api._auth = _fake_auth
    api._GhReader = lambda owner, name, mode, token: _FakeGh(api, calls)
    api._CACHE.clear()
    try:
        payload = asyncio.run(api.github(str(folder), **params))
    finally:
        api._repo_context, api._auth, api._GhReader = originals
    return payload, calls


def main() -> int:
    if SCRATCH.resolve() == (Path.home() / ".hermes").resolve():
        sys.exit("refusing to build fixtures in ~/.hermes")
    started = time.monotonic()
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    folder = SCRATCH / "repo"
    folder.mkdir(parents=True)

    api = load_api()

    section("state=all is the default and it is one client read")
    payload, calls = board(api, folder, refresh=1)
    check("the read is ok", payload.get("ok") is True, payload.get("code"))
    check("four backend reads, open first then closed",
          calls == ["issues:open", "pulls:open", "issues:closed", "pulls:closed"], str(calls))
    check("the payload names its halves", payload.get("states") == ["open", "closed"],
          str(payload.get("states")))

    section("issue rows carry state and the close reason")
    issues = {row["number"]: row for row in payload.get("issues") or []}
    check("open issues are present", 1 in issues and 2 in issues, sorted(issues))
    check("closed issue is present", 3 in issues, sorted(issues))
    check("not planned issue is present", 4 in issues, sorted(issues))
    check("the leaked pull request is not an issue", 70 not in issues, sorted(issues))
    check("open row reason is null", issues.get(1, {}).get("state_reason") is None,
          str(issues.get(1, {}).get("state_reason")))
    check("completed reason", issues.get(3, {}).get("state_reason") == "completed")
    check("not planned reason", issues.get(4, {}).get("state_reason") == "not_planned")
    check("closed rows say closed", issues.get(3, {}).get("state") == "closed")

    section("pull rows carry state, draft and merged")
    pulls = {row["number"]: row for row in payload.get("prs") or []}
    check("all four pull requests are present", sorted(pulls) == [6, 7, 8, 9], sorted(pulls))
    check("merged flag on the merged PR", pulls.get(7, {}).get("merged") is True)
    check("merged PR is closed", pulls.get(7, {}).get("state") == "closed")
    check("closed unmerged PR is not merged", pulls.get(8, {}).get("merged") is False)
    check("draft flag on the draft PR", pulls.get(9, {}).get("draft") is True)
    check("open PR is open", pulls.get(6, {}).get("state") == "open")
    check("a closed PR is not a draft", pulls.get(8, {}).get("draft") is False)

    section("counts still count open rows only")
    counts = payload.get("counts") or {}
    check("open issues", counts.get("issues_open") == 2, counts.get("issues_open"))
    check("open PRs", counts.get("prs_open") == 2, counts.get("prs_open"))
    check("drafts", counts.get("drafts") == 1, counts.get("drafts"))

    section("limit caps each list per state, so open rows cannot be crowded out")
    limited, _ = board(api, folder, limit=1, refresh=1)
    check("one open and one closed issue", len(limited.get("issues") or []) == 2,
          [r["number"] for r in limited.get("issues") or []])
    check("one open and one closed PR", len(limited.get("prs") or []) == 2,
          [r["number"] for r in limited.get("prs") or []])
    check("the open half survives", any(r["state"] == "open" for r in limited.get("issues") or []))

    section("state=open keeps the old contract exactly")
    opened, calls = board(api, folder, state="open", refresh=1)
    check("two reads only", calls == ["issues:open", "pulls:open"], str(calls))
    check("only open issues", all(r["state"] == "open" for r in opened.get("issues") or []),
          [r["state"] for r in opened.get("issues") or []])
    check("only open PRs", all(r["state"] == "open" for r in opened.get("prs") or []))
    check("the draft is in the open half", any(r["draft"] for r in opened.get("prs") or []))

    section("state=closed carries merged rows")
    closed, calls = board(api, folder, state="closed", refresh=1)
    check("two reads only", calls == ["issues:closed", "pulls:closed"], str(calls))
    check("only closed issues", all(r["state"] == "closed" for r in closed.get("issues") or []))
    check("the merged PR is there", any(r["merged"] for r in closed.get("prs") or []))

    section("the 60 s cache answers without a second read")
    calls = []
    api._CACHE.clear()
    originals = (api._repo_context, api._auth, api._GhReader)
    api._repo_context = lambda path: _fake_ctx(api, path)
    api._auth = _fake_auth
    api._GhReader = lambda owner, name, mode, token: _FakeGh(api, calls)
    try:
        first = asyncio.run(api.github(str(folder), state="all", limit=50, refresh=0))
        after_first = len(calls)
        second = asyncio.run(api.github(str(folder), state="all", limit=50, refresh=0))
        check("a cached read makes no calls", len(calls) == after_first, f"{after_first} -> {len(calls)}")
        check("the second answer is flagged cached", second.get("cached") is True)
        check("the cached answer is the same board", len(second.get("issues") or []) == len(first.get("issues") or []))
        forced = asyncio.run(api.github(str(folder), state="all", limit=50, refresh=1))
        check("refresh=1 reads again", len(calls) > after_first, f"{after_first} -> {len(calls)}")
        check("a forced answer is not flagged cached", forced.get("cached") is False)
    finally:
        api._repo_context, api._auth, api._GhReader = originals

    section("ref pills carry the same state vocabulary")
    calls = []
    api._CACHE.clear()
    originals = api._repo_context, api._auth, api._GhReader
    api._repo_context = lambda path: _fake_ctx(api, path)
    api._auth = _fake_auth
    api._GhReader = lambda owner, name, mode, token: _FakeRefs(api, calls)
    try:
        refs = asyncio.run(api.github_refs(str(folder), "4,7,9", 1))
    finally:
        api._repo_context, api._auth, api._GhReader = originals
    found = refs.get("refs") or {}
    check("refs answered", refs.get("ok") is True, refs.get("code"))
    check("the not planned issue keeps its reason",
          found.get("4", {}).get("state_reason") == "not_planned", str(found.get("4")))
    check("the merged PR is closed and merged",
          found.get("7", {}).get("state") == "closed" and found.get("7", {}).get("merged") is True,
          str(found.get("7")))
    check("the draft PR is flagged", found.get("9", {}).get("draft") is True, str(found.get("9")))

    section("router still answers")
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    check("health is still 200", client.get("/health").status_code == 200)

    section("the HTTP contract accepts state=all")
    calls = []
    originals = api._repo_context, api._auth, api._GhReader
    api._repo_context = lambda path: _fake_ctx(api, path)
    api._auth = _fake_auth
    api._GhReader = lambda owner, name, mode, token: _FakeGh(api, calls)
    api._CACHE.clear()
    try:
        response = client.get("/github", params={"path": str(folder), "state": "all", "limit": 2, "refresh": 1})
    finally:
        api._repo_context, api._auth, api._GhReader = originals
    check("200", response.status_code == 200, response.status_code)
    body = response.json() if response.status_code == 200 else {}
    check("both halves in one response", body.get("states") == ["open", "closed"], str(body.get("states")))
    check("issues carry a state", all("state" in row for row in body.get("issues") or []))
    check("prs carry merged and draft",
          all("merged" in row and "draft" in row for row in body.get("prs") or []))
    check("a bad state still 422s",
          client.get("/github", params={"path": str(folder), "state": "merged"}).status_code == 422)

    print(f"\n{len(FAILURES)} failure(s) in {time.monotonic() - started:.1f}s")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
