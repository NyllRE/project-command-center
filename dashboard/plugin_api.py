"""Project Command Center backend: git and GitHub reads per project folder, plus the board store.

The gateway mounts this router at ``/api/plugins/project-command-center/`` because
``dashboard/manifest.json`` names this file, so ``desktop/plugin.js`` reaches it
with ``ctx.rest('/repo?path=...')``.

  * git: work tree, remote, branch, dirty counts, ahead/behind and branches for
    one folder, cached 20 s per resolved root (a board asks for many projects).
  * git history: ``GET /git/graph`` returns the newest commits across local
    branches, remotes and HEAD for the graph tab, cached 300 s under a
    fingerprint of the refs and HEAD, so a re-opened tab pays three cheap reads
    and no log; ``GET /git/commit/{sha}`` returns one commit with a unified diff
    against its first parent, capped while it is read. Fixed argv, no shell, and
    no git command mutates the repository.
  * GitHub: issues and pull requests for that folder's ``origin``, with PR to
    issue links taken from GraphQL ``closingIssuesReferences`` (GitHub's own
    answer) merged with the ``closes #12`` body fallback. One issue or PR
    resolves to a full detail payload (body, first 100 comments oldest first,
    submitted reviews, checks, changed files), and ``GET /github/refs`` resolves
    a list of ``#numbers`` in one batched GraphQL call for the chip row.
  * board: the server side state of the board itself, in
    ``<hermes home>/plugin-data/project-command-center/board.db`` through
    ``board_store.py`` (statuses, session links, the project ids the board knows).
    ``GET /board`` reads it; ``POST /board/status``, ``/board/link``,
    ``/board/projects`` and ``/board/seed`` write it, each inside one short SQLite
    transaction, and each broadcast ``board.changed`` so the Desktop refreshes.
  * icons: ``GET /icon`` answers "what is the best icon for this folder" from a
    fixed candidate path list first, then one bounded walk for a nested
    ``public`` directory, with the bytes sniffed, then a remote favicon or the
    GitHub owner avatar behind an SSRF guard and a kill file, then a stack key
    (``laravel``, ``nuxt``, ``python``, ...) for the Desktop's glyph.
    It always answers 200: an image, a glyph, or a code for a folder that is gone.

Git is read only except one route: ``POST /branch/switch`` runs ``git switch``
on the main checkout after refusing a dirty tree, a linked worktree, a branch
checked out elsewhere and any in-progress operation. It never stashes, never
discards and never forces, and every git child is a fixed argv.
GitHub writes are four routes and nothing else: ``POST /github/issue/{n}/comment`` and
``POST /github/pr/{n}/comment`` post one comment, and
``POST /github/issue/{n}/state`` / ``POST /github/pr/{n}/state`` set open or
closed. All four write over the authenticated ``gh`` CLI as that account (never
the token), with a JSON body on the child's stdin so no comment text is ever
quoted into a command line.
No shell: every child is ``create_subprocess_exec`` with an argv and a
timeout; no ``shell=True``, no ``os.system``, no command string. No credential
files: ``GITHUB_TOKEN`` comes from ``agent.secret_scope.get_secret`` inside the
handler (catalog rule 11); with no token the user's own authenticated ``gh`` CLI
is used, and with neither, public reads go unauthenticated while a private read
reports ``auth: "none"``. A token
that GitHub rejects is retried once through ``gh``, and ``auth``/``auth_note``
report which credential answered and why it changed; the token value itself is
never logged, returned or compared against a file.

Failures are codes, not exceptions: every repo, git history, GitHub, PR and
issue payload carries ``ok``, ``code`` (``folder_missing``, ``not_a_repo``,
``no_remote``, ``repo_not_found``, ``no_auth``, ``rate_limited``, ``network``,
``upstream_error``, ``empty_repo``, ``bad_sha``, or ``ok``), ``error`` (one
human sentence) and ``detail``
(the endpoint and status), all with HTTP 200, so the Desktop half renders one
quiet line instead of a bare status. Only a malformed path is a 400.
``GET /diagnose`` reports folder, git, token presence, gh login and a labelled
probe per credential, which is how "why is my repo not loading" gets answered
from inside the gateway.

Caching is one in process dict keyed by (owner, repo, endpoint, params) with a
60 s TTL; with a token the board read revalidates with the stored ETag, and
``refresh=1`` bypasses it. Icon results use their own keys: a local image 300 s
(folder, file path, mtime), a remote image 3600 s, a glyph 300 s.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple, NotRequired, Optional, Sequence, TypedDict, Union
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Body, HTTPException, Query

log = logging.getLogger(__name__)

router = APIRouter()

PLUGIN_NAME = "project-command-center"
PLUGIN_VERSION = "0.4.1"
GIT_TIMEOUT = 5.0
# A checkout is the one git call that can legitimately take minutes: a large tree,
# an LFS smudge, a slow disk. Killing it mid-run leaves a half-moved work tree and
# a stale index.lock, so the switch gets its own budget instead of GIT_TIMEOUT.
SWITCH_TIMEOUT = 120.0
# `git worktree add` copies a checkout the same way `switch` moves one, so it gets the
# same budget for the same reason: a killed add leaves a half-populated directory behind.
WORKTREE_TIMEOUT = 120.0
GH_TIMEOUT = 15.0  # the GraphQL PR pass is the slow gh call; everything else is under a second
HTTP_TIMEOUT = 10.0
REPO_TTL = 20.0
GITHUB_TTL = 60.0
MAX_CACHED_ENTRIES = 256
MAX_CONCURRENT_GITHUB_CALLS = 2
# GitHub resolves one aliased pullRequest(number:) in roughly 0.6 s on a busy
# repository (checks rollup and mergeability are computed per PR), so the board
# enriches only the most recently updated window that fits one 10 s call. Rows
# past it carry REST fields and enriched: false.
MAX_PRS_ENRICHED = 10
MAX_BRANCHES = 40
MAX_COMMENTS = 100
MAX_REFS = 50
MAX_COUNT_PATHS = 50
API = "https://api.github.com"

ISOLATION_NOTE = (
    "git runs as a local subprocess inside the gateway process (no shell, 5 s for a status "
    "read and 15 s for a commit diff), "
    "only inside the absolute folder passed as path. It reads history and diffs for the graph "
    "routes; a diff runs with --no-ext-diff --no-textconv --no-show-signature, because a "
    "repository's own config can name a diff driver, a textconv command or a gpg program, and is "
    "cut at 512 KiB, 6,000 lines and 300 files. GitHub reads go to api.github.com using the "
    "profile's GITHUB_TOKEN, or through your authenticated gh CLI when no token is set. GitHub "
    "writes (a comment on an issue or pull request, and opening or closing one) run only through "
    "your authenticated gh CLI, as that account, with the request body on the child's stdin. The "
    "/icon route reads icon files inside that folder and, unless "
    "<hermes home>/plugin-data/project-command-center/icons_remote.off exists, may fetch one "
    "favicon from the homepage host the project declares or the GitHub owner avatar, https only "
    "and never a private address. No "
    "file contents are returned and no credential file is opened. The other write surface is the "
    "board store: statuses, session links and project ids in a local SQLite file under "
    "<hermes home>/plugin-data/project-command-center/board.db."
)


# --- Response types ---------------------------------------------------------

class Dirty(TypedDict):
    count: int
    staged: int
    unstaged: int
    untracked: int


class BranchRow(TypedDict):
    name: str
    upstream: Optional[str]
    ahead: int
    behind: int
    last_commit_at: Optional[str]
    subject: str


class IssueRow(TypedDict):
    number: int
    title: str
    state: str
    state_reason: Optional[str]
    url: Optional[str]
    author: Optional[str]
    author_avatar: NotRequired[str]
    author_bot: bool
    labels: list[str]
    assignees: list[str]
    assignee_avatars: NotRequired[dict[str, str]]
    comments: int
    updated_at: Optional[str]
    created_at: Optional[str]


class Checks(TypedDict):
    state: str  # success | failure | pending | none
    total: int
    failing: int


class PullRow(TypedDict):
    number: int
    title: str
    state: str
    draft: bool
    merged: bool
    url: Optional[str]
    author: Optional[str]
    author_avatar: NotRequired[str]
    author_bot: bool
    head_ref: str
    base_ref: str
    updated_at: Optional[str]
    created_at: Optional[str]
    additions: int
    deletions: int
    changed_files: int
    review_decision: Optional[str]
    checks: Checks
    linked_issues: list[int]
    mergeable_state: str
    enriched: bool


class CommentRow(TypedDict):
    """One issue or PR comment, as the modal renders it."""

    id: int
    author: Optional[str]
    author_avatar: NotRequired[str]
    author_bot: bool
    body: str
    created_at: Optional[str]
    updated_at: Optional[str]
    url: Optional[str]


class ReviewRow(TypedDict):
    author: Optional[str]
    author_avatar: NotRequired[str]
    author_bot: bool
    state: Optional[str]
    body: str
    submitted_at: Optional[str]


class RefRow(TypedDict):
    """One resolved ``#number`` chip: which kind of thing it is and its state."""

    number: int
    kind: str  # "issue" | "pr"
    title: str
    state: str
    state_reason: NotRequired[Optional[str]]
    draft: NotRequired[bool]
    merged: NotRequired[bool]
    author: Optional[str]
    author_avatar: NotRequired[str]
    author_bot: bool
    updated_at: Optional[str]
    url: Optional[str]


class Counts(TypedDict):
    issues_open: int
    prs_open: int
    drafts: int
    review_ready: int
    changes_requested: int
    approved: int
    checks_failing: int


# --- Small utilities --------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# GitHub serves avatars from this one host. A payload keeps a URL only when it
# is https, exactly that host, and a query made of ``s``, ``v`` and ``u``: the
# renderer loads it as an image, so anything else is a URL someone else chose.
AVATAR_HOST = "avatars.githubusercontent.com"
AVATAR_SIZE = 48
# ``s`` and ``v`` come from REST; GraphQL adds ``u``, the user id hash. Anything
# else is someone else's parameter.
_AVATAR_QUERY_KEYS = frozenset({"s", "v", "u"})


def _avatar_url(value: Any) -> Optional[str]:
    """A normalized GitHub avatar URL, or None for anything off the allow-list.

    The size is always rewritten to ``s=48`` (``v`` and the GraphQL ``u`` are
    kept), so one user is one browser cache entry. A foreign host, http,
    userinfo, a path that climbs, or an unknown query parameter is dropped
    instead of passed through.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname != AVATAR_HOST:
        return None
    if port not in (None, 443):
        return None
    if parts.username or parts.password or not parts.path.startswith("/"):
        return None
    if parts.path in ("", "/") or ".." in parts.path:
        return None
    kept: list[tuple[str, str]] = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        if key not in _AVATAR_QUERY_KEYS:
            return None
        if key != "s":
            kept.append((key, val))
    kept.append(("s", str(AVATAR_SIZE)))
    return urlunsplit(("https", AVATAR_HOST, parts.path, urlencode(kept), ""))


def _actor(user: Any) -> tuple[Optional[str], Optional[str], bool]:
    """(login, avatar, bot) from a REST user or a GraphQL author node.

    REST says ``avatar_url`` and ``type: "Bot"``; GraphQL says ``avatarUrl`` and
    ``__typename: "Bot"``. A GraphQL bot login has no ``[bot]`` suffix, so the
    flag has to come from this field and not from sniffing the login.
    """
    if not isinstance(user, dict):
        return None, None, False
    login = user.get("login")
    login = login if isinstance(login, str) and login else None
    avatar = _avatar_url(user.get("avatar_url") or user.get("avatarUrl"))
    bot = str(user.get("type") or user.get("__typename") or "") == "Bot"
    return login, avatar, bot


def _actor_fields(user: Any) -> dict[str, Any]:
    """The author triple for a payload; ``author_avatar`` is absent, not null."""
    login, avatar, bot = _actor(user)
    fields: dict[str, Any] = {"author": login, "author_bot": bot}
    if avatar:
        fields["author_avatar"] = avatar
    return fields


def _assignee_avatars(rows: Any) -> dict[str, str]:
    """``{login: avatar}`` for the assignees that have an allow-listed avatar."""
    out: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        login, avatar, _bot = _actor(row)
        if login and avatar:
            out[login] = avatar
    return out


def _names(rows: Any, key: str) -> list[str]:
    return [str(row[key]) for row in rows or [] if isinstance(row, dict) and row.get(key)]


def _uniq(numbers: Sequence[Any]) -> list[int]:
    out = {_int(n) for n in numbers if isinstance(n, (int, str)) and str(n).strip().isdigit()}
    out.discard(0)
    return sorted(out)


_CHILD_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_CONFIG_HOME",
                   "SSH_AUTH_SOCK", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")


def _child_env() -> dict[str, str]:
    """A child environment by allowlist: locale, PATH, HOME, git's config paths.

    ``gh`` finds its login under ``$HOME/.config/gh`` because HOME is passed, and
    no credential variable (``GITHUB_TOKEN``, ``GH_TOKEN``, ``*_KEY``) reaches a
    child process, so its auth is the CLI's own store, never ours.
    """
    return {key: os.environ[key] for key in _CHILD_ENV_KEYS if key in os.environ}


class _GitError(RuntimeError):
    pass


# The codes the Desktop half maps to one quiet line. "ok" is the success code,
# so every GitHub or repo payload carries the same four fields.
FAILURE_CODES = frozenset({"folder_missing", "not_a_repo", "no_remote", "repo_not_found",
                           "no_auth", "rate_limited", "network", "upstream_error",
                           "empty_repo", "bad_sha"})

# The codes a write route answers with. A write names its own failure in one
# sentence and never raises: shell level exit codes and HTTP statuses both become
# one of these, and an unknown code is upstream_error rather than a class name.
WRITE_CODES = frozenset({"empty_comment", "bad_state", "not_found", "forbidden",
                         "rate_limited", "upstream_error"})


class _FolderMissing(Exception):
    """An absolute, well formed path that names no existing directory."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


class _UpstreamError(RuntimeError):
    """A GitHub read failed. Always HTTP 200 with a code, never a 500.

    ``code`` is one of FAILURE_CODES, ``detail`` carries the endpoint and status
    (never a credential), and ``rejected`` marks a credential the server refused
    (401, 403 that is not a rate limit, 404): the one class that earns a retry
    through the gh CLI.
    """

    def __init__(self, message: str, *, code: str = "upstream_error", detail: Optional[str] = None,
                 status: Optional[int] = None, endpoint: Optional[str] = None,
                 credential: Optional[str] = None, rejected: bool = False) -> None:
        super().__init__(message)
        self.code = code if code in FAILURE_CODES else "upstream_error"
        self.detail = detail
        self.status = status
        self.endpoint = endpoint
        self.credential = credential
        self.rejected = rejected


def _classify(status: int, headers: Mapping[str, str], endpoint: str, message: str,
              credential: Optional[str]) -> _UpstreamError:
    """Turn one HTTP status into a code, a detail line and a rejection flag."""
    remaining = headers.get("x-ratelimit-remaining")
    limited = status == 429 or (status == 403 and remaining is not None and _int(remaining) == 0)
    if limited:
        code = "rate_limited"
    elif status in (401, 403):
        code = "no_auth"
    elif status == 404:
        # Unauthenticated, a 404 is usually a private repository, not a deleted one.
        code = "repo_not_found" if credential in ("token", "gh") else "no_auth"
    else:
        code = "upstream_error"
    return _UpstreamError(
        f"GitHub {endpoint}: HTTP {status} {message}".strip(),
        code=code, detail=f"GET {endpoint} -> HTTP {status}", status=status,
        endpoint=endpoint, credential=credential,
        rejected=credential == "token" and not limited and status in (401, 403, 404),
    )


class _Response(NamedTuple):
    status: int
    data: Any
    headers: dict[str, str]


