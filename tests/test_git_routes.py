#!/usr/bin/env python3
"""Git history harness for project-command-center: /git/graph and /git/commit/{sha}.

Plain Python like tests/test_hooks.py: it builds throwaway repositories under
``~/.hermes/cache/scratch/pcc-git`` (override with PCC_GIT_SCRATCH), mounts the
plugin router on a bare FastAPI app and drives it with TestClient. Every route
answer must be HTTP 200 with ``ok`` and ``code``; a 500 fails the run.

    python3 tests/test_git_routes.py

The hostile repository is the point of the diff flags: its own .git/config names
an external diff and a textconv command that touch a marker file, so the proof is
that a plain ``git show`` creates the marker and the route does not.
"""

from __future__ import annotations

import sys as _sys

_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import _bootstrap  # noqa: E402,F401  finds the Hermes tree and a sandbox home

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN = Path(__file__).resolve().parent.parent
SCRATCH = Path(os.environ.get("PCC_GIT_SCRATCH",
                              Path.home() / ".hermes" / "cache" / "scratch" / "pcc-git"))
FAILURES: list[str] = []
_TICK = [1700000000]
_BASE_ENV = {
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
    _TICK[0] += 60
    stamp = f"@{_TICK[0]} +0300"
    env = {**os.environ, **_BASE_ENV,
           "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                            text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def write(repo: Path, rel: str, data: "str | bytes") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data)


def commit(repo: Path, message: str, files: dict[str, "str | bytes"]) -> str:
    for rel, data in files.items():
        write(repo, rel, data)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def init(name: str) -> Path:
    repo = SCRATCH / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    return repo


def build_fixtures() -> dict[str, Any]:
    """Repositories covering every branch of the two routes."""
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)
    repos: dict[str, Any] = {}

    linear = init("linear")
    first = commit(linear, "first", {"a.txt": "one\n"})
    second = commit(linear, "second", {"a.txt": "one\ntwo\n"})
    third = commit(linear, "third", {"b.txt": "three\n"})
    repos["linear"] = {"path": linear, "first": first, "second": second, "third": third}

    merge = init("merge")
    base = commit(merge, "base", {"base.txt": "base\n"})
    git(merge, "checkout", "-q", "-b", "feature")
    feature = commit(merge, "feature work", {"feature.txt": "feature\n"})
    git(merge, "checkout", "-q", "main")
    main_side = commit(merge, "main work", {"main.txt": "main\n"})
    git(merge, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    merge_head = git(merge, "rev-parse", "HEAD").strip()
    repos["merge"] = {"path": merge, "base": base, "feature": feature,
                      "main": main_side, "merge": merge_head}

    octo = init("octopus")
    commit(octo, "base", {"base.txt": "base\n"})
    git(octo, "checkout", "-q", "-b", "one")
    commit(octo, "one", {"one.txt": "one\n"})
    git(octo, "checkout", "-q", "main")
    git(octo, "checkout", "-q", "-b", "two")
    commit(octo, "two", {"two.txt": "two\n"})
    git(octo, "checkout", "-q", "main")
    git(octo, "checkout", "-q", "-b", "three")
    commit(octo, "three", {"three.txt": "three\n"})
    git(octo, "checkout", "-q", "main")
    commit(octo, "main", {"main.txt": "main\n"})
    git(octo, "merge", "-q", "--no-ff", "-m", "octopus", "one", "two", "three")
    octo_head = git(octo, "rev-parse", "HEAD").strip()
    repos["octopus"] = {"path": octo, "head": octo_head,
                        "parents": git(octo, "rev-parse", "HEAD^@").split()}

    rename = init("rename")
    body = "".join(f"line {index}\n" for index in range(12))
    commit(rename, "old file", {"old.txt": body})
    git(rename, "mv", "old.txt", "new.txt")
    write(rename, "new.txt", body.replace("line 5", "line five changed"))
    git(rename, "add", "-A")
    git(rename, "commit", "-q", "-m", "rename and edit")
    repos["rename"] = {"path": rename,
                       "head": git(rename, "rev-parse", "HEAD").strip()}

    typechange = init("typechange")
    commit(typechange, "file", {"link": "ordinary file\n"})
    (typechange / "link").unlink()
    os.symlink("target", typechange / "link")
    git(typechange, "add", "-A")
    git(typechange, "commit", "-q", "-m", "become a symlink")
    repos["typechange"] = {"path": typechange,
                           "head": git(typechange, "rev-parse", "HEAD").strip()}

    binary = init("binary")
    commit(binary, "binary", {"blob.bin": bytes(range(0, 256)) * 8})
    git(binary, "commit", "-q", "--allow-empty", "-m", "keep")
    write(binary, "blob.bin", bytes(range(0, 256)) * 8 + b"\x00\x01\x02")
    write(binary, "new.bin", b"\x00\x02\x04\x06")
    git(binary, "add", "-A")
    git(binary, "commit", "-q", "-m", "binary change")
    repos["binary"] = {"path": binary,
                       "head": git(binary, "rev-parse", "HEAD").strip()}

    unicode = init("unicode")
    head = commit(unicode, "unicode path", {"sp ace/héllo wörld.txt": "héllo\n"})
    repos["unicode"] = {"path": unicode, "head": head}

    big = init("big")
    big_text = ("A line of generated text that is long enough to matter.\n" * 40000)
    repos["big"] = {"path": big, "head": commit(big, "large file", {"big.txt": big_text})}

    liney = init("liney")
    line_text = "".join(f"l{index}\n" for index in range(7000))
    repos["liney"] = {"path": liney, "head": commit(liney, "many lines", {"lines.txt": line_text})}

    wide = init("wide")
    wide_files = {f"f{index:03}.txt": f"{index}\n" for index in range(305)}
    repos["wide"] = {"path": wide, "head": commit(wide, "many files", wide_files)}

    many = init("many")
    shas = []
    for index in range(55):
        shas.append(commit(many, f"commit {index}", {"count.txt": f"{index}\n"}))
    git(many, "tag", "-a", "early", "-m", "early tag", shas[0])
    repos["many"] = {"path": many, "shas": shas}

    empty = init("empty")
    repos["empty"] = {"path": empty}

    plain = SCRATCH / "plain"
    plain.mkdir()
    repos["plain"] = {"path": plain}

    hostile = init("hostile")
    commit(hostile, "one", {"a.txt": "one\n"})
    commit(hostile, "attrs", {".gitattributes": "*.txt diff=evil\n"})
    marker = SCRATCH / "hostile.marker"
    script = SCRATCH / "hostile.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    script.chmod(0o755)
    git(hostile, "config", "diff.external", str(script))
    git(hostile, "config", "diff.evil.textconv", str(script))
    hostile_head = commit(hostile, "edit", {"a.txt": "two\n"})
    git(hostile, "config", "gpg.program", str(script))
    git(hostile, "config", "commit.gpgsign", "true")
    repos["hostile"] = {"path": hostile, "head": hostile_head, "marker": marker,
                        "script": script}
    return repos


def load_api() -> Any:
    path = PLUGIN / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("pcc_api_git_test", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pcc_api_git_test"] = module
    spec.loader.exec_module(module)
    return module


def get(client: TestClient, url: str, **params: Any) -> tuple[int, dict[str, Any]]:
    response = client.get(url, params=params)
    try:
        body = response.json()
    except ValueError:
        body = {"ok": None, "code": "not_json", "error": response.text[:200]}
    if response.status_code != 200:
        FAILURES.append(f"{url} HTTP {response.status_code}")
        print(f"  FAIL {url} HTTP {response.status_code}: {body}")
    return response.status_code, body


def sha_shape(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(
        char in "0123456789abcdef" for char in value)


def main() -> int:
    if SCRATCH.resolve() == (Path.home() / ".hermes").resolve():
        sys.exit("refusing to build fixtures in ~/.hermes")
    started = time.monotonic()
    repos = build_fixtures()
    print(f"fixtures in {SCRATCH} ({time.monotonic() - started:.1f}s)")

    api = load_api()
    api._GRAPH_CACHE.clear()
    api._DIFF_CACHE.clear()
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)

    section("version and codes")
    check("PLUGIN_VERSION is 0.4.2", api.PLUGIN_VERSION == "0.4.2", api.PLUGIN_VERSION)
    check("empty_repo is a failure code", "empty_repo" in api.FAILURE_CODES)
    check("bad_sha is a failure code", "bad_sha" in api.FAILURE_CODES)

    section("graph: linear repository")
    linear = repos["linear"]["path"]
    status, graph = get(client, "/git/graph", path=str(linear))
    check("HTTP 200 and ok", status == 200 and graph.get("ok") is True, graph.get("code"))
    check("commits are newest first", [c["sha"] for c in graph["commits"]] == [
        repos["linear"]["third"], repos["linear"]["second"], repos["linear"]["first"]])
    first_row = graph["commits"][0]
    check("commit carries sha, short, parents", sha_shape(first_row["sha"])
          and first_row["short"] == first_row["sha"][:7]
          and first_row["parents"] == [repos["linear"]["second"]])
    check("author name and email", first_row["author"] == {
        "name": "Fixture Author", "email": "fixture@example.com"})
    check("authored and committed dates", first_row["authored_at"].endswith("+03:00")
          and first_row["committed_at"].endswith("+03:00"))
    check("subject", first_row["subject"] == "third", first_row["subject"])
    check("head names the branch", graph["head"] == {
        "sha": repos["linear"]["third"], "branch": "main", "detached": False})
    check("is_head only on the tip", [c["is_head"] for c in graph["commits"]]
          == [True, False, False])
    check("local ref points at the tip",
          any(r["short"] == "main" and r["kind"] == "local" and r["sha"] == repos["linear"]["third"]
              and r["is_head"] and not r["is_remote"] for r in graph["refs"]))
    check("fingerprint is 16 hex", isinstance(graph["fingerprint"], str)
          and len(graph["fingerprint"]) == 16
          and all(c in "0123456789abcdef" for c in graph["fingerprint"]))
    check("limit echoes 200", graph["limit"] == 200)
    check("not truncated", graph["truncated"] is False)
    check("refs_outside is zero", graph["refs_outside"] == 0)

    section("graph: cache and freshness")
    t0 = time.monotonic()
    _, warm = get(client, "/git/graph", path=str(linear))
    warm_ms = (time.monotonic() - t0) * 1000
    check("second call cached", warm.get("cached") is True, f"{warm_ms:.0f} ms")
    calls: list[list[str]] = []
    real_run = api._run

    async def counting(argv: Any, **kwargs: Any) -> Any:
        calls.append(list(argv))
        return await real_run(argv, **kwargs)

    api._run = counting
    try:
        _, again = get(client, "/git/graph", path=str(linear))
    finally:
        api._run = real_run
    check("cache hit still true with the counter", again.get("cached") is True)
    check("cache hit runs only the work tree read and F1..F3", len(calls) == 4,
          f"{len(calls)} calls: {[c[3] for c in calls]}")
    check("no log on a cache hit", all("log" not in call for call in calls))

    _, refreshed = get(client, "/git/graph", path=str(linear), refresh=1)
    check("refresh=1 skips the cache", refreshed.get("cached") is False)

    before = refreshed["fingerprint"]
    commit(linear, "fourth", {"c.txt": "four\n"})
    _, after_commit = get(client, "/git/graph", path=str(linear))
    check("fingerprint changes after a commit", after_commit["fingerprint"] != before,
          f"{before} -> {after_commit['fingerprint']}")
    check("new call is not cached", after_commit["cached"] is False)
    check("new tip is in the window", after_commit["commits"][0]["subject"] == "fourth")

    git(linear, "checkout", "-q", "-b", "side")
    _, after_checkout = get(client, "/git/graph", path=str(linear))
    check("fingerprint changes after checkout",
          after_checkout["fingerprint"] != after_commit["fingerprint"])
    check("head follows the checkout", after_checkout["head"]["branch"] == "side")
    git(linear, "checkout", "-q", "main")

    section("graph: refs, tags, ahead and behind")
    many = repos["many"]["path"]
    _, many_graph = get(client, "/git/graph", path=str(many), limit=50)
    check("limit 50 truncates 55 commits", len(many_graph["commits"]) == 50
          and many_graph["truncated"] is True)
    check("tag outside the window is counted", many_graph["refs_outside"] == 1,
          str(many_graph["refs_outside"]))
    _, page = get(client, "/git/graph", path=str(many), limit=500)
    tag = next((r for r in page["refs"] if r["short"] == "early"), None)
    check("annotated tag is present", tag is not None)
    check("tag sha is the peeled commit, not the tag object",
          tag is not None and tag["sha"] == repos["many"]["shas"][0]
          and tag["kind"] == "tag" and tag["is_remote"] is False)
    check("tag is not a head", tag is not None and tag["is_head"] is False)

    _, clamped_low = get(client, "/git/graph", path=str(many), limit=5)
    check("limit below 50 clamps up", clamped_low["limit"] == 50)
    _, clamped_high = get(client, "/git/graph", path=str(many), limit=9000)
    check("limit above 500 clamps down", clamped_high["limit"] == 500)

    tracking = init("tracking")
    base_sha = commit(tracking, "base", {"a.txt": "a\n"})
    tip_sha = commit(tracking, "tip", {"a.txt": "b\n"})
    git(tracking, "branch", "basebranch", base_sha)
    git(tracking, "branch", "--set-upstream-to=basebranch", "main")
    git(tracking, "update-ref", "refs/remotes/origin/main", tip_sha)
    git(tracking, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _, track_graph = get(client, "/git/graph", path=str(tracking))
    local = next(r for r in track_graph["refs"] if r["short"] == "main")
    check("ahead and behind come from the track atom", local["ahead"] == 1
          and local["behind"] == 0 and local["upstream"] == "basebranch",
          f"ahead={local['ahead']} behind={local['behind']} up={local['upstream']}")
    check("origin/HEAD is the default branch", track_graph["default_branch"] == "origin/main",
          str(track_graph["default_branch"]))
    check("origin/HEAD is not a pill",
          all(r["short"] != "origin/HEAD" for r in track_graph["refs"]))
    check("remote ref is marked remote",
          any(r["short"] == "origin/main" and r["is_remote"] for r in track_graph["refs"]))
    git(tracking, "checkout", "-q", "--detach", tip_sha)
    _, detached = get(client, "/git/graph", path=str(tracking), refresh=1)
    check("detached head carries the sha and no branch",
          detached["head"] == {"sha": tip_sha, "branch": None, "detached": True},
          str(detached["head"]))

    section("graph: failure codes")
    _, empty = get(client, "/git/graph", path=str(repos["empty"]["path"]))
    check("empty repo is empty_repo", empty["ok"] is False and empty["code"] == "empty_repo",
          empty.get("code"))
    check("empty repo sentence", empty["error"] == "No commits yet.", str(empty.get("error")))
    check("empty repo shape is stable", empty["commits"] == [] and empty["refs"] == []
          and empty["head"]["sha"] is None)
    _, plain = get(client, "/git/graph", path=str(repos["plain"]["path"]))
    check("folder outside git is not_a_repo", plain["code"] == "not_a_repo", plain.get("code"))
    _, missing = get(client, "/git/graph", path=str(SCRATCH / "gone"))
    check("missing folder is folder_missing", missing["code"] == "folder_missing",
          missing.get("code"))
    relative = client.get("/git/graph", params={"path": "relative/folder"})
    check("relative path is HTTP 400", relative.status_code == 400, str(relative.status_code))
    _, commit_plain = get(client, f"/git/commit/{repos['linear']['third']}",
                          path=str(repos["plain"]["path"]))
    check("commit outside git is not_a_repo", commit_plain["code"] == "not_a_repo",
          commit_plain.get("code"))

    section("commit: root, metadata and hunks")
    root_sha = repos["linear"]["first"]
    _, root = get(client, f"/git/commit/{root_sha}", path=str(linear))
    check("root commit ok", root.get("ok") is True, root.get("code"))
    check("root diff base", root["diff_base"] == "root")
    check("root has no parents", root["parents"] == [])
    check("root file is an addition", all(f["status"] == "A" for f in root["files"]),
          str([(f["path"], f["status"]) for f in root["files"]]))
    check("root additions counted", root["stats"]["additions"] >= 1)
    check("author and committer blocks", root["author"]["email"] == "fixture@example.com"
          and root["committer"]["email"] == "fixture@example.com")
    check("body is trimmed", root["body"] == "", repr(root["body"]))

    _, second = get(client, f"/git/commit/{repos['linear']['second']}", path=str(linear))
    file_row = second["files"][0]
    check("file row keys", set(file_row) == {
        "path", "old_path", "status", "additions", "deletions", "binary", "truncated",
        "hunks"}, str(sorted(file_row)))
    check("modified file status", file_row["status"] == "M" and file_row["old_path"] is None)
    check("numstat totals match stats", sum(f["additions"] for f in second["files"])
          == second["stats"]["additions"]
          and sum(f["deletions"] for f in second["files"]) == second["stats"]["deletions"])
    hunk = file_row["hunks"][0]
    check("hunk header and ranges", hunk["header"].startswith("@@ -1 +1,2 @@")
          and hunk["old_start"] == 1 and hunk["old_lines"] == 1
          and hunk["new_start"] == 1 and hunk["new_lines"] == 2, hunk["header"])
    check("hunk lines are (op, text)", hunk["lines"][0] == [" ", "one"]
          and hunk["lines"][-1] == ["+", "two"], str(hunk["lines"]))
    check("no newline marker uses the text only",
          all(not text.startswith(" ") for op, text in hunk["lines"] if op == "\\"))
    check("on_remote false without remote refs", root["on_remote"] is False)

    _, cached_commit = get(client, f"/git/commit/{repos['linear']['second']}", path=str(linear))
    check("commit second call cached", cached_commit["cached"] is True)

    section("commit: merge, octopus, rename, binary, unicode")
    _, merged = get(client, f"/git/commit/{repos['merge']['merge']}", path=str(repos["merge"]["path"]))
    check("merge is ok", merged.get("ok") is True, merged.get("code"))
    check("merge diff base", merged["diff_base"] == "first_parent")
    check("merge parents are both recorded", len(merged["parents"]) == 2)
    merge_paths = {f["path"]: f for f in merged["files"]}
    check("merge shows what it brought in", "feature.txt" in merge_paths,
          str(sorted(merge_paths)))
    check("merge file is an addition", merge_paths.get("feature.txt", {}).get("status") == "A")
    check("merge hunk has added lines",
          any(line[0] == "+" for h in merge_paths["feature.txt"]["hunks"]
              for line in h["lines"]))

    _, octo = get(client, f"/git/commit/{repos['octopus']['head']}",
                  path=str(repos["octopus"]["path"]))
    check("octopus commit records every parent", len(octo["parents"]) == 4,
          str(len(octo["parents"])))
    check("octopus diff is against the first parent",
          {"one.txt", "two.txt", "three.txt"}.issubset({f["path"] for f in octo["files"]}))

    _, renamed = get(client, f"/git/commit/{repos['rename']['head']}", path=str(repos["rename"]["path"]))
    rename_row = renamed["files"][0]
    check("rename status", rename_row["status"] == "R", rename_row["status"])
    check("rename keeps both paths", rename_row["path"] == "new.txt"
          and rename_row["old_path"] == "old.txt",
          f"{rename_row['old_path']} -> {rename_row['path']}")
    check("rename carries edit hunks", bool(rename_row["hunks"]))

    _, typechange = get(client, f"/git/commit/{repos['typechange']['head']}",
                        path=str(repos["typechange"]["path"]))
    check("mode change is status T", typechange["files"][0]["status"] == "T",
          typechange["files"][0]["status"])

    _, binary = get(client, f"/git/commit/{repos['binary']['head']}",
                    path=str(repos["binary"]["path"]))
    binary_rows = {row["path"]: row for row in binary["files"]}
    binary_row = binary_rows["blob.bin"]
    check("binary is flagged", binary_row["binary"] is True, str(binary_row))
    check("binary has no hunks and no counts", binary_row["hunks"] == []
          and binary_row["additions"] == 0 and binary_row["deletions"] == 0)
    check("new binary file is an addition", binary_rows["new.bin"]["status"] == "A"
          and binary_rows["new.bin"]["binary"] is True,
          str(binary_rows["new.bin"]))

    _, uni = get(client, f"/git/commit/{repos['unicode']['head']}",
                 path=str(repos["unicode"]["path"]))
    check("unicode and space path survives", uni["files"][0]["path"]
          == "sp ace/héllo wörld.txt", uni["files"][0]["path"])

    section("commit: cap, sha validation and not found")
    _, big = get(client, f"/git/commit/{repos['big']['head']}", path=str(repos["big"]["path"]))
    check("2 MB patch is ok", big.get("ok") is True, big.get("code"))
    check("2 MB patch is capped at bytes", big["truncated"] is True
          and big["truncated_reason"] == "bytes",
          f"{big['truncated']} {big['truncated_reason']}")
    check("cap still lists the file", len(big["files"]) == 1
          and big["files"][0]["truncated"] is True)
    check("stats still count the whole file", big["stats"]["additions"] >= 40000,
          str(big["stats"]))
    _, liney = get(client, f"/git/commit/{repos['liney']['head']}",
                   path=str(repos["liney"]["path"]))
    check("7000 short lines hit the line cap", liney["truncated"] is True
          and liney["truncated_reason"] == "lines",
          f"{liney['truncated']} {liney['truncated_reason']}")
    check("line cap keeps the file row", len(liney["files"]) == 1)
    _, wide = get(client, f"/git/commit/{repos['wide']['head']}", path=str(repos["wide"]["path"]))
    check("305 files hit the file cap", wide["truncated"] is True
          and wide["truncated_reason"] == "files", f"{wide['truncated_reason']}")
    check("file cap lists 300 rows", len(wide["files"]) == 300, str(len(wide["files"])))
    check("file cap keeps the real total", wide["stats"]["files"] == 305,
          str(wide["stats"]))

    for bad in ("zzz", "123", "x" * 40, "abc123g"):
        _, bad_body = get(client, f"/git/commit/{bad}", path=str(linear))
        check(f"bad sha {bad[:8]!r} is bad_sha",
              bad_body.get("ok") is False and bad_body.get("code") == "bad_sha",
              bad_body.get("code"))

    _, unknown = get(client, "/git/commit/" + "0" * 40, path=str(linear))
    check("unknown sha is not_found", unknown["code"] == "not_found", unknown.get("code"))
    check("not_found sentence is plain", "No commit" in (unknown["error"] or ""),
          str(unknown.get("error")))
    blob_sha = git(linear, "rev-parse", "HEAD:a.txt").strip()
    _, blob = get(client, f"/git/commit/{blob_sha}", path=str(linear))
    check("blob sha is not_found", blob["code"] == "not_found", blob.get("code"))

    section("commit: hostile repository")
    hostile = repos["hostile"]
    subprocess.run(["git", "-C", str(hostile["path"]), "show", "--format=", "--patch",
                    hostile["head"]], capture_output=True, text=True, env=os.environ)
    check("the hostile config really runs on a plain git show", hostile["marker"].exists())
    if hostile["marker"].exists():
        hostile["marker"].unlink()
    _, hostile_body = get(client, f"/git/commit/{hostile['head']}", path=str(hostile["path"]))
    check("hostile repo still answers ok", hostile_body.get("ok") is True,
          hostile_body.get("code"))
    check("no external diff or textconv ran", not hostile["marker"].exists())
    check("hostile diff still has hunks", bool(hostile_body["files"][0]["hunks"]))

    section("real repositories")
    hermes = Path.home() / ".hermes" / "hermes-agent"
    if hermes.exists():
        t0 = time.monotonic()
        _, real = get(client, "/git/graph", path=str(hermes), limit=50)
        cold = (time.monotonic() - t0) * 1000
        check("hermes-agent graph ok", real.get("ok") is True, real.get("code"))
        check("hermes-agent limit 50 truncates", len(real["commits"]) == 50
              and real["truncated"] is True, f"{cold:.0f} ms cold")
        check("hermes-agent carries refs", len(real["refs"]) > 0)
    else:
        print("  skip hermes-agent is not at ~/.hermes/hermes-agent")

    print(f"\n{len(FAILURES)} failure(s) in {time.monotonic() - started:.1f}s")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
