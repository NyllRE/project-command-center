#!/usr/bin/env python3
"""Avatar field harness for project-command-center.

Plain Python like tests/test_git_routes.py: the plugin is path-loaded, its pure
row builders are driven directly, and the issue detail and comment routes run
against a fake GitHub reader so no request leaves the machine.

    python3 tests/test_avatars.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN = Path(__file__).resolve().parent.parent
SCRATCH = Path(os.environ.get(
    "PCC_AVATAR_SCRATCH", Path.home() / ".hermes" / "cache" / "scratch" / "pcc-avatars"))
FAILURES: list[str] = []
AVATAR = "https://avatars.githubusercontent.com/u/42?v=4"
AVATAR_48 = "https://avatars.githubusercontent.com/u/42?v=4&s=48"


def check(name: str, condition: bool, detail: Any = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}{f' ({detail})' if detail else ''}")
    if not condition:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ".ljust(78, "="))


def load_api() -> Any:
    path = PLUGIN / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("pcc_api_avatar_test", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pcc_api_avatar_test"] = module
    spec.loader.exec_module(module)
    return module


def size_of(url: str) -> Optional[str]:
    return parse_qs(urlsplit(url).query).get("s", [None])[0]


def main() -> int:
    if SCRATCH.resolve() == (Path.home() / ".hermes").resolve():
        sys.exit("refusing to build fixtures in ~/.hermes")
    started = time.monotonic()
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)

    api = load_api()

    section("avatar URL allow-list")
    check("a GitHub avatar is kept", api._avatar_url(AVATAR) == AVATAR_48, api._avatar_url(AVATAR))
    check("the size is rewritten to 48", size_of(api._avatar_url(AVATAR)) == "48")
    check("v is kept", "v=4" in api._avatar_url(AVATAR))
    check("an existing s=200 becomes s=48",
          api._avatar_url("https://avatars.githubusercontent.com/u/7?s=200") ==
          "https://avatars.githubusercontent.com/u/7?s=48")
    check("a fragment is dropped",
          api._avatar_url(AVATAR + "#frag") == AVATAR_48, api._avatar_url(AVATAR + "#frag"))
    graphql_url = ("https://avatars.githubusercontent.com/u/52743522?s=48"
                   "&u=d5a70144dadee379c0df198fa601a6f93e0ec530&v=4")
    check("the GraphQL u parameter is kept",
          api._avatar_url(graphql_url) ==
          "https://avatars.githubusercontent.com/u/52743522?u=d5a70144dadee379c0df198fa601a6f93e0ec530&v=4&s=48",
          api._avatar_url(graphql_url))
    for label, url in (
        ("http", "http://avatars.githubusercontent.com/u/42"),
        ("the website host", "https://github.com/u/42"),
        ("a lookalike host", "https://avatars.githubusercontent.com.evil.test/u/42"),
        ("userinfo", "https://user:pass@avatars.githubusercontent.com/u/42"),
        ("a non-default port", "https://avatars.githubusercontent.com:8443/u/42"),
        ("an invalid port", "https://avatars.githubusercontent.com:bad/u/42"),
        ("a bare host path", "https://avatars.githubusercontent.com/"),
        ("a path that climbs", "https://avatars.githubusercontent.com/u/../../x"),
        ("an unknown query key",
         "https://avatars.githubusercontent.com/u/42?s=48&next=https://evil.test"),
        ("a non-string", 42),
        ("None", None),
    ):
        check(f"rejects {label}", api._avatar_url(url) is None, api._avatar_url(url))

    section("actor shapes")
    rest = {"login": "alice", "avatar_url": AVATAR, "type": "User"}
    check("REST user is not a bot", api._actor(rest) == ("alice", AVATAR_48, False))
    bot = {"login": "dependabot[bot]", "avatar_url": AVATAR, "type": "Bot"}
    check("REST bot flag comes from type", api._actor(bot)[2] is True)
    gql_bot = {"login": "dependabot", "avatarUrl": AVATAR, "__typename": "Bot"}
    check("GraphQL bot has no [bot] suffix but is still a bot",
          api._actor(gql_bot)[2] is True and api._actor(gql_bot)[0] == "dependabot")
    gql_user = {"login": "alice", "avatarUrl": AVATAR, "__typename": "User"}
    check("GraphQL user avatar is read from avatarUrl",
          api._actor(gql_user) == ("alice", AVATAR_48, False))
    check("a ghost has no login, avatar or bot flag", api._actor({}) == (None, None, False))
    check("a null user is safe", api._actor(None) == (None, None, False))
    fields = api._actor_fields({})
    check("a missing avatar omits the key", "author_avatar" not in fields, str(fields))
    check("author_bot is always present", fields.get("author_bot") is False)

    section("issue rows")
    assignee_avatar = "https://avatars.githubusercontent.com/u/7?s=16"
    raw = {
        "number": 3, "title": "t", "state": "open",
        "html_url": "https://github.com/o/r/issues/3",
        "user": {"login": "alice", "avatar_url": AVATAR, "type": "User"},
        "labels": [{"name": "bug"}],
        "assignees": [
            {"login": "bob", "avatar_url": assignee_avatar, "type": "User"},
            {"login": "ghost"},
        ],
        "comments": 1, "updated_at": "2026-01-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
    }
    row = api._issue_row(raw)
    check("issue author avatar", row.get("author_avatar") == AVATAR_48, row.get("author_avatar"))
    check("issue author bot is false", row.get("author_bot") is False)
    check("assignees keep their plain login list", row["assignees"] == ["bob", "ghost"])
    check("assignee avatars hold only the ones with a URL",
          row.get("assignee_avatars") == {
              "bob": "https://avatars.githubusercontent.com/u/7?s=48"},
          str(row.get("assignee_avatars")))

    raw_bot = {**raw, "user": {"login": "dependabot[bot]", "avatar_url": AVATAR, "type": "Bot"},
               "assignees": []}
    bot_row = api._issue_row(raw_bot)
    check("issue bot author", bot_row.get("author_bot") is True)
    check("no assignee avatars key when there are none",
          "assignee_avatars" not in bot_row, str(bot_row))

    raw_ghost = {**raw, "user": None, "assignees": [{"login": "ghost"}]}
    ghost_row = api._issue_row(raw_ghost)
    check("a ghost issue author is null", ghost_row["author"] is None)
    check("a ghost has no avatar key", "author_avatar" not in ghost_row)
    check("an assignee without an avatar is left out",
          "assignee_avatars" not in ghost_row, str(ghost_row))

    section("comment, review and PR rows")
    comments = api._comment_rows([{
        "id": 9, "user": {"login": "alice", "avatar_url": AVATAR, "type": "User"},
        "body": "hi", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": None, "html_url": "https://github.com/o/r/issues/3#c9",
    }, {
        "id": 10, "user": None, "body": "ghost", "created_at": None,
        "updated_at": None, "html_url": None,
    }])
    check("comment carries the avatar", comments[0].get("author_avatar") == AVATAR_48)
    check("comment ghost omits the avatar", "author_avatar" not in comments[1])
    check("comment ghost still has the bot flag", comments[1].get("author_bot") is False)

    reviews = api._review_rows([{
        "user": {"login": "review-bot", "avatar_url": AVATAR, "type": "Bot"},
        "state": "APPROVED", "body": "lgtm", "submitted_at": "2026-01-01T00:00:00Z",
    }])
    check("review bot avatar", reviews[0].get("author_avatar") == AVATAR_48)
    check("review bot flag", reviews[0].get("author_bot") is True)

    pull = api._pull_row({
        "number": 4, "title": "pr", "state": "open", "draft": False,
        "merged_at": None, "html_url": "https://github.com/o/r/pull/4",
        "user": {"login": "alice", "avatar_url": AVATAR, "type": "User"},
        "head": {"ref": "feature"}, "base": {"ref": "main"},
        "updated_at": None, "created_at": None,
    }, None)
    check("PR row carries the avatar", pull.get("author_avatar") == AVATAR_48)
    check("PR row carries the bot flag", pull.get("author_bot") is False)

    graphql_field = api._REF_FIELDS
    check("ref query asks for __typename", "__typename login avatarUrl(size: 48)" in graphql_field,
          graphql_field.strip().splitlines()[1][:80])

    section("issue detail route with a fake reader")
    folder = SCRATCH / "repo"
    (folder / "public").mkdir(parents=True)
    original_ctx, original_auth, original_reader = api._repo_context, api._auth, api._GhReader
    api._repo_context = lambda path: _fake_ctx(api, path)
    api._auth = _fake_auth
    api._GhReader = lambda owner, name, mode, token: _FakeGh(api, owner, name)
    api._CACHE.clear()
    try:
        detail = asyncio.run(api.github_issue(1, str(folder), 0))
    finally:
        api._repo_context, api._auth, api._GhReader = original_ctx, original_auth, original_reader
    check("issue detail is ok", detail.get("ok") is True, detail.get("code"))
    check("issue detail author avatar", detail.get("author_avatar") == AVATAR_48)
    check("issue detail bot flag", detail.get("author_bot") is False)
    check("issue detail assignee avatars",
          detail.get("assignee_avatars") == {"bob": AVATAR_48},
          str(detail.get("assignee_avatars")))
    check("issue detail comment avatar",
          detail["comments"][0].get("author_avatar") == AVATAR_48)
    check("issue detail comment bot flag",
          detail["comments"][0].get("author_bot") is False)

    section("comment POST response with fake writes")
    originals = (api._write_repo, api._gh_user, api._confirm_kind, api._gh_write)
    api._write_repo = _fake_repo
    api._gh_user = _fake_user
    api._confirm_kind = _fake_confirm
    api._gh_write = lambda method, path, body, **kwargs: _fake_write(api)
    try:
        posted = asyncio.run(api._post_comment(1, str(folder), {"body": "hello"}, "issue"))
    finally:
        (api._write_repo, api._gh_user, api._confirm_kind, api._gh_write) = originals
    check("comment POST is ok", posted.get("ok") is True, posted.get("code"))
    comment = posted.get("comment") or {}
    check("comment POST carries the avatar", comment.get("author_avatar") == AVATAR_48,
          str(comment))
    check("comment POST carries the bot flag", comment.get("author_bot") is False)

    section("router still answers")
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    check("health is still 200", client.get("/health").status_code == 200)

    print(f"\n{len(FAILURES)} failure(s) in {time.monotonic() - started:.1f}s")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    return 0


async def _fake_ctx(api: Any, path: Path) -> Any:
    return api._RepoContext("o", "r", str(path), "ok", None, None)


async def _fake_auth() -> tuple[str, None]:
    return "gh", None


class _FakeGh:
    """Minimal reader: the issue, an empty timeline, one comment."""

    def __init__(self, api: Any, owner: str, name: str) -> None:
        self.api = api
        self.auth = "gh"
        self.auth_note = None
        self.credential = "gh"

    async def rest(self, endpoint: str, params: Any = None) -> Any:
        if endpoint.endswith("/timeline"):
            return self.api._Response(200, [], {})
        if endpoint.endswith("/comments"):
            return self.api._Response(200, [{
                "id": 9, "user": {"login": "alice", "avatar_url": AVATAR, "type": "User"},
                "body": "hi", "created_at": "2026-01-01T00:00:00Z",
                "updated_at": None, "html_url": None,
            }], {})
        return self.api._Response(200, {
            "number": 1, "title": "issue", "state": "open",
            "html_url": "https://github.com/o/r/issues/1",
            "user": {"login": "alice", "avatar_url": AVATAR, "type": "User"},
            "labels": [], "comments": 1,
            "assignees": [{"login": "bob", "avatar_url": AVATAR, "type": "User"}],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z", "body": "body",
        }, {})


async def _fake_repo(path: str) -> tuple[str, str]:
    return "o", "r"


async def _fake_user() -> str:
    return "alice"


async def _fake_confirm(owner: str, name: str, number: int, kind: str) -> None:
    return None


async def _fake_write(api: Any) -> Any:
    return {"id": 11, "user": {"login": "alice", "avatar_url": AVATAR, "type": "User"},
            "body": "hello", "created_at": "2026-01-01T00:00:00Z"}


if __name__ == "__main__":
    sys.exit(main())