async def _run(argv: Sequence[str], *, timeout: float,
               stdin_data: Optional[str] = None) -> tuple[int, str, str]:
    """Run argv with no shell. Raises TimeoutError after *timeout* seconds.

    ``stdin_data`` is the only way a request body reaches a child: it is written
    to the pipe, never spliced into an argument, so quotes and newlines in a
    comment cannot change what the child runs.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=_child_env(),
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin_data.encode("utf-8") if stdin_data is not None else None), timeout)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"{Path(argv[0]).name} timed out after {timeout:.0f}s") from exc
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _resolve_folder(path: str) -> Path:
    """An absolute existing directory, resolved.

    Only a malformed request is a 400. A path that is absolute but names nothing
    on disk raises ``_FolderMissing`` so the caller can answer ``folder_missing``
    with HTTP 200 and let the UI say which folder is gone.
    """
    if not path or not str(path).strip():
        raise HTTPException(status_code=400, detail="path is required (an absolute folder)")
    folder = Path(path)
    if not folder.is_absolute():
        raise HTTPException(status_code=400, detail="path must be absolute")
    try:
        resolved = folder.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise _FolderMissing(str(folder)) from None
    if not resolved.is_dir():
        raise _FolderMissing(str(folder))
    return resolved


# --- Cache ------------------------------------------------------------------

class _Entry(NamedTuple):
    expires: float
    etag: Optional[str]
    payload: dict[str, Any]


_CACHE: dict[tuple[Any, ...], _Entry] = {}


def _cache_put(key: tuple[Any, ...], payload: dict[str, Any], *, etag: Optional[str] = None,
               ttl: float = GITHUB_TTL) -> None:
    if len(_CACHE) >= MAX_CACHED_ENTRIES:
        now = time.monotonic()
        for expired in [k for k, entry in _CACHE.items() if entry.expires < now]:
            _CACHE.pop(expired, None)
        if len(_CACHE) >= MAX_CACHED_ENTRIES:
            _CACHE.clear()
    _CACHE[key] = _Entry(time.monotonic() + ttl, etag, payload)


def _cache_hit(key: tuple[Any, ...]) -> Optional[dict[str, Any]]:
    """The cached payload with ``cached: True``, or None once the TTL has passed."""
    entry = _CACHE.get(key)
    if entry is None or entry.expires < time.monotonic():
        return None
    return {**entry.payload, "cached": True}


# --- git --------------------------------------------------------------------

async def _git(root: str, *args: str, timeout: float = GIT_TIMEOUT) -> str:
    code, out, err = await _run(["git", "-C", root, *args], timeout=timeout)
    if code != 0:
        raise _GitError(err.strip() or f"git {args[0] if args else ''} exited {code}")
    return out


async def _work_tree(folder: Path) -> Optional[str]:
    """The top level of the work tree containing *folder*, or None outside a repo."""
    try:
        out = await _git(str(folder), "rev-parse", "--show-toplevel")
    except _GitError:
        return None
    return out.strip() or None


async def _primary_remote(root: str) -> str:
    names = (await _git(root, "remote")).split()
    if not names:
        return ""
    return (await _git(root, "remote", "get-url", "origin" if "origin" in names else names[0])).strip()


_GH_REMOTE_PATTERNS = (
    re.compile(r"^(?:ssh://)?git@github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/?#]+?)(?:\.git)?/?$", re.I),
    re.compile(r"^(?:git\+)?https?://(?:[^@/]+@)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/?#]+?)(?:\.git)?/?$", re.I),
)


def _parse_remote(url: str) -> tuple[Optional[str], Optional[str]]:
    for pattern in _GH_REMOTE_PATTERNS:
        match = pattern.match((url or "").strip())
        if match:
            return match.group("owner"), match.group("repo")
    return None, None


def _dirty_from_status(porcelain: str) -> Dirty:
    staged = unstaged = untracked = paths = 0
    for line in porcelain.splitlines():
        if len(line) < 2:
            continue
        code, paths = line[:2], paths + 1
        if code == "??":
            untracked += 1
            continue
        staged += code[0] not in (" ", "!")
        unstaged += code[1] not in (" ", "!")
    return {"count": paths, "staged": staged, "unstaged": unstaged, "untracked": untracked}


_REF_FORMAT = "\x1f".join(("%(refname:short)", "%(upstream:short)", "%(upstream:track)",
                           "%(committerdate:iso-strict)", "%(contents:subject)"))


def _track(track: str) -> tuple[int, int]:
    ahead = re.search(r"ahead (\d+)", track or "")
    behind = re.search(r"behind (\d+)", track or "")
    return (int(ahead.group(1)) if ahead else 0, int(behind.group(1)) if behind else 0)


def _branch_rows(refs: str) -> list[BranchRow]:
    rows: list[BranchRow] = []
    for line in refs.splitlines()[:MAX_BRANCHES]:
        parts = line.split("\x1f")
        if len(parts) != 5 or not parts[0]:
            continue
        ahead, behind = _track(parts[2])
        rows.append({"name": parts[0], "upstream": parts[1] or None, "ahead": ahead,
                     "behind": behind, "last_commit_at": parts[3] or None, "subject": parts[4]})
    return rows


async def _ahead_behind(root: str) -> tuple[int, int]:
    try:
        out = await _git(root, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    except (TimeoutError, _GitError):
        return 0, 0
    parts = out.split()
    return (_int(parts[0]), _int(parts[1])) if len(parts) == 2 else (0, 0)


async def _repo_payload(folder: Path, root: Optional[str], error: Optional[str]) -> dict[str, Any]:
    inside_root = root if root and os.path.realpath(root) != str(folder) else None
    payload: dict[str, Any] = {
        "ok": True, "code": "ok", "error": None, "detail": None, "warning": None,
        "path": str(folder), "root": root, "root_matches_path": bool(root) and inside_root is None,
        "inside_root": inside_root,
        "remote": None, "owner": None, "repo": None,
        "branch": None, "detached": False,
        "dirty": {"count": 0, "staged": 0, "unstaged": 0, "untracked": 0},
        "ahead": 0, "behind": 0, "branches": [],
    }
    if not root:
        if error:
            payload.update(ok=False, code="upstream_error",
                           error=f"git could not resolve the work tree for {folder}: {error}",
                           detail="git rev-parse --show-toplevel")
        else:
            payload.update(ok=False, code="not_a_repo",
                           error=f"{folder} is not inside a git work tree.",
                           detail=f"git -C {folder} rev-parse --show-toplevel")
        return payload

    try:
        remote_url = await _primary_remote(root)
    except (TimeoutError, _GitError) as exc:
        remote_url = ""
        payload["warning"] = f"cannot read the remote: {exc}"
    if remote_url:
        payload["remote"] = remote_url
        payload["owner"], payload["repo"] = _parse_remote(remote_url)

    branch_raw, status_raw, refs_raw, ahead_behind = await asyncio.gather(
        _git(root, "symbolic-ref", "--quiet", "--short", "HEAD"),
        _git(root, "status", "--porcelain"),
        _git(root, "for-each-ref", "--sort=-committerdate", f"--format={_REF_FORMAT}", "refs/heads"),
        _ahead_behind(root),
        return_exceptions=True,
    )

    if isinstance(branch_raw, str) and branch_raw.strip():
        payload["branch"] = branch_raw.strip()
    elif isinstance(branch_raw, BaseException):
        # No symbolic ref: a detached HEAD, or a repository with no commits yet.
        code, sha_out, _ = await _run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                                      timeout=GIT_TIMEOUT)
        if code == 0 and sha_out.strip():
            payload["detached"] = True
            payload["branch"] = sha_out.strip()
        else:
            payload["warning"] = "the repository has no commits yet"

    if isinstance(status_raw, str):
        payload["dirty"] = _dirty_from_status(status_raw)
    elif isinstance(status_raw, BaseException):
        payload["warning"] = payload["warning"] or f"cannot read git status: {status_raw}"

    if isinstance(refs_raw, str):
        payload["branches"] = _branch_rows(refs_raw)
    if isinstance(ahead_behind, tuple):
        payload["ahead"], payload["behind"] = ahead_behind
    return payload


# --- git history: graph and commit diff -------------------------------------

# A graph payload carries up to 500 commits, so it lives in its own small LRU
# instead of _CACHE: one 500 row window would otherwise flush the 256 GitHub
# entries the board reads on every refresh. Diffs are immutable but large, so
# they get a third, smaller cache with a longer TTL.
GRAPH_TTL = 300.0
COMMIT_TTL = 600.0
GRAPH_MIN_LIMIT = 50
GRAPH_MAX_LIMIT = 500
GRAPH_TIMEOUT = 10.0
COMMIT_TIMEOUT = 15.0
DIFF_MAX_BYTES = 512 * 1024
DIFF_MAX_LINES = 6000
DIFF_MAX_FILES = 300
MAX_GRAPH_ENTRIES = 32
MAX_DIFF_ENTRIES = 24

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")

_LOG_FORMAT = "%H%x1f%P%x1f%aN%x1f%aE%x1f%aI%x1f%cI%x1f%s"
_GRAPH_REF_FORMAT = "\x1f".join((
    "%(refname)", "%(objectname)", "%(*objectname)", "%(upstream:short)",
    "%(upstream:track,nobracket)", "%(HEAD)", "%(symref)",
))

_GRAPH_CACHE: "OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]]" = OrderedDict()
_DIFF_CACHE: "OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]]" = OrderedDict()
_GRAPH_LOCKS: dict[tuple[str, int], tuple[Any, asyncio.Lock]] = {}


def _lru_get(cache: Any, key: tuple[Any, ...]) -> Optional[dict[str, Any]]:
    """The cached payload with ``cached: True``, or None once its TTL passed."""
    entry = cache.get(key)
    if entry is None:
        return None
    expires, payload = entry
    if expires < time.monotonic():
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return {**payload, "cached": True}


def _lru_put(cache: Any, key: tuple[Any, ...], payload: dict[str, Any], *,
             ttl: float, size: int) -> None:
    cache[key] = (time.monotonic() + ttl, payload)
    cache.move_to_end(key)
    while len(cache) > size:
        cache.popitem(last=False)


def _graph_lock(key: tuple[str, int]) -> asyncio.Lock:
    """One lock per (root, limit) on the running loop, so two misses read once."""
    loop = asyncio.get_running_loop()
    entry = _GRAPH_LOCKS.get(key)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _GRAPH_LOCKS[key] = entry
    return entry[1]


async def _run_capped(argv: Sequence[str], *, max_bytes: int,
                      timeout: float) -> tuple[int, str, bool]:
    """Run argv with no shell, stop reading at *max_bytes* and kill the child.

    ``truncated`` says the output was cut, which also means the exit code is the
    kill signal and not a git answer. A 2 MB patch therefore costs 512 KiB and
    the route does not wait for the rest of a file nobody will render.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=_child_env(),
    )
    chunks: list[bytes] = []
    total = 0
    truncated = False

    def stop() -> None:
        try:
            if proc.returncode is None:
                proc.kill()
        except ProcessLookupError:
            pass

    async def read_stdout() -> None:
        nonlocal total, truncated
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return
            room = max_bytes - total
            if len(chunk) > room:
                if room > 0:
                    chunks.append(chunk[:room])
                    total += room
                truncated = True
                stop()
                return
            chunks.append(chunk)
            total += len(chunk)

    async def read_stderr() -> None:
        assert proc.stderr is not None
        while await proc.stderr.read(8192):
            pass

    try:
        await asyncio.wait_for(asyncio.gather(read_stdout(), read_stderr()), timeout)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        stop()
        await proc.wait()
        raise TimeoutError(f"{Path(argv[0]).name} timed out after {timeout:.0f}s") from exc
    await proc.wait()
    return proc.returncode, b"".join(chunks).decode("utf-8", "replace"), truncated


async def _history_root(folder: Path) -> tuple[Optional[str], str, Optional[str], Optional[str]]:
    """The work tree top level for a folder, or the code that explains why not.

    ``_work_tree`` treats every git failure as "not a repo"; this one keeps git's
    sentence, so "dubious ownership" is not reported as a folder outside git.
    """
    code, out, err = await _run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
                                timeout=GIT_TIMEOUT)
    if code == 0 and out.strip():
        return out.strip(), "ok", None, None
    text = err.strip()
    if "not a git repository" in text.lower():
        return (None, "not_a_repo", f"{folder} is not inside a git work tree.",
                f"git -C {folder} rev-parse --show-toplevel")
    return (None, "upstream_error", f"Git could not read {folder}.",
            text or f"git rev-parse exited {code}")


async def _graph_probe(root: str) -> tuple[Optional[str], Optional[str], str, str]:
    """Three cheap reads every call: (head sha, branch ref, fingerprint, refs).

    Refs and HEAD are the whole input to the graph, so the same three answers
    mean the same graph and the expensive log is not read again.
    """
    refs, head, sym = await asyncio.gather(
        _run(["git", "-C", root, "for-each-ref", "--format=%(objectname) %(refname)",
              "refs/heads", "refs/remotes", "refs/tags"], timeout=GIT_TIMEOUT),
        _run(["git", "-C", root, "rev-parse", "--verify", "--quiet", "HEAD"],
             timeout=GIT_TIMEOUT),
        _run(["git", "-C", root, "symbolic-ref", "--quiet", "HEAD"], timeout=GIT_TIMEOUT),
    )
    ref_code, ref_out, ref_err = refs
    if ref_code != 0:
        raise _GitError(ref_err.strip() or f"git for-each-ref exited {ref_code}")
    head_code, head_out, _ = head
    sym_code, sym_out, _ = sym
    head_sha = (head_out.strip() or None) if head_code == 0 else None
    branch = (sym_out.strip() or None) if sym_code == 0 else None
    digest = hashlib.sha256(
        "\0".join((ref_out, head_sha or "", branch or "")).encode("utf-8")).hexdigest()
    return head_sha, branch, digest[:16], ref_out


def _parse_graph_log(text: str, limit: int,
                     head_sha: Optional[str]) -> tuple[list[dict[str, Any]], bool]:
    """Records are NUL separated, so a subject with a newline cannot split one."""
    records = [record for record in text.split("\0") if record.strip()]
    truncated = len(records) > limit
    commits: list[dict[str, Any]] = []
    for record in records[:limit]:
        parts = record.split("\x1f", 6)
        if len(parts) != 7 or not re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            continue
        commits.append({
            "sha": parts[0], "short": parts[0][:7],
            "parents": parts[1].split() if parts[1] else [],
            "author": {"name": parts[2], "email": parts[3]},
            "authored_at": parts[4], "committed_at": parts[5],
            "subject": parts[6], "is_head": parts[0] == head_sha,
        })
    return commits, truncated


def _graph_refs(text: str, window: set[str]) -> tuple[list[dict[str, Any]], int, Optional[str]]:
    """Refs whose peeled sha is in the window, plus the default branch symref."""
    refs: list[dict[str, Any]] = []
    outside = 0
    default_branch: Optional[str] = None
    for line in text.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 7:
            continue
        name, obj, peeled, upstream, track, head_mark, symref = parts
        if symref:
            # refs/remotes/<remote>/HEAD names the default branch; it is not a pill.
            if name.startswith("refs/remotes/") and name.endswith("/HEAD"):
                default_branch = (symref[len("refs/remotes/"):]
                                  if symref.startswith("refs/remotes/") else symref)
            continue
        sha = peeled or obj
        if name.startswith("refs/heads/"):
            kind, short = "local", name[len("refs/heads/"):]
        elif name.startswith("refs/remotes/"):
            kind, short = "remote", name[len("refs/remotes/"):]
        elif name.startswith("refs/tags/"):
            kind, short = "tag", name[len("refs/tags/"):]
        else:
            continue
        if sha not in window:
            outside += 1
            continue
        ahead, behind = _track(track)
        refs.append({
            "name": name, "short": short, "kind": kind, "sha": sha,
            "upstream": upstream or None, "ahead": ahead, "behind": behind,
            "gone": (track or "").strip() == "gone",
            "is_head": head_mark.strip() == "*", "is_remote": kind == "remote",
        })
    return refs, outside, default_branch


async def _graph_read(root: str, limit: int, head_sha: Optional[str],
                      branch_ref: Optional[str], fingerprint: str) -> dict[str, Any]:
    log_args = ["git", "-C", root, "-c", "log.showSignature=false",
                "-c", "i18n.logOutputEncoding=UTF-8", "log", "--date-order",
                "--no-color", "--no-decorate", "-z", "-n", str(limit + 1),
                f"--format={_LOG_FORMAT}", "--branches", "--remotes"]
    if head_sha:
        log_args.append("HEAD")
    log_args.append("--")
    ref_args = ["git", "-C", root, "for-each-ref", f"--format={_GRAPH_REF_FORMAT}",
                "refs/heads", "refs/remotes", "refs/tags"]
    log, refs = await asyncio.gather(_run(log_args, timeout=GRAPH_TIMEOUT),
                                     _run(ref_args, timeout=GRAPH_TIMEOUT))
    log_code, log_out, log_err = log
    ref_code, ref_out, ref_err = refs
    if log_code != 0:
        raise _GitError(log_err.strip() or f"git log exited {log_code}")
    if ref_code != 0:
        raise _GitError(ref_err.strip() or f"git for-each-ref exited {ref_code}")
    commits, truncated = _parse_graph_log(log_out, limit, head_sha)
    window = {commit["sha"] for commit in commits}
    ref_rows, outside, default_branch = _graph_refs(ref_out, window)
    branch = (branch_ref[len("refs/heads/"):]
              if branch_ref and branch_ref.startswith("refs/heads/") else branch_ref)
    return {
        "ok": True, "code": "ok", "error": None, "detail": None, "cached": False,
        "root": root, "fingerprint": fingerprint, "limit": limit, "truncated": truncated,
        "head": {"sha": head_sha, "branch": branch,
                 "detached": bool(head_sha and not branch_ref)},
        "default_branch": default_branch,
        "commits": commits, "refs": ref_rows, "refs_outside": outside,
    }


def _graph_failure(path: str, code: str, error: str, detail: Optional[str], *,
                   limit: int = 200, root: Optional[str] = None) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error, "detail": detail, "cached": False,
            "path": path, "root": root, "fingerprint": None, "limit": limit,
            "truncated": False, "head": {"sha": None, "branch": None, "detached": False},
            "default_branch": None, "commits": [], "refs": [], "refs_outside": 0}


def _commit_failure(sha: str, code: str, error: str, detail: Optional[str]) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error, "detail": detail, "cached": False,
            "sha": sha, "short": sha[:7], "parents": [], "author": None,
            "authored_at": None, "committer": None, "committed_at": None,
            "subject": None, "body": None, "diff_base": None, "on_remote": False,
            "stats": {"files": 0, "additions": 0, "deletions": 0}, "files": [],
            "truncated": False, "truncated_reason": None}


def _parse_numstat(text: str) -> list[dict[str, Any]]:
    """numstat -z: ``add<TAB>del<TAB>path<NUL>`` or ``add<TAB>del<TAB><NUL>old<NUL>new<NUL>``."""
    tokens = text.split("\0")
    rows: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index].lstrip("\n")
        index += 1
        if not token:
            continue
        fields = token.split("\t")
        if len(fields) < 2:
            continue
        added, deleted = fields[0], fields[1]
        path = "\t".join(fields[2:]) if len(fields) > 2 else ""
        old_path: Optional[str] = None
        rename = False
        if not path:
            if index + 1 >= len(tokens):
                break
            old_path = tokens[index].lstrip("\n")
            path = tokens[index + 1]
            index += 2
            rename = True
        binary = added == "-" and deleted == "-"
        rows.append({
            "path": path, "old_path": old_path,
            "additions": 0 if binary else _int(added),
            "deletions": 0 if binary else _int(deleted),
            "binary": binary, "rename": rename,
        })
    return rows


def _patch_path(value: str) -> Optional[str]:
    if value == "/dev/null":
        return None
    if value.startswith("a/") or value.startswith("b/"):
        return value[2:]
    return value


def _parse_patch(text: str) -> tuple[dict[str, dict[str, Any]], bool, Optional[str]]:
    """Diff blocks by path, whether the line cap cut the read, and the last path.

    Headers are only read before a file's first hunk, because a removed line can
    itself start with ``--- ``, ``+++ `` or ``diff --git``.
    """
    blocks: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    hunk: Optional[dict[str, Any]] = None
    seen_hunk = False
    line_cut = False
    for count, line in enumerate(text.splitlines()):
        if count >= DIFF_MAX_LINES:
            line_cut = True
            break
        if line.startswith("diff --git "):
            current = {"path": None, "old_path": None, "status": "M",
                       "old_mode": None, "new_mode": None, "binary": False, "hunks": []}
            blocks.append(current)
            hunk = None
            seen_hunk = False
            continue
        if current is None:
            continue
        if hunk is not None:
            op = line[:1]
            if op in (" ", "+", "-"):
                hunk["lines"].append([op, line[1:]])
                continue
            if op == "\\":
                hunk["lines"].append(["\\", line[2:] if line[1:2] == " " else line[1:]])
                continue
            hunk = None
        if line.startswith("@@"):
            match = _HUNK_RE.match(line)
            if match is None:
                continue
            hunk = {"header": line, "old_start": int(match.group(1)),
                    "old_lines": int(match.group(2) or 1),
                    "new_start": int(match.group(3)),
                    "new_lines": int(match.group(4) or 1), "lines": []}
            current["hunks"].append(hunk)
            seen_hunk = True
            continue
        if seen_hunk:
            continue
        if line.startswith("old mode "):
            current["old_mode"] = line[len("old mode "):]
        elif line.startswith("new mode "):
            current["new_mode"] = line[len("new mode "):]
        elif line.startswith("new file mode "):
            current["status"] = "A"
        elif line.startswith("deleted file mode "):
            current["status"] = "D"
        elif line.startswith("rename from "):
            current["status"] = "R"
            current["old_path"] = line[len("rename from "):]
        elif line.startswith("rename to "):
            current["path"] = line[len("rename to "):]
        elif line.startswith("copy from "):
            current["status"] = "C"
            current["old_path"] = line[len("copy from "):]
        elif line.startswith("copy to "):
            current["path"] = line[len("copy to "):]
        elif line.startswith("--- "):
            current["old_path"] = _patch_path(line[4:])
        elif line.startswith("+++ "):
            path = _patch_path(line[4:])
            if path is None:
                current["path"] = current["old_path"]
                if current["status"] == "M":
                    current["status"] = "D"
            else:
                current["path"] = path
        elif line.startswith("Binary files "):
            current["binary"] = True
            match = re.match(r"^Binary files (.*) and (.*) differ$", line)
            if match:
                old = _patch_path(match.group(1))
                new = _patch_path(match.group(2))
                if old:
                    current["old_path"] = old
                current["path"] = new or old
    by_path: dict[str, dict[str, Any]] = {}
    last_path: Optional[str] = None
    for block in blocks:
        if (block["status"] == "M" and block["old_mode"] and block["new_mode"]
                and block["old_mode"][:3] != block["new_mode"][:3]):
            block["status"] = "T"
        path = block["path"] or block["old_path"]
        if path:
            prior = by_path.get(path)
            if prior is not None and {prior["status"], block["status"]} == {"A", "D"}:
                # A mode change renders as a delete and a create of one path.
                block["status"] = "T"
                block["hunks"] = prior["hunks"] + block["hunks"]
            by_path[path] = block
            last_path = path
    return by_path, line_cut, last_path


def _show_argv(root: str, full: str, *, numstat: bool, legacy: bool) -> list[str]:
    argv = ["git", "-C", root, "-c", "core.quotePath=false", "show",
            "--no-ext-diff", "--no-textconv", "--no-color", "--no-show-signature",
            "--format="]
    if numstat:
        argv += ["--numstat", "-z", "-M"]
    else:
        argv += ["--patch", "-M", "--unified=3"]
    if legacy:
        argv += ["-m", "--first-parent"]
    else:
        argv += ["--diff-merges=first-parent"]
    return argv + [full, "--"]


def _needs_legacy(err: str) -> bool:
    text = (err or "").lower()
    return "diff-merges" in text or "unknown option" in text or "unrecognized option" in text


