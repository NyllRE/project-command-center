// Proof for the inline GitHub reference cards (`::pcc-gh{...}`) in
// desktop/plugin.js.
//
// The pure half is not copied here: the `pcc-inline` marker block is sliced out
// of the live file and run in a vm, so anything in it that stops being
// self-contained or compilable fails this suite. The rest of the file is read as
// source, because the faults that actually shipped in this plugin before were
// CALL-SITE faults: a helper that was called but never defined (the click did
// nothing), and a style key referenced but never added (a card with no shape).
//
// Run with:
//   node --test tests/inline_cards.test.mjs

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const PLUGIN = join(ROOT, 'desktop', 'plugin.js')
const HOOKS = join(ROOT, 'hooks.py')
const BEGIN = '// pcc-inline:begin'
const END = '// pcc-inline:end'

const source = readFileSync(PLUGIN, 'utf8')
const hooks = readFileSync(HOOKS, 'utf8')
const beginAt = source.indexOf(BEGIN)
const endAt = source.indexOf(END)

assert.ok(beginAt >= 0, `${BEGIN} missing from desktop/plugin.js`)
assert.ok(endAt > beginAt, `${END} missing or before the begin marker`)
assert.equal(source.indexOf(BEGIN, beginAt + 1), -1, `${BEGIN} appears more than once`)

const block = source.slice(beginAt, endAt + END.length)

