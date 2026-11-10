# Development

## Layout

```
plugin.yaml                 agent half manifest: tools, hooks, optional env
__init__.py                 register(ctx): three board tools, two board hooks
hooks.py                    card context before a turn, PR auto-link after a terminal call
board_store.py              the SQLite store both halves write through
desktop/plugin.js           the Desktop half: board, project page, GitHub section, git graph
dashboard/plugin_api.py     the FastAPI router
dashboard/manifest.json     { name, api }
docs/                       this directory, plus covers and screenshots
```

## Running it from source

```bash
git clone https://github.com/NyllRE/project-command-center
cd project-command-center
make dev              # copies desktop/plugin.js to ~/.hermes/desktop-plugins/
hermes plugins validate .
hermes plugins doctor .
```

The Desktop half hot reloads from `~/.hermes/desktop-plugins/project-command-center/`, so a JS change appears on the next load with no restart. The Python half is imported once when `hermes-serve` boots, so a route change needs:

```bash
systemctl --user restart hermes-serve.service
```

That restart also kills any shell running inside the same unit, which is why it is run on its own.

## Tests

```bash
node --test tests/graph_layout.test.mjs tests/github_status.test.mjs tests/session_lane.test.mjs
python3 tests/test_hooks.py
python3 tests/test_session_status.py
python3 tests/test_git_routes.py
python3 tests/test_branch_switch.py
python3 tests/test_icons.py
python3 tests/test_avatars.py
python3 tests/test_github_states.py
```

| Suite | Covers |
|---|---|
| `graph_layout.test.mjs` | the lane engine, sliced out of the real `desktop/plugin.js` and checked against hand-written cases and real repositories |
| `github_status.test.mjs` | icon colour, status classification and filter union |
| `session_lane.test.mjs` | lane resolution: explicit status, PR state, project fallback, nothing |
| `test_hooks.py` | both agent hooks, every positive and negative case, a timing loop |
| `test_session_status.py` | the link-row lane migration, session scope, project fallback |
| `test_git_routes.py` | graph and diff routes, including a hostile repository whose config points at a script |
| `test_branch_switch.py` | every refusal code, exercised against real repositories |
| `test_icons.py` | search order, the `.git` fence, depth and count caps |
| `test_avatars.py` | host allow-list, size normalisation, bot flags |
| `test_github_states.py` | the `/github` state contract, open, closed, merged and draft |

The node tests read the real `desktop/plugin.js` rather than a copy, and the Python suites import the real router by path, so a test cannot pass against a stale duplicate.

## Conventions

- No shell strings. Every subprocess is fixed argv.
- A failure is HTTP 200 with `ok: false` and a stable `code`, except a bad path on a git route, which is 400.
- Writes are batched into one Python change so one restart covers them.
- Versions live in `plugin.yaml`, `dashboard/manifest.json` and `__init__.py`, and move together.