def _commit_payload(root: str, meta: str, numstat: str, patch: str, patch_cut: bool,
                    on_remote: bool) -> dict[str, Any]:
    fields = meta.split("\x1f", 8)
    if len(fields) != 9:
        raise _GitError("git show returned an unexpected commit header")
    (sha, parents_raw, author_name, author_email, authored_at, committer_name,
     committer_email, committed_at, message) = fields
    message = message.rstrip("\n")
    subject, _, body = message.partition("\n")
    rows = _parse_numstat(numstat)
    blocks, line_cut, last_path = _parse_patch(patch)
    files_cut = len(rows) > DIFF_MAX_FILES
    out_files: list[dict[str, Any]] = []
    for row in rows[:DIFF_MAX_FILES]:
        block = blocks.get(row["path"])
        if block is None and row["old_path"]:
            block = blocks.get(row["old_path"])
        status = block["status"] if block else ("R" if row["rename"] else "M")
        old_path = (row["old_path"] if row["rename"]
                    else block["old_path"] if block and status in ("R", "C") else None)
        file_cut = bool(row["path"] == last_path and (patch_cut or line_cut))
        if block is None and (patch_cut or line_cut) and not row["binary"]:
            file_cut = True
        out_files.append({
            "path": row["path"],
            "old_path": old_path,
            "status": status,
            "additions": row["additions"], "deletions": row["deletions"],
            "binary": row["binary"], "truncated": file_cut,
            "hunks": block["hunks"] if block else [],
        })
    reason = ("bytes" if patch_cut else "lines" if line_cut
              else "files" if files_cut else None)
    return {
        "ok": True, "code": "ok", "error": None, "detail": None, "cached": False,
        "root": root, "sha": sha, "short": sha[:7],
        "parents": parents_raw.split() if parents_raw else [],
        "author": {"name": author_name, "email": author_email}, "authored_at": authored_at,
        "committer": {"name": committer_name, "email": committer_email},
        "committed_at": committed_at, "subject": subject, "body": body.strip(),
        "diff_base": "first_parent" if parents_raw else "root",
        "on_remote": on_remote,
        "stats": {"files": len(rows),
                  "additions": sum(row["additions"] for row in rows),
                  "deletions": sum(row["deletions"] for row in rows)},
        "files": out_files,
        "truncated": reason is not None, "truncated_reason": reason,
    }


async def _commit_read(root: str, full: str) -> dict[str, Any]:
    meta_argv = ["git", "-C", root, "-c", "i18n.logOutputEncoding=UTF-8", "show", "-s",
                 "--no-show-signature",
                 "--format=%H%x1f%P%x1f%aN%x1f%aE%x1f%aI%x1f%cN%x1f%cE%x1f%cI%x1f%B",
                 full, "--"]
    remote_argv = ["git", "-C", root, "for-each-ref", "--contains", full, "--count=1",
                   "--format=%(refname)", "refs/remotes"]
    meta, numstat, patch, remote = await asyncio.gather(
        _run(meta_argv, timeout=COMMIT_TIMEOUT),
        _run(_show_argv(root, full, numstat=True, legacy=False), timeout=COMMIT_TIMEOUT),
        _run_capped(_show_argv(root, full, numstat=False, legacy=False),
                    max_bytes=DIFF_MAX_BYTES, timeout=COMMIT_TIMEOUT),
        _run(remote_argv, timeout=GIT_TIMEOUT),
        return_exceptions=True,
    )
    if isinstance(meta, BaseException):
        raise _GitError(str(meta) or "git show failed")
    meta_code, meta_out, meta_err = meta
    if meta_code != 0:
        raise _GitError(meta_err.strip() or f"git show exited {meta_code}")
    if isinstance(numstat, BaseException):
        raise _GitError(str(numstat) or "git show --numstat failed")
    if isinstance(patch, BaseException):
        raise _GitError(str(patch) or "git show --patch failed")

    num_code, num_out, num_err = numstat
    patch_code, patch_out, patch_cut = patch
    if num_code != 0 or (patch_code != 0 and not patch_cut):
        # git older than 2.31 does not know --diff-merges: retry once with -m.
        num_retry, patch_retry = await asyncio.gather(
            _run(_show_argv(root, full, numstat=True, legacy=True), timeout=COMMIT_TIMEOUT),
            _run_capped(_show_argv(root, full, numstat=False, legacy=True),
                        max_bytes=DIFF_MAX_BYTES, timeout=COMMIT_TIMEOUT),
        )
        retry_code, retry_out, retry_err = num_retry
        if retry_code != 0:
            raise _GitError(retry_err.strip() or num_err.strip()
                            or f"git show exited {num_code}")
        patch_code, patch_out, patch_cut = patch_retry
        num_out = retry_out
        if patch_code != 0 and not patch_cut:
            raise _GitError(f"git show could not produce the diff for {full[:7]}")
        if not _needs_legacy(num_err) and num_code != 0:
            log.info("project-command-center: retried git show for %s after: %s",
                     full[:7], num_err.strip())

    on_remote = False
    if not isinstance(remote, BaseException):
        remote_code, remote_out, _ = remote
        on_remote = remote_code == 0 and bool(remote_out.strip())
    return _commit_payload(root, meta_out, num_out, patch_out, patch_cut, on_remote)


# --- GitHub client ----------------------------------------------------------

def _secret_token() -> Optional[str]:
    """GITHUB_TOKEN through Hermes's secret scope: never os.environ, never a file."""
    try:
        from agent.secret_scope import get_secret
    except ImportError:  # tree without the module
        return None
    try:
        value = get_secret("GITHUB_TOKEN")
    except Exception:  # UnscopedSecretError under multiplexing: no route, treat as absent
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


_GH_STATE: dict[str, Any] = {"checked": 0.0, "user": None}


async def _gh_user() -> Optional[str]:
    """The gh CLI's login, or None when gh is missing or not signed in. Cached 60 s."""
    if shutil.which("gh") is None:
        return None
    if _GH_STATE["checked"] and time.monotonic() - _GH_STATE["checked"] < GITHUB_TTL:
        return _GH_STATE["user"]
    user = None
    try:
        code, out, _ = await _run(["gh", "api", "user", "--jq", ".login"], timeout=GH_TIMEOUT)
        user = out.strip() if code == 0 and out.strip() else None
    except (TimeoutError, OSError):
        user = None
    _GH_STATE.update({"checked": time.monotonic(), "user": user})
    return user


async def _auth() -> tuple[str, Optional[str]]:
    """("token", token) | ("gh", None) | ("none", None)."""
    token = _secret_token()
    if token:
        return "token", token
    return ("gh", None) if await _gh_user() else ("none", None)


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text) if text.strip() else None
    except ValueError:
        return None


def _gh_message(body: str) -> str:
    data = _json_or_none(body)
    return (str(data.get("message") or data)[:200] if isinstance(data, dict)
            else body.strip()[:200])


def _parse_gh_response(text: str) -> tuple[int, dict[str, str], str]:
    """Split ``gh api -i`` output into (status, lowercase headers, body)."""
    head, _, rest = text.partition("\n")
    if not head.startswith("HTTP/"):
        return 0, {}, text
    parts = head.split()
    status = _int(parts[1]) if len(parts) > 1 else 0
    block, body = rest, ""
    for separator in ("\r\n\r\n", "\n\n"):
        candidate, found, remainder = rest.partition(separator)
        if found:
            block, body = candidate, remainder
            break
    headers: dict[str, str] = {}
    for line in block.splitlines():
        key, _, value = line.partition(":")
        if key and value:
            headers[key.strip().lower()] = value.strip()
    return status, headers, body


def _rate_limit(headers: Mapping[str, str]) -> Optional[dict[str, Any]]:
    if "x-ratelimit-remaining" not in headers:
        return None
    return {
        "limit": _int(headers.get("x-ratelimit-limit")),
        "remaining": _int(headers.get("x-ratelimit-remaining")),
        "reset": _int(headers.get("x-ratelimit-reset")),
        "resource": headers.get("x-ratelimit-resource"),
    }


class _GitHub:
    """One repo seen through one auth mode, with at most two calls in flight."""

    def __init__(self, owner: str, repo: str, mode: str, token: Optional[str]) -> None:
        self.owner, self.repo, self.mode, self._token = owner, repo, mode, token
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_GITHUB_CALLS)

    async def rest(self, path: str, params: Optional[Mapping[str, Any]] = None,
                   etag: Optional[str] = None) -> _Response:
        url = f"{path}?{urlencode(params)}" if params else path
        endpoint = url.split("?")[0]
        async with self._slots:
            if self.mode != "gh":
                return await self._http(url, etag)
            try:
                argv = ["gh", "api", "-i", "--method", "GET"]
                if etag:
                    argv += ["-H", f"If-None-Match: {etag}"]
                code, out, err = await _run([*argv, url], timeout=GH_TIMEOUT)
            except (TimeoutError, OSError) as exc:
                raise _UpstreamError(
                    f"gh is installed but not usable: {exc}", code="network",
                    detail=f"GET {endpoint} -> {type(exc).__name__}", endpoint=endpoint,
                    credential=self.mode) from exc
            status, headers, body = _parse_gh_response(out)
            if status == 0:
                text = (err or out).strip()
                code_name = "no_auth" if re.search(r"auth|logged in", text, re.I) else "network"
                raise _UpstreamError(
                    f"gh api {path} failed: {text[:300] or f'exit {code}'}", code=code_name,
                    detail=f"gh api {endpoint} -> no HTTP answer", endpoint=endpoint,
                    credential=self.mode)
            if status >= 400:
                raise _classify(status, headers, endpoint, _gh_message(body), self.mode)
            return _Response(status, _json_or_none(body), headers)

    async def _http(self, url: str, etag: Optional[str]) -> _Response:
        endpoint = url.split("?")[0]
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "hermes-project-command-center"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if etag:
            headers["If-None-Match"] = etag
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                response = await client.get(f"{API}{url}", headers=headers)
        except httpx.HTTPError as exc:
            raise _UpstreamError(
                f"GitHub request failed: {exc}", code="network",
                detail=f"GET {endpoint} -> {type(exc).__name__}", endpoint=endpoint,
                credential=self.mode) from exc
        lowered = {k.lower(): v for k, v in response.headers.items()}
        if response.status_code == 304:
            return _Response(304, None, lowered)
        if response.status_code >= 400:
            raise _classify(response.status_code, lowered, endpoint,
                            _gh_message(response.text), self.mode)
        try:
            return _Response(response.status_code, response.json(), lowered)
        except ValueError as exc:
            raise _UpstreamError(
                f"GitHub {endpoint} returned non JSON: {exc}", code="upstream_error",
                detail=f"GET {endpoint} -> HTTP {response.status_code}, non JSON body",
                endpoint=endpoint, status=response.status_code,
                credential=self.mode) from exc

    async def graphql(self, query: str, variables: Mapping[str, Any],
                      partial: bool = False) -> Optional[dict[str, Any]]:
        """The GraphQL answer, or None when there is no authenticated route or it errored.

        ``partial`` accepts an answer that carries ``errors`` as long as it also
        carries ``data``. A batched alias per number needs it: GitHub answers
        NOT_FOUND for each number that does not exist, which is exactly the case
        the caller handles by leaving that number out.
        """
        if self.mode == "none":
            return None
        async with self._slots:
            if self.mode == "gh":
                argv = ["gh", "api", "graphql", "-f", f"query={query}"]
                for key, value in variables.items():
                    argv += ["-F", f"{key}={value}"]
                try:
                    code, out, err = await _run(argv, timeout=GH_TIMEOUT)
                except (TimeoutError, OSError) as exc:
                    raise _UpstreamError(f"gh is installed but not usable: {exc}", code="network",
                                         detail="POST /graphql -> no answer", endpoint="/graphql",
                                         credential="gh") from exc
                if code != 0 and not partial:
                    raise _UpstreamError(f"gh api graphql failed: {(err or out).strip()[:300]}",
                                         code="upstream_error", detail="POST /graphql failed",
                                         endpoint="/graphql", credential="gh")
                body = _json_or_none(out) or {}
            else:
                headers = {"Accept": "application/vnd.github+json",
                           "Authorization": f"Bearer {self._token}",
                           "User-Agent": "hermes-project-command-center"}
                try:
                    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                        response = await client.post(f"{API}/graphql",
                                                     json={"query": query, "variables": dict(variables)},
                                                     headers=headers)
                except httpx.HTTPError as exc:
                    raise _UpstreamError(f"GitHub GraphQL request failed: {exc}", code="network",
                                         detail="POST /graphql -> no answer", endpoint="/graphql",
                                         credential=self.mode) from exc
                if response.status_code >= 400:
                    raise _classify(response.status_code,
                                    {k.lower(): v for k, v in response.headers.items()},
                                    "/graphql", "GraphQL", self.mode)
                body = response.json()
        if not isinstance(body, dict):
            return None
        if body.get("errors"):
            first = body["errors"][0] if isinstance(body["errors"], list) else {}
            if not partial:
                log.info("project-command-center: GraphQL error: %s",
                         (first.get("message") if isinstance(first, dict) else None)
                         or json.dumps(first)[:200])
                return None
            # partial: NOT_FOUND per missing alias is expected, data still stands
            log.debug("project-command-center: GraphQL partial answer with %s error(s)",
                      len(body["errors"]))
        return body.get("data") if isinstance(body.get("data"), dict) else None


# --- Credential fallback ----------------------------------------------------

_TOKEN_USER: dict[str, Any] = {"checked": 0.0, "user": None}


async def _token_user(token: str) -> Optional[str]:
    """The login behind the configured token, asked only to name an account.

    Cached 60 s and keyed by nothing: the token itself is never stored, logged or
    returned, and this runs only on a failure path where the name explains the
    answer (for example "as NyllRE").
    """
    if _TOKEN_USER["checked"] and time.monotonic() - _TOKEN_USER["checked"] < GITHUB_TTL:
        return _TOKEN_USER["user"]
    user = None
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.get(f"{API}/user", headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "hermes-project-command-center"})
        body = response.json() if response.status_code == 200 else None
        user = body.get("login") if isinstance(body, dict) else None
    except (httpx.HTTPError, ValueError):
        user = None
    _TOKEN_USER.update({"checked": time.monotonic(), "user": user})
    return user


async def _account_name(reader: "_GhReader", token: Optional[str]) -> Optional[str]:
    """The login of whichever credential the reader used last, or None."""
    if reader.credential == "gh":
        return await _gh_user()
    if reader.credential == "token" and token:
        return await _token_user(token)
    return None


def _github_sentence(exc: _UpstreamError, owner: str, repo: str,
                     account: Optional[str]) -> str:
    """One human sentence for a failed GitHub read: the repo, the account, the cause."""
    slug = f"{owner}/{repo}"
    who = account or "the configured credential"
    status = exc.status or 0
    if exc.code == "repo_not_found":
        return (f"GitHub could not read {slug} as {who} ({status or 404}). The repo may be "
                "private, renamed or deleted, or the account may not have access.")
    if exc.code == "no_auth":
        return (f"GitHub not authenticated for {slug} as {who}. Set GITHUB_TOKEN or run "
                "gh auth login.")
    if exc.code == "rate_limited":
        return (f"GitHub rate limit reached for {slug} as {who}. The next read retries "
                "after the reset.")
    if exc.code == "network":
        return f"GitHub could not be reached for {slug} as {who}."
    return f"GitHub could not answer for {slug} as {who} ({status or 'no status'})."


_REPO_ALIAS_RE = re.compile(r"^r\d+$")


def _graphql_blind(data: Optional[dict[str, Any]]) -> bool:
    """True when a batched ``repository`` query came back with every alias null.

    A token that cannot see a private repository gets ``repository: null`` with a
    NOT_FOUND beside it, so ``partial`` would read a blind credential as "no such
    repository". Every alias null is the signal that the credential, not the repo,
    is the problem. A mixed answer is a real "some of these do not exist" and is
    left alone.
    """
    if not isinstance(data, dict):
        return False
    aliases = [value for key, value in data.items()
               if isinstance(key, str) and _REPO_ALIAS_RE.match(key)]
    return bool(aliases) and all(value is None for value in aliases)


class _GhReader:
    """One repository read through the preferred credential, with a gh retry.

    A token GitHub rejects (401, 403 that is not a rate limit, 404 on a repo the
    CLI can see) is retried once through the user's own gh CLI and the gh answer
    wins; this reader then stays on gh so a board read does not pay the rejection
    twice. ``credential`` names whatever answered (or was tried) last and
    ``auth_note`` explains a fallback without naming the token.
    """

    def __init__(self, owner: str, repo: str, mode: str, token: Optional[str]) -> None:
        self.owner, self.repo = owner, repo
        self._token = token
        self._primary = _GitHub(owner, repo, mode, token)
        self._gh = _GitHub(owner, repo, "gh", None) if mode == "token" else None
        self._fell_back = False
        self.credential = mode
        self.answered: Optional[str] = "none" if mode == "none" else None
        self.auth_note: Optional[str] = None

    @property
    def _client(self) -> "_GitHub":
        return self._gh if (self._fell_back and self._gh is not None) else self._primary

    def _can_retry(self, exc: _UpstreamError) -> bool:
        return (self._gh is not None and not self._fell_back and exc.rejected
                and shutil.which("gh") is not None)

    async def _fall_back(self, exc: _UpstreamError) -> None:
        self._fell_back = True
        self.credential = "gh"
        self.auth_note = f"token was rejected with {exc.status}; retried through gh"

    async def _confirm(self, exc: _UpstreamError) -> None:
        """Upgrade the note once gh actually answered: the account name helps."""
        user = await _gh_user()
        if user:
            self.auth_note = f"token was rejected with {exc.status}; answered through gh as {user}"

    async def rest(self, path: str, params: Optional[Mapping[str, Any]] = None,
                   etag: Optional[str] = None) -> _Response:
        if self._fell_back:
            # Already retried once: stay on gh for the rest of this read.
            response = await self._client.rest(path, params, etag)
            self.answered = self.credential
            return response
        try:
            response = await self._primary.rest(path, params, etag)
        except _UpstreamError as exc:
            if not self._can_retry(exc):
                raise
            await self._fall_back(exc)
            response = await self._client.rest(path, params)
            await self._confirm(exc)
        self.answered = self.credential
        return response

    async def graphql(self, query: str, variables: Mapping[str, Any],
                      partial: bool = False) -> Optional[dict[str, Any]]:
        if self._fell_back:
            data = await self._client.graphql(query, variables, partial)
            self.answered = self.credential
            return data
        try:
            data = await self._primary.graphql(query, variables, partial)
        except _UpstreamError as exc:
            if not self._can_retry(exc):
                raise
            await self._fall_back(exc)
            data = await self._client.graphql(query, variables, partial)
            await self._confirm(exc)
        else:
            # A token that cannot see a private repository answers ``repository: null``
            # with a NOT_FOUND beside it, which ``partial`` would accept as "no such
            # repository". Every alias being null is a credential blind spot, not an
            # empty answer, so it earns the same one retry through gh.
            if partial and _graphql_blind(data) and self._can_retry(
                    _UpstreamError(f"the credential cannot see {self.owner}/{self.repo}",
                                   code="repo_not_found", status=404, endpoint="/graphql",
                                   credential=self.credential, rejected=True)):
                exc = _UpstreamError(f"the credential cannot see {self.owner}/{self.repo}",
                                     code="repo_not_found", status=404, endpoint="/graphql",
                                     credential=self.credential, rejected=True)
                await self._fall_back(exc)
                data = await self._client.graphql(query, variables, partial)
                await self._confirm(exc)
        self.answered = self.credential
        return data

    @property
    def auth(self) -> str:
        """The credential that answered; on a failure, the last one tried."""
        return self.answered or self.credential


# --- Linking ----------------------------------------------------------------

_CLOSING_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs?)\s*:?\s*#(\d+)", re.I)


def _linked_numbers(text: str) -> list[int]:
    """Issue numbers a PR title or body claims to close, GitHub's keyword syntax."""
    return _uniq([match.group(1) for match in _CLOSING_RE.finditer(text or "")])


def _timeline_refs(events: Any) -> tuple[list[int], list[int]]:
    """(pull numbers, issue numbers) a timeline cross references."""
    pulls: list[int] = []
    issues: list[int] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or event.get("event") not in ("cross-referenced", "connected"):
            continue
        source = event.get("source")
        issue = source.get("issue") if isinstance(source, dict) else None
        if isinstance(issue, dict) and isinstance(issue.get("number"), int):
            (pulls if "pull_request" in issue else issues).append(issue["number"])
    return _uniq(pulls), _uniq(issues)


_PR_FIELDS = """
  number reviewDecision mergeStateStatus isDraft mergedAt
  additions deletions changedFiles headRefName baseRefName
  closingIssuesReferences(first: 20) { nodes { number } }
  statusCheckRollup {
    state
    contexts(first: 50) {
      totalCount
      nodes { __typename ... on CheckRun { status conclusion } ... on StatusContext { state } }
    }
  }
"""


def _pr_query(owner: str, repo: str, numbers: Sequence[int]) -> tuple[str, dict[str, Any]]:
    """One aliased pullRequest field per number: no pagination guessing."""
    definitions = ["$owner: String!", "$repo: String!"]
    variables: dict[str, Any] = {"owner": owner, "repo": repo}
    fields = []
    for index, number in enumerate(numbers):
        definitions.append(f"$n{index}: Int!")
        variables[f"n{index}"] = number
        fields.append(f"p{index}: pullRequest(number: $n{index}) {{{_PR_FIELDS}}}")
    query = (f"query({', '.join(definitions)}) {{ repository(owner: $owner, name: $repo) "
             f"{{ {' '.join(fields)} }} }}")
    return query, variables


