# Project Command Center

**Your projects, your agent sessions, and your GitHub issues. One board.**

Click an issue and a session opens in that repo with the issue already sitting in the composer. The agent works. When it opens the pull request, the card moves itself to Review.

![Project Command Center](./docs/covers/cover-final.png)

**[Watch the demo on X](https://x.com/NyllRe/status/2120286833397354861)**

```bash
hermes plugins install NyllRE/project-command-center --enable
```

A Hermes Desktop plugin. If you already push to GitHub and use Hermes Projects, you are set. No new account, no new tracker, nothing to configure beyond the `gh` login you already have.

## The loop worth feeling

You stop translating between three tools.

![The board](./docs/screenshots/board.png)

1. The board shows every project at once: its lane, its live sessions, its branch, what is dirty, how many issues and pull requests are open.
2. You click an issue. A session opens **inside that project's folder**, with the number, title, URL and repo already in the composer. Nothing sends. You add your own first line if you want.
3. The agent reads the issue itself, works, and opens a pull request.
4. The card moves to Review on its own, and the next turn already knows which project, lane, issue and PR it is on. It never asks "which project am I in" again.

This is what you land in. The issue is already written for the agent, and nothing sends until you press send.

![A session opened from an issue](./docs/screenshots/composer.png)

## The project page

Everything about one project without leaving it: token spend, session count, what is live right now, and the GitHub work in progress.

![The project page](./docs/screenshots/project.png)

Sessions are grouped by the lane they belong to, each with its own status badge, so you can see what is blocked, what is waiting on review, and what is still moving.

## GitHub, without opening GitHub

Issues and pull requests render the way you expect: real markdown bodies, the full comment thread, reviews, checks, labels, assignees, and avatars that are actually people. You can comment and close from here.

![Pull requests](./docs/screenshots/pull-requests.png)

Status filters are cumulative. Start on open, tick Merged, and see merged and open together. Icons carry the GitHub colour language: green open, purple merged or closed, red not planned, grey draft.

![An issue, rendered in full](./docs/screenshots/issue-modal.png)

The whole issue, not a summary of it: markdown body, labels, assignees, and the comment thread with a composer at the bottom. Close or reopen without leaving.

`#123` references turn into chips with a preview, so a PR that mentions three issues is three clicks, not three tabs.

![A reference chip with its preview](./docs/screenshots/ref-preview.png)

### References arrive as cards, in the chat itself

When the agent references an issue or a pull request in its own reply, the reference lands as a small card inside the message: the GitHub state colour, the number, the title, who opened it and how long ago. Click it and the same detail view opens in place, thread and all. Follow a link inside it and you get a back chevron, so a chain of references does not lose your place.

A comment permalink is a card too: `#issuecomment-…` quotes the comment and opens the thread scrolled to it, with a brief outline on the line it landed on. Review-comment anchors are not in the thread read, so those cards name the pull request and say so.

The agent writes that card itself (`::pcc-gh{url="…"}`, a transcript directive this plugin registers), and it is told how to on every turn. A plain link stays a plain link, so nothing is invented and nothing is guessed.

## History that looks like history

A commit graph with real lanes. Fork, merge, track across branches, and open any commit to read its diff, files and line changes.

![The commit graph](./docs/screenshots/graph.png)

## Sessions that know what they are

Each branch row gives you a session, not a chore. Start one in a worktree, or check the branch out here first. When the project is already on that branch, the menu says so instead of pretending to switch.

![Every issue row is a session door](./docs/screenshots/issues.png)

A running session wears the app's travelling ring on its card, and while you drag one the lane under the cursor says "Release to drop in <lane>" before you let go.

Archived sessions are not silent: the bottom of the board tells you how many are hidden, and one click brings the newest 300 back into their lanes and project columns, each marked `Archived`. That count is why a board can look emptier than your session list: the gateway never returns archived rows, so the plugin reads them from its own state database.

Sessions carry a lane: unfiled, backlog, active, review, blocked, done or canceled. A merged PR or a completed issue lands in done, a closed one in canceled, and a session nothing claims sits in unfiled. The board summarises them per project, so "who is on review" is a glance instead of a hunt.

## What it never does

It does not send your prompts. It does not stash, discard or force anything in your repos. It does not phone home. Avatar images, GitHub and the favicon of a project you point it at are the only things it fetches, and every one of those paths has a kill file.

The details are honest and boring, which is how you want them:

- [docs/security.md](./docs/security.md): what it reads, what it writes, what runs on your machine
- [docs/backend.md](./docs/backend.md): every route, payload and cache
- [docs/development.md](./docs/development.md): the repo, the tests, how to run it from source

## Requirements

- Hermes Desktop, installed and running
- `gh` CLI, authenticated (`gh auth login`)
- At least one Hermes Project pointing at a folder that has a git remote on GitHub

Built by Ali Sokkar.
