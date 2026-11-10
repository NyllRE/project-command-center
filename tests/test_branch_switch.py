#!/usr/bin/env python3
"""Branch switch harness for project-command-center: POST /branch/switch.

Plain Python like tests/test_git_routes.py: throwaway repositories under
``~/.hermes/cache/scratch/pcc-branch`` (override with PCC_BRANCH_SCRATCH), the
plugin router mounted on a bare FastAPI app, driven with TestClient. Every
answer must be HTTP 200 with ``ok`` and ``code``; a 500 fails the run. A
malformed path is the one deliberate 400.

    python3 tests/test_branch_switch.py
"""

from __future__ import annotations

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
    "PCC_BRANCH_SCRATCH", Path.home() / ".hermes" / "cache" / "scratch" / "pcc-branch"))
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
    """One repository per guard, plus the remote and overwrite cases."""
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)
    repos: dict[str, Any] = {}

    basic = init("basic")
    commit(basic, "base", {"a.txt": "base\n"})
    git(basic, "branch", "feature")
    repos["basic"] = {"path": basic}

    dirty = init("dirty")
    commit(dirty, "base", {"a.txt": "base\n"})
    git(dirty, "branch", "feature")
    (dirty / "a.txt").write_text("changed\n")
    (dirty / "staged.txt").write_text("staged\n")
    git(dirty, "add", "staged.txt")
    repos["dirty"] = {"path": dirty}

    untracked = init("untracked")
    commit(untracked, "base", {"a.txt": "base\n"})
    git(untracked, "branch", "feature")
    (untracked / "new.txt").write_text("untracked\n")
    repos["untracked"] = {"path": untracked}

    merge = init("merge")
    commit(merge, "base", {"f.txt": "base\n"})
    git(merge, "checkout", "-q", "-b", "other")
    commit(merge, "other", {"f.txt": "other\n"})
    git(merge, "checkout", "-q", "main")
    commit(merge, "main", {"f.txt": "main\n"})
    subprocess.run(["git", "-C", str(merge), "merge", "other"], capture_output=True,
                   text=True, env={**os.environ, **_ENV})
    repos["merge"] = {"path": merge, "marker": merge / ".git" / "MERGE_HEAD"}

    detached = init("detached")
    commit(detached, "base", {"a.txt": "base\n"})
    git(detached, "branch", "feature")
    git(detached, "checkout", "-q", "--detach")
    commit(detached, "only on HEAD", {"orphan.txt": "orphan\n"})
    repos["detached"] = {"path": detached}

    worktree = init("worktree")
    commit(worktree, "base", {"a.txt": "base\n"})
    git(worktree, "branch", "feature")
    linked = SCRATCH / "linked"
    git(worktree, "worktree", "add", "-q", str(linked), "feature")
    repos["worktree"] = {"path": worktree, "linked": linked}

    inside = basic / "src" / "deep"
    inside.mkdir(parents=True)
    repos["inside"] = {"path": inside, "root": basic}

    plain = SCRATCH / "plain"
    plain.mkdir()
    repos["plain"] = {"path": plain}

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
    git(seed, "checkout", "-q", "main")
    subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"],
                   capture_output=True, text=True, env={**os.environ, **_ENV}, check=True)
    clone = SCRATCH / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], capture_output=True,
                   text=True, env={**os.environ, **_ENV}, check=True)
    repos["clone"] = {"path": clone}

    overwrite = init("overwrite")
    commit(overwrite, "base", {"a.txt": "base\n"})
    git(overwrite, "checkout", "-q", "-b", "feature")
    commit(overwrite, "adds g", {"g.txt": "from feature\n"})
    git(overwrite, "checkout", "-q", "main")
    (overwrite / "g.txt").write_text("untracked\n")
    repos["overwrite"] = {"path": overwrite}

    return repos