_REF_FIELDS = """
  __typename
  ... on Issue { number title state stateReason url updatedAt author { __typename login avatarUrl(size: 48) } }
  ... on PullRequest { number title state isDraft merged url updatedAt author { __typename login avatarUrl(size: 48) } }
"""


def _refs_query(owner: str, repo: str, numbers: Sequence[int]) -> tuple[str, dict[str, Any]]:
    """One aliased ``issueOrPullRequest`` per number: the whole set in one call.

    A number that does not exist resolves to null and is simply left out of the
    answer; a PR state of MERGED is reported as ``closed``, because that is what
    the chip shows. MAX_REFS bounds the aliases at GitHub's own field limit.
    """
    definitions = ["$owner: String!", "$repo: String!"]
    variables: dict[str, Any] = {"owner": owner, "repo": repo}
    fields = []
    for index, number in enumerate(numbers):
        definitions.append(f"$n{index}: Int!")
        variables[f"n{index}"] = number
        fields.append(f"r{index}: issueOrPullRequest(number: $n{index}) {{{_REF_FIELDS}}}")
    query = (f"query({', '.join(definitions)}) {{ repository(owner: $owner, name: $repo) "
             f"{{ {' '.join(fields)} }} }}")
    return query, variables


def _counts_query(repos: Sequence[tuple[str, str]]) -> tuple[str, dict[str, Any]]:
    """One aliased ``repository`` per (owner, repo): open issue and PR totals, one call.

    GitHub's GraphQL ``issues`` connection holds issues only (a pull request is
    not an issue there), so ``issues`` and ``pullRequests`` cannot double count;
    REST's ``/issues`` endpoint, which does return PRs, is not used here.
    """
    definitions: list[str] = []
    variables: dict[str, Any] = {}
    fields = []
    for index, (owner, repo) in enumerate(repos):
        definitions += [f"$o{index}: String!", f"$n{index}: String!"]
        variables[f"o{index}"] = owner
        variables[f"n{index}"] = repo
        fields.append(f"r{index}: repository(owner: $o{index}, name: $n{index}) "
                      "{ issues(states: OPEN) { totalCount } "
                      "pullRequests(states: OPEN) { totalCount } }")
    query = f"query({', '.join(definitions)}) {{ {' '.join(fields)} }}"
    return query, variables


async def _enrich_pulls(gh: _GhReader, numbers: Sequence[int]) -> dict[int, dict[str, Any]]:
    """GraphQL per PR number: review decision, merge state, checks, closing refs."""
    wanted = list(dict.fromkeys(n for n in numbers if isinstance(n, int)))[:MAX_PRS_ENRICHED]
    if not wanted or gh.credential == "none":
        return {}
    query, variables = _pr_query(gh.owner, gh.repo, wanted)
    try:
        data = await gh.graphql(query, variables)
    except _UpstreamError as exc:
        log.warning("project-command-center: PR enrichment unavailable: %s", exc)
        return {}
    repository = (data or {}).get("repository") or {}
    return {number: repository[f"p{index}"] for index, number in enumerate(wanted)
            if isinstance(repository.get(f"p{index}"), dict)}


_FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
_ROLLUP_STATES = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure",
                  "PENDING": "pending", "EXPECTED": "pending"}
_MERGE_STATES = {"CLEAN": "clean", "DIRTY": "dirty", "BLOCKED": "blocked", "BEHIND": "behind",
                 "UNSTABLE": "unstable", "DRAFT": "draft", "HAS_HOOKS": "has_hooks", "UNKNOWN": "unknown"}


def _checks(node: Optional[Mapping[str, Any]]) -> Checks:
    rollup = (node or {}).get("statusCheckRollup")
    if not isinstance(rollup, dict):
        return {"state": "none", "total": 0, "failing": 0}
    contexts = rollup.get("contexts") if isinstance(rollup.get("contexts"), dict) else {}
    nodes = [c for c in (contexts.get("nodes") or []) if isinstance(c, dict)]
    total = _int(contexts.get("totalCount")) or len(nodes)
    failing = pending = 0
    for context in nodes:
        if context.get("__typename") == "StatusContext":
            state = str(context.get("state") or "").upper()
            if state in ("FAILURE", "ERROR"):
                failing += 1
            elif state in ("PENDING", "EXPECTED", ""):
                pending += 1
        elif str(context.get("status") or "").upper() != "COMPLETED":
            pending += 1
        elif str(context.get("conclusion") or "").upper() in _FAILED_CONCLUSIONS:
            failing += 1
    if failing:
        state = "failure"
    elif pending:
        state = "pending"
    elif total:
        state = "success"
    else:
        state = _ROLLUP_STATES.get(str(rollup.get("state") or "").upper(), "none")
    return {"state": state, "total": total, "failing": failing}


def _issue_row(raw: Mapping[str, Any]) -> IssueRow:
    assignee_avatars = _assignee_avatars(raw.get("assignees"))
    login, avatar, bot = _actor(raw.get("user"))
    row: IssueRow = {
        "number": _int(raw.get("number")),
        "title": str(raw.get("title") or ""),
        "state": str(raw.get("state") or "open"),
        # completed | not_planned | duplicate | reopened as GitHub reports it, so
        # a closed row can be coloured and filtered without a second read.
        "state_reason": raw.get("state_reason"),
        "url": raw.get("html_url"),
        "author": login,
        "author_bot": bot,
        "labels": _names(raw.get("labels"), "name"),
        "assignees": _names(raw.get("assignees"), "login"),
        "comments": _int(raw.get("comments")),
        "updated_at": raw.get("updated_at"),
        "created_at": raw.get("created_at"),
    }
    if avatar:
        row["author_avatar"] = avatar
    if assignee_avatars:
        row["assignee_avatars"] = assignee_avatars
    return row


def _pull_row(raw: Mapping[str, Any], node: Optional[Mapping[str, Any]]) -> PullRow:
    node = node or {}
    title = str(raw.get("title") or "")
    links = set(_linked_numbers(f"{title}\n{raw.get('body') or ''}"))
    for ref in ((node.get("closingIssuesReferences") or {}).get("nodes") or []):
        if isinstance(ref, dict) and isinstance(ref.get("number"), int):
            links.add(ref["number"])
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    base = raw.get("base") if isinstance(raw.get("base"), dict) else {}
    merge_state = str(node.get("mergeStateStatus") or "").upper()
    login, avatar, bot = _actor(raw.get("user"))
    row: PullRow = {
        "number": _int(raw.get("number")),
        "title": title,
        "state": str(raw.get("state") or node.get("state") or "open").lower(),
        "draft": bool(raw.get("draft")) or bool(node.get("isDraft")),
        "merged": bool(raw.get("merged_at") or node.get("mergedAt")),
        "url": raw.get("html_url"),
        "author": login,
        "author_bot": bot,
        "head_ref": str(head.get("ref") or node.get("headRefName") or ""),
        "base_ref": str(base.get("ref") or node.get("baseRefName") or ""),
        "updated_at": raw.get("updated_at"),
        "created_at": raw.get("created_at"),
        "additions": _int(node.get("additions", raw.get("additions"))),
        "deletions": _int(node.get("deletions", raw.get("deletions"))),
        "changed_files": _int(node.get("changedFiles", raw.get("changed_files"))),
        "review_decision": node.get("reviewDecision"),
        "checks": _checks(node),
        "linked_issues": sorted(links),
        "mergeable_state": _MERGE_STATES.get(merge_state) or str(
            raw.get("mergeable_state") or merge_state.lower() or "unknown"),
        "enriched": bool(node),
    }
    if avatar:
        row["author_avatar"] = avatar
    return row


def _comment_rows(rows: Any) -> list[CommentRow]:
    """The modal's comment shape: id, author login, markdown verbatim, times, url.

    Oldest first, first MAX_COMMENTS: the REST list is asked for
    ``sort=created&direction=asc`` and sliced here so the cap holds even if a
    server ignores the direction.
    """
    out: list[CommentRow] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        login, avatar, bot = _actor(row.get("user"))
        entry: CommentRow = {
            "id": _int(row.get("id")),
            "author": login,
            "author_bot": bot,
            "body": str(row.get("body") or ""),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "url": row.get("html_url"),
        }
        if avatar:
            entry["author_avatar"] = avatar
        out.append(entry)
    return out[:MAX_COMMENTS]


def _review_rows(rows: Any) -> list[ReviewRow]:
    """Submitted reviews only: an unsubmitted PENDING review has no submitted_at."""
    out: list[ReviewRow] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("submitted_at"):
            continue
        login, avatar, bot = _actor(row.get("user"))
        entry: ReviewRow = {
            "author": login,
            "author_bot": bot,
            "state": row.get("state"),
            "body": str(row.get("body") or ""),
            "submitted_at": row.get("submitted_at"),
        }
        if avatar:
            entry["author_avatar"] = avatar
        out.append(entry)
    return out


def _counts(issues: Sequence[IssueRow], pulls: Sequence[PullRow]) -> Counts:
    """Board aggregates. ``review_ready`` is: open, not a draft, no failing checks and no
    requested changes, so the number matches the rows a reviewer can act on today. The four
    review and check counters cover the enriched window only (GitHub charges a GraphQL
    resolve per PR); with the default limit every row is enriched."""
    open_pulls = [p for p in pulls if p["state"] == "open"]
    judged = [p for p in open_pulls if p["enriched"]]
    return {
        "issues_open": sum(1 for i in issues if i["state"] == "open"),
        "prs_open": len(open_pulls),
        "drafts": sum(1 for p in open_pulls if p["draft"]),
        "review_ready": sum(1 for p in judged if not p["draft"]
                            and p["checks"]["state"] != "failure"
                            and p["review_decision"] != "CHANGES_REQUESTED"),
        "changes_requested": sum(1 for p in judged if p["review_decision"] == "CHANGES_REQUESTED"),
        "approved": sum(1 for p in judged if p["review_decision"] == "APPROVED"),
        "checks_failing": sum(1 for p in judged if p["checks"]["state"] == "failure"),
    }


