# Project Command Center

Every project you are working on, in one board: its sessions, its lane, its git state, and the GitHub issues and pull requests that belong to it. Click an issue and a chat about it opens in that project's folder with the issue already sitting in the composer.

<!-- screenshot: docs/board.png once the board capture lands -->

## Why

Hermes already knows which project a session belongs to, what branch it is on and what the folder's git state is. It does not show any of that in one place, and it does not connect a session to the issue or pull request it is working on. This plugin joins the four things a person actually holds in their head while working, project, session, branch, issue, and shows them as one card per project.

## What you get

- **The board.** One column per project with its live sessions, branch chips, running rings, token and cost totals, and a lane per project (backlog, active, review, blocked, done) that you set by hand or let the agent move.
- **The project page.** A stats strip, sessions grouped by lane and branch, activity over time, and the folder's local git state: branch, dirty counts, ahead and behind.
- **The GitHub section.** Five cuts of the same data: In flight, By issue, By branch, Unlinked, Shipped. Pull requests link to issues through GitHub's own `closingIssuesReferences`, with a keyword fallback, never a guess.
- **A chat from an issue.** New chat opens a fresh session anchored to the project's folder, with the issue number, title, URL, repo and one line of instruction already in the composer. Nothing is sent until you press Enter.
- **An agent that knows its card.** Before each turn a short card context (project, lane, issue, PR) is injected into the model's request. After a terminal call, a hook links the pull request that `gh pr create` just opened and moves the card to review. Three tools let the agent read and move the board itself.
- **Worktrees by convention.** Work lives in `<repo>/.worktrees/<name>`, one checkout per branch, instead of a second clone that reinstalls everything.

## The two halves

Shipped as one installable folder:

- `desktop/plugin.js`, the Desktop half: titlebar button, the board page at `/project-command-center`, and the project detail page with a stats strip and sessions grouped by lane.
- `dashboard/plugin_api.py`, the Python half: git and GitHub reads for a project folder plus the board store routes, mounted at `/api/plugins/project-command-center/` and reached from the Desktop half with `ctx.rest`.
- `board_store.py`, the shared state: one local SQLite file holding each project's lane, each session's project and work item, and the project ids the board knows.
- `__init__.py`, the agent half: three tools (`pcc_board`, `pcc_set_status`, `pcc_work_item`) over the same store, in the `project_command_center` toolset.


## Commands

```bash
make dev       # copy desktop/plugin.js into ~/.hermes/desktop-plugins/project-command-center/
make validate  # run the catalog admission checks
```

`make dev` copies the Desktop half only. The Python half is imported by the gateway process from the plugin's install folder, so it needs the plugin enabled in `plugins.enabled` in `config.yaml`; the gateway imports the router at startup, and hot mounts it on the first request when it is not already mounted.

## Layout

```
plugin.yaml                 agent half manifest: provides_tools (three), provides_hooks (two), optional_env: GITHUB_TOKEN
__init__.py                 register(ctx): the three board tools and the two board hooks
hooks.py                    the two agent hooks: card context before a turn, PR auto-link after a terminal
board_store.py              the SQLite store both halves write through
desktop/plugin.js           the Desktop half
dashboard/manifest.json     { "name": "project-command-center", "api": "plugin_api.py" }
dashboard/plugin_api.py     the backend router
tests/test_hooks.py         hook harness: both callbacks, every positive and negative case, a timing loop
```

## Backend

The git, GitHub and diagnose routes read. The `/board` routes read and write the local board store. Failures come back as HTTP 200 with `ok: false` and a stable `code` so a card can show a state instead of blanking, except a bad `path` to a git route, which is HTTP 400.

