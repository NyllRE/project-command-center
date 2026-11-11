#!/usr/bin/env python3
"""Worktree harness for project-command-center: GET /worktrees and POST /worktree.

Plain Python like tests/test_branch_switch.py: throwaway repositories under
``~/.hermes/cache/scratch/pcc-worktrees`` (override with PCC_WORKTREE_SCRATCH),
the plugin router mounted on a bare FastAPI app, driven with TestClient. Every
refusal is HTTP 200 with ``ok`` false and a distinct ``code``; only a malformed
path is a 400. The happy paths really run ``git worktree add`` in the fixture and
check the checkout git produced.

    python3 tests/test_worktrees.py
"""

from __future__ import annotations

import sys as _sys

_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402,F401  finds the Hermes tree and a sandbox home

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN = Path(__file__).resolve().parent.parent
SCRATCH = Path(os.environ.get(
    "PCC_WORKTREE_SCRATCH", Path.home() / ".hermes" / "cache" / "scratch" / "pcc-worktrees"))
FAILURES: list[str] = []
_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Fixture Author",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "Fixture Author",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
}


def check(name: str, condition: bool, detail: Any = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}{f' ({detail})' if detail else ''}")
    if not condition:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ".ljust(78, "="))


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                            text=True, env={**os.environ, **_ENV})
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def commit(repo: Path, message: str, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def init(name: str) -> Path:
    repo = SCRATCH / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    return repo


def build_fixtures() -> dict[str, Any]:
    """One repository per guard, plus a real git origin for the default base."""
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)
    repos: dict[str, Any] = {}

    basic = init("basic")
    commit(basic, "base", {"a.txt": "base\n"})
    git(basic, "branch", "feature")
    repos["basic"] = {"path": basic}

    shapes = init("shapes")
    commit(shapes, "base", {"a.txt": "base\n"})
    git(shapes, "branch", "side")
    detached = SCRATCH / "shapes-detached"
    git(shapes, "worktree", "add", "-q", "--detach", str(detached), "main")
    locked = SCRATCH / "shapes-locked"
    git(shapes, "worktree", "add", "-q", str(locked), "side")
    git(shapes, "worktree", "lock", str(locked))
    repos["shapes"] = {"path": shapes, "detached": detached, "locked": locked}

    checkedout = init("checkedout")
    commit(checkedout, "base", {"a.txt": "base\n"})
    git(checkedout, "branch", "feature")
    linked = SCRATCH / "checkedout-linked"
    git(checkedout, "worktree", "add", "-q", str(linked), "feature")
    repos["checkedout"] = {"path": checkedout, "linked": linked}

    inside = init("inside")
    commit(inside, "base", {"a.txt": "base\n"})
    child = SCRATCH / "inside-linked"
    git(inside, "worktree", "add", "-q", "-b", "child", str(child), "main")
    repos["inside"] = {"path": inside, "child": child}

    plain = SCRATCH / "plain"
    plain.mkdir()
    repos["plain"] = {"path": plain}
    repos["missing"] = {"path": SCRATCH / "gone"}

    origin = SCRATCH / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], capture_output=True,
                   text=True, env={**os.environ, **_ENV}, check=True)
    seed = init("seed")
    commit(seed, "base", {"a.txt": "base\n"})
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "-u", "origin", "main")
    git(seed, "checkout", "-q", "-b", "remoteonly")
    commit(seed, "remote only", {"r.txt": "r\n"})
    git(seed, "push", "-q", "-u", "origin", "remoteonly")
    subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"],
                   capture_output=True, text=True, env={**os.environ, **_ENV}, check=True)
    clone = SCRATCH / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], capture_output=True,
                   text=True, env={**os.environ, **_ENV}, check=True)
    repos["clone"] = {"path": clone}

    return repos