def _state_sort(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first, de-duplicated by number: one list built from two reads."""
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: str(r.get("updated_at") or ""), reverse=True):
        number = _int(row.get("number"))
        if number in seen:
            continue
        seen.add(number)
        out.append(row)
    return out


def _cap_states(rows: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` open and up to ``limit`` closed rows, in the order given.

    ``state=all`` must not let a busy closed history push every open row out of
    the window: the default (open) view keeps exactly the rows a plain
    ``state=open`` read returns, and the closed half is capped on its own.
    """
    counts = {"open": 0, "closed": 0}
    out: list[dict[str, Any]] = []
    for row in rows:
        bucket = "open" if str(row.get("state") or "open").lower() == "open" else "closed"
        if counts[bucket] >= limit:
            continue
        counts[bucket] += 1
        out.append(row)
    return out


def _empty_board(*, owner: Optional[str] = None, repo: Optional[str] = None,
                 url: Optional[str] = None, code: str = "ok", error: Optional[str] = None,
                 detail: Optional[str] = None, auth: str = "none",
                 auth_note: Optional[str] = None, auth_account: Optional[str] = None,
                 fetched_at: Optional[str] = None) -> dict[str, Any]:
    stamp = fetched_at or _now_iso()
    return {"ok": code == "ok", "code": code, "error": error, "detail": detail,
            "repo": repo, "owner": owner, "url": url, "fetched_at": stamp, "cached": False,
            "auth": auth, "auth_note": auth_note, "auth_account": auth_account,
            "rate_limit": None, "issues": [], "prs": [], "counts": _counts([], []),
            "updated_at": stamp}


def _repo_failure(path: str, code: str, error: str, detail: Optional[str]) -> dict[str, Any]:
    """A /repo payload for a folder that cannot be read, same shape as a cold read."""
    return {"ok": False, "code": code, "error": error, "detail": detail, "warning": None,
            "path": path, "root": None, "root_matches_path": False, "inside_root": None,
            "remote": None, "owner": None, "repo": None, "branch": None, "detached": False,
            "dirty": {"count": 0, "staged": 0, "unstaged": 0, "untracked": 0},
            "ahead": 0, "behind": 0, "branches": []}


def _item_failure(number: int, code: str, error: Optional[str], detail: Optional[str],
                  *, owner: Optional[str] = None, repo: Optional[str] = None,
                  auth: str = "none", auth_note: Optional[str] = None,
                  auth_account: Optional[str] = None) -> dict[str, Any]:
    """A one PR or one issue payload for a read that could not happen."""
    return {"ok": False, "code": code, "error": error, "detail": detail,
            "repo": repo, "owner": owner, "number": number, "auth": auth,
            "auth_note": auth_note, "auth_account": auth_account}


class _RepoContext(NamedTuple):
    owner: Optional[str]
    repo: Optional[str]
    root: Optional[str]
    code: str
    error: Optional[str]
    detail: Optional[str]


async def _repo_context(folder: Path) -> _RepoContext:
    """(owner, repo, root) for a project folder, from git metadata only.

    Failures are codes, never exceptions: ``not_a_repo`` and ``no_remote`` are
    normal states of a folder on this machine, not server errors.
    """
    try:
        root = await _work_tree(folder)
    except TimeoutError as exc:
        return _RepoContext(None, None, None, "upstream_error",
                            f"git timed out resolving the work tree for {folder}.", str(exc))
    if not root:
        return _RepoContext(None, None, None, "not_a_repo",
                            f"{folder} is not inside a git work tree.",
                            f"git -C {folder} rev-parse --show-toplevel")
    # The board calls /repo and /github for the same folder back to back: reuse
    # the git read instead of spawning a second `git remote` inside 20 s.
    known = _CACHE.get(("repo", root))
    if known is not None and known.payload.get("owner") and known.payload.get("repo"):
        return _RepoContext(known.payload["owner"], known.payload["repo"], root,
                            "ok", None, None)
    try:
        remote = await _primary_remote(root)
    except TimeoutError as exc:
        return _RepoContext(None, None, root, "upstream_error",
                            f"git timed out reading the remote of {root}.", str(exc))
    except _GitError as exc:
        return _RepoContext(None, None, root, "upstream_error",
                            f"git could not read the remote of {root}.", str(exc))
    owner, repo = _parse_remote(remote)
    detail = f"git -C {root} remote get-url origin -> {remote or 'no remote'}"
    if not owner or not repo:
        return _RepoContext(None, None, root, "no_remote",
                            (f"No GitHub remote on {root}." if not remote else
                             f"The remote of {root} is not a github.com URL."), detail)
    return _RepoContext(owner, repo, root, "ok", None, None)


async def _quiet(coro: Any) -> Any:
    """Run one enrichment read; its failure degrades that part, never the response."""
    try:
        return await coro
    except _UpstreamError as exc:
        log.info("project-command-center: %s", exc)
        return None


def _as_list(response: Any) -> list[Any]:
    return response.data if isinstance(response, _Response) and isinstance(response.data, list) else []


def _cached_inverse(owner: str, repo: str, issue: int) -> list[int]:
    """The issue's PRs from the board cache: the inverse of the PR to issue map."""
    found: set[int] = set()
    for key, entry in list(_CACHE.items()):
        if len(key) < 3 or key[0] != "github" or key[1] != owner.lower() or key[2] != repo.lower():
            continue
        for pull in entry.payload.get("prs") or []:
            if isinstance(pull, dict) and issue in (pull.get("linked_issues") or []):
                found.add(_int(pull.get("number")))
    found.discard(0)
    return sorted(found)


# --- GitHub writes ------------------------------------------------------------
#
# Four routes and nothing else writes to GitHub: a comment on an issue, a comment
# on a pull request, and open/closed on either. All of them run as the gh CLI's
# own account (never the configured token, which is a read credential) and all of
# them hand the JSON body to the child on stdin. GitHub keeps issues and pull
# requests in one number sequence and its comment and state endpoints accept
# either, so each route confirms the number is the kind it says before writing.


class _WriteFailure(Exception):
    """A write failed in a way the Desktop renders as one code and one sentence."""

    def __init__(self, code: str, error: str, detail: Optional[str] = None) -> None:
        super().__init__(error)
        self.code = code if code in WRITE_CODES else "upstream_error"
        self.error = error
        self.detail = detail


def _write_failure(exc: _WriteFailure) -> dict[str, Any]:
    return {"ok": False, "code": exc.code, "error": exc.error, "detail": exc.detail}


def _cache_drop(owner: str, name: str) -> None:
    """Forget this repo's cached reads so the modal's next read sees the write."""
    doomed = [key for key in _CACHE
              if len(key) > 2 and key[0] in ("github", "issue", "pr", "refs")
              and key[1] == owner.lower() and key[2] == name.lower()]
    for key in doomed:
        _CACHE.pop(key, None)


async def _gh_write(method: str, path: str, body: Mapping[str, Any], *, subject: str,
                    action: str, owner: str, repo: str, number: int) -> Any:
    """One ``gh api`` write with the JSON body on the child's stdin.

    The argv is fixed — ``gh api -i --method <M> --input - <endpoint>`` — so the
    body never appears in an argument and there is no shell: a comment holding
    quotes, backticks, ``$(`` or newlines arrives byte for byte. Statuses become
    codes: 404 ``not_found``, 401/403 ``forbidden`` (a spent 403 or 429 is
    ``rate_limited``), anything else ``upstream_error``.
    """
    argv = ["gh", "api", "-i", "--method", method.upper(), "--input", "-", path]
    try:
        code, out, err = await _run(argv, timeout=GH_TIMEOUT,
                                    stdin_data=json.dumps(dict(body), ensure_ascii=False))
    except TimeoutError as exc:
        raise _WriteFailure(
            "upstream_error",
            f"GitHub did not answer within {GH_TIMEOUT:.0f} s; nothing was written to "
            f"{subject} #{number}.") from exc
    except OSError as exc:
        raise _WriteFailure("upstream_error",
                            f"gh could not be started ({type(exc).__name__}); nothing was written.") from exc
    status, headers, text = _parse_gh_response(out)
    if status == 0:
        message = (err or out).strip()[:300]
        authish = re.search(r"auth|logged in|permission|forbidden", message, re.I)
        raise _WriteFailure(
            "forbidden" if authish else "upstream_error",
            (f"gh could not {action} {subject} #{number} on {owner}/{repo}: {message}"
             if message else f"gh exited {code} without an answer; nothing was written."),
            "gh api -> no HTTP answer")
    if 200 <= status < 300:
        return _json_or_none(text)
    remaining = headers.get("x-ratelimit-remaining")
    if status == 429 or (status == 403 and remaining is not None and _int(remaining) == 0):
        raise _WriteFailure(
            "rate_limited",
            f"GitHub's rate limit is spent for {owner}/{repo}; {subject} #{number} was not changed.",
            f"{method.upper()} {path} -> HTTP {status}")
    if status == 404:
        raise _WriteFailure(
            "not_found",
            f"{subject.capitalize()} #{number} was not found on {owner}/{repo} (HTTP 404); "
            "nothing was written.", f"{method.upper()} {path} -> HTTP 404")
    if status in (401, 403):
        raise _WriteFailure(
            "forbidden",
            f"the gh account is not allowed to {action} {subject} #{number} on {owner}/{repo} "
            f"(HTTP {status}); nothing was written.", f"{method.upper()} {path} -> HTTP {status}")
    raise _WriteFailure(
        "upstream_error",
        f"GitHub answered HTTP {status} while trying to {action} {subject} #{number} on "
        f"{owner}/{repo}; nothing was written.", f"{method.upper()} {path} -> HTTP {status}")


async def _confirm_kind(owner: str, name: str, number: int, want: str) -> None:
    """Refuse a route aimed at the wrong kind of number before it writes.

    ``/github/pr/1/comment`` on issue #1 would otherwise post to that issue's
    conversation, because both are issues to the REST API. One GET of
    ``/issues/{number}``: the ``pull_request`` key is GitHub's own answer.
    """
    client = _GitHub(owner, name, "gh", None)
    try:
        response = await client.rest(f"/repos/{owner}/{name}/issues/{number}")
    except _UpstreamError as exc:
        if exc.status == 404:
            raise _WriteFailure(
                "not_found",
                f"#{number} was not found on {owner}/{name} (HTTP 404); nothing was written.") from exc
        raise _WriteFailure(
            "upstream_error",
            f"GitHub could not confirm #{number} on {owner}/{name} "
            f"(HTTP {exc.status or 'no status'}); nothing was written.") from exc
    data = response.data if isinstance(response.data, dict) else {}
    actual = "pr" if isinstance(data.get("pull_request"), dict) else "issue"
    if actual != want:
        raise _WriteFailure(
            "not_found",
            f"#{number} on {owner}/{name} is "
            f"{'a pull request' if actual == 'pr' else 'an issue'}, not "
            f"{'a pull request' if want == 'pr' else 'an issue'}.")


async def _write_repo(path: str) -> Union[tuple[str, str], dict[str, Any]]:
    """``(owner, name)`` for a write route's folder, or the failure payload.

    A bad folder keeps the read routes' own codes (``folder_missing``,
    ``not_a_repo``, ``no_remote``), because a path that is wrong is not a GitHub
    answer and the sentence says which folder it was.
    """
    try:
        folder = _resolve_folder(path)
    except _FolderMissing as exc:
        return {"ok": False, "code": "folder_missing",
                "error": f"Folder not found: {exc.path}", "detail": exc.path}
    ctx = await _repo_context(folder)
    if ctx.code != "ok" or not ctx.owner or not ctx.repo:
        return {"ok": False, "code": ctx.code,
                "error": ctx.error or f"No GitHub remote on {folder}.",
                "detail": ctx.detail}
    return ctx.owner, ctx.repo


def _not_signed_in() -> dict[str, Any]:
    return {"ok": False, "code": "forbidden",
            "error": "GitHub writes run as the gh CLI's account and gh is not signed in. "
                     "Run gh auth login, then retry."}


async def _post_comment(number: int, path: str, payload: Mapping[str, Any],
                        kind: str) -> dict[str, Any]:
    """One comment on an issue or a pull request, as the gh account."""
    raw_body = payload.get("body")
    text = raw_body if isinstance(raw_body, str) else ""
    if not text.strip():
        # Checked before any folder or network call: an empty comment never leaves.
        return {"ok": False, "code": "empty_comment",
                "error": "A comment needs text: the body was empty or whitespace, "
                         "so nothing was sent."}
    repo = await _write_repo(path)
    if isinstance(repo, dict):
        return repo
    owner, name = repo
    if await _gh_user() is None:
        return _not_signed_in()
    subject = "pull request" if kind == "pr" else "issue"
    try:
        await _confirm_kind(owner, name, number, kind)
        data = await _gh_write("POST", f"/repos/{owner}/{name}/issues/{number}/comments",
                               {"body": text}, subject=subject, action="comment on",
                               owner=owner, repo=name, number=number)
    except _WriteFailure as exc:
        log.info("project-command-center: comment write failed: %s", exc.error)
        return _write_failure(exc)
    _cache_drop(owner, name)
    raw = data if isinstance(data, dict) else {}
    return {"ok": True, "comment": {
        "id": _int(raw.get("id")),
        **_actor_fields(raw.get("user")),
        "body": str(raw.get("body") or text),
        "created_at": raw.get("created_at"),
    }}


async def _set_state(number: int, path: str, payload: Mapping[str, Any],
                     kind: str) -> dict[str, Any]:
    """Open or close one issue or pull request, as the gh account."""
    value = payload.get("state")
    state = value.strip().lower() if isinstance(value, str) else ""
    if state not in ("open", "closed"):
        received = str(value)[:40] if value is not None else "nothing"
        return {"ok": False, "code": "bad_state",
                "error": f'state must be "open" or "closed", not {received}; nothing was changed.'}
    repo = await _write_repo(path)
    if isinstance(repo, dict):
        return repo
    owner, name = repo
    if await _gh_user() is None:
        return _not_signed_in()
    subject = "pull request" if kind == "pr" else "issue"
    action = "close" if state == "closed" else "reopen"
    try:
        await _confirm_kind(owner, name, number, kind)
        data = await _gh_write("PATCH", f"/repos/{owner}/{name}/issues/{number}",
                               {"state": state}, subject=subject, action=action,
                               owner=owner, repo=name, number=number)
    except _WriteFailure as exc:
        log.info("project-command-center: state write failed: %s", exc.error)
        return _write_failure(exc)
    _cache_drop(owner, name)
    written = data.get("state") if isinstance(data, dict) else None
    return {"ok": True, "state": str(written or state).lower()}


# --- Icons -------------------------------------------------------------------
#
# "What is the best icon for this folder", answered without asking the user: a
# fixed list of candidate paths, a magic byte sniff (never the extension), then a
# narrow remote fallback, then a stack key the Desktop maps to a glyph.
#
# Local is a fixed list first, then one small bounded walk. The fixed list keeps
# absolute priority: root files, then the usual public/static/app roots, 91 stats
# and no recursion. Only when none of those holds an image does a second step
# look for a ``public`` directory below the project root (a Vite playground, an
# apps/web layout), at most 3 levels down, 64 directories and 400 entries read,
# and never inside another repository. The walk runs in a worker thread; the
# cache stays on the event loop.

ICON_TTL = 300.0
ICON_REMOTE_TTL = 3600.0
ICON_MAX_BYTES = 96 * 1024
ICON_REMOTE_MAX_BYTES = 256 * 1024
ICON_REMOTE_TIMEOUT = 2.0
ICON_MAX_REDIRECTS = 2
ICON_MAX_ROOT_DEPTH = 3
ICON_ROOTS = (".", "public", "static", "assets", "app", "src", "resources",
              "frontend/public", "frontend/static", "frontend/app", "web/public",
              "docs", ".github")
ICON_NAMES = ("favicon.svg", "favicon.png", "favicon.ico", "logo.svg", "logo.png",
              "icon.png", "app-icon.png")
# One level down, for a Laravel app whose front end is a sibling folder.
ICON_FRONTEND_DIRS = ("frontend", "web", "client", "apps", "ui")
# The second local step: a bounded search for ``public`` directories.
ICON_PUBLIC_MAX_DEPTH = 3
ICON_WALK_MAX_DIRS = 64
ICON_WALK_MAX_ENTRIES = 400
ICON_PUBLIC_MAX = 8
ICON_PREFERRED_PARENTS = ("playground", "app", "web", "frontend", "packages")
ICON_SKIP_DIRS = frozenset({
    "node_modules", "vendor", "dist", "build", "out", ".output", ".nuxt", ".next",
    ".svelte-kit", "target", "venv", "__pycache__", "coverage", "storage", "result",
    "cache", "tmp",
})
# In the second step only: a package's own mark is often named after it.
ICON_SUFFIX_NAMES = ("-logo", "-favicon")
ICON_SUFFIX_EXTS = ("svg", "png", "ico")
ICON_KILL_FILENAME = "icons_remote.off"
ICON_UA = (f"project-command-center/{PLUGIN_VERSION} "
           "(+https://github.com/NyllRE/project-command-center)")


def _icon_mime(data: bytes) -> Optional[str]:
    """The sniffed content type, or None when *data* is not one of six images.

    The extension says nothing: an HTML error page saved as ``favicon.ico`` is
    HTML, and must fall through to the next candidate instead of becoming a
    broken ``<img>``.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if head[:5].lower() == b"<?xml" or head[:4].lower() == b"<svg":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if "<svg" in text.lower():
            return "image/svg+xml"
    return None


def _icon_label(root: str, name: str) -> str:
    return name if root == "." else f"{root}/{name}"


def _icon_paths(folder: Path):
    """The fixed candidate space: fixed roots, fixed names, in priority order."""
    for root in ICON_ROOTS:
        if len(Path(root).parts) > ICON_MAX_ROOT_DEPTH:
            continue
        base = folder if root == "." else folder / root
        for name in ICON_NAMES:
            yield base / name, _icon_label(root, name)


def _icon_data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _icon_walk_order(depth: int, path: Path) -> tuple[int, int, str]:
    """Queue order: depth, then a preferred directory name, then the path."""
    name = path.name.lower()
    try:
        rank = ICON_PREFERRED_PARENTS.index(name)
    except ValueError:
        rank = len(ICON_PREFERRED_PARENTS)
    return (depth, rank, str(path))


def _icon_rel(folder: Path, path: Path) -> str:
    try:
        return path.relative_to(folder).as_posix()
    except ValueError:
        return path.name


def _icon_public_dirs(folder: Path) -> list[Path]:
    """``public`` directories below *folder*, bounded and never inside a repo.

    Breadth first with a hard budget of 64 scanned directories and 400 entries.
    Hidden directories, symlinks, build and dependency folders, and any
    directory that holds a ``.git`` are skipped: a nested checkout is another
    project, not part of this one. The root ``public`` is left out because the
    fixed list already tests it first.
    """
    found: list[Path] = []
    queue: list[tuple[Path, int]] = [(folder, 0)]
    visited = entries = 0
    while queue and visited < ICON_WALK_MAX_DIRS and len(found) < ICON_PUBLIC_MAX:
        queue.sort(key=lambda item: _icon_walk_order(item[1], item[0]))
        current, depth = queue.pop(0)
        if depth >= ICON_PUBLIC_MAX_DEPTH:
            continue
        visited += 1
        try:
            with os.scandir(current) as scan:
                for entry in scan:
                    if entries >= ICON_WALK_MAX_ENTRIES:
                        break
                    entries += 1
                    name = entry.name
                    if name.startswith(".") or name in ICON_SKIP_DIRS:
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    child = Path(entry.path)
                    if (child / ".git").exists():
                        continue
                    if name == "public":
                        if depth + 1 > 1:
                            found.append(child)
                        continue
                    if depth + 1 < ICON_PUBLIC_MAX_DEPTH:
                        queue.append((child, depth + 1))
        except OSError:
            continue
    return found


def _icon_public_order(folder: Path, path: Path) -> tuple[int, int, str]:
    """Shallowest first, then the preferred parent, then the path alphabetically."""
    rel = _icon_rel(folder, path)
    parts = Path(rel).parts
    parent = parts[-2].lower() if len(parts) > 1 else ""
    try:
        rank = ICON_PREFERRED_PARENTS.index(parent)
    except ValueError:
        rank = len(ICON_PREFERRED_PARENTS)
    return (len(parts), rank, rel)


def _icon_suffix_names(directory: Path) -> list[str]:
    """``*-logo`` and ``*-favicon`` images already listed in one public folder."""
    names: list[str] = []
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                if len(names) >= ICON_PUBLIC_MAX:
                    break
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                stem, dot, ext = entry.name.rpartition(".")
                if not dot or ext.lower() not in ICON_SUFFIX_EXTS:
                    continue
                if stem.lower().endswith(ICON_SUFFIX_NAMES):
                    names.append(entry.name)
    except OSError:
        return []
    return sorted(names)


def _icon_scan(folder: Path, public_dirs: Sequence[str]) -> list[tuple[str, str, int]]:
    """Every candidate file that exists, in priority order: (path, label, mtime).

    Runs in a worker thread and touches only the filesystem: the cache is read
    and written by the event loop, never here.
    """
    candidates: list[tuple[Path, str]] = list(_icon_paths(folder))
    for text in public_dirs:
        base = Path(text)
        for name in ICON_NAMES:
            path = base / name
            candidates.append((path, _icon_rel(folder, path)))
        for name in _icon_suffix_names(base):
            path = base / name
            candidates.append((path, _icon_rel(folder, path)))
    out: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for path, label in candidates:
        text = str(path)
        if text in seen:
            continue
        seen.add(text)
        try:
            stat = path.stat()
        except OSError:
            continue
        if not stat.st_size or stat.st_size > ICON_MAX_BYTES:
            continue
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        out.append((text, label, stat.st_mtime_ns))
    return out


async def _icon_local(folder: Path, *, refresh: bool) -> Optional[dict[str, Any]]:
    """First local candidate that sniffs as an image, keyed by its own mtime.

    The fixed list is scanned first and the ``public`` walk second, so a root
    file always wins. Disk work happens in a worker thread; both the per-file
    cache and the negative cache are read and written here, and the discovered
    ``public`` folder list is cached for 300 s under ``icon-dirs``. ``refresh=1``
    bypasses all of them.
    """
    miss_key = ("icon-local-miss", str(folder))
    if refresh:
        # A refresh after a miss must not leave the old miss behind: otherwise
        # the next ordinary read would answer the glyph for another 300 s.
        _CACHE.pop(miss_key, None)
    elif _cache_hit(miss_key) is not None:
        return None

    dirs: list[str] = []
    dirs_key = ("icon-dirs", str(folder))
    cached_dirs = None if refresh else _cache_hit(dirs_key)
    if cached_dirs is not None and isinstance(cached_dirs.get("dirs"), list):
        dirs = [str(item) for item in cached_dirs["dirs"]]
    else:
        found = await asyncio.to_thread(_icon_public_dirs, folder)
        found.sort(key=lambda path: _icon_public_order(folder, path))
        dirs = [str(path) for path in found]
        _cache_put(dirs_key, {"dirs": dirs}, ttl=ICON_TTL)

    scanned = await asyncio.to_thread(_icon_scan, folder, dirs)
    for text, label, mtime in scanned:
        key = ("icon-local", str(folder), text, mtime)
        if not refresh:
            cached = _cache_hit(key)
            if cached is not None:
                return cached
        try:
            data = await asyncio.to_thread(Path(text).read_bytes)
        except OSError:
            continue
        if len(data) > ICON_MAX_BYTES:
            continue
        mime = _icon_mime(data)
        if mime is None:
            log.debug("icon: %s is not an image, next candidate", text)
            continue
        payload = {"ok": True, "kind": "image", "data_url": _icon_data_url(data, mime),
                   "mime": mime, "source": "local", "detail": label}
        _cache_put(key, payload, ttl=ICON_TTL)
        return payload
    if not refresh:
        _cache_put(miss_key, {}, ttl=ICON_TTL)
    return None


def _icon_stack(folder: Path) -> str:
    """A fixed key for the card's glyph: the folder's root markers, in order."""
    if (folder / "composer.json").is_file() and (folder / "artisan").is_file():
        return "laravel"
    if any(folder.glob("nuxt.config.*")):
        return "nuxt"
    if (folder / "package.json").is_file():
        return "node"
    if (folder / "pyproject.toml").is_file() or (folder / "setup.py").is_file():
        return "python"
    if (folder / "Cargo.toml").is_file():
        return "rust"
    if (folder / "go.mod").is_file():
        return "go"
    if (folder / "flake.nix").is_file() or (folder / "default.nix").is_file():
        return "nix"
    if (folder / ".obsidian").is_dir():
        return "obsidian"
    if (folder / "Dockerfile").is_file() or (folder / "docker-compose.yml").is_file():
        return "docker"
    if (folder / "mkdocs.yml").is_file() or (folder / "docs" / "conf.py").is_file():
        return "docs"
    for name in ICON_FRONTEND_DIRS:
        base = folder / name
        if any(base.glob("nuxt.config.*")):
            return "nuxt"
        if (base / "package.json").is_file():
            return "node"
    return "git"


def _icon_kill_file() -> Path:
    """``<hermes home>/plugin-data/project-command-center/icons_remote.off``."""
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME) / ICON_KILL_FILENAME


def _icon_remote_off() -> bool:
    """True when the remote fallback is switched off. Checked on every call.

    Deliberately not cached at import: creating the file takes effect on the next
    request. A data directory that cannot be resolved counts as off, the same rule
    ``hooks.py`` uses for ``auto_link.off``: a fetch whose kill switch cannot be
    proven absent must not happen.
    """
    try:
        return _icon_kill_file().exists()
    except Exception:
        log.debug("icon: kill file unreadable, treating remote lookup as off", exc_info=True)
        return True


def _icon_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _icon_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python without tomllib: declared TOML URLs are skipped
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _icon_homepage(folder: Path) -> Optional[str]:
    """The homepage a project declares about itself, or None.

    Four manifests, no guessing: composer.json / package.json ``homepage``,
    ``[project.urls] Homepage|Documentation`` in pyproject.toml, and
    ``[package] homepage`` in Cargo.toml.
    """
    for name in ("composer.json", "package.json"):
        value = _icon_json(folder / name).get("homepage")
        if isinstance(value, dict):
            value = value.get("url")
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            return value.strip()
    project = _icon_toml(folder / "pyproject.toml").get("project")
    urls = project.get("urls") if isinstance(project, dict) else None
    if isinstance(urls, dict):
        for wanted in ("homepage", "documentation"):
            for key, value in urls.items():
                if str(key).lower() == wanted and isinstance(value, str) \
                        and value.strip().startswith(("http://", "https://")):
                    return value.strip()
    package = _icon_toml(folder / "Cargo.toml").get("package")
    value = package.get("homepage") if isinstance(package, dict) else None
    if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
        return value.strip()
    return None


def _icon_home_urls(homepage: str) -> list[str]:
    """``https://<host>/favicon.ico`` then ``.png``; the declared scheme and path are ignored."""
    try:
        parts = urlsplit(homepage)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return []
    if not host:
        return []
    origin = f"https://{host}" + (f":{port}" if port else "")
    return [f"{origin}/favicon.ico", f"{origin}/favicon.png"]


def _icon_ip_public(ip: Any) -> bool:
    """Only globally routable addresses: no loopback, RFC1918, link local, CGNAT, ULA."""
    if getattr(ip, "version", None) == 6 and ip.ipv4_mapped is not None:
        return bool(ip.ipv4_mapped.is_global)
    return bool(ip.is_global)


async def _icon_host_allowed(host: str, deadline: float) -> tuple[bool, str]:
    """Resolve *host* and refuse it unless every address is public.

    Resolution happens here, before httpx opens a socket, so a name that points at
    127.0.0.1 or a link local metadata address is rejected rather than connected.
    """
    name = (host or "").strip().strip(".").lower()
    if not name or len(name) > 253:
        return False, "empty or overlong host"
    if name == "localhost" or name.endswith(".localhost"):
        return False, "localhost is refused"
    try:
        literal = ipaddress.ip_address(name)
    except ValueError:
        literal = None
    if literal is not None:
        if _icon_ip_public(literal):
            return True, f"{literal} is public"
        return False, f"{literal} is not a public address"
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False, "out of time before resolution"
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, name, 443, 0, socket.SOCK_STREAM),
            timeout=remaining)
    except asyncio.TimeoutError:
        return False, f"{name} did not resolve within the timeout"
    except (OSError, UnicodeError, ValueError):
        return False, f"{name} does not resolve"
    if not infos:
        return False, f"{name} does not resolve"
    for info in infos:
        text = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            return False, f"{name} resolved to an unparseable address"
        if not _icon_ip_public(ip):
            return False, f"{name} resolves to {text}, which is not a public address"
    return True, f"{name} resolves to a public address"