| Route | Returns |
|---|---|
| `GET /health` | `ok`, `version`, `gh: {available, authenticated, user}`, `token` (bool, never the value), `isolation_note` |
| `GET /repo?path=<abs folder>` | `path`, `root`, `remote`, `owner`, `repo`, `branch`, `detached`, `dirty: {count, staged, unstaged, untracked}`, `ahead`, `behind`, `branches: [{name, upstream, ahead, behind, last_commit_at, subject}]`, `error` |
| `GET /github?path=<abs folder>&state=open&limit=50&refresh=0` | `repo`, `owner`, `url`, `fetched_at`, `cached`, `auth`, `rate_limit`, `issues[]`, `prs[]`, `counts`, `updated_at`, `error` |
| `GET /github/pr/{number}?path=<abs folder>` | one PR: the list row fields plus `body`, `files_changed` (first 100 names), `reviews`, `comments_count`, `linked_issues`, `timeline_linked_issues` |
| `GET /github/issue/{number}?path=<abs folder>` | one issue: `body`, `labels`, `assignees`, `comments_count`, `timeline_prs`, `linked_prs` |
| `GET /board?session_id=` | `statuses[]`, `links[]`, `projects_synced`, `auto_link: {session_id, project_id, via, cwd}` |
| `POST /board/status` | body `{project_id, status, note?}` → the written `status` row |
| `POST /board/link` | body `{session_id, project_id, repo, issue?, pr?, title?, pr_url?}` → the written `link` row |
| `POST /board/projects` | body `{projects: [{id, label, paths}]}` → `projects_synced`, `changed`; idempotent upsert, broadcasts `kind: "projects"` only when something changed |
| `POST /board/seed` | body `{statuses: [...]}` → `seeded`, `skipped`; only while the store has no statuses |

`issues[]` excludes pull requests even though GitHub returns PRs from its issues endpoint. `prs[].head_ref` is returned verbatim because it is the key the Desktop half joins sessions on (`session.git_branch`).

### Linking

PR to issue links come from GitHub's own answer, GraphQL `closingIssuesReferences`, and are merged with the `closes #12` / `fixes #12` / `refs #12` keyword fallback parsed from the title and body. Issue to PR is the same map read backwards, taken from the board cache, plus the issue timeline's `cross-referenced` events. The PR detail also reports `timeline_linked_issues` from its own timeline, and the issue detail reports `timeline_prs` the same way.

### Caching

One in process cache keyed by (owner, repo, endpoint, params), 60 s TTL, 20 s for `/repo`. With a token or `gh` login the board read revalidates with the stored ETag: a 304 extends the TTL and returns the cached payload instead of refetching four times. `refresh=1` bypasses the cache.

GraphQL resolves one pull request per alias at roughly 0.6 s on a busy repository, so the board enriches the 10 most recently updated PRs per page; `prs[].enriched` marks them. Rows past that window carry the REST fields (state, draft, refs, keyword links) and `enriched: false`, and the review and check counters in `counts` cover the enriched window only.

## Board store

`board_store.py` is the one place the board's state lives: `<hermes home>/plugin-data/project-command-center/board.db`, opened with the public `plugins.plugin_storage.plugin_db` helper (WAL, `check_same_thread=False`, profile scoped). Three tables:

| Table | One row per | Columns |
|---|---|---|
| `status` | project | `status` (backlog, active, review, blocked, done), `note`, `source` (user, agent, auto), `updated_at` |
| `link` | session | `project_id`, `repo`, `issue`, `pr`, `pr_url`, `title`, `resolved_by` (`cwd` / `desktop` / `agent`), `created_at`, `updated_at` |
| `project` | known project | `label`, `paths` (JSON array of absolute paths), `updated_at` |

Every write is one short `BEGIN IMMEDIATE` transaction, so the gateway process (routes) and the agent process (tools) can write the same file at once; `plugin_db` sets a 5 s busy timeout. No row is cached in memory on either side: both halves load `board_store.py` by path and it opens a fresh connection per call, which is why a tool's write is visible to the next `GET /board`.

`resolve_project(session_id, explicit)` decides which project a request or a tool is about, in this order: the explicit id, a unique project label match, the session's `link` row, then the longest path prefix of `agent.runtime_cwd.resolve_agent_cwd()` over `project.paths`, else `None`. When the runtime `session_id` has no link row, the lookup walks the session's compression lineage (`hermes_state.SessionDB.get_compression_lineage`) and uses the stored ancestor's row, so a compression between turns does not hide the card or write an orphan.

