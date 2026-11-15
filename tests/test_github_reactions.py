#!/usr/bin/env python3
"""Reaction harness for project-command-center.

Plain Python like tests/test_github_states.py: the plugin is path-loaded, the
folder/auth helpers are replaced, and ``_GhReader`` / ``_gh_write`` are fakes
that answer from canned rows, so no request leaves the machine.

What is under test is the contract the chip row rests on: the four reacting
objects each read their own reactions URL, ``mine`` names this account only, a
failed reactions read degrades that comment to ``[]`` without failing the
detail, the write route toggles when the account already reacted (DELETE on the
reaction id), adds otherwise, and every refusal is HTTP 200 with a stable code.

    python3 tests/test_github_reactions.py
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
    "PCC_REACTION_SCRATCH", Path.home() / ".hermes" / "cache" / "scratch" / "pcc-reactions"))
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: Any = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}{f' ({detail})' if detail else ''}")
    if not condition:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ".ljust(78, "="))


def load_api() -> Any:
    path = PLUGIN / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("pcc_api_reaction_test", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pcc_api_reaction_test"] = module
    spec.loader.exec_module(module)
    return module


ISSUE = {
    "number": 1, "title": "reactions", "state": "open", "state_reason": None,
    "html_url": "https://github.com/o/r/issues/1", "user": {"login": "alice"},
    "labels": [], "assignees": [], "comments": 2,
    "created_at": "2026-11-01T10:00:00Z", "updated_at": "2026-11-09T10:00:00Z",
    "body": "The body everybody reacts to.",
}
COMMENTS = [
    {"id": 11, "user": {"login": "alice"}, "body": "first", "created_at": "2026-11-02T10:00:00Z",
     "updated_at": None, "html_url": "https://github.com/o/r/issues/1#issuecomment-11"},
    {"id": 12, "user": {"login": "bob"}, "body": "second", "created_at": "2026-11-03T10:00:00Z",
     "updated_at": None, "html_url": "https://github.com/o/r/issues/1#issuecomment-12"},
]
PULL = {
    "number": 2, "title": "a pull", "state": "open", "draft": False, "merged_at": None,
    "html_url": "https://github.com/o/r/pull/2", "user": {"login": "alice"},
    "head": {"ref": "feat/x"}, "base": {"ref": "main"},
    "created_at": "2026-11-01T10:00:00Z", "updated_at": "2026-11-09T10:00:00Z",
    "body": "PR body",
}

BODY_REACTIONS = [
    {"id": 901, "content": "heart", "user": {"login": "alice"}},
    {"id": 902, "content": "heart", "user": {"login": "bob"}},
    {"id": 903, "content": "+1", "user": {"login": "bob"}},
    {"id": 904, "content": "heart", "user": {"login": "me"}},
]
COMMENT_REACTIONS = {
    11: [{"id": 911, "content": "+1", "user": {"login": "me"}},
         {"id": 912, "content": "eyes", "user": {"login": "bob"}},
         {"id": 913, "content": "eyes", "user": {"login": "alice"}}],
    # 12's read raises, to prove one object cannot sink the detail.
}


class _FakeReader:
    """Canned REST rows keyed by endpoint, plus a log of what was asked."""

    def __init__(self, api: Any, calls: list[str], *, fail: tuple[str, ...] = ()) -> None:
        self.api = api
        self.calls = calls
        self.fail = set(fail)
        self.auth = "gh"
        self.auth_note = None
        self.credential = "gh"
        self.owner, self.repo = "o", "r"

    async def rest(self, endpoint: str, params: Any = None, etag: Optional[str] = None) -> Any:
        self.calls.append(f"GET {endpoint}")
        if endpoint in self.fail:
            raise self.api._UpstreamError(
                f"boom at {endpoint}", code="upstream_error", detail=f"GET {endpoint} -> HTTP 500",
                endpoint=endpoint, credential="gh")
        if endpoint == "/repos/o/r/issues/1":
            return self.api._Response(200, ISSUE, {})
        if endpoint == "/repos/o/r/pulls/2":
            return self.api._Response(200, PULL, {})
        if endpoint.endswith("/timeline"):
            return self.api._Response(200, [], {})
        if endpoint.endswith("/files"):
            return self.api._Response(200, [], {})
        if endpoint.endswith("/reviews"):
            return self.api._Response(200, [], {})
        if endpoint == "/repos/o/r/issues/1/comments":
            return self.api._Response(200, [dict(row) for row in COMMENTS], {})
        if endpoint == "/repos/o/r/issues/1/reactions" or endpoint == "/repos/o/r/issues/2/reactions":
            return self.api._Response(200, [dict(row) for row in BODY_REACTIONS], {})
        if endpoint == "/repos/o/r/issues/comments/11/reactions":
            return self.api._Response(200, [dict(row) for row in COMMENT_REACTIONS[11]], {})
        if endpoint == "/repos/o/r/issues/comments/12/reactions":
            raise self.api._UpstreamError("no reactions here", code="upstream_error",
                                          detail="GET reactions -> HTTP 500", endpoint=endpoint)
        return self.api._Response(200, [], {})

    async def graphql(self, query: str, variables: Any, partial: bool = False) -> Any:
        raise self.api._UpstreamError("no graphql in this harness", code="upstream_error",
                                      detail="POST /graphql -> HTTP 500")


async def _fake_ctx(api: Any, path: Path) -> Any:
    return api._RepoContext("o", "r", str(path), "ok", None, None)


async def _fake_auth() -> tuple[str, None]:
    return "gh", None


async def _fake_write_repo(path: str) -> Any:
    return "o", "r"


def detail(api: Any, folder: Path, kind: str, number: int, **kwargs: Any) -> dict[str, Any]:
    originals = (api._repo_context, api._auth, api._GhReader, api._gh_user)
    calls: list[str] = []
    fail = kwargs.pop("fail", ())
    api._repo_context = lambda path: _fake_ctx(api, path)
    api._auth = _fake_auth
    api._GhReader = lambda owner, name, mode, token: _FakeReader(api, calls, fail=fail)
    api._gh_user = _async("me")
    api._CACHE.clear()
    try:
        route = api.github_issue if kind == "issue" else api.github_pr
        payload = asyncio.run(route(number, str(folder), 0))
    finally:
        api._repo_context, api._auth, api._GhReader, api._gh_user = originals
    payload["_calls"] = calls
    return payload


def _async(value: Any):
    async def answer(*_args: Any, **_kwargs: Any) -> Any:
        return value
    return answer


class _WriteLog:
    """The gh write fake: records the argv-level facts and mutates the store."""

    def __init__(self, api: Any, fail: bool = False) -> None:
        self.api = api
        self.calls: list[tuple[str, str, Any]] = []
        self.fail = fail

    async def __call__(self, method: str, path: str, body: Any, **_kwargs: Any) -> Any:
        self.calls.append((method, path, body))
        if self.fail:
            raise self.api._WriteFailure("forbidden", "the gh account may not react here.",
                                         "POST -> HTTP 403")
        reaction_id = int(path.rsplit("/", 1)[1]) if method == "DELETE" else 999
        if method == "DELETE":
            _REACTION_STORE[:] = [row for row in _REACTION_STORE if row.get("id") != reaction_id]
        else:
            _REACTION_STORE.append({"id": reaction_id, "content": body["content"],
                                    "user": {"login": "me"}})
        return {}


def react(api: Any, payload: dict[str, Any], *, store: list[dict[str, Any]] | None = None,
          gh_user: Optional[str] = "me", write_fail: bool = False,
          repo: Any = ("o", "r"), read_fail: bool = False) -> tuple[dict[str, Any], _WriteLog]:
    _REACTION_STORE[:] = [dict(row) for row in store or []]
    log = _WriteLog(api, fail=write_fail)
    originals = (api._write_repo, api._gh_user, api._gh_write, api._GitHub, api._CACHE)
    api._write_repo = _fake_write_repo if repo == ("o", "r") else _const(repo)
    api._gh_user = _async(gh_user)
    api._gh_write = log
    api._GitHub = lambda owner, name, mode, token: _FakeClient(api, read_fail=read_fail)
    api._CACHE.clear()
    try:
        answer = asyncio.run(api.github_reaction(path=str(SCRATCH / "repo"), payload=payload))
    finally:
        (api._write_repo, api._gh_user, api._gh_write, api._GitHub, api._CACHE) = originals
    return answer, log


def _const(value: Any):
    async def answer(*_args: Any, **_kwargs: Any) -> Any:
        return value
    return answer


# The one store the write fake and the read fake share: a DELETE leaves a row
# the confirming re-read then sees gone, exactly like the round trip it stands in for.
_REACTION_STORE: list[dict[str, Any]] = []


class _FakeClient:
    """The reaction read client: the module's own URL, answered from the store."""

    def __init__(self, api: Any, *, read_fail: bool = False) -> None:
        self.api = api
        self.read_fail = read_fail

    async def rest(self, endpoint: str, params: Any = None, etag: Optional[str] = None) -> Any:
        if self.read_fail:
            raise self.api._UpstreamError("read failed", code="upstream_error",
                                          detail="GET reactions -> HTTP 500", endpoint=endpoint)
        return self.api._Response(200, [dict(row) for row in _REACTION_STORE], {})


