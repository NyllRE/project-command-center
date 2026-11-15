# Backend

The plugin mounts one FastAPI router under `/api/plugins/project-command-center/`, behind the gateway's normal auth. Every route answers HTTP 200 with `ok: false` and a stable `code` on failure, so the UI can render a state instead of blanking. A bad `path` on a git route is the one exception, and answers HTTP 400.

## Read routes

| Route | What it returns |
|---|---|
| `GET /health` | `ok`, `version`, `gh: {available, authenticated, user}`, `token` (bool, never the value), `isolation_note` |
| `GET /repo?path=<abs>` | `path`, `root`, `remote`, `owner`, `repo`, `branch`, `detached`, `dirty: {count, staged, unstaged, untracked}`, `ahead`, `behind`, `branches: [{name, upstream, ahead, behind, last_commit_at, subject}]` |
| `GET /git/graph?path=<abs>&limit=200&refresh=0` | `root`, `fingerprint`, `limit` (50..500), `truncated`, `head: {sha, branch, detached}`, `default_branch`, `commits: [{sha, short, parents, author, authored_at, committed_at, subject, is_head}]`, `refs: [{name, short, kind, sha, upstream, ahead, behind, gone, is_head, is_remote}]`, `refs_outside` |
| `GET /git/commit/{sha}?path=<abs>` | one commit: metadata, `diff_base` (`first_parent` or `root`), `on_remote`, `stats`, `files: [{path, old_path, status, additions, deletions, binary, truncated, hunks: [{header, old_start, old_lines, new_start, new_lines, lines: [[op, text]]}]}]`, `truncated`, `truncated_reason` |
| `GET /github?path=<abs>&state=all&limit=50&refresh=0` | `states`, `issues[]`, `prs[]`, `counts`. Issue rows carry `state` and `state_reason`; PR rows carry `state`, `draft` and `merged` |
| `GET /github/issue/{n}` / `GET /github/pr/{n}` | one item: body, comments, reviews, checks, labels, assignees, linked items, `author_avatar`, `author_bot`. The body and each of the first 30 comments carry `reactions: [{content, count, mine}]`; a reactions read that fails is `[]` for that object only |
| `GET /github/refs?path=<abs>&numbers=1,2,3` | `refs` keyed by number, up to 50 in one batched GraphQL call |
| `GET /github/counts?path=<abs>&path=<abs>...` | open issue and PR counts for up to 50 folders in one query, one alias per repository |
| `GET /icon?path=<abs>&refresh=0` | the best mark for a project folder: a local favicon or logo, then a declared homepage favicon or the GitHub owner avatar, then a stack glyph |
| `GET /worktrees?path=<abs>` | `<mainRoot>`, `branch`, `isMain`, `detached`, `locked` per worktree, from `git worktree list --porcelain` |
| `GET /board` | `statuses`, `links`, `projects`: the board state the Desktop half renders |
| `GET /goals?session_ids=<id,id,...>` | the ACTIVE `/goal` on up to 60 sessions: `goals` keyed by session id, each with `title`, `status`, `turns_used`, `max_turns`, `subgoals`, `subgoals_done`, `gates`, `updated_at`. Sessions with no goal, a paused, done or cleared goal, and unknown ids are absent |
| `GET /sessions/archived?limit=<1..300>` | archived sessions, newest first: `sessions[]` with `id`, `title`, `source`, `cwd`, `git_branch`, `started_at`, `last_active`, `message_count`, plus `total` (the real count, not the page size). `ok: false` with `sessions_unavailable` when the state database cannot be read |
| `GET /diagnose` | why a project reads as it does: auth route, which folders resolved, what was skipped |

## Write routes

| Route | Body | Effect |
|---|---|---|
| `POST /branch/switch?path=<abs>` | `{branch}` | checks the branch out in the project's main folder |
| `POST /worktree` | `{path, branch, name?, base?}` | makes `<mainRoot>/.worktrees/<slug>` with `git worktree add` |
| `POST /github/issue/{n}/comment` | `{body}` | comments on an issue, text on stdin to `gh` |
| `POST /github/pr/{n}/comment` | `{body}` | comments on a PR conversation |
| `POST /github/issue/{n}/state` | `{state}` | opens or closes an issue |
| `POST /github/pr/{n}/state` | `{state}` | opens or closes a PR |
| `POST /github/reaction` | `{kind, number?, comment_id?, path?, content}` | toggles one reaction on `issue`, `pr`, `issue_comment` or `review_comment` as the `gh` account; `content` is one of `+1`, `-1`, `laugh`, `hooray`, `confused`, `heart`, `rocket`, `eyes`. Answers `{ok, reactions, toggled}`; refusals are 200 with `bad_kind`, `bad_content`, `gh_unavailable` or `gh_error` |
| `POST /board/status` | `{project_id, status, note, session_id?, scope?}` | sets a project lane, or a session's lane when a session is named |
| `POST /board/lanes` | `{id?, label, order?}` | creates a custom lane, or renames and/or moves a live one |
| `POST /board/lanes/order` | `{ids: [...]}` | a permutation of the live lane ids, written as positions `0..n-1` |
| `POST /board/lanes/delete` | `{id, reassign?}` | deletes a custom lane; `reassign` moves its rows in the same transaction |
| `POST /board/link` | `{session_id, project_id, issue?, pr?, pr_url?}` | links a session to its work item |
| `POST /board/projects` | `{project_id, label, paths}` | records a project and its folders |
| `POST /board/seed` | `{}` | seeds project rows from Hermes Projects |