The agent tools (`project_command_center` toolset) are `pcc_board` (read the board, or one project's lane and links), `pcc_set_status` (move a lane, optional note, recorded with `source: "agent"`) and `pcc_work_item` (attach or detach the issue or PR the current session works on). `pcc_set_status` and `pcc_work_item` create a bare project row only while the project table is empty — a fresh install the Desktop has not synced yet — so an early agent write is not lost; once any project is known, an id the table does not have is a typo and both tools answer `unknown_project`, exactly like the routes. A lane move that omits the note keeps the note already on the card.

Every successful write broadcasts `board.changed` through `hermes_cli.plugin_events.broadcast_plugin_event` with `{project_id, kind, status?, issue?, pr?, source}`, so the Desktop refreshes without polling. `POST /board/projects` broadcasts `kind: "projects"` only when a label or path actually changed; `POST /board/link` records `resolved_by: "desktop"`.

The `/board` handlers are plain `def`, not `async def`: their SQLite calls block (5 s busy timeout), so FastAPI runs them in its threadpool instead of stalling the gateway event loop. A `board.db` deleted or replaced under a running process is re-created on the next call: the schema cache is verified against `sqlite_master` and cleared on `DatabaseError`, rather than failing `no such table` until a restart.

Failure codes on these routes, all with HTTP 200: `bad_status`, `bad_source`, `bad_resolved_by`, `unknown_project`, `empty_project`, `empty_session`, `bad_issue`, `bad_pr`, `bad_projects`, `bad_kind`, `bad_number`, `not_attached`, `empty_seed`, `seed_not_empty`, `store_error`. The store's compare-and-set lane move (`move_status_if_current`) can also answer `status_changed`.

## Agent hooks

Two hooks ship beside the tools, registered from the same `register(ctx)` and declared under `provides_hooks`. Both run in the agent process, both use the local store, and neither makes a network call or starts a process.

| Hook | Reads | Writes |
|---|---|---|
| `pre_llm_call` | the session's `link` row, and its project's `status` and `project` rows | nothing |
| `post_tool_call` | `tool_name`, `args.command` and `result` of a terminal call, the `link` row, the `status` row, the kill file | `link.pr`, `link.pr_url`; `status` (lane `review`, `source: "auto"`) |

**`pre_llm_call`** fires once per turn. When the payload's `session_id` has a link row, it injects at most 400 characters naming the project label, the card's lane, the linked issue number and title, the PR number (or `no PR yet`), and two instructions: put `Closes #<issue>` in the PR body, and move the card with `pcc_set_status` (`backlog`, `active`, `review`, `blocked`, `done`). No project, no injection: the callback returns `None`, and a missing, empty or `None` `session_id` does the same. Every failure is caught and becomes `None`, so a broken board never costs a turn. The read is bounded: one indexed link lookup, the project's `status` row by primary key and its `project` row by primary key — never a scan of the board. When the runtime id has no row, the compression lineage is consulted on the same handle. Hermes bounds the callback at `plugins.hook_callback_timeout` (30 s by default); 1000 invocations against a warm local store measured 1.3 ms mean, 1.9 ms max, and an unlinked session (the lineage-miss path) 1.4 ms p95.

**`post_tool_call`** fires after a `terminal` call that really ran `gh pr create` and whose result contains `https://github.com/<owner>/<repo>/pull/<n>`. The command is matched as a command (`^`/newline, or after `;`, `&`, `|`, `(`), not as a substring, so `grep -rn 'gh pr create' .` that prints a PR URL does not count. The call must also not have failed: an `error` / `cancelled` / `timeout` hook `status`, an `error_type`/`error_message`, a result JSON with `error` or `exit_code != 0`, or unambiguous failure text (`already exists`, `failed to create`, …) stops the write. On success it writes that number and URL to the session's link row, moves the card to `review` with `source: "auto"` **only** when the card was in `backlog` or `active` (a card in `review`, `blocked` or `done` keeps its lane and its note), and broadcasts `board.changed` with `kind: "link"` and the PR number. The lane move is a compare-and-set inside one `BEGIN IMMEDIATE` transaction, so a user's move landing in between wins rather than being overwritten. It writes nothing when the tool is not `terminal`, the command is not a `gh pr create` invocation, the result carries no PR URL or reports failure, the session has no link row, the row names a `repo` that differs from the URL's `owner/repo`, the row names no repo and was not resolved from the session's own working directory (`resolved_by` not `cwd`), or the kill file exists. A runtime session id newer than the stored row (a compression continuation) resolves to the ancestor row, so the write lands where the Desktop reads it.

**Kill file.** `<hermes home>/plugin-data/project-command-center/auto_link.off`: create it and the next auto-link is skipped, because the check runs on every call — no restart, no config edit. Delete it to re-enable. A data directory that cannot be resolved counts as off.

Neither hook touches the repository or GitHub: no `git` or `gh` subprocess, no HTTP, nothing written outside `board.db`.

## Desktop: the GitHub section

The project detail page carries an `On GitHub` section between the stats strip and the per-lane sessions. It reads `/github` (open) plus `/repo`, and `/github` with `state=closed` only when the Shipped preset is selected. Five presets over the same data, each a different cut of it:

| Preset | What it shows |
|---|---|
| In flight (default) | Open PRs by `updated_at`, with head branch, checks, review decision, linked issue chips, session count on `head_ref`, age. One quiet row counts open issues with no PR. |
| By issue | Open issues by `updated_at`, issues with a linked PR first, labels (3 chips then `+N`), assignee initials, comments. Linked PRs sit under each issue as sub-rows. |
| By branch | The union of `/repo` branches, `prs[].head_ref` and session `git_branch`. Running session first, then branches with a PR, then last commit. Ahead/behind and upstream from `/repo`. |
| Unlinked | Branches with sessions but no PR, then PRs with no linked issue. |
| Shipped | Closed PRs with `merged === true`, capped at 20, each line naming the issues it closed. |

Sessions are joined by `git_branch` from `projects.project_sessions`, which the page already loads: one `Map` to `{count, live}`, where live means the session is starting, working, streaming or resuming. A PR or issue row expands in place (body first 600 characters, changed file names, reviews, issue links) and fetches its detail lazily. Preset choice persists in `ctx.storage` under `githubPreset.v1`.

The section degrades in three steps: a payload `{error}` (no remote, no auth, rate limit) is a quiet inline line; a thrown request means the backend is not mounted yet and renders one compact ErrorState row naming `/github` and `/repo`; the rest of the page is unaffected either way.

## Credentials

`GITHUB_TOKEN` is optional and declared under `optional_env`. It is read with `agent.secret_scope.get_secret("GITHUB_TOKEN")` inside the request handler, never from `os.environ` directly, so a profile scope or multiplexing gateway resolves it correctly. With no token the backend falls back to the user's own authenticated `gh` CLI. With neither, public reads still work unauthenticated, and a private read reports `auth: "none"` with GitHub's error message. No credential file (`.env`, `auth.json`, `~/.config/gh/hosts.yml`) is opened by this plugin.

## What it runs, and what it reads (disclosure)

- `git` subprocesses with no shell and a 5 s timeout: `rev-parse`, `symbolic-ref`, `status --porcelain`, `for-each-ref`, `rev-list --left-right --count`, `remote`, `remote get-url`. Read only commands against the absolute folder you pass; nothing writes to the repository.
- `gh` subprocesses with no shell: `gh api -i` and `gh api graphql`, GET and query only. The child gets an allowlisted environment (PATH, HOME, locale, git config paths), so no credential variable is passed through to it.
- `https://api.github.com` over httpx when a token is set, or unauthenticated for public reads. No other host is contacted.
- No file contents are returned. No git, GitHub, issue, PR, comment, label or ref write exists in this plugin: the only writes are the two local ones below.
- Local writes, new in 0.3.0: `GET /board` reads and `POST /board/status`, `POST /board/link`, `POST /board/projects` and `POST /board/seed` write statuses (lane, note, who set it, when), session links (project, repo, issue, PR, title) and project ids (label, absolute paths) to `<hermes home>/plugin-data/project-command-center/board.db`, a SQLite file owned by the active profile. Nothing leaves the machine, and no credential is read or stored there.
- The three agent tools (`pcc_board`, `pcc_set_status`, `pcc_work_item`) read the same file and write `status` and `link` rows with `source: "agent"`. They make no network call and run no shell command.
- The two agent hooks (`pre_llm_call`, `post_tool_call`) read the same database inside the agent process, and `post_tool_call` also reads `tool_name`, `args.command` and `result` of `terminal` calls in memory (the text it was given, not a re-run). It writes the PR number and URL on a session's `link` row, and moves that project's `status` row to `review` with `source: "auto"` only from `backlog` or `active`. It checks `<hermes home>/plugin-data/project-command-center/auto_link.off` on every call and writes nothing while that file exists. No network call, no shell command, nothing written outside `board.db`.

## Phases

1. Shell: titlebar button, `/project-command-center` page, project kanban, sessions strip. Desktop only, state in `ctx.storage`.
2. Project detail page (`/project-command-center/project`): stats strip and sessions grouped by lane, from `projects.project_sessions`.
3. Backend: git status and branches, GitHub issues and PRs with issue links, plus the `On GitHub` section on the detail page. Shipped read only; writing back to GitHub comes later.
4. Agent layer: the server side board store and its `/board` routes, the three tools (`pcc_board`, `pcc_set_status`, `pcc_work_item`) and the two hooks (`pre_llm_call` card context, `post_tool_call` PR auto-link with a kill file). The store and tools shipped in 0.3.0; the running indicator and open-session-from-issue follow.
5. Polish and catalog packaging.