assert.doesNotMatch(block, /^\s*import\s/m, 'the inline block must not import anything')
assert.doesNotMatch(block, /^\s*export\s/m, 'the inline block must not export anything')
assert.doesNotMatch(block, /(?<![\w$])jsx\(/, 'the inline block must not render')
assert.doesNotMatch(block, /\u2014/, 'the inline block must not carry an em dash')

vm.runInThisContext(
  `${block}
;globalThis.__PCC_INLINE__ = {
  GH_INLINE_DIRECTIVE,
  ghInlineKind,
  ghInlineRef,
  ghInlineKey,
  ghInlineUrl,
  ghInlineSameRepo,
  ghInlineRow,
  ghInlineOpenable,
  ghInlineRepoText,
  ghInlineAnchor,
  ghInlineComment,
  ghInlineExcerpt
}`,
  { filename: PLUGIN }
)

const I = globalThis.__PCC_INLINE__

// --- what a reference IS -----------------------------------------------------

test('a pull request URL is read as a pull request', () => {
  const parsed = I.ghInlineRef({ url: 'https://github.com/acme/app/pull/34' })

  assert.equal(parsed.ok, true)
  assert.deepEqual(parsed.ref, {
    owner: 'acme',
    repo: 'app',
    kind: 'pr',
    number: 34,
    url: 'https://github.com/acme/app/pull/34'
  })
})

test('an issue URL is read as an issue, and a comment permalink collapses to its item', () => {
  const plain = I.ghInlineRef({ url: 'https://github.com/acme/app/issues/12' })

  assert.equal(plain.ref.kind, 'issue')
  assert.equal(plain.ref.number, 12)

  // The comment body is not fetched in this cut, so the card must be the ITEM
  // the comment lives in. Anything else would promise a comment view it does not
  // have.
  const comment = I.ghInlineRef({
    url: 'https://github.com/acme/app/issues/12#issuecomment-3456789'
  })

  assert.equal(comment.ok, true)
  assert.equal(comment.ref.kind, 'issue')
  assert.equal(comment.ref.number, 12)

  const review = I.ghInlineRef({
    url: 'https://github.com/acme/app/pull/34#discussion_r99887766'
  })

  assert.equal(review.ref.kind, 'pr')
  assert.equal(review.ref.number, 34)
})

test('a URL wins over number and kind, because it names the repository itself', () => {
  const parsed = I.ghInlineRef({
    url: 'https://github.com/acme/app/pull/34',
    kind: 'issue',
    number: '99',
    repo: 'other/thing'
  })

  assert.deepEqual({ owner: parsed.ref.owner, repo: parsed.ref.repo, kind: parsed.ref.kind, number: parsed.ref.number }, {
    owner: 'acme',
    repo: 'app',
    kind: 'pr',
    number: 34
  })
})

test('a bare number with a repo is a reference, defaulting to an issue', () => {
  const parsed = I.ghInlineRef({ number: '#7', repo: 'acme/app' })

  assert.equal(parsed.ok, true)
  assert.equal(parsed.ref.kind, 'issue')
  assert.equal(parsed.ref.number, 7)

  const pull = I.ghInlineRef({ kind: 'PR', number: '7', repo: 'acme/app' })

  assert.equal(pull.ref.kind, 'pr')
})

test('a URL for another host, or a reference missing a part, is refused with a reason', () => {
  assert.deepEqual(I.ghInlineRef({ url: 'https://gitlab.com/acme/app/issues/1' }), {
    ok: false,
    reason: 'not_github'
  })
  assert.deepEqual(I.ghInlineRef({ repo: 'acme/app' }), { ok: false, reason: 'no_number' })
  // `#0` is not a number GitHub has, in either shape.
  assert.deepEqual(I.ghInlineRef({ number: '0', repo: 'acme/app' }), { ok: false, reason: 'no_number' })
  assert.deepEqual(I.ghInlineRef({ number: '12' }), { ok: false, reason: 'no_repo' })
  assert.deepEqual(I.ghInlineRef({}), { ok: false, reason: 'no_number' })
  assert.deepEqual(I.ghInlineRef(null), { ok: false, reason: 'no_number' })
})

// --- matching the chat's repository ------------------------------------------

test('a reference only reads GitHub when it names this project repository', () => {
  const ref = I.ghInlineRef({ url: 'https://github.com/Acme/App/pull/34' }).ref

  assert.equal(I.ghInlineSameRepo(ref, { owner: 'acme', repo: 'app' }), true)
  // Another repository is the case the refs route cannot answer for: it would
  // hand back THIS repository's #34 and draw a confident wrong card.
  assert.equal(I.ghInlineSameRepo(ref, { owner: 'acme', repo: 'other' }), false)
  assert.equal(I.ghInlineSameRepo(ref, null), false)
})

// --- the row the card draws ---------------------------------------------------

test('a row is only read from a payload that answered', () => {
  const row = { number: 34, kind: 'pr', title: 'Ship it', state: 'open' }

  assert.equal(I.ghInlineRow({ ok: true, refs: { 34: row } }, 34), row)
  assert.equal(I.ghInlineRow({ ok: true, refs: {} }, 34), null)
  assert.equal(I.ghInlineRow({ ok: false, refs: { 34: row } }, 34), null)
  assert.equal(I.ghInlineRow({ ok: true, refs: { 34: 'nope' } }, 34), null)
  assert.equal(I.ghInlineRow(null, 34), null)
})

test('the fallback address is the one that was written, else the canonical one', () => {
  const written = I.ghInlineRef({ url: 'https://github.com/acme/app/pull/34#discussion_r1' }).ref

  assert.equal(I.ghInlineUrl(written), 'https://github.com/acme/app/pull/34#discussion_r1')

  const bare = I.ghInlineRef({ number: '12', repo: 'acme/app' }).ref

  assert.equal(I.ghInlineUrl(bare), 'https://github.com/acme/app/issues/12')
  assert.equal(I.ghInlineUrl({ ...bare, kind: 'pr' }), 'https://github.com/acme/app/pull/12')
  assert.equal(I.ghInlineUrl(null), '')
})

// The HIGH defect an independent review of the first cut found: the card's repo
// guard stopped the READ but not the CLICK, and the modal resolves its item by
// the folder's number alone. These cases pin the decision that closes it.
test('a card opens the modal unless it names another repository', () => {
  const ok = { folder: '/repo', number: 34, sameRepo: true }

  assert.equal(I.ghInlineOpenable(ok), true)

  // The one thing that must NOT open: the modal resolves its item by the FOLDER's
  // number, so another repository's #34 would show this repo's #34, with its
  // comment box and Close button.
  assert.equal(I.ghInlineOpenable({ ...ok, sameRepo: false }), false)
  assert.equal(I.ghInlineOpenable({ ...ok, sameRepo: undefined }), false)
  // No project folder for the chat: nothing to read against.
  assert.equal(I.ghInlineOpenable({ ...ok, folder: '' }), false)
  // A number GitHub does not have.
  assert.equal(I.ghInlineOpenable({ ...ok, number: 0 }), false)
})

test('the modal answers for a missing item itself, so the card still opens', () => {
  // Ali's correction: a reference to a number GitHub does not have (or a read
  // that failed) belongs in the modal, which shows its own failure in place.
  // Kicking the reader out to a browser is not what a card is for.
  const ok = { folder: '/repo', number: 34, sameRepo: true }

  assert.equal(I.ghInlineOpenable({ ...ok, row: null, readFailed: true }), true)
})

// --- comment permalinks -------------------------------------------------------

test('a permalink that carries a comment anchor names the comment', () => {
  const issue = I.ghInlineRef({ url: 'https://github.com/acme/app/issues/12#issuecomment-6886329479' })

  assert.equal(issue.ref.kind, 'issue')
  assert.equal(issue.ref.number, 12)
  assert.deepEqual({ ...issue.ref.comment }, { id: 6886329479, kind: 'issue' })

  // A review comment is a different object, and the thread read does not carry
  // it: the kind has to survive parsing so the card can say so.
  const review = I.ghInlineRef({ url: 'https://github.com/acme/app/pull/34#discussion_r99887766' })

  assert.equal(review.ref.kind, 'pr')
  assert.deepEqual({ ...review.ref.comment }, { id: 99887766, kind: 'review' })

  const reviewObject = I.ghInlineRef({ url: 'https://github.com/acme/app/pull/34#pullrequestreview-55' })

  assert.deepEqual({ ...reviewObject.ref.comment }, { id: 55, kind: 'review' })
})

test('a plain reference names no comment', () => {
  assert.equal('comment' in I.ghInlineRef({ url: 'https://github.com/acme/app/issues/12' }).ref, false)
  assert.equal('comment' in I.ghInlineRef({ repo: 'acme/app', number: '12' }).ref, false)
  assert.equal(I.ghInlineAnchor('https://github.com/acme/app/issues/12'), null)
  assert.equal(I.ghInlineAnchor('#issuecomment-0'), null)
})

test('the comment attribute is the shorthand when there is no permalink', () => {
  const parsed = I.ghInlineRef({ comment: '42', number: '12', repo: 'acme/app' })

  assert.deepEqual({ ...parsed.ref.comment }, { id: 42, kind: 'issue' })
  // Zero and junk are not ids: the card must not claim a comment it cannot name.
  assert.equal('comment' in I.ghInlineRef({ comment: '0', number: '12', repo: 'acme/app' }).ref, false)
  assert.equal('comment' in I.ghInlineRef({ comment: 'abc', number: '12', repo: 'acme/app' }).ref, false)
})

test('the comment is read out of the thread by id, or not at all', () => {
  const detail = { comments: [{ id: 6886329479, body: 'first' }, { id: 2, body: 'second' }] }

  assert.equal(I.ghInlineComment(detail, { id: 6886329479 }).body, 'first')
  assert.equal(I.ghInlineComment(detail, { id: '2' }).body, 'second')
  // Past the route's 30-object cap, or deleted: no row, and the card says so.
  assert.equal(I.ghInlineComment(detail, { id: 999 }), null)
  assert.equal(I.ghInlineComment(null, { id: 2 }), null)
  assert.equal(I.ghInlineComment(detail, null), null)
  assert.equal(I.ghInlineComment({ comments: 'nope' }, { id: 2 }), null)
})

test('a card excerpt is one clean line of the comment', () => {
  assert.equal(I.ghInlineExcerpt('Ship it.\n\nMore detail here.'), 'Ship it.')
  assert.equal(I.ghInlineExcerpt('> **quoted** text'), 'quoted text')
  assert.equal(I.ghInlineExcerpt(''), '')
  assert.equal(I.ghInlineExcerpt(null), '')
  assert.equal(I.ghInlineExcerpt('   \n  \n'), '')

  const long = 'x'.repeat(200)

  assert.equal(I.ghInlineExcerpt(long).length, 120)
  assert.equal(I.ghInlineExcerpt(long).endsWith('…'), true)
  // A nonsense cap falls back to the default room instead of chopping to nothing.
  assert.equal(I.ghInlineExcerpt(long, 4).length, 120)
  assert.equal(I.ghInlineExcerpt('short', 4), 'short')
})

test('a folder that cannot be read says WHY, never a guessed diagnosis', () => {
  // `/repo` answers refusals with HTTP 200 and a code, so "no payload" is not
  // "this is not a repository". A missing folder reported as "not a git repo"
  // sends the reader to the wrong problem.
  assert.equal(I.ghInlineRepoText({ code: 'not_a_repo' }), 'this folder is not a git repository')
  assert.equal(I.ghInlineRepoText({ code: 'no_remote' }), 'this folder has no git remote')
  assert.equal(I.ghInlineRepoText({ code: 'folder_missing' }), 'this folder is missing')
  assert.equal(I.ghInlineRepoText({ code: 'git_failed', error: 'git timed out' }), 'git timed out')
  assert.equal(I.ghInlineRepoText({ code: 'git_failed' }), 'could not read this folder')
  assert.equal(I.ghInlineRepoText(null), 'could not read this folder')
})

test('the card identity is repository and number, lowercased', () => {
  const ref = I.ghInlineRef({ url: 'https://github.com/Acme/App/issues/12' }).ref

  assert.equal(I.ghInlineKey(ref), 'acme/app#12')
  assert.equal(I.ghInlineKey(null), '')
})

// --- wiring: the claim, the call site, and the names it uses ------------------

test('the registered directive is the one the block and the hook name', () => {
  assert.equal(I.GH_INLINE_DIRECTIVE, 'pcc-gh')
  // The model is told which name to write by the plugin's own turn context. Two
  // spellings of one name is a card that never renders and no error anywhere.
  assert.match(hooks, /::pcc-gh\{/, 'hooks.py must name the EXACT directive the plugin claims')
  assert.match(
    source,
    /area:\s*TRANSCRIPT_DIRECTIVE_AREA/,
    'the directive must be registered in the transcript area'
  )
  assert.match(source, /data:\s*\{\s*name:\s*GH_INLINE_DIRECTIVE,\s*render:/, 'the claim must use the constant')
})

test('every helper the card calls is defined in the file', () => {
  // The shipped failure mode in this plugin: a helper that is CALLED but never
  // defined throws inside the render, so the card disappears with no message and
  // `node --check` stays green because it never resolves identifiers.
  const called = [
    'GhInlineCard',
    'GhDetailModal',
    'useInlineCardProject',
    'ghOpenExternal',
    'useBoardLinks',
    'linkProjectForSession',
    'projectFoldersOf',
    'projectForPath',
    'primaryPath',
    'focusedSessionId',
    'focusedCwd',
    'profileParam',
    'ghRest',
    'ghStatusLook',
    'ghStatusKind',
    'ghKindWord',
    'ghNumber',
    'ageText'
  ]

  for (const name of called) {
    const defined = new RegExp(`(?:function\\s+${name}\\s*\\()|(?:const\\s+${name}\\s*=)`).test(source)

    assert.ok(defined, `${name} is used by the inline card but never defined`)
  }

  // And the constant the registration and the hook share.
  assert.match(source, /const GH_INLINE_DIRECTIVE = 'pcc-gh'/)
})

test('every S.ghInline* key the card references is defined in the style object', () => {
  const referenced = new Set([...source.matchAll(/S\.(ghInline[A-Za-z]*)/g)].map(match => match[1]))

  assert.ok(referenced.size >= 5, 'the card should lay out its own rows')

  for (const key of referenced) {
    assert.match(source, new RegExp(`\\n  ${key}: \\{`), `${key} is referenced but missing from S`)
  }
})

test('the claim passes the props the card reads, including the owning session', () => {
  // `sessionId` is what resolves the chat's project, and the host only supplies
  // it as a prop. Dropping it at the call site leaves every card with no folder
  // and no error.
  assert.match(source, /attrs:\s*props\.attrs/)
  assert.match(source, /sessionId:\s*props\.sessionId/)
  assert.match(source, /source:\s*props\.source/)
})

test('the card opens the shared detail modal rather than a second issue view', () => {
  const card = source.slice(source.indexOf('function GhInlineCard'), source.indexOf('const GRAPH_COMMIT_LOCK_RE'))

  assert.match(card, /jsx\(\s*GhDetailModal,\s*\{/, 'the card must hand the click to the same modal the board uses')
  assert.match(card, /onOpen:/, 'following a reference inside the modal must push, not replace')
  assert.match(card, /onBack:/, 'the pushed entry needs a way back')
})

test('the modal is keyed and handed the repository NAME, never a slug', () => {
  const card = source.slice(source.indexOf('function GhInlineCard'), source.indexOf('const GRAPH_COMMIT_LOCK_RE'))

  // The modal composes `owner/repo` itself for session drafts and issue URLs.
  // Handing it a slug produced `acme/acme/app` in both.
  assert.match(card, /repo: repo \? String\(repo\.repo\) : ref\.repo/, 'the modal takes the repo name')
  // Unkeyed, the thread's draft and Close confirmation survive into the next
  // entry: type a comment on #34, press Back, post it on #12.
  assert.match(card, /`\$\{entry\.kind\}-\$\{entry\.number\}`/, 'the modal is keyed on the entry')
})

test('the card button and its click handler ask the same question', () => {
  const card = source.slice(source.indexOf('function GhInlineCard'), source.indexOf('const GRAPH_COMMIT_LOCK_RE'))

  // The first cut guarded the READ with `sameRepo` and the CLICK with `folder`
  // alone, so a card labelled "another repository" opened this repo's item.
  assert.match(card, /const usable = ghInlineOpenable\(/, 'the open decision is the shared one')
  assert.match(card, /onClick: usable \? openItem :/, 'a card that cannot open the modal opens GitHub')
  assert.match(card, /if \(!usable\) return/, 'the handler refuses on its own, not only the button')
})

// The verification pass proved these two were unguarded: deleting them left the
// suite fully green. They are behavioural enough to matter (a card that never
// repaints after a close, and a read issued for the wrong repository) and there
// is no renderer harness in this repo, so they are pinned at the call site.
test('a card never reads GitHub for a repository the chat is not in', () => {
  const card = source.slice(source.indexOf('function GhInlineCard'), source.indexOf('const GRAPH_COMMIT_LOCK_RE'))

  assert.match(card, /enabled: sameRepo,/, 'the refs read must be gated on the repository matching')
})

test('both modal writes repaint the card that opened them', () => {
  const invalidations = [...source.matchAll(/invalidateQueries\(\{ queryKey: \[QUERY_ROOT, 'github', 'inline'\] \}\)/g)]

  // Two write paths: posting a comment, and changing the item's state. Posting
  // can reopen nothing, but a comment changes the thread a card's count reads;
  // the state write is the one that flips a card from Open while it says closed.
  assert.equal(invalidations.length, 2, 'comment and state writes must each drop the inline read')
})

test('a comment card lands the modal ON that comment', () => {
  const card = source.slice(source.indexOf('function GhInlineCard'), source.indexOf('const GRAPH_COMMIT_LOCK_RE'))

  // The id rides on the stack entry, so Back and forward keep the anchor.
  assert.match(card, /commentId, kind, number/, 'the entry must carry the comment id')
  assert.match(card, /focusComment: entry\.commentId \|\| 0/, 'the modal must be told which comment')
  // The anchor the modal scrolls to, and the outline that says "here".
  assert.match(source, /id: comment\.id \? `pcc-comment-\$\{comment\.id\}`/, 'a comment needs an anchor id')
  assert.match(source, /scrollIntoView\(\{ block: 'center' \}\)/, 'the thread must scroll to it')
  assert.match(source, /\n  ghCommentFlash: \{/, 'the flash style must exist')
})

test('the inline card code carries no em dash', () => {
  const card = source.slice(source.indexOf('function GhInlineCard'), source.indexOf('const GRAPH_COMMIT_LOCK_RE'))

  assert.doesNotMatch(block, /\u2014/)
  assert.doesNotMatch(card, /\u2014/)
})
