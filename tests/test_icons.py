#!/usr/bin/env python3
"""Icon search harness for project-command-center: GET /icon local tiers.

Plain Python like tests/test_git_routes.py: each case builds a throwaway tree
under ``~/.hermes/cache/scratch/pcc-icons`` (override with PCC_ICON_SCRATCH),
mounts the plugin router on a bare FastAPI app and drives it with TestClient.

The remote step is switched off in these cases, so every fallback is the glyph
and no network is touched. What is under test is the fixed list's priority, the
bounded ``public`` walk, its .git fence and depth cap, the sniff, and the cache.

    python3 tests/test_icons.py
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN = Path(__file__).resolve().parent.parent
SCRATCH = Path(os.environ.get(
    "PCC_ICON_SCRATCH", Path.home() / ".hermes" / "cache" / "scratch" / "pcc-icons"))
FAILURES: list[str] = []
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"></svg>'
PNG = b"\x89PNG\r\n\x1a\n" + b"icon" * 8


def check(name: str, condition: bool, detail: Any = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {name}{f' ({detail})' if detail else ''}")
    if not condition:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n== {title} ".ljust(78, "="))


def put(root: Path, rel: str, data: bytes = SVG) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def load_api() -> Any:
    path = PLUGIN / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("pcc_api_icon_test", path)
    if spec is None or spec.loader is None:
        sys.exit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pcc_api_icon_test"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if SCRATCH.resolve() == (Path.home() / ".hermes").resolve():
        sys.exit("refusing to build fixtures in ~/.hermes")
    started = time.monotonic()
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)

    api = load_api()
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    # The remote step is not what this file tests; the glyph is a stable answer.
    api._icon_remote_off = lambda: True  # type: ignore[assignment]

    def icon(name: str, refresh: int = 0) -> dict[str, Any]:
        root = SCRATCH / name
        root.mkdir(parents=True, exist_ok=True)
        response = client.get("/icon", params={"path": str(root), "refresh": refresh})
        try:
            body = response.json()
        except ValueError:
            body = {"ok": None, "code": "not_json", "error": response.text[:200]}
        if response.status_code != 200:
            FAILURES.append(f"{name} HTTP {response.status_code}")
        return body

    def detail(body: dict[str, Any]) -> str:
        return str(body.get("detail") or "")

    section("fixed list stays first")
    root = SCRATCH / "root_wins"
    put(root, "favicon.svg")
    put(root, "playground/public/favicon.svg")
    body = icon("root_wins")
    check("root favicon beats a deeper public folder", body.get("kind") == "image"
          and detail(body) == "favicon.svg", f"{body.get('kind')} {detail(body)}")

    empty = SCRATCH / "empty_root_public"
    (empty / "public").mkdir(parents=True)
    put(empty, "playground/public/favicon.png", PNG)
    body = icon("empty_root_public")
    check("an empty root public falls to playground/public",
          body.get("kind") == "image" and detail(body) == "playground/public/favicon.png",
          f"{body.get('kind')} {detail(body)}")

    # built below in one go
    (SCRATCH / "app_vs_zzz" / "app" / "public").mkdir(parents=True, exist_ok=True)
    put(SCRATCH / "app_vs_zzz", "app/public/favicon.svg")
    put(SCRATCH / "app_vs_zzz", "zzz/public/favicon.svg")
    body = icon("app_vs_zzz", refresh=1)
    check("preferred parent app beats an alphabetical zzz at equal depth",
          body.get("kind") == "image" and detail(body) == "app/public/favicon.svg",
          detail(body))

    put(SCRATCH / "packages_vs_zzz", "zzz/public/favicon.svg")
    put(SCRATCH / "packages_vs_zzz", "packages/public/favicon.svg")
    body = icon("packages_vs_zzz")
    check("preferred parent packages beats zzz", detail(body) == "packages/public/favicon.svg",
          detail(body))

    put(SCRATCH / "hhh_vs_zzz", "zzz/public/favicon.svg")
    put(SCRATCH / "hhh_vs_zzz", "hhh/public/favicon.svg")
    body = icon("hhh_vs_zzz")
    check("equal depth and rank is alphabetical", detail(body) == "hhh/public/favicon.svg",
          detail(body))

    put(SCRATCH / "depth_first", "apps/web/public/favicon.svg")
    put(SCRATCH / "depth_first", "playground/public/favicon.svg")
    body = icon("depth_first")
    check("shallowest public wins over a preferred deeper one",
          detail(body) == "playground/public/favicon.svg", detail(body))

    section("bounded walk: depth, skips and the .git fence")
    put(SCRATCH / "too_deep", "a/b/c/public/favicon.svg")
    body = icon("too_deep")
    check("a public folder at depth 4 is ignored", body.get("kind") == "glyph",
          f"{body.get('kind')} {detail(body)}")
    check("the walk collected no public dirs at depth 4",
          api._icon_public_dirs(SCRATCH / "too_deep") == [])

    put(SCRATCH / "node_modules_case", "node_modules/x/public/favicon.svg")
    body = icon("node_modules_case")
    check("node_modules/public is ignored", body.get("kind") == "glyph", body.get("kind"))

    nested = SCRATCH / "nested_repo"
    (nested / "child" / ".git").mkdir(parents=True)
    (nested / "child" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    put(nested, "child/public/favicon.svg")
    body = icon("nested_repo")
    check("a nested repository's public folder is ignored",
          body.get("kind") == "glyph", f"{body.get('kind')} {detail(body)}")
    check("the .git fence is why", api._icon_public_dirs(nested) == [])

    outside = SCRATCH / "symlink_target"
    put(outside, "public/favicon.svg")
    symlinked = SCRATCH / "symlink_case"
    (symlinked / "link").parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, symlinked / "link")
    body = icon("symlink_case")
    check("a symlinked directory is not followed", body.get("kind") == "glyph",
          f"{body.get('kind')} {detail(body)}")

    put(SCRATCH / "html_ico", "public/favicon.ico", b"<!doctype html><title>404</title>")
    put(SCRATCH / "html_ico", "playground/public/favicon.svg")
    body = icon("html_ico")
    check("an HTML page named favicon.ico falls through",
          body.get("kind") == "image" and detail(body) == "playground/public/favicon.svg",
          detail(body))

    section("second step extras")
    put(SCRATCH / "suffix", "apps/web/public/nuxt-file-storage-logo.svg")
    body = icon("suffix")
    check("a *-logo file in a public folder is accepted",
          body.get("kind") == "image"
          and detail(body) == "apps/web/public/nuxt-file-storage-logo.svg", detail(body))

    put(SCRATCH / "suffix_favicon", "playground/public/red-favicon.png", PNG)
    body = icon("suffix_favicon")
    check("a *-favicon file in a public folder is accepted",
          body.get("kind") == "image" and detail(body) == "playground/public/red-favicon.png",
          detail(body))

    many = SCRATCH / "many_publics"
    for index in range(12):
        put(many, f"p{index:02d}/public/favicon.svg")
    dirs = api._icon_public_dirs(many)
    check("at most 8 public folders are kept", len(dirs) == api.ICON_PUBLIC_MAX, str(len(dirs)))
    ordered = sorted(dirs, key=lambda path: api._icon_public_order(many, path))
    check("the kept folders are the first eight alphabetically",
          [p.parent.name for p in ordered] == [f"p{index:02d}" for index in range(8)],
          str([p.parent.name for p in ordered]))

    section("cache and refresh")
    cache = SCRATCH / "cache_case"
    cache.mkdir()
    body = icon("cache_case")
    check("a folder with no image answers the glyph", body.get("kind") == "glyph",
          body.get("kind"))
    put(cache, "playground/public/favicon.svg")
    body = icon("cache_case")
    check("the miss is cached for 300 s", body.get("kind") == "glyph", body.get("kind"))
    body = icon("cache_case", refresh=1)
    check("refresh=1 bypasses the miss", body.get("kind") == "image"
          and detail(body) == "playground/public/favicon.svg", detail(body))
    body = icon("cache_case")
    check("the hit is cached and marked", body.get("kind") == "image"
          and body.get("cached") is True, f"{body.get('cached')}")

    section("real folders on this machine")
    red = Path.home() / "Development" / "RedEast"
    if red.is_dir():
        api._CACHE.clear()
        body = client.get("/icon", params={"path": str(red), "refresh": 1}).json()
        check("RedEast does not take a nested repo's favicon",
              not (body.get("source") == "local" and detail(body).count("/") > 1),
              f"{body.get('kind')} {body.get('source')} {detail(body)}")
        check("RedEast finds no public dir behind the .git fence",
              api._icon_public_dirs(red) == [], str(api._icon_public_dirs(red)))
    else:
        print("  skip RedEast is not at ~/Development/RedEast")

    ping = Path.home() / "Development" / "Ping"
    if ping.is_dir():
        api._CACHE.clear()
        body = client.get("/icon", params={"path": str(ping), "refresh": 1}).json()
        check("Ping resolves frontend/public/favicon.svg",
              body.get("kind") == "image" and detail(body) == "frontend/public/favicon.svg",
              f"{body.get('kind')} {detail(body)}")
    else:
        print("  skip Ping is not at ~/Development/Ping")

    print(f"\n{len(FAILURES)} failure(s) in {time.monotonic() - started:.1f}s")
    if FAILURES:
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
