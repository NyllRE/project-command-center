# Security

Short version: this plugin reads your projects and your GitHub work, writes only where you press something, and runs no shell strings. Everything below is what that means in practice.

## What runs on your machine

- Every subprocess call is a fixed argv `create_subprocess_exec` with no shell. Git commands are built from validated arguments, the folder path is treated as untrusted input, and `git show` always carries `--no-ext-diff --no-textconv --no-show-signature` so a repository's own config cannot execute a command through a diff.
- A branch name is validated with `git check-ref-format --branch` before it reaches `git switch`, which rejects leading dashes and revision expressions.
- Commit ids must match 7 to 40 hex characters and are resolved with `rev-parse --verify` before anything else uses them.

## What it writes

| Where | What |
|---|---|
| your repositories | nothing, with one exception: `POST /branch/switch` checks out the branch you pick in the project's main folder. It refuses on a dirty tree, a merge or rebase in progress, a branch already checked out in another worktree, or a detached HEAD with unreachable commits. It never stashes, discards or forces, and switching runs git's own hooks exactly as a checkout you run yourself would |
| GitHub | a comment on an issue or PR, and open or close on an issue or PR. Nothing is deleted, labelled, assigned, merged or edited. Each write runs as the `gh` CLI account you authenticated, with the body on stdin |
| Hermes home | `plugin-data/project-command-center/board.db`, a SQLite file owned by the active profile: project lanes, session lanes, and session to issue/PR links. Nothing leaves the machine and no credential is stored there |

## What it fetches

- `https://api.github.com` for reads, through `GITHUB_TOKEN` when set, unauthenticated for public data otherwise.
- `https://avatars.githubusercontent.com` for profile pictures, only https, only that host, only the `s`, `v` and `u` query keys, and the URL is rendered with `referrerPolicy: no-referrer`.
- `https://<homepage host>/favicon.ico` and `.png`, plus `https://github.com/<owner>.png`, for project icons. These are the only requests whose host comes from repository content, so the host is resolved before connecting and refused unless every address is globally routable. That blocks loopback, private ranges, link local, CGNAT and ULA addresses. Redirects are capped at two with the check repeated on each hop, there is no proxy from the environment, and reads stop at 256 KB.

## Kill files

Both are checked on every call, so they take effect without a restart.

| File | Effect |
|---|---|
| `<hermes home>/plugin-data/project-command-center/icons_remote.off` | no remote icon fetch at all, local disk or the stack glyph only |
| `<hermes home>/plugin-data/project-command-center/auto_link.off` | the PR auto-link hook writes nothing |

## Agent tools and hooks

- `pcc_board` reads the board. `pcc_set_status` sets a lane on a project or on the calling session. `pcc_work_item` attaches or detaches the current session's issue or PR. None of them make a network call or run a shell command.
- `pre_llm_call` injects one short block naming the project, lane, issue and PR of the current session, so the agent knows where it is. It is text in the request, not a tool call.
- `post_tool_call` inspects `terminal` calls for `gh pr create` and the resulting PR URL, then records the link and moves the session to review. It never overwrites a lane a person set, and it writes nothing while `auto_link.off` exists.

## Trust boundary

Routes are reachable only through the plugin's own mounted API, behind the gateway's header auth. A caller who holds that token can already drive an agent with a terminal, so the plugin does not add an escalation path. A directory path passed to a git or icon route is read with the permissions of the user running Hermes.

## Reporting

Open an issue at `NyllRE/project-command-center`, or mail the address on the GitHub profile for anything you would rather not post publicly.