`/branch/switch` refuses rather than forcing. Codes: `switched`, `already_on`, `dirty_tree`, `unknown_branch`, `bad_branch`, `not_a_repo`, `inside_root`, `not_main_worktree`, `checked_out_elsewhere`, `operation_in_progress`, `detached_unreachable`, `switch_failed`. Untracked files alone are allowed and reported as a warning.

`/board/lanes/delete` refuses a system lane (`system_lane`), an unknown id (`unknown_lane`), the last lane (`last_lane`), and a lane rows still hold without `reassign` (`lane_in_use`, carrying the counts). The seven system lanes (`unfiled`, `backlog`, `active`, `review`, `blocked`, `done`, `canceled`) can be renamed and reordered but never removed; a lane nothing claims is `unfiled`, never `backlog`.

`/worktree` creates and never repairs. Refusal codes: `folder_missing`, `not_a_repo`, `bad_branch`, `bad_slug`, `worktree_exists`, `branch_checked_out`, `unknown_base`, `worktree_failed`, `upstream_error`. The target is always `<mainRoot>/.worktrees/<slug>`, where mainRoot is the first record of `git worktree list --porcelain`; a directory already on disk is a refusal, never a reuse, and the branch is checked out as it is when it exists locally or made from `base` (defaulting to `origin/<branch>` only when that ref resolves).

## Caching

| Payload | Key | TTL |
|---|---|---|
| git graph | root plus a 16 hex fingerprint over the refs and HEAD reads | 300 s, and the expensive log only reruns when the fingerprint moves |
| commit diff | root plus full sha | 600 s |
| GitHub reads | path plus route parameters | 60 s |
| icons | local: folder plus file plus mtime, remote: 3600 s, glyph: 300 s | 300 s to 3600 s |
| session goals | sorted comma list of session ids (up to 60) | 30 s |

`refresh=1` bypasses the cache on every route that has one. Failures are never cached.

## Notes

- `/github` defaults to `state=all`, which is two REST reads per resource behind one cache entry, because GitHub cannot return open and closed rows in a single windowed call. `state=open` and `state=closed` behave as single reads.
- Everything the plugin runs is a fixed argv subprocess call. There is no shell string anywhere, and `git show` always runs with `--no-ext-diff --no-textconv --no-show-signature`, so a repository's own config cannot execute anything through a diff.
- The plugin has no bearer token of its own. It reaches GitHub through the configured `GITHUB_TOKEN` for reads and through the authenticated `gh` CLI for writes.
- `GET /sessions/archived` exists because the gateway cannot answer it: `session.list` lists with `archived_only=False` and exposes no flag for it, so an archived session is absent from every board row. On one working store that is 1,929 of 2,010 sessions, which from the Desktop reads as sessions vanishing. The plugin reads its own `state.db` through the same published `hermes_state` surface the goals read uses (`SessionDB(read_only=True)`, `list_sessions_rich(archived_only=True, compact_rows=True, order_by_last_active=True)`), and `total` comes from a matching `session_count(archived_only=True)` so the footer can say "the newest 300 of 1,929" instead of reporting its own cap as the truth. `ARCHIVED_MAX` in this module and `ARCHIVED_LIMIT` in `desktop/plugin.js` are the same number: asking for more is a 422.
- `GET /goals` reads each session's persisted `/goal` row from `state_meta` (`goal:<session id>`) through the **published** `hermes_state` module, with `SessionDB(read_only=True)`. It does not import `hermes_cli.goals` (a private core module: catalog rule 9, "public surfaces only"), and a read-only handle can never become a second writer beside the gateway's registry handle. `session.control.read` was the other candidate and was rejected: it resolves a **live** session by its minted runtime id, while a board card carries the stored id from `session.list`, so it answers `4001` for every card and the gateway docs state these polls never restore a cold session. See `dashboard/plugin_api.py`, "Session goals".