def load_api() -> Any:
    path = PLUGIN / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("pcc_api_branch_test", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pcc_api_branch_test"] = module
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
        response = client.post("/branch/switch", json=body)
        try:
            payload = response.json()
        except ValueError:
            payload = {"ok": None, "code": "not_json", "error": response.text[:200]}
        if response.status_code not in (200, 400):
            FAILURES.append(f"{label} HTTP {response.status_code}")
            print(f"  FAIL {label} HTTP {response.status_code}")
        return response.status_code, payload

    section("switch: ok, already_on and untracked")
    basic = repos["basic"]["path"]
    status, switched = post("basic", path=str(basic), branch="feature")
    check("feature switch is HTTP 200 ok", status == 200 and switched.get("ok") is True,
          switched.get("code"))
    check("switch reports the previous branch", switched.get("previous_branch") == "main",
          switched.get("previous_branch"))
    check("switch reports the branch", switched.get("branch") == "feature")
    check("feature is really checked out",
          git(basic, "symbolic-ref", "--short", "HEAD").strip() == "feature")

    status, already = post("basic", path=str(basic), branch="feature")
    check("same branch is already_on", status == 200 and already.get("code") == "already_on",
          already.get("code"))

    untracked = repos["untracked"]["path"]
    status, allowed = post("untracked", path=str(untracked), branch="feature")
    check("untracked files alone are allowed", allowed.get("ok") is True, allowed.get("code"))
    check("untracked files are reported", "untracked" in (allowed.get("warning") or ""),
          allowed.get("warning"))
    check("untracked file is still there", (untracked / "new.txt").is_file())

    section("switch: every refusal")
    dirty = repos["dirty"]["path"]
    before = (dirty / "a.txt").read_text()
    _, dirty_body = post("dirty", path=str(dirty), branch="feature")
    check("tracked changes are dirty_tree", dirty_body.get("code") == "dirty_tree",
          dirty_body.get("code"))
    check("dirty_tree carries counts", dirty_body.get("dirty", {}).get("staged") == 1
          and dirty_body.get("dirty", {}).get("unstaged") == 1, str(dirty_body.get("dirty")))
    check("never discards the change", (dirty / "a.txt").read_text() == before)
    check("dirty repo did not move", git(dirty, "symbolic-ref", "--short", "HEAD").strip() == "main")

    merge = repos["merge"]["path"]
    check("fixture really is mid-merge", repos["merge"]["marker"].exists())
    _, merge_body = post("merge", path=str(merge), branch="other")
    check("merge in progress is operation_in_progress",
          merge_body.get("code") == "operation_in_progress", merge_body.get("code"))

    detached = repos["detached"]["path"]
    _, detached_body = post("detached", path=str(detached), branch="feature")
    check("detached commit on no branch is refused",
          detached_body.get("code") == "detached_unreachable", detached_body.get("code"))

    worktree = repos["worktree"]["path"]
    _, elsewhere = post("worktree", path=str(worktree), branch="feature")
    check("branch checked out elsewhere is refused",
          elsewhere.get("code") == "checked_out_elsewhere", elsewhere.get("code"))
    check("refusal names the worktree", elsewhere.get("worktree") == str(repos["worktree"]["linked"]),
          str(elsewhere.get("worktree")))

    _, linked = post("linked", path=str(repos["worktree"]["linked"]), branch="main")
    check("a linked worktree is not the main checkout",
          linked.get("code") == "not_main_worktree", linked.get("code"))

    _, inside = post("inside", path=str(repos["inside"]["path"]), branch="feature")
    check("a folder inside a bigger repo is inside_root",
          inside.get("code") == "inside_root", inside.get("code"))

    plain = repos["plain"]["path"]
    _, not_repo = post("plain", path=str(plain), branch="feature")
    check("a folder outside git is not_a_repo", not_repo.get("code") == "not_a_repo",
          not_repo.get("code"))

    _, unknown = post("basic", path=str(basic), branch="does-not-exist")
    check("a branch not in the clone is unknown_branch",
          unknown.get("code") == "unknown_branch", unknown.get("code"))

    for bad in ("-force", "bad..name", "", "a b"):
        _, bad_body = post("basic", path=str(basic), branch=bad)
        check(f"branch {bad!r} is bad_branch", bad_body.get("code") == "bad_branch",
              bad_body.get("code"))

    missing = SCRATCH / "gone"
    _, missing_body = post("gone", path=str(missing), branch="feature")
    check("missing folder is folder_missing", missing_body.get("code") == "folder_missing",
          missing_body.get("code"))

    section("switch: remote track, overwrite refusal, malformed path")
    clone = repos["clone"]["path"]
    _, tracked = post("clone", path=str(clone), branch="remoteonly")
    check("remote-only branch switches", tracked.get("ok") is True, tracked.get("code"))
    check("remote-only branch tracks origin", tracked.get("tracked") == "origin/remoteonly",
          str(tracked.get("tracked")))
    check("remote-only branch is checked out",
          git(clone, "symbolic-ref", "--short", "HEAD").strip() == "remoteonly")

    overwrite = repos["overwrite"]["path"]
    _, failed = post("overwrite", path=str(overwrite), branch="feature")
    check("untracked file that git would overwrite is switch_failed",
          failed.get("code") == "switch_failed", failed.get("code"))
    check("switch_failed is a short plain line", isinstance(failed.get("error"), str)
          and "\n" not in failed["error"] and len(failed["error"]) <= 200,
          failed.get("error"))

    relative = client.post("/branch/switch", json={"path": "relative", "branch": "feature"})
    check("a relative path is HTTP 400", relative.status_code == 400,
          f"HTTP {relative.status_code}")

    section("switch: fixed argv and cache")
    fresh = init("argv")
    commit(fresh, "base", {"a.txt": "base\n"})
    git(fresh, "branch", "feature")
    seen: list[list[str]] = []
    real_run = api._run

    async def recording(argv: Any, **kwargs: Any) -> Any:
        seen.append([str(item) for item in argv])
        return await real_run(argv, **kwargs)

    api._run = recording
    try:
        _, argv_body = post("argv", path=str(fresh), branch="feature")
    finally:
        api._run = real_run
    check("argv switch succeeds", argv_body.get("ok") is True, argv_body.get("code"))
    switch_calls = [row for row in seen if "switch" in row and "--track" not in row]
    check("git switch ran once", len(switch_calls) == 1, str(len(switch_calls)))
    check("switch is an argv list with one branch token",
          bool(switch_calls) and switch_calls[0][:2] == ["git", "-C"]
          and switch_calls[0][-2:] == ["switch", "feature"], str(switch_calls[-1:] if switch_calls else None))
    check("no argv token carries a shell string",
          all(isinstance(item, str) and item for row in seen for item in row))

    api._CACHE[("repo", str(fresh))] = api._Entry(time.monotonic() + 60, None, {"branch": "feature"})
    git(fresh, "switch", "-q", "main")
    post("argv", path=str(fresh), branch="feature")
    check("the /repo cache entry is dropped after a switch",
          ("repo", str(fresh)) not in api._CACHE)

    print(f"\n{len(FAILURES)} failure(s) in {time.monotonic() - started:.1f}s")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