def load_api() -> Any:
    path = PLUGIN / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("pcc_api_worktree_test", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pcc_api_worktree_test"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if SCRATCH.resolve() == (Path.home() / ".hermes").resolve():
        sys.exit("refusing to build fixtures in ~/.hermes")
    started = time.monotonic()
    repos = build_fixtures()
    print(f"fixtures in {SCRATCH} ({time.monotonic() - started:.1f}s)")

    api = load_api()
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)

    def post(label: str, **body: Any) -> tuple[int, dict[str, Any]]:
        response = client.post("/worktree", json=body)
        try:
            payload = response.json()
        except ValueError:
            payload = {"ok": None, "code": "not_json", "error": response.text[:200]}
        if response.status_code not in (200, 400):
            FAILURES.append(f"{label} HTTP {response.status_code}")
            print(f"  FAIL {label} HTTP {response.status_code}")
        return response.status_code, payload

    def listing(label: str, path: Path) -> tuple[int, dict[str, Any]]:
        response = client.get("/worktrees", params={"path": str(path)})
        try:
            payload = response.json()
        except ValueError:
            payload = {"ok": None, "code": "not_json", "error": response.text[:200]}
        if response.status_code not in (200, 400):
            FAILURES.append(f"{label} HTTP {response.status_code}")
            print(f"  FAIL {label} HTTP {response.status_code}")
        return response.status_code, payload

    section("parser: porcelain records")
    sample = (
        "worktree /repo\n"
        "HEAD 1111111111111111111111111111111111111111\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /repo-detached\n"
        "HEAD 2222222222222222222222222222222222222222\n"
        "detached\n"
        "\n"
        "worktree /repo-locked\n"
        "HEAD 3333333333333333333333333333333333333333\n"
        "branch refs/heads/side\n"
        "locked another process\n"
        "\n"
        "worktree /repo-bare\n"
        "bare"
    )
    rows = api._worktree_entries(sample)
    check("four records, one per worktree line", len(rows) == 4, str(len(rows)))
    check("branch is the short name", rows[0]["branch"] == "main", rows[0]["branch"])
    check("main record is neither detached nor locked",
          rows[0]["detached"] is False and rows[0]["locked"] is False, str(rows[0]))
    check("a detached record names no branch",
          rows[1]["path"] == "/repo-detached" and rows[1]["branch"] is None
          and rows[1]["detached"] is True, str(rows[1]))
    check("a locked record keeps its branch flag",
          rows[2]["locked"] is True and rows[2]["detached"] is False
          and rows[2]["branch"] == "side", str(rows[2]))
    check("a last record with no trailing blank line is kept",
          rows[3]["path"] == "/repo-bare" and rows[3]["branch"] is None, str(rows[3]))
    check("an empty listing has no records", api._worktree_entries("") == [])

    section("worktrees: list, flags and isMain ordering")
    basic = repos["basic"]["path"]
    status, body = listing("basic", basic)
    check("list is HTTP 200 ok", status == 200 and body.get("ok") is True, body.get("code"))
    trees = body.get("worktrees") or []
    check("a fresh repo lists exactly the main worktree", len(trees) == 1, str(len(trees)))
    check("the main record is first and isMain",
          bool(trees) and trees[0]["path"] == str(basic) and trees[0]["isMain"] is True,
          str(trees[:1]))
    check("the main record names its branch", trees[0]["branch"] == "main", trees[0]["branch"])
    check("the main record is neither detached nor locked",
          trees[0]["detached"] is False and trees[0]["locked"] is False, str(trees[0]))

    shapes = repos["shapes"]["path"]
    _, shape_body = listing("shapes", shapes)
    shape_trees = shape_body.get("worktrees") or []
    check("three worktrees on disk are three records", len(shape_trees) == 3, str(len(shape_trees)))
    check("isMain is true only for the first record",
          [tree["isMain"] for tree in shape_trees] == [True, False, False],
          str([tree["isMain"] for tree in shape_trees]))
    by_path = {tree["path"]: tree for tree in shape_trees}
    check("a detached worktree is flagged detached",
          by_path.get(str(repos["shapes"]["detached"]), {}).get("detached") is True,
          str(by_path.get(str(repos["shapes"]["detached"]))))
    check("a locked worktree is flagged locked",
          by_path.get(str(repos["shapes"]["locked"]), {}).get("locked") is True,
          str(by_path.get(str(repos["shapes"]["locked"]))))

    section("worktree: the happy paths")
    status, created = post("basic", path=str(basic), branch="feature", name="feature")
    target = basic / ".worktrees" / "feature"
    check("existing branch is HTTP 200 ok", status == 200 and created.get("ok") is True,
          created.get("code"))
    check("the worktree lands under .worktrees",
          created.get("path") == str(target), created.get("path"))
    check("the directory really exists", target.is_dir())
    check("git checked the branch out there",
          git(target, "symbolic-ref", "--short", "HEAD").strip() == "feature")
    check("the answer names the branch", created.get("branch") == "feature", created.get("branch"))

    _, after = listing("basic", basic)
    after_trees = after.get("worktrees") or []
    check("the new worktree is listed second",
          len(after_trees) == 2 and after_trees[1]["path"] == str(target)
          and after_trees[1]["isMain"] is False and after_trees[1]["branch"] == "feature",
          str(after_trees[1:]))

    _, existed = post("basic", path=str(basic), branch="feature", name="feature")
    check("a second add for the same directory is worktree_exists",
          existed.get("code") == "worktree_exists", existed.get("code"))

    status, fresh = post("basic", path=str(basic), branch="topic", name="Topic Work", base="main")
    fresh_target = basic / ".worktrees" / "topic-work"
    check("a new branch from an explicit base is created",
          status == 200 and fresh.get("ok") is True, fresh.get("code"))
    check("the slug names the directory", fresh.get("path") == str(fresh_target),
          fresh.get("path"))
    check("the new branch is checked out",
          git(fresh_target, "symbolic-ref", "--short", "HEAD").strip() == "topic")

    clone = repos["clone"]["path"]
    _, remote = post("clone", path=str(clone), branch="remoteonly")
    remote_target = clone / ".worktrees" / "remoteonly"
    check("a remote-only branch defaults to origin/<branch>", remote.get("ok") is True,
          remote.get("code"))
    check("the default base creates the tracking branch locally",
          remote_target.is_dir()
          and git(remote_target, "symbolic-ref", "--short", "HEAD").strip() == "remoteonly")

    inside = repos["inside"]["path"]
    child = repos["inside"]["child"]
    _, from_linked = post("inside", path=str(child), branch="fromlinked", base="main")
    check("a linked worktree still nests under the main checkout",
          from_linked.get("ok") is True
          and from_linked.get("path") == str(inside / ".worktrees" / "fromlinked"),
          from_linked.get("path"))
    check("nothing was created under the linked worktree",
          not (child / ".worktrees").exists())

    section("worktree: every refusal")
    status, missing = post("missing", path=str(repos["missing"]["path"]), branch="feature")
    check("a missing folder is folder_missing",
          status == 200 and missing.get("code") == "folder_missing", missing.get("code"))
    _, missing_list = listing("missing", repos["missing"]["path"])
    check("listing a missing folder is folder_missing",
          missing_list.get("code") == "folder_missing", missing_list.get("code"))

    plain = repos["plain"]["path"]
    _, not_repo = post("plain", path=str(plain), branch="feature")
    check("a plain directory is not_a_repo", not_repo.get("code") == "not_a_repo",
          not_repo.get("code"))
    _, not_repo_list = listing("plain", plain)
    check("listing a plain directory is not_a_repo",
          not_repo_list.get("code") == "not_a_repo", not_repo_list.get("code"))

    _, bad_branch = post("bad", path=str(basic), branch="///")
    check("a branch name that sanitizes to nothing is bad_branch",
          bad_branch.get("code") == "bad_branch", bad_branch.get("code"))
    _, bad_slug = post("bad", path=str(basic), branch="valid-name", name="///")
    check("a name that slugifies to nothing is bad_slug",
          bad_slug.get("code") == "bad_slug", bad_slug.get("code"))

    checkedout = repos["checkedout"]["path"]
    _, elsewhere = post("checkedout", path=str(checkedout), branch="feature", name="feature-two")
    check("a branch checked out elsewhere is branch_checked_out",
          elsewhere.get("code") == "branch_checked_out", elsewhere.get("code"))
    check("the refusal names the worktree",
          elsewhere.get("worktree") == str(repos["checkedout"]["linked"]),
          str(elsewhere.get("worktree")))
    check("no directory was made for the refused branch",
          not (checkedout / ".worktrees" / "feature-two").exists())

    _, unknown = post("unknown", path=str(basic), branch="ghost")
    check("no local branch and no origin ref is unknown_base",
          unknown.get("code") == "unknown_base", unknown.get("code"))
    _, unknown_base = post("unknown-base", path=str(basic), branch="ghost", base="no-such-ref")
    check("a base that does not resolve is unknown_base",
          unknown_base.get("code") == "unknown_base", unknown_base.get("code"))
    check("a refused add leaves no directory behind",
          not (basic / ".worktrees" / "ghost").exists())

    relative = client.post("/worktree", json={"path": "relative", "branch": "feature"})
    check("a relative path is HTTP 400", relative.status_code == 400,
          f"HTTP {relative.status_code}")
    no_path = client.get("/worktrees", params={"path": "relative"})
    check("listing a relative path is HTTP 400", no_path.status_code == 400,
          f"HTTP {no_path.status_code}")

    section("worktree: fixed argv")
    seen: list[list[str]] = []
    real_run = api._run

    async def recording(argv: Any, **kwargs: Any) -> Any:
        seen.append([str(item) for item in argv])
        return await real_run(argv, **kwargs)

    api._run = recording
    try:
        _, argv_body = post("argv", path=str(shapes), branch="argvtest", base="main")
    finally:
        api._run = real_run
    check("the add succeeds", argv_body.get("ok") is True, argv_body.get("code"))
    adds = [row for row in seen if "add" in row and "worktree" in row]
    check("git worktree add ran once", len(adds) == 1, str(len(adds)))
    check("the add is an argv list with the expected tokens",
          bool(adds) and adds[0] == ["git", "-C", str(shapes), "worktree", "add", "-b",
                                     "argvtest", str(shapes / ".worktrees" / "argvtest"),
                                     "main"],
          str(adds[-1:] if adds else None))
    check("no argv token is an empty string",
          all(isinstance(item, str) and item for row in seen for item in row))

    # An @ inside a ref name is legal git (`user@host`): sanitizing must keep it
    # rather than silently renaming a real remote branch into one that is not there.
    check("an @ inside a branch name survives sanitizing",
          api._sanitize_branch("user@host") == "user@host",
          api._sanitize_branch("user@host"))
    check("a leading dash still cannot reach git",
          not api._sanitize_branch("--upload-pack=touch").startswith("-"),
          api._sanitize_branch("--upload-pack=touch"))
    check("shell metacharacters never survive",
          api._sanitize_branch("x; touch /tmp/pwned") == "x-touch-/tmp/pwned",
          api._sanitize_branch("x; touch /tmp/pwned"))

    print(f"\n{len(FAILURES)} failure(s) in {time.monotonic() - started:.1f}s")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