async def _icon_url_allowed(url: str, deadline: float) -> tuple[bool, str]:
    """https only, no userinfo, public host; every redirect hop goes through here again."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False, "unparseable url"
    if parts.scheme.lower() != "https":
        return False, f"{parts.scheme or 'no'} scheme is refused, only https is fetched"
    if parts.username or parts.password:
        return False, "credentials in the url are refused"
    if port not in (None, 443):
        return False, f"port {port} is refused, only the default https port is fetched"
    return await _icon_host_allowed(parts.hostname or "", deadline)


async def _icon_fetch(url: str, deadline: float) -> Optional[dict[str, Any]]:
    """One image over https, at most ICON_MAX_REDIRECTS redirects, 256 KB, sniffed.

    No cookies, no auth header, no proxy from the environment: the URL came out of
    repository content, so the request carries exactly a UA and an Accept.
    """
    current = url
    for hop in range(ICON_MAX_REDIRECTS + 1):
        allowed, reason = await _icon_url_allowed(current, deadline)
        if not allowed:
            log.info("icon: refused %s: %s", current, reason)
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        headers = {"user-agent": ICON_UA, "accept": "image/*"}
        try:
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                         timeout=httpx.Timeout(remaining),
                                         headers=headers) as client:
                async with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location or hop == ICON_MAX_REDIRECTS:
                            return None
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        return None
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > ICON_REMOTE_MAX_BYTES or time.monotonic() > deadline:
                            return None
        except (httpx.HTTPError, OSError, ValueError):
            return None
        mime = _icon_mime(bytes(data))
        if mime is None:
            return None
        return {"ok": True, "kind": "image", "data_url": _icon_data_url(bytes(data), mime),
                "mime": mime, "source": "remote", "detail": current}
    return None


async def _icon_remote(folder: Path, *, refresh: bool) -> Optional[dict[str, Any]]:
    """Declared homepage first, then the GitHub owner avatar. One 2 s budget."""
    deadline = time.monotonic() + ICON_REMOTE_TIMEOUT
    candidates: list[tuple[str, str]] = []
    homepage = _icon_homepage(folder)
    if homepage:
        candidates.extend((url, "homepage") for url in _icon_home_urls(homepage))
    try:
        root = await _work_tree(folder)
        remote = await _primary_remote(root) if root else ""
    except (TimeoutError, _GitError):
        remote = ""
    owner, _repo = _parse_remote(remote)
    if owner:
        candidates.append((f"https://github.com/{owner}.png?size=64", "avatar"))
    for url, source in candidates:
        key = ("icon-remote", str(folder), source, url)
        if not refresh:
            cached = _cache_hit(key)
            if cached is not None:
                return cached
        result = await _icon_fetch(url, deadline)
        if result is not None:
            _cache_put(key, result, ttl=ICON_REMOTE_TTL)
            return result
        if time.monotonic() >= deadline:
            break
    return None


async def _icon_answer(folder: Path, *, refresh: bool) -> dict[str, Any]:
    """Local image, else remote image, else the stack glyph. Never raises for a folder."""
    local = await _icon_local(folder, refresh=refresh)
    if local is not None:
        return local
    glyph_key = ("icon-glyph", str(folder))
    if not refresh:
        cached = _cache_hit(glyph_key)
        if cached is not None:
            return cached
    if not _icon_remote_off():
        remote = await _icon_remote(folder, refresh=refresh)
        if remote is not None:
            return remote
    payload = {"ok": True, "kind": "glyph", "stack": _icon_stack(folder), "source": "stack"}
    _cache_put(glyph_key, payload, ttl=ICON_TTL)
    return payload


# --- Branch switch -----------------------------------------------------------
#
# The one route that runs a mutating git command. It refuses every state where a
# switch would lose work or surprise another checkout, and it never stashes,
# never discards and never forces. The branch name is validated by git itself
# and must resolve to an existing ref before it reaches `git switch`; the folder
# is untrusted input like every other path here.

_SWITCH_MARKERS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG",
                   "rebase-merge", "rebase-apply", "sequencer")


def _switch_refusal(code: str, error: str, detail: Optional[str] = None,
                    **extra: Any) -> dict[str, Any]:
    return {"ok": False, "code": code, "error": error, "detail": detail, **extra}


def _git_line(message: str) -> str:
    for line in (message or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return "git switch failed"


def _worktree_rows(listing: str) -> list[tuple[str, str]]:
    """(branch ref, worktree path) rows from ``git worktree list --porcelain``."""
    rows: list[tuple[str, str]] = []
    path: Optional[str] = None
    for line in listing.splitlines():
        if not line:
            path = None
        elif line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch ") and path:
            rows.append((line[len("branch "):], path))
    return rows


async def _switch_operation(root: str) -> Optional[str]:
    """The marker of an in-progress merge, rebase, cherry-pick, revert or bisect."""
    argv = ["git", "-C", root, "rev-parse"]
    for marker in _SWITCH_MARKERS:
        argv += ["--git-path", marker]
    code, out, _err = await _run(argv, timeout=GIT_TIMEOUT)
    if code != 0:
        return None
    for marker, line in zip(_SWITCH_MARKERS, out.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        if not os.path.isabs(candidate):
            candidate = os.path.join(root, candidate)
        if os.path.exists(candidate):
            return marker
    return None


async def _switch_head_on_a_branch(root: str) -> bool:
    """True when HEAD is contained by at least one local or remote branch."""
    code, out, _err = await _run(
        ["git", "-C", root, "for-each-ref", "--count=1", "--contains", "HEAD",
         "--format=%(refname)", "refs/heads", "refs/remotes"],
        timeout=GIT_TIMEOUT)
    return code == 0 and bool(out.strip())


@router.post("/branch/switch")
async def branch_switch(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Switch the main checkout to *branch*, behind guards, with a fixed argv.

    ``{"path": folder, "branch": name}`` answers ``{"ok": true, "code":
    "switched"|"already_on", ...}`` or ``{"ok": false, "code": ...}`` with one of
    ``folder_missing``, ``not_a_repo``, ``inside_root``, ``not_main_worktree``,
    ``operation_in_progress``, ``detached_unreachable``, ``dirty_tree``,
    ``unknown_branch``, ``checked_out_elsewhere``, ``bad_branch``,
    ``switch_failed`` or ``upstream_error``. Untracked files alone are allowed
    and reported in ``warning``. HTTP 200 for every refusal; only a malformed
    path is a 400.
    """
    branch = str(payload.get("branch") or "").strip()
    raw_path = str(payload.get("path") or "").strip()
    try:
        folder = _resolve_folder(raw_path)
    except _FolderMissing as exc:
        return _switch_refusal("folder_missing", f"Folder not found: {exc.path}", exc.path)
    if not branch:
        return _switch_refusal("bad_branch", "The branch name is empty.")

    try:
        root = await _work_tree(folder)
    except TimeoutError as exc:
        return _switch_refusal("upstream_error", f"git did not answer for {folder}.",
                               str(exc))
    if not root:
        return _switch_refusal("not_a_repo", f"{folder} is not inside a git work tree.",
                               f"git -C {folder} rev-parse --show-toplevel")
    if os.path.realpath(root) != str(folder):
        return _switch_refusal("inside_root", f"{folder} is inside the repository at {root}.",
                               root)

    head: Optional[str] = None
    previous_sha: Optional[str] = None
    dirty = _dirty_from_status("")
    local_code = 1
    try:
        code, _out, _err = await _run(
            ["git", "-C", root, "check-ref-format", "--branch", branch], timeout=GIT_TIMEOUT)
        if code != 0:
            return _switch_refusal("bad_branch", "The branch name is not valid.")

        git_dir = (await _git(root, "rev-parse", "--absolute-git-dir")).strip()
        common_dir = (await _git(root, "rev-parse", "--git-common-dir")).strip()
        if not os.path.isabs(common_dir):
            common_dir = os.path.join(root, common_dir)
        if os.path.realpath(git_dir) != os.path.realpath(common_dir):
            return _switch_refusal(
                "not_main_worktree",
                "This folder is a linked worktree, not the main checkout.",
                f"git dir {git_dir}, common dir {common_dir}")

        marker = await _switch_operation(root)
        if marker:
            return _switch_refusal(
                "operation_in_progress",
                "A merge, rebase or other git operation is in progress.",
                marker)

        try:
            head = (await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")).strip()
        except _GitError:
            head = None
        if not head:
            previous_sha = (await _git(root, "rev-parse", "--short", "HEAD")).strip()
            if not await _switch_head_on_a_branch(root):
                return _switch_refusal(
                    "detached_unreachable",
                    "HEAD is detached and its commit is on no branch.",
                    previous_sha)
        elif head == branch:
            return {"ok": True, "code": "already_on", "error": None, "detail": None,
                    "path": str(folder), "branch": branch, "previous_branch": head,
                    "detached": False, "warning": None}

        porcelain = await _git(root, "status", "--porcelain")
        dirty = _dirty_from_status(porcelain)
        if dirty["staged"] or dirty["unstaged"]:
            return _switch_refusal(
                "dirty_tree",
                "Tracked files have changes. Commit or discard them first.",
                f"{dirty['staged']} staged, {dirty['unstaged']} unstaged",
                dirty=dirty)

        local_code, _local_out, _local_err = await _run(
            ["git", "-C", root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            timeout=GIT_TIMEOUT)
        remote_code = 1
        if local_code != 0:
            remote_code, _remote_out, _remote_err = await _run(
                ["git", "-C", root, "show-ref", "--verify", "--quiet",
                 f"refs/remotes/origin/{branch}"], timeout=GIT_TIMEOUT)
        if local_code != 0 and remote_code != 0:
            return _switch_refusal("unknown_branch",
                                   f"The branch {branch} is not in this clone.",
                                   "git show-ref refs/heads and refs/remotes/origin")

        if local_code == 0:
            listing = await _git(root, "worktree", "list", "--porcelain")
            for ref, path in _worktree_rows(listing):
                if ref == f"refs/heads/{branch}" and os.path.realpath(path) != os.path.realpath(root):
                    return _switch_refusal(
                        "checked_out_elsewhere",
                        f"The branch is checked out in another worktree: {path}",
                        path, worktree=path)

        argv = (["git", "-C", root, "switch", branch] if local_code == 0 else
                ["git", "-C", root, "switch", "--track", f"origin/{branch}"])
        code, _out, err = await _run(argv, timeout=SWITCH_TIMEOUT)
        if code != 0:
            return _switch_refusal("switch_failed", _git_line(err),
                                   "git switch exited non-zero", branch=branch)
        now = (await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")).strip()
        if now != branch:
            return _switch_refusal("switch_failed", "The folder did not move to the branch.",
                                   f"HEAD is on {now or 'a detached commit'}")
    except TimeoutError as exc:
        return _switch_refusal("upstream_error", f"git did not answer for {folder}.", str(exc))
    except _GitError as exc:
        return _switch_refusal("upstream_error", f"git could not read {folder}.", str(exc))

    _CACHE.pop(("repo", root), None)
    _CACHE.pop(("repo", str(folder)), None)
    warning = (f"{dirty['untracked']} untracked file(s) stay where they are."
               if dirty["untracked"] else None)
    return {"ok": True, "code": "switched", "error": None, "detail": None,
            "path": str(folder), "branch": branch, "previous_branch": head,
            "previous_sha": previous_sha, "detached": False,
            "tracked": None if local_code == 0 else f"origin/{branch}",
            "warning": warning, "dirty": dirty}


# --- Worktrees ---------------------------------------------------------------
#
# The Desktop half used to reach core's preload bridge for these two calls, which
# a community plugin may not do. The same git, and the same guards, live here
# instead: a fixed argv, the child env allowlist and `_switch_refusal` shapes, so
# the renderer only needs ctx.rest.

def _sanitize_branch(name: str) -> str:
    """A git-ref-safe branch name, or "" when nothing usable is left.

    The rules are core's ``sanitizeBranch`` in the Desktop worktree module: spaces
    become ``-``, anything outside ``[A-Za-z0-9_./-]`` goes, runs of ``-``, ``/``
    and ``.`` collapse, and leading/trailing punctuation is trimmed.
    """
    text = re.sub(r"\s+", "-", str(name or ""))
    text = re.sub(r"[^A-Za-z0-9_./-]", "", text)
    text = re.sub(r"-{2,}", "-", text)
    text = re.sub(r"/{2,}", "/", text)
    text = re.sub(r"\.{2,}", ".", text)
    return re.sub(r"^[-./]+|[-./]+$", "", text)


def _slugify(name: str) -> str:
    """The ``.worktrees/<slug>`` directory name for *name*, or "" when empty.

    Core's ``slugify`` lower-cases, folds anything outside ``[a-z0-9]`` into ``-``
    and stops at 40 characters. Core falls back to ``work`` for an empty result;
    here an empty slug is a refusal instead, so a name of only punctuation never
    lands in the same directory as a real one.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower())
    return slug.strip("-")[:40].rstrip("-")


def _worktree_entries(listing: str) -> list[dict[str, Any]]:
    """Porcelain records: path, short branch (None when detached), detached, locked.

    Records are separated by a blank line, and the first one is the main worktree,
    which is the only way to name it: ``rev-parse --show-toplevel`` answers with the
    linked worktree when it runs inside one.
    """
    rows: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for line in listing.splitlines():
        if not line.strip():
            if current:
                rows.append(current)
                current = None
            continue
        if line.startswith("worktree "):
            if current:
                rows.append(current)
            current = {"path": line[len("worktree "):].strip(), "branch": None,
                       "detached": False, "locked": False}
        elif current is None:
            continue
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].strip().removeprefix("refs/heads/")
        elif line.strip() == "detached":
            current["detached"] = True
        elif line.startswith("locked"):
            current["locked"] = True
    if current:
        rows.append(current)
    return rows


async def _main_worktree_root(root: str) -> Optional[str]:
    """The main checkout's path: the first record of ``git worktree list --porcelain``."""
    entries = _worktree_entries(await _git(root, "worktree", "list", "--porcelain"))
    return entries[0]["path"] if entries else None


async def _ref_resolves(root: str, ref: str) -> bool:
    """True when *ref* names a commit in this repository."""
    if not ref:
        return False
    code, _out, _err = await _run(
        ["git", "-C", root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        timeout=GIT_TIMEOUT)
    return code == 0


@router.get("/worktrees")
async def worktrees(
        path: str = Query(..., description="Absolute path to a folder inside the repository"),
) -> dict[str, Any]:
    """Every worktree of the repository containing *path*, main one first.

    Answers ``{"ok": true, "worktrees": [{"path", "branch", "isMain", "detached",
    "locked"}]}`` or ``{"ok": false, "code": "folder_missing" | "not_a_repo",
    "error", "detail"}``. HTTP 200 for every refusal; only a malformed path is a 400.
    """
    try:
        folder = _resolve_folder(path)
    except _FolderMissing as exc:
        return _switch_refusal("folder_missing", f"Folder not found: {exc.path}", exc.path)
    try:
        listing = await _git(str(folder), "worktree", "list", "--porcelain")
    except TimeoutError as exc:
        return _switch_refusal("upstream_error", f"git did not answer for {folder}.", str(exc))
    except _GitError as exc:
        return _switch_refusal("not_a_repo", f"{folder} is not inside a git work tree.",
                               str(exc))
    entries = _worktree_entries(listing)
    if not entries:
        return _switch_refusal("not_a_repo", f"{folder} is not inside a git work tree.",
                               f"git -C {folder} worktree list --porcelain")
    return {"ok": True, "worktrees": [
        {"path": entry["path"], "branch": entry["branch"], "isMain": index == 0,
         "detached": entry["detached"], "locked": entry["locked"]}
        for index, entry in enumerate(entries)]}


@router.post("/worktree")
async def create_worktree(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Create a worktree at ``<mainRoot>/.worktrees/<slug>``, with a fixed argv.

    ``{"path": folder, "branch": name, "name": slug?, "base": ref?}`` answers
    ``{"ok": true, "path", "branch"}`` or ``{"ok": false, "code": ...}`` with one of
    ``folder_missing``, ``not_a_repo``, ``bad_branch``, ``bad_slug``,
    ``worktree_exists``, ``branch_checked_out``, ``unknown_base``,
    ``worktree_failed`` or ``upstream_error``. An existing local branch is checked
    out as it is; otherwise a branch is made from ``base``, defaulting to
    ``origin/<branch>`` only when that ref resolves. Nothing is forced, nothing is
    reused and nothing is removed. HTTP 200 for every refusal; only a malformed
    path is a 400.
    """
    raw_path = str(payload.get("path") or "").strip()
    try:
        folder = _resolve_folder(raw_path)
    except _FolderMissing as exc:
        return _switch_refusal("folder_missing", f"Folder not found: {exc.path}", exc.path)

    branch = _sanitize_branch(str(payload.get("branch") or payload.get("name") or ""))
    if not branch:
        return _switch_refusal("bad_branch", "The branch name is empty after sanitizing.")
    slug = _slugify(str(payload.get("name") or branch))
    if not slug:
        return _switch_refusal("bad_slug", "The worktree name is empty after sanitizing.")

    root: Optional[str] = None
    try:
        root = await _main_worktree_root(str(folder))
    except TimeoutError as exc:
        return _switch_refusal("upstream_error", f"git did not answer for {folder}.", str(exc))
    except _GitError as exc:
        return _switch_refusal("not_a_repo", f"{folder} is not inside a git work tree.",
                               str(exc))
    if not root:
        return _switch_refusal("not_a_repo", f"{folder} is not inside a git work tree.",
                               f"git -C {folder} worktree list --porcelain")

    target = Path(root) / ".worktrees" / slug
    try:
        code, _out, _err = await _run(
            ["git", "-C", root, "check-ref-format", "--branch", branch], timeout=GIT_TIMEOUT)
        if code != 0:
            return _switch_refusal("bad_branch", "The branch name is not valid.", branch)

        if target.exists() or target.is_symlink():
            return _switch_refusal(
                "worktree_exists", f"A worktree directory already exists at {target}.",
                str(target))

        listing = await _git(root, "worktree", "list", "--porcelain")
        for entry in _worktree_entries(listing):
            if entry["branch"] == branch:
                return _switch_refusal(
                    "branch_checked_out",
                    f"The branch is checked out in another worktree: {entry['path']}",
                    entry["path"], worktree=entry["path"])

        local_code, _local_out, _local_err = await _run(
            ["git", "-C", root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            timeout=GIT_TIMEOUT)
        if local_code == 0:
            argv = ["git", "-C", root, "worktree", "add", str(target), branch]
        else:
            base = _sanitize_branch(str(payload.get("base") or ""))
            if not base and await _ref_resolves(root, f"origin/{branch}"):
                base = f"origin/{branch}"
            if not await _ref_resolves(root, base):
                return _switch_refusal(
                    "unknown_base",
                    f"The branch {branch} is not in this clone and no base ref resolves.",
                    base or f"origin/{branch}")
            argv = ["git", "-C", root, "worktree", "add", "-b", branch, str(target), base]

        code, _out, err = await _run(argv, timeout=WORKTREE_TIMEOUT)
        if code != 0:
            return _switch_refusal("worktree_failed", _git_line(err),
                                   "git worktree add exited non-zero", branch=branch)
        if not target.is_dir():
            return _switch_refusal("worktree_failed", "git did not create the worktree.",
                                   str(target))
    except TimeoutError as exc:
        return _switch_refusal("upstream_error", f"git did not answer for {folder}.", str(exc))
    except _GitError as exc:
        return _switch_refusal("upstream_error", f"git could not read {folder}.", str(exc))

    _CACHE.pop(("repo", root), None)
    _CACHE.pop(("repo", str(folder)), None)
    return {"ok": True, "path": str(target), "branch": branch}


# --- Routes -----------------------------------------------------------------

@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus what the GitHub side can do, without echoing a secret."""
    token = _secret_token()
    gh_available = shutil.which("gh") is not None
    gh_user = await _gh_user() if gh_available else None
    return {
        "ok": True,
        "version": PLUGIN_VERSION,
        "gh": {"available": gh_available, "authenticated": bool(gh_user), "user": gh_user},
        "token": bool(token),
        "isolation_note": ISOLATION_NOTE,
    }


@router.get("/repo")
async def repo(path: str = Query(..., description="Absolute path to the project folder")) -> dict[str, Any]:
    """Git state for one folder: work tree, remote, branch, dirty counts, branches.

    ``root_matches_path`` says the folder is exactly the work tree top level, and
    ``inside_root`` carries the real root when the folder sits inside a bigger
    tree (a vault subfolder, say). A missing folder or a folder outside git is a
    code with HTTP 200; only a malformed path is a 400.
    """
    try:
        folder = _resolve_folder(path)
    except _FolderMissing as exc:
        return _repo_failure(exc.path, "folder_missing", f"Folder not found: {exc.path}", exc.path)

    root: Optional[str] = None
    error: Optional[str] = None
    try:
        root = await _work_tree(folder)
    except TimeoutError as exc:
        error = str(exc)
    key = ("repo", root or str(folder))
    cached_entry = _CACHE.get(key)
    if cached_entry is not None and cached_entry.expires >= time.monotonic():
        # Same shape as a cold read: this endpoint has no "cached" field. The two
        # folder relative fields are recomputed, because one cached root can be
        # read through several folders (a folder and a subfolder of it).
        inside = cached_entry.payload.get("root")
        inside_root = inside if inside and os.path.realpath(inside) != str(folder) else None
        return {**cached_entry.payload, "path": str(folder),
                "root_matches_path": bool(inside) and inside_root is None,
                "inside_root": inside_root}
    payload = await _repo_payload(folder, root, error)
    _cache_put(key, payload, ttl=REPO_TTL)
    return payload


@router.get("/git/graph")
async def git_graph(path: str = Query(..., description="Absolute path to the project folder"),
                    limit: int = Query(200),
                    refresh: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
    """The local commit DAG for one folder: rows newest first, refs, and their track.

    ``limit`` clamps to 50..500. The refs and HEAD reads run on every call and
    form a fingerprint; the log is read only when that fingerprint changes or
    the 300 s entry has expired. Tags are labels on a commit and never a lane.

    ``head`` names the checked out branch (or the sha when detached),
    ``default_branch`` is ``origin/main`` style from the remote HEAD symref, and
    ``refs_outside`` counts refs pointing past the window. Failures are codes
    with HTTP 200: a folder that is gone, outside git, or has no commits yet.
    """
    try:
        folder = _resolve_folder(path)
    except _FolderMissing as exc:
        return _graph_failure(exc.path, "folder_missing", f"Folder not found: {exc.path}",
                              exc.path)
    limit = max(GRAPH_MIN_LIMIT, min(GRAPH_MAX_LIMIT, limit))
    try:
        root, code, error, detail = await _history_root(folder)
    except TimeoutError as exc:
        return _graph_failure(str(folder), "upstream_error",
                              f"Git could not read {folder} in time.", str(exc))
    if not root:
        return _graph_failure(str(folder), code, error or f"Git could not read {folder}.",
                              detail)
    try:
        head_sha, branch_ref, fingerprint, probe_refs = await _graph_probe(root)
    except TimeoutError as exc:
        return _graph_failure(str(folder), "upstream_error",
                              f"Git could not read the history of {root} in time.", str(exc))
    except _GitError as exc:
        return _graph_failure(str(folder), "upstream_error",
                              f"Git could not read the history of {root}.", str(exc))
    if not probe_refs.strip() and not head_sha:
        return _graph_failure(str(folder), "empty_repo", "No commits yet.",
                              f"git -C {root} for-each-ref")

    key = ("git-graph", root, fingerprint, limit)
    if not refresh:
        hit = _lru_get(_GRAPH_CACHE, key)
        if hit is not None:
            return hit
    async with _graph_lock((root, limit)):
        if not refresh:
            hit = _lru_get(_GRAPH_CACHE, key)
            if hit is not None:
                return hit
        try:
            payload = await _graph_read(root, limit, head_sha, branch_ref, fingerprint)
        except TimeoutError as exc:
            return _graph_failure(str(folder), "upstream_error",
                                  f"Git could not read the history of {root} in time.",
                                  str(exc), limit=limit, root=root)
        except _GitError as exc:
            return _graph_failure(str(folder), "upstream_error",
                                  f"Git could not read the history of {root}.", str(exc),
                                  limit=limit, root=root)
        _lru_put(_GRAPH_CACHE, key, payload, ttl=GRAPH_TTL, size=MAX_GRAPH_ENTRIES)
        return payload


@router.get("/git/commit/{sha}")
async def git_commit(sha: str,
                     path: str = Query(..., description="Absolute path to the project folder"),
                     refresh: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
    """One commit: message, numstat totals and a unified diff against the first parent.

    ``sha`` must be 7 to 40 hex characters and is resolved with
    ``rev-parse`` before any other argv sees it, so a blob or an unknown id is
    ``not_found`` and never reaches ``show``. The diff runs with
    ``--no-ext-diff --no-textconv --no-show-signature`` because the folder's own
    config can name a diff driver, a textconv command or a gpg program.

    The patch is capped while it is read: 512 KiB, 6,000 lines, 300 files. Past
    a cap the remaining files are still listed with ``hunks: []`` and
    ``truncated_reason`` says which cap was hit. Merges are diffed against the
    first parent; a root commit is diffed against the empty tree.
    """
    if not _SHA_RE.match(sha or ""):
        return _commit_failure(sha, "bad_sha",
                               "That commit id is not valid. Use 7 to 40 hex characters.",
                               "git rev-parse was not called")
    try:
        folder = _resolve_folder(path)
    except _FolderMissing as exc:
        return _commit_failure(sha, "folder_missing", f"Folder not found: {exc.path}", exc.path)
    try:
        root, code, error, detail = await _history_root(folder)
    except TimeoutError as exc:
        return _commit_failure(sha, "upstream_error",
                               f"Git could not read {folder} in time.", str(exc))
    if not root:
        return _commit_failure(sha, code, error or f"Git could not read {folder}.", detail)

    try:
        full_code, full_out, full_err = await _run(
            ["git", "-C", root, "rev-parse", "--verify", "--quiet", "--end-of-options",
             f"{sha}^{{commit}}"], timeout=GIT_TIMEOUT)
    except TimeoutError as exc:
        return _commit_failure(sha, "upstream_error",
                               f"Git could not resolve {sha} in time.", str(exc))
    full = full_out.strip()
    if full_code != 0 or not full:
        return _commit_failure(sha, "not_found", f"No commit {sha} in this repository.",
                               full_err.strip() or f"git -C {root} rev-parse {sha}^{{commit}}")

    key = (root, full)
    if not refresh:
        hit = _lru_get(_DIFF_CACHE, key)
        if hit is not None:
            return hit
    try:
        payload = await _commit_read(root, full)
    except TimeoutError as exc:
        return _commit_failure(sha, "upstream_error",
                               f"The diff for {full[:7]} took too long to read.", str(exc))
    except _GitError as exc:
        return _commit_failure(sha, "upstream_error",
                               f"The diff for {full[:7]} could not be read.", str(exc))
    _lru_put(_DIFF_CACHE, key, payload, ttl=COMMIT_TTL, size=MAX_DIFF_ENTRIES)
    return payload


@router.get("/github")
async def github(path: str = Query(..., description="Absolute path to the project folder"),
                 state: str = Query("all", pattern="^(open|closed|all)$"),
                 limit: int = Query(50, ge=1, le=100),
                 refresh: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
    """Issues and PRs for the folder's GitHub remote, with resolved issue links.

    ``state`` defaults to ``all``: one Desktop read carries the open half and the
    closed half, so the Open / Closed / Merged / Draft filters never ask again.
    ``limit`` caps each list per state, so ``all`` returns up to ``2 * limit``
    rows split between open and closed; ``open`` and ``closed`` keep the old
    meaning exactly (one list, at most ``limit`` rows).

    Every issue row carries ``state`` and, once closed, ``state_reason``
    (completed, not_planned, duplicate) as GitHub reports it. Every PR row
    carries ``state``, ``draft`` and ``merged``, and a merged PR stays in the
    closed half. ``issues`` excludes pull requests (GitHub returns PRs from the
    issues endpoint). ``prs[].head_ref`` is the key the Desktop half joins
    sessions on, so it is returned verbatim. ``counts`` is the aggregate block
    for the charts and always counts open rows only. ``prs[].enriched`` marks
    the rows carrying the GraphQL pass (review decision, checks rollup, merge
    state): the most recently updated MAX_PRS_ENRICHED rows.

    Every payload carries ``ok``, ``code``, ``error`` and ``detail``; a rejected
    token is retried through the gh CLI and ``auth``/``auth_note`` say which
    credential answered and why it changed.
    """
    try:
        folder = _resolve_folder(path)
    except _FolderMissing as exc:
        return _empty_board(code="folder_missing", error=f"Folder not found: {exc.path}",
                            detail=exc.path)
    ctx = await _repo_context(folder)
    if ctx.code != "ok" or not ctx.owner or not ctx.repo:
        return _empty_board(owner=ctx.owner, repo=ctx.repo, code=ctx.code,
                            error=ctx.error, detail=ctx.detail)
    owner, name = ctx.owner, ctx.repo
    url = f"https://github.com/{owner}/{name}"

    mode, token = await _auth()
    key = ("github", owner.lower(), name.lower(), state, limit, mode)
    stale = None
    if not refresh:
        if (hit := _cache_hit(key)) is not None:
            return hit
        stale = _CACHE.get(key)

    gh = _GhReader(owner, name, mode, token)
    board = f"/repos/{owner}/{name}"
    # GitHub cannot return open and closed while keeping a bounded open window,
    # so `all` is two reads of each resource: open first (the same window a plain
    # `state=open` read gives), then the closed half. That is still one request
    # from the Desktop and one cache entry, never one per filter click.
    reads = ("open", "closed") if state == "all" else (state,)
    issues_raw: list[dict[str, Any]] = []
    pulls_raw: list[dict[str, Any]] = []
    headers: Mapping[str, str] = {}
    try:
        for index, read_state in enumerate(reads):
            # A conditional read is only honest for a single-state fetch: with
            # two reads a 304 on one half would not describe the other.
            etag = stale.etag if (stale is not None and len(reads) == 1) else None
            # /issues also returns pull requests, so fetch a full page and slice
            # the filtered result: asking for 50 issues should yield up to 50.
            issues_response = await gh.rest(
                f"{board}/issues",
                {"state": read_state, "per_page": 100, "sort": "updated", "direction": "desc"},
                etag=etag,
            )
            if issues_response.status == 304 and stale is not None:
                payload = {**stale.payload, "cached": True}
                _cache_put(key, payload, etag=stale.etag)
                return payload
            pulls_response = await gh.rest(
                f"{board}/pulls", {"state": read_state, "per_page": limit,
                                   "sort": "updated", "direction": "desc"})
            if index == 0:
                headers = issues_response.headers
            issues_raw.extend(i for i in (issues_response.data
                                          if isinstance(issues_response.data, list) else [])
                              if isinstance(i, dict) and "pull_request" not in i)
            pulls_raw.extend(p for p in (pulls_response.data
                                         if isinstance(pulls_response.data, list) else [])
                             if isinstance(p, dict))
        issues_window = _cap_states(_state_sort(issues_raw), limit)
        pulls_window = _cap_states(_state_sort(pulls_raw), limit)
        enriched = await _enrich_pulls(gh, [_int(p.get("number")) for p in pulls_window])
    except _UpstreamError as exc:
        account = await _account_name(gh, token)
        log.warning("project-command-center: GitHub read failed for %s/%s: %s", owner, name, exc)
        return _empty_board(owner=owner, repo=name, url=url, code=exc.code,
                            error=_github_sentence(exc, owner, name, account),
                            detail=exc.detail, auth=gh.auth, auth_note=gh.auth_note,
                            auth_account=account)

    issues = [_issue_row(i) for i in issues_window]
    pulls = [_pull_row(p, enriched.get(_int(p.get("number")))) for p in pulls_window]
    fetched_at = _now_iso()
    account = await _account_name(gh, token) if gh.auth_note else None
    payload = {
        "ok": True, "code": "ok", "error": None, "detail": None,
        "repo": name, "owner": owner, "url": url, "fetched_at": fetched_at, "cached": False,
        "auth": gh.auth, "auth_note": gh.auth_note, "auth_account": account,
        "rate_limit": _rate_limit(headers),
        "states": list(reads),
        "issues": issues, "prs": pulls, "counts": _counts(issues, pulls),
        "updated_at": fetched_at,
    }
    _cache_put(key, payload, etag=headers.get("etag"))
    return payload


@router.get("/github/pr/{number}")
async def github_pr(number: int, path: str = Query(..., description="Absolute path to the project folder"),
                    refresh: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
    """One pull request: body, changed file names, reviews, resolved issue links."""
    try:
        folder = _resolve_folder(path)
    except _FolderMissing as exc:
        return _item_failure(number, "folder_missing", f"Folder not found: {exc.path}", exc.path)
    ctx = await _repo_context(folder)
    if ctx.code != "ok" or not ctx.owner or not ctx.repo:
        return _item_failure(number, ctx.code, ctx.error, ctx.detail,
                             owner=ctx.owner, repo=ctx.repo)
    owner, name = ctx.owner, ctx.repo
    mode, token = await _auth()
    key = ("pr", owner.lower(), name.lower(), number, mode)
    if not refresh and (hit := _cache_hit(key)) is not None:
        return hit

    gh = _GhReader(owner, name, mode, token)
    base = f"/repos/{owner}/{name}/pulls/{number}"
    try:
        detail = await gh.rest(base)
    except _UpstreamError as exc:
        account = await _account_name(gh, token)
        return _item_failure(number, exc.code, _github_sentence(exc, owner, name, account),
                             exc.detail, owner=owner, repo=name, auth=gh.auth,
                             auth_note=gh.auth_note, auth_account=account)
    raw = detail.data if isinstance(detail.data, dict) else {}

    files_response, reviews_response, timeline_response, comments_response = await asyncio.gather(
        _quiet(gh.rest(f"{base}/files", {"per_page": 100})),
        _quiet(gh.rest(f"{base}/reviews", {"per_page": 100})),
        _quiet(gh.rest(f"/repos/{owner}/{name}/issues/{number}/timeline", {"per_page": 100})),
        _quiet(gh.rest(f"/repos/{owner}/{name}/issues/{number}/comments",
                       {"per_page": MAX_COMMENTS, "sort": "created", "direction": "asc"})),
    )
    files = [str(f.get("filename")) for f in _as_list(files_response)
             if isinstance(f, dict) and f.get("filename")][:100]
    reviews = _review_rows(_as_list(reviews_response))
    comments = _comment_rows(_as_list(comments_response))
    timeline_issues = _timeline_refs(_as_list(timeline_response))[1]

    node = (await _enrich_pulls(gh, [number])).get(number)
    row = _pull_row(raw, node)
    linked = sorted(set(row["linked_issues"]) | set(timeline_issues))
    account = await _account_name(gh, token) if gh.auth_note else None
    payload = {
        "ok": True, "code": "ok", "error": None, "detail": None,
        "repo": name, "owner": owner, "url": row["url"], "auth": gh.auth,
        "auth_note": gh.auth_note, "auth_account": account,
        **row, "linked_issues": linked,
        "body": raw.get("body") or "",
        "state_reason": raw.get("state_reason"),
        "files_changed": files,
        "reviews": reviews,
        "comments": comments,
        # None, not {"state": "none"}, when the GraphQL pass could not answer:
        # the modal shows a checks row only for a real rollup.
        "checks": row["checks"] if node is not None else None,
        "comments_count": _int(raw.get("comments")),
        "timeline_linked_issues": timeline_issues,
    }
    _cache_put(key, payload)
    return payload


@router.get("/github/issue/{number}")
async def github_issue(number: int, path: str = Query(..., description="Absolute path to the project folder"),
                       refresh: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
    """One issue: body, labels, and the PRs that reference it."""
    try:
        folder = _resolve_folder(path)
    except _FolderMissing as exc:
        return _item_failure(number, "folder_missing", f"Folder not found: {exc.path}", exc.path)
    ctx = await _repo_context(folder)
    if ctx.code != "ok" or not ctx.owner or not ctx.repo:
        return _item_failure(number, ctx.code, ctx.error, ctx.detail,
                             owner=ctx.owner, repo=ctx.repo)
    owner, name = ctx.owner, ctx.repo
    mode, token = await _auth()
    key = ("issue", owner.lower(), name.lower(), number, mode)
    if not refresh and (hit := _cache_hit(key)) is not None:
        return hit

    gh = _GhReader(owner, name, mode, token)
    try:
        detail = await gh.rest(f"/repos/{owner}/{name}/issues/{number}")
    except _UpstreamError as exc:
        account = await _account_name(gh, token)
        return _item_failure(number, exc.code, _github_sentence(exc, owner, name, account),
                             exc.detail, owner=owner, repo=name, auth=gh.auth,
                             auth_note=gh.auth_note, auth_account=account)
    raw = detail.data if isinstance(detail.data, dict) else {}
    timeline_response, comments_response = await asyncio.gather(
        _quiet(gh.rest(f"/repos/{owner}/{name}/issues/{number}/timeline", {"per_page": 100})),
        _quiet(gh.rest(f"/repos/{owner}/{name}/issues/{number}/comments",
                       {"per_page": MAX_COMMENTS, "sort": "created", "direction": "asc"})),
    )
    timeline_pulls = _timeline_refs(_as_list(timeline_response))[0]
    comments = _comment_rows(_as_list(comments_response))
    account = await _account_name(gh, token) if gh.auth_note else None
    payload = {
        "ok": True, "code": "ok", "error": None, "detail": None,
        "repo": name, "owner": owner, "url": raw.get("html_url"), "auth": gh.auth,
        "auth_note": gh.auth_note, "auth_account": account,
        "number": number,
        "title": str(raw.get("title") or ""),
        "state": str(raw.get("state") or "open"),
        "state_reason": raw.get("state_reason"),
        **_actor_fields(raw.get("user")),
        "labels": _names(raw.get("labels"), "name"),
        "assignees": _names(raw.get("assignees"), "login"),
        **({"assignee_avatars": avatars} if (avatars := _assignee_avatars(raw.get("assignees"))) else {}),
        "comments": comments,
        "comments_count": _int(raw.get("comments")),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "body": raw.get("body") or "",
        "timeline_prs": timeline_pulls,
        "linked_prs": sorted(set(timeline_pulls) | set(_cached_inverse(owner, name, number))),
    }
    _cache_put(key, payload)
    return payload


def _ref_numbers(raw: str) -> list[int]:
    """Numbers from ``numbers=1,2,3``: digits only, ``#`` tolerated, de-duplicated, MAX_REFS wide."""
    out: list[int] = []
    for piece in (raw or "").split(","):
        text = piece.strip().lstrip("#")
        if not text.isdigit():
            continue
        number = _int(text)
        if number > 0 and number not in out:
            out.append(number)
        if len(out) >= MAX_REFS:
            break
    return out


@router.get("/github/refs")
async def github_refs(path: str = Query(..., description="Absolute path to the project folder"),
                      numbers: str = Query("", description="Comma separated issue or PR numbers"),
                      refresh: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
    """Resolve ``#number`` chips: one batched GraphQL call, up to MAX_REFS numbers.

    ``refs`` is keyed by the number as a string: ``{number, kind: "issue"|"pr",
    title, state, author, updated_at, url}``. A number that does not exist is
    left out instead of guessed, and a merged PR reports ``closed``. Cache 60 s
    like every other read; ``refresh=1`` bypasses it.
    """
    wanted = _ref_numbers(numbers)
    if not wanted:
        return {"ok": True, "refs": {}}
    try:
        folder = _resolve_folder(path)
    except _FolderMissing as exc:
        return {"ok": False, "code": "folder_missing",
                "error": f"Folder not found: {exc.path}", "detail": exc.path}
    ctx = await _repo_context(folder)
    if ctx.code != "ok" or not ctx.owner or not ctx.repo:
        return {"ok": False, "code": ctx.code, "error": ctx.error, "detail": ctx.detail}
    owner, name = ctx.owner, ctx.repo
    mode, token = await _auth()
    key = ("refs", owner.lower(), name.lower(), tuple(wanted), mode)
    if not refresh and (hit := _cache_hit(key)) is not None:
        return hit
    if mode == "none":
        return {"ok": False, "code": "no_auth",
                "error": "Resolving #numbers needs a GitHub login: set GITHUB_TOKEN or run "
                         "gh auth login.",
                "detail": "GET /github/refs -> no credential"}

    gh = _GhReader(owner, name, mode, token)
    query, variables = _refs_query(owner, name, wanted)
    try:
        # partial: a number that does not exist comes back as a NOT_FOUND error
        # beside the data, and that is the "omit it" case, not a failed read.
        data = await gh.graphql(query, variables, partial=True)
    except _UpstreamError as exc:
        account = await _account_name(gh, token)
        log.warning("project-command-center: refs lookup failed for %s/%s: %s", owner, name, exc)
        return {"ok": False, "code": exc.code,
                "error": _github_sentence(exc, owner, name, account), "detail": exc.detail}
    if data is None:
        return {"ok": False, "code": "upstream_error",
                "error": f"GitHub did not answer the reference lookup for {owner}/{name}.",
                "detail": "POST /graphql -> no data"}

    def parse(payload: Optional[dict[str, Any]]) -> dict[str, RefRow]:
        repository = (payload or {}).get("repository")
        if not isinstance(repository, dict):
            return {}
        found: dict[str, RefRow] = {}
        for index, number in enumerate(wanted):
            node = repository.get(f"r{index}")
            if not isinstance(node, dict):
                continue  # GitHub has no such number: omitted, never invented
            state = str(node.get("state") or "").lower()
            login, avatar, bot = _actor(node.get("author"))
            kind = "pr" if str(node.get("__typename") or "") == "PullRequest" else "issue"
            entry: RefRow = {
                "number": _int(node.get("number")) or number,
                "kind": kind,
                "title": str(node.get("title") or ""),
                "state": "closed" if state in ("closed", "merged") else "open",
                # The chip colours like the row: a merged PR and a closed issue
                # closed as completed are purple, not planned or duplicate red.
                "state_reason": str(node.get("stateReason") or "").lower() or None,
                "draft": bool(node.get("isDraft")),
                "merged": bool(node.get("merged")),
                "author": login,
                "author_bot": bot,
                "updated_at": node.get("updatedAt"),
                "url": node.get("url"),
            }
            if avatar:
                entry["author_avatar"] = avatar
            found[str(number)] = entry
        return found

    refs = parse(data)

    # A token that cannot see the repository answers ``repository: null`` with a
    # NOT_FOUND beside it, which ``partial`` swallows as "no such number". That is
    # the board's original blind spot: retry the whole batch through ``gh`` once.
    repository_missing = not isinstance((data or {}).get("repository"), dict)
    if repository_missing and mode == "token" and shutil.which("gh") is not None:
        try:
            data = await _GitHub(owner, name, "gh", None).graphql(query, variables, partial=True)
            refs = parse(data)
            repository_missing = not isinstance((data or {}).get("repository"), dict)
        except _UpstreamError as exc:
            log.warning("project-command-center: refs gh retry failed for %s/%s: %s", owner, name, exc)

    if not refs and repository_missing:
        return {"ok": False, "code": "repo_not_found",
                "error": f"GitHub could not read {owner}/{name} for reference lookup: the configured "
                         f"credential cannot see the repository and gh did not answer.",
                "detail": "POST /graphql -> repository null"}

    payload: dict[str, Any] = {"ok": True, "refs": refs}
    _cache_put(key, payload)
    return payload


def _count_totals(data: Optional[dict[str, Any]], repos: Sequence[tuple[str, str]]
                  ) -> dict[tuple[str, str], dict[str, Any]]:
    """``{alias index}`` rows to ``{(owner, repo): counts}``; a null alias is dropped."""
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for index, (repo_owner, repo_name) in enumerate(repos):
        node = (data or {}).get(f"r{index}")
        if not isinstance(node, dict):
            continue  # unreadable or gone: omitted, so no zero is read as a fact
        issues_raw, pulls_raw = node.get("issues"), node.get("pullRequests")
        issues: dict[str, Any] = issues_raw if isinstance(issues_raw, dict) else {}
        pulls: dict[str, Any] = pulls_raw if isinstance(pulls_raw, dict) else {}
        totals[(repo_owner.lower(), repo_name.lower())] = {
            "owner": repo_owner, "repo": repo_name,
            "issues_open": _int(issues.get("totalCount")),
            "prs_open": _int(pulls.get("totalCount")),
        }
    return totals


@router.get("/github/counts")
async def github_counts(path: list[str] = Query(
                            default=[], description="Absolute path to a project folder; repeat the parameter"),
                        refresh: int = Query(0, description="1 bypasses the 60 s cache")) -> dict[str, Any]:
    """Open issue and open PR counts for up to MAX_COUNT_PATHS folders, one call.

    ``counts`` is keyed by the path as requested: ``{owner, repo, issues_open,
    prs_open}``. A folder with no GitHub remote, a folder that is gone, and a
    repository this credential cannot read are all left out of ``counts``: a
    zero here always means zero open items, never "unknown". Two folders on the
    same repository share one alias, so the whole board costs one GraphQL call.
    Cache 60 s keyed by the sorted path list; ``refresh=1`` bypasses it.
    """
    requested: list[str] = []
    for raw in path or []:
        text = str(raw or "").strip()
        if text and text not in requested:
            requested.append(text)
            if len(requested) >= MAX_COUNT_PATHS:
                break
    if not requested:
        return {"ok": False, "code": "no_paths",
                "error": "Pass at least one absolute folder as path.",
                "detail": "GET /github/counts -> no path"}

    mode, token = await _auth()
    key = ("ghcounts", tuple(sorted(requested)), mode)
    if not refresh and (hit := _cache_hit(key)) is not None:
        return hit
    if mode == "none":
        # No credential: every repository is unreadable, so every folder is absent.
        payload: dict[str, Any] = {"ok": True, "cached": False, "counts": {}}
        _cache_put(key, payload)
        return payload

    folders: list[tuple[str, str, str]] = []  # (requested path, owner, repo)
    aliases: dict[tuple[str, str], tuple[str, str]] = {}
    for text in requested:
        if not Path(text).is_absolute():
            continue  # a relative path names nothing this route can count: absent
        try:
            folder = _resolve_folder(text)
        except _FolderMissing:
            continue  # gone from disk: absent, never an error for the whole batch
        ctx = await _repo_context(folder)
        if ctx.code != "ok" or not ctx.owner or not ctx.repo:
            continue  # no remote, or not a repo: absent
        folders.append((text, ctx.owner, ctx.repo))
        aliases.setdefault((ctx.owner.lower(), ctx.repo.lower()), (ctx.owner, ctx.repo))
    if not aliases:
        payload = {"ok": True, "cached": False, "counts": {}}
        _cache_put(key, payload)
        return payload

    wanted = list(aliases.values())
    owner, name = wanted[0]
    gh = _GhReader(owner, name, mode, token)
    query, variables = _counts_query(wanted)
    try:
        # partial: a repository this credential cannot read comes back as a null
        # alias with a NOT_FOUND beside it, and that is the "leave it out" case.
        data = await gh.graphql(query, variables, partial=True)
    except _UpstreamError as exc:
        account = await _account_name(gh, token)
        log.warning("project-command-center: counts lookup failed: %s", exc)
        return {"ok": False, "code": exc.code,
                "error": _github_sentence(exc, owner, name, account),
                "detail": exc.detail, "counts": {}}

    totals = _count_totals(data, wanted)

    # A token blind to one private org repository answers a null alias beside a
    # NOT_FOUND, which ``partial`` accepts as "gone". In a mixed batch that is
    # indistinguishable from a repository that does not exist, so retry ONLY the
    # aliases that came back null through the user's own gh login.
    missing = [repo for index, repo in enumerate(wanted)
               if not isinstance((data or {}).get(f"r{index}"), dict)]
    if missing and mode == "token" and shutil.which("gh") is not None:
        retry_query, retry_vars = _counts_query(missing)
        try:
            retry_data = await _GitHub(missing[0][0], missing[0][1], "gh", None).graphql(
                retry_query, retry_vars, partial=True)
            totals.update(_count_totals(retry_data, missing))
        except _UpstreamError as exc:
            log.warning("project-command-center: counts gh retry failed: %s", exc)

    counts = {text: totals[(path_owner.lower(), path_name.lower())]
              for text, path_owner, path_name in folders
              if (path_owner.lower(), path_name.lower()) in totals}
    payload = {"ok": True, "cached": False, "counts": counts}
    _cache_put(key, payload)
    return payload


@router.post("/github/issue/{number}/comment")
async def github_issue_comment(number: int, path: str = Query(...),
                               payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """``{"body": "markdown"}`` -> ``{"ok": true, "comment": {id, author, body, created_at}}``.

    The text goes to ``gh api`` on stdin, so quotes and newlines survive intact.
    An empty or whitespace only body is ``empty_comment`` before any folder or
    network call; a missing number is ``not_found``. HTTP 200 either way.
    """
    return await _post_comment(number, path, payload, "issue")


@router.post("/github/pr/{number}/comment")
async def github_pr_comment(number: int, path: str = Query(...),
                            payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Comment on a pull request's conversation, same body and answer as the issue route.

    The number is confirmed to be a pull request first: GitHub's comment endpoint
    would otherwise happily comment on an issue with the same number.
    """
    return await _post_comment(number, path, payload, "pr")


@router.post("/github/issue/{number}/state")
async def github_issue_state(number: int, path: str = Query(...),
                             payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """``{"state": "open"|"closed"}`` -> ``{"ok": true, "state": ...}``; else ``bad_state``."""
    return await _set_state(number, path, payload, "issue")


@router.post("/github/pr/{number}/state")
async def github_pr_state(number: int, path: str = Query(...),
                          payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Open or close a pull request; the number must be a PR, not an issue."""
    return await _set_state(number, path, payload, "pr")


# --- Diagnose ---------------------------------------------------------------

def _length_bucket(value: Optional[str]) -> str:
    size = len(value or "")
    if size == 0:
        return "0"
    return "1-20" if size <= 20 else "21-60" if size <= 60 else "60+"


async def _probe_credential(via: str, owner: str, repo: str,
                            token: Optional[str]) -> dict[str, Any]:
    """One authenticated GET of /repos/{owner}/{repo} through one credential.

    Nothing secret is kept: ``via`` is the credential's name and the answer is a
    status, a code and a sentence. A rejected probe is data, not an exception.
    """
    endpoint = f"/repos/{owner}/{repo}"
    client = _GitHub(owner, repo, via, token)
    try:
        response = await client.rest(endpoint)
    except _UpstreamError as exc:
        account = await _gh_user() if via == "gh" else None
        return {"via": via, "status": exc.status, "code": exc.code,
                "message": _github_sentence(exc, owner, repo, account),
                "detail": exc.detail, "endpoint": endpoint}
    return {"via": via, "status": response.status, "code": "ok",
            "message": f"GitHub answered for {owner}/{repo} through {via}.",
            "detail": f"GET {endpoint} -> HTTP {response.status}", "endpoint": endpoint}


@router.get("/diagnose")
async def diagnose(path: str = Query(..., description="Absolute path to the project folder")) -> dict[str, Any]:
    """Why a folder's GitHub read works or fails, from inside the gateway.

    Answers the question a raw status line cannot: does the folder exist, is it a
    work tree, what remote does it carry, is a token present (a boolean and a
    length bucket, never the value), is the gh CLI signed in, and what does one
    probe of /repos/{owner}/{repo} return through each credential. Both probe
    attempts are reported, labelled by credential.
    """
    if not path or not str(path).strip():
        raise HTTPException(status_code=400, detail="path is required (an absolute folder)")
    requested = Path(path)
    if not requested.is_absolute():
        raise HTTPException(status_code=400, detail="path must be absolute")

    exists = requested.exists()
    is_dir = requested.is_dir()
    folder = requested.resolve() if exists else requested

    git_available = shutil.which("git") is not None
    root: Optional[str] = None
    branch: Optional[str] = None
    remote: Optional[str] = None
    owner: Optional[str] = None
    repo: Optional[str] = None
    git_error: Optional[str] = None

    if not exists:
        git_error = f"{requested} does not exist"
    elif not is_dir:
        git_error = f"{requested} is not a directory"
    elif not git_available:
        git_error = "git is not on PATH"
    else:
        try:
            root = await _work_tree(folder)
        except TimeoutError as exc:
            git_error = str(exc)
        if root:
            try:
                branch = (await _git(root, "rev-parse", "--abbrev-ref", "HEAD")).strip() or None
            except (TimeoutError, _GitError) as exc:
                git_error = f"cannot read the branch: {exc}"
            try:
                remote = (await _primary_remote(root)).strip() or None
            except (TimeoutError, _GitError) as exc:
                git_error = git_error or f"cannot read the remote: {exc}"
            owner, repo = _parse_remote(remote or "")
            if remote and (not owner or not repo):
                git_error = git_error or "the remote is not a github.com URL"
        elif not git_error:
            git_error = f"{folder} is not inside a git work tree"

    token_value = _secret_token()
    gh_available = shutil.which("gh") is not None
    gh_user = await _gh_user() if gh_available else None
    gh_error = None
    if not gh_available:
        gh_error = "gh is not on PATH"
    elif not gh_user:
        gh_error = "gh is installed but not signed in"

    attempts: list[dict[str, Any]] = []
    if owner and repo:
        if token_value:
            attempts.append(await _probe_credential("token", owner, repo, token_value))
        if gh_available:
            attempts.append(await _probe_credential("gh", owner, repo, None))

    if attempts:
        answered = next((a for a in attempts if a["code"] == "ok"), attempts[-1])
        probe = {"status": answered["status"], "code": answered["code"],
                 "message": answered["message"], "endpoint": answered["endpoint"],
                 "via": answered["via"], "attempts": attempts}
    else:
        if not exists:
            fallback, fallback_code = f"{requested} does not exist", "folder_missing"
        elif not is_dir:
            fallback, fallback_code = f"{requested} is not a directory", "folder_missing"
        elif not root:
            fallback, fallback_code = (git_error or f"{folder} is not inside a git work tree",
                                       "not_a_repo")
        elif not owner or not repo:
            fallback, fallback_code = git_error or "no GitHub remote", "no_remote"
        elif not token_value and not gh_available:
            fallback = "No credential is available: GITHUB_TOKEN is unset and gh is not on PATH."
            fallback_code = "no_auth"
        else:
            fallback, fallback_code = "No credential could be probed.", "no_auth"
        probe = {"status": None, "code": fallback_code, "message": fallback,
                 "endpoint": None, "via": None, "attempts": []}

    return {
        "folder": {"path": str(requested), "exists": exists, "is_dir": is_dir},
        "git": {"available": git_available, "root": root,
                "matches": bool(root) and os.path.realpath(root) == os.path.realpath(folder),
                "branch": branch, "remote": remote, "owner": owner, "repo": repo,
                "error": git_error},
        "token": {"present": bool(token_value), "length_bucket": _length_bucket(token_value)},
        "gh": {"available": gh_available, "path": shutil.which("gh"),
               "authenticated": bool(gh_user), "user": gh_user, "error": gh_error},
        "probe": probe,
    }


@router.get("/icon")
async def icon(path: str = Query(..., description="Absolute path to the project folder"),
                refresh: int = Query(0, ge=0, le=1)) -> dict[str, Any]:
    """The best icon for a project folder: a local image, a remote one, or a stack key.

    Always HTTP 200 for a well formed request. Three answers:
    ``kind: "image"`` with a ``data:`` URL whose mime was sniffed from the bytes and
    whose ``detail`` names the file or URL, ``kind: "glyph"`` with a ``stack`` key
    (``laravel``, ``nuxt``, ``node``, ``python``, ``rust``, ``go``, ``nix``,
    ``obsidian``, ``docker``, ``docs``, ``git``) when nothing was found, and
    ``ok: false`` with a code for a folder that is gone or malformed. ``refresh=1``
    bypasses the local (300 s), remote (3600 s) and glyph (300 s) caches.
    """
    try:
        folder = _resolve_folder(path)
    except _FolderMissing as exc:
        return {"ok": False, "code": "folder_missing", "error": f"Folder not found: {exc.path}"}
    except HTTPException as exc:
        return {"ok": False, "code": "bad_path", "error": str(exc.detail)}
    try:
        result = await _icon_answer(folder, refresh=bool(refresh))
    except Exception as exc:  # a card must render even if a manifest or a socket misbehaves
        log.warning("icon: %s failed: %s", folder, exc, exc_info=True)
        return {"ok": False, "code": "icon_error",
                "error": f"the icon for {folder.name} could not be resolved: {type(exc).__name__}"}
    result.setdefault("cached", False)
    return result


# --- Board state -------------------------------------------------------------
#
# The rows live in board_store.py's SQLite file; nothing about the board is cached here. The module
# handle below is code, not state: board_store opens its own connection per call and reads the file
# every time, which is what makes the agent process's writes visible to this one. It is loaded by
# path with importlib and never inserted into sys.modules (a second copy of the plugin under another
# name must not inherit this one's module entry).

_store_lock = threading.Lock()
_store_module: Any = None


def _board_store() -> Any:
    """``board_store.py`` from the plugin root, loaded once by path."""
    global _store_module
    if _store_module is not None:
        return _store_module
    with _store_lock:
        if _store_module is None:
            path = Path(__file__).resolve().parent.parent / "board_store.py"
            spec = importlib.util.spec_from_file_location("pcc_board_store", path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _store_module = module
    return _store_module


def _board_failure(exc: BaseException, **extra: Any) -> dict[str, Any]:
    log.warning("board store failed: %s", exc, exc_info=True)
    return {"ok": False, "code": "store_error",
            "error": f"the board database could not be reached: {type(exc).__name__}", **extra}


def _broadcast_board(kind: str, project_id: Optional[str], **fields: Any) -> None:
    """``board.changed`` to every connected client; a missing gateway is not a failed write."""
    payload: dict[str, Any] = {"project_id": project_id, "kind": kind,
                               **{k: v for k, v in fields.items() if v is not None}}
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN_NAME, "board.changed", payload)
    except Exception:  # a plugin-only process has no gateway; the write already happened
        log.warning("board.changed broadcast failed", exc_info=True)


def _auto_link(store: Any, session_id: Optional[str]) -> dict[str, Any]:
    """Which project the asking session is in, how that was decided, and the lane its card shows."""
    _, link = store.resolve_link(session_id) if session_id else ("", None)
    resolved = store.resolve_project(session_id=session_id)
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        cwd = str(resolve_agent_cwd())
    except Exception:
        cwd = ""
    if not resolved:
        via = None
    elif link and (link.get("project_id") or "") == resolved:
        via = "session_link"
    else:
        via = "cwd_prefix"
    # The same rule the renderer applies: the session's own lane wins, else the project's.
    lane = store.resolve_lane(link, store.get_status(resolved) if resolved else None)
    return {"session_id": session_id or "", "project_id": resolved, "via": via, "cwd": cwd,
            "lane": lane}


@router.get("/board")
def board(session_id: Optional[str] = Query(None, description="Session asking, for auto_link")) -> dict[str, Any]:
    """Every board row: statuses, session links (each with its own lane), how many projects are
    synced, and where the asking session is. A broken database is ``ok: false, code: store_error``
    with empty collections, not a 500: the Desktop page still renders.

    ``def``, not ``async def``: this body does blocking SQLite with a 5 s busy timeout, so FastAPI
    must run it in the threadpool instead of stalling the gateway event loop.
    """
    try:
        payload = _board_store().board()
        auto_link = _auto_link(_board_store(), session_id)
    except Exception as exc:
        empty: dict[str, Any] = {"statuses": [], "links": [], "projects_synced": 0}
        return _board_failure(exc, auto_link={"session_id": session_id or "", "project_id": None,
                                              "via": None, "cwd": "",
                                              "lane": {"status": "", "scope": "", "source": "",
                                                       "note": "", "updated_at": None}}, **empty)
    return {
        "ok": True,
        "statuses": payload.get("statuses", []),
        "links": payload.get("links", []),
        "projects_synced": payload.get("projects_synced", 0),
        "auto_link": auto_link,
    }


@router.post("/board/status")
def board_status(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Set one card's lane. With a ``session_id`` (or ``scope: "session"``) this moves that session's
    own card and note, creating its link row when it has none; otherwise it moves the project lane,
    as before. ``source`` is always ``user`` here; the agent tool records ``agent``. A bad lane is
    ``code: bad_status``, an id the board never synced is ``code: unknown_project``, and a session
    carded under a different project is ``code: project_mismatch``, all HTTP 200."""
    project_id = str(payload.get("project_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    note = payload.get("note")
    scope = str(payload.get("scope") or "auto").strip().lower()
    try:
        result = _board_store().set_status(project_id, status, note, "user",
                                           session_id=session_id or None, scope=scope)
    except Exception as exc:
        return _board_failure(exc)
    if result.get("ok"):
        if result.get("scope") == "session":
            link = result["link"]
            _broadcast_board("status", link.get("project_id") or project_id,
                             status=link.get("status"), session_id=link.get("session_id"),
                             source="user")
        else:
            _broadcast_board("status", project_id, status=result["status"]["status"], source="user")
    return result


@router.post("/board/link")
def board_link(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Attach a session to a project and, when given, to an issue and/or a PR. One row per session:
    fields left out keep their current value. The Desktop path records ``resolved_by: "desktop"``.
    """
    try:
        result = _board_store().set_link(
            payload.get("session_id"), payload.get("project_id"), payload.get("repo"),
            payload.get("issue"), payload.get("pr"), payload.get("pr_url"), payload.get("title"),
            resolved_by="desktop")
    except Exception as exc:
        return _board_failure(exc)
    if result.get("ok"):
        link = result["link"]
        _broadcast_board("link", link.get("project_id") or payload.get("project_id"),
                         issue=link.get("issue"), pr=link.get("pr"), source="user")
    return result


@router.post("/board/projects")
def board_projects(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Idempotent upsert of the projects the board knows: ``{"projects": [{id, label, paths}]}``.
    Sending the same list twice writes the same rows; projects left out of a later list are kept.
    Broadcasts ``kind: "projects"`` only when a label or path actually changed."""
    try:
        result = _board_store().set_projects(payload.get("projects"))
    except Exception as exc:
        return _board_failure(exc)
    if result.get("ok") and result.get("changed"):
        _broadcast_board("projects", None, source="user")
    return result


@router.post("/board/seed")
def board_seed(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Restore statuses on a board that has none. Refused with ``code: seed_not_empty`` once any
    status row exists, so a seed can never overwrite live state; an empty or unusable list is
    ``code: empty_seed``."""
    try:
        result = _board_store().seed(payload.get("statuses"))
    except Exception as exc:
        return _board_failure(exc)
    if result.get("ok"):
        _broadcast_board("status", None, source="user")
    return result