def main() -> int:
    if SCRATCH.resolve() == (Path.home() / ".hermes").resolve():
        sys.exit("refusing to build fixtures in ~/.hermes")
    started = time.monotonic()
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    folder = SCRATCH / "repo"
    folder.mkdir(parents=True)

    api = load_api()

    section("the aggregation names canonical order, counts and mine")
    rows = api._reaction_count(
        [{"content": "eyes", "user": {"login": "bob"}},
         {"content": "heart", "user": {"login": "me"}},
         {"content": "heart", "user": {"login": "bob"}},
         {"content": "mindblown", "user": {"login": "bob"}}],
        "me")
    check("hearts before eyes is canonical, not insertion order",
          [row["content"] for row in rows] == ["heart", "eyes"], rows)
    check("counts add up", [row["count"] for row in rows] == [2, 1], rows)
    check("mine only on my own heart", rows[0]["mine"] is True and rows[1]["mine"] is False, rows)
    check("an unknown content is dropped", all(row["content"] != "mindblown" for row in rows))
    check("no account means nothing is mine",
          all(row["mine"] is False for row in api._reaction_count(
              [{"content": "heart", "user": {"login": "me"}}], None)))

    section("an issue detail carries reactions on the body and every comment")
    payload = detail(api, folder, "issue", 1)
    check("the read is ok", payload.get("ok") is True, payload.get("code"))
    check("body reactions ride the issues endpoint",
          "GET /repos/o/r/issues/1/reactions" in payload["_calls"], payload["_calls"])
    check("body reactions aggregate", payload.get("reactions") == [
        {"content": "+1", "count": 1, "mine": False},
        {"content": "heart", "count": 3, "mine": True}], payload.get("reactions"))
    comments = {row["id"]: row for row in payload.get("comments") or []}
    check("comment 11 carries its rows",
          comments.get(11, {}).get("reactions") == [
              {"content": "+1", "count": 1, "mine": True},
              {"content": "eyes", "count": 2, "mine": False}], comments.get(11, {}).get("reactions"))
    check("a failed comment read is [] and the detail still answers",
          comments.get(12, {}).get("reactions") == [] and payload.get("ok") is True,
          comments.get(12, {}).get("reactions"))
    check("every comment has the key", all("reactions" in row for row in comments.values()))

    section("a failed body read degrades to [] without failing the detail")
    failed = detail(api, folder, "issue", 1, fail=("/repos/o/r/issues/1/reactions",))
    check("the detail still answers", failed.get("ok") is True, failed.get("code"))
    check("the body row is empty", failed.get("reactions") == [], failed.get("reactions"))
    check("the comments still carry theirs",
          any(row.get("reactions") for row in failed.get("comments") or []))

    section("a PR body reads the same issues reactions endpoint")
    pr = detail(api, folder, "pr", 2)
    check("PR detail ok", pr.get("ok") is True, pr.get("code"))
    check("body reactions came from /issues/2/reactions",
          "GET /repos/o/r/issues/2/reactions" in pr["_calls"], pr["_calls"])
    check("PR body reactions aggregate", pr.get("reactions") == [
        {"content": "+1", "count": 1, "mine": False},
        {"content": "heart", "count": 3, "mine": True}], pr.get("reactions"))

    section("one click toggles: my own reaction is removed with its id")
    store = [{"id": 900, "content": "heart", "user": {"login": "me"}},
             {"id": 901, "content": "heart", "user": {"login": "bob"}}]
    answer, log = react(api, {"kind": "issue", "number": 3, "content": "heart"}, store=store)
    check("ok", answer.get("ok") is True, answer)
    check("DELETE on the reaction id",
          log.calls == [("DELETE", "/repos/o/r/issues/3/reactions/900", None)], log.calls)
    check("the answer is the remaining row",
          answer.get("reactions") == [{"content": "heart", "count": 1, "mine": False}],
          answer.get("reactions"))
    check("toggled says removed", answer.get("toggled") == "removed")

    section("a missing reaction is added as the account")
    answer, log = react(api, {"kind": "pr", "number": 4, "content": "rocket"}, store=[])
    check("ok", answer.get("ok") is True, answer)
    check("POST with the content in the body",
          log.calls == [("POST", "/repos/o/r/issues/4/reactions", {"content": "rocket"})], log.calls)
    check("the answer carries the new mine row",
          answer.get("reactions") == [{"content": "rocket", "count": 1, "mine": True}],
          answer.get("reactions"))

    section("comment and review comment kinds use their own collections")
    answer, log = react(api, {"kind": "issue_comment", "comment_id": 55, "content": "eyes"},
                        store=[])
    check("issue comment ok", answer.get("ok") is True, answer)
    check("issue comment URL",
          log.calls == [("POST", "/repos/o/r/issues/comments/55/reactions",
                         {"content": "eyes"})], log.calls)
    answer, log = react(api, {"kind": "review_comment", "comment_id": 77, "content": "+1"},
                        store=[])
    check("review comment ok", answer.get("ok") is True, answer)
    check("review comment URL",
          log.calls == [("POST", "/repos/o/r/pulls/comments/77/reactions",
                         {"content": "+1"})], log.calls)

    section("refusals are HTTP 200 bodies with one stable code")
    answer, log = react(api, {"kind": "discussion", "number": 1, "content": "heart"})
    check("bad_kind for an unknown kind", answer.get("code") == "bad_kind", answer)
    answer, log = react(api, {"kind": "issue", "number": 1, "content": "mindblown"})
    check("bad_content for an unknown content", answer.get("code") == "bad_content", answer)
    answer, log = react(api, {"kind": "issue", "content": "heart"})
    check("bad_kind without a number", answer.get("code") == "bad_kind", answer)
    answer, log = react(api, {"kind": "issue_comment", "content": "heart"})
    check("bad_kind without a comment id", answer.get("code") == "bad_kind", answer)
    answer, log = react(api, {"kind": "issue", "number": 1, "content": "heart"}, gh_user=None)
    check("gh_unavailable without a gh account", answer.get("code") == "gh_unavailable", answer)
    answer, log = react(api, {"kind": "issue", "number": 1, "content": "heart"},
                        repo={"ok": False, "code": "no_remote", "error": "No GitHub remote."})
    check("gh_unavailable when the folder has no remote",
          answer.get("code") == "gh_unavailable", answer)
    answer, log = react(api, {"kind": "issue", "number": 1, "content": "heart"}, write_fail=True)
    check("gh_error when the write refuses", answer.get("code") == "gh_error", answer)
    check("a refusal keeps the empty reactions key", answer.get("reactions") == [], answer)
    answer, log = react(api, {"kind": "issue", "number": 1, "content": "heart"}, read_fail=True)
    check("gh_error when the reactions read fails", answer.get("code") == "gh_error", answer)
    check("a refusal never wrote", log.calls == [], log.calls)

    section("the HTTP contract: one route, 200 refusals, folder in body or query")
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    response = client.post("/github/reaction", json={"kind": "discussion", "content": "heart"})
    check("bad kind is HTTP 200", response.status_code == 200, response.status_code)
    body = response.json() if response.status_code == 200 else {}
    check("bad kind carries its code", body.get("code") == "bad_kind", body)
    response = client.post("/github/reaction", json={"kind": "issue", "number": 1, "content": "mindblown"})
    check("bad content is HTTP 200 with bad_content",
          response.status_code == 200 and response.json().get("code") == "bad_content",
          response.status_code)
    # The folder may ride in the body: a folder that is gone says so as
    # gh_unavailable, and the route still answers 200.
    response = client.post("/github/reaction", json={
        "kind": "issue", "number": 1, "content": "heart", "path": "/no/such/folder"})
    body = response.json() if response.status_code == 200 else {}
    check("a missing folder is a 200 gh_unavailable",
          response.status_code == 200 and body.get("code") == "gh_unavailable", body)

    print(f"\n{len(FAILURES)} failure(s) in {time.monotonic() - started:.1f}s")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
