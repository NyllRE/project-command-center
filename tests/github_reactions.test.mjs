// Proof for the reaction chip model and the Link to session draft embedded in
// desktop/plugin.js. The functions are not copied here: the marker block is
// sliced out of the live file, so a change that is not compilable or not self
// contained fails this test.
//
// Run with:
//   node --test tests/github_reactions.test.mjs

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(resolve(HERE, '..'), 'desktop', 'plugin.js')
const BEGIN = '// github-reactions:begin'
const END = '// github-reactions:end'

const source = readFileSync(PLUGIN, 'utf8')
const beginAt = source.indexOf(BEGIN)
const endAt = source.indexOf(END)

assert.ok(beginAt >= 0, `${BEGIN} missing from desktop/plugin.js`)
assert.ok(endAt > beginAt, `${END} missing or before the begin marker`)
assert.equal(source.indexOf(BEGIN, beginAt + 1), -1, `${BEGIN} appears more than once`)

const block = source.slice(beginAt, endAt + END.length)
assert.doesNotMatch(block, /^\s*import\s/m, 'reaction block must not import anything')
assert.doesNotMatch(block, /^\s*export\s/m, 'reaction block must not export anything')
assert.doesNotMatch(block, /(?<![\w$])jsx\(/, 'reaction block must not render')

vm.runInThisContext(
  `${block}
;globalThis.__PCC_REACTIONS__ = { GH_REACTION_CHIPS, GH_REACTION_ORDER, GH_DRAFT_QUOTE_MAX,
  ghReactionGlyph, ghReactionLabel, ghReactionModel, ghReactionPicker, ghReactionToggle,
  ghCommentDraftText, ghLinkedSessions, ghReactionTarget, ghReactionRequest }`,
  { filename: PLUGIN }
)
const S = globalThis.__PCC_REACTIONS__

test('the chip glyphs are text, not emoji codepoints', () => {
  const glyphs = S.GH_REACTION_ORDER.map(S.ghReactionGlyph)

  assert.deepEqual(glyphs, ['+1', '\u22121', ':D', '\\o/', ':/', '\u2665', '^', 'oo'])
  for (const glyph of glyphs) {
    // U+1F300..U+1FAFF is the emoji block this must never need a font for.
    assert.ok(!/[\u{1F300}-\u{1FAFF}]/u.test(glyph), `${glyph} is an emoji codepoint`)
  }
  assert.equal(S.ghReactionLabel('+1'), 'Thumbs up')
  assert.equal(S.ghReactionLabel('eyes'), 'Eyes')
})

test('the row is the five most used that exist, canonical order breaking ties', () => {
  const model = S.ghReactionModel([
    { content: 'eyes', count: 2, mine: false },
    { content: 'heart', count: 5, mine: true },
    { content: '+1', count: 2, mine: false },
    { content: 'laugh', count: 1, mine: false },
    { content: 'rocket', count: 1, mine: false },
    { content: 'hooray', count: 1, mine: false },
    { content: 'confused', count: 1, mine: false }
  ])

  assert.equal(model.length, S.GH_REACTION_CHIPS)
  assert.deepEqual(model.map(row => row.content), ['heart', '+1', 'eyes', 'laugh', 'hooray'])
  assert.deepEqual(model.map(row => row.count), [5, 2, 2, 1, 1])
  assert.equal(model[0].mine, true)
  assert.ok(model.slice(1).every(row => row.mine === false))
  assert.equal(model[0].glyph, '\u2665')
})

test('zero, unknown and malformed rows never reach a chip', () => {
  const model = S.ghReactionModel([
    { content: 'heart', count: 0 },
    { content: 'mindblown', count: 9 },
    { content: '+1', count: 'x' },
    null,
    { content: 'eyes', count: 1, mine: 'yes' }
  ])

  assert.deepEqual(model.map(row => row.content), ['eyes'])
  assert.equal(model[0].mine, false, 'mine is a strict boolean, not truthy')
  assert.deepEqual(S.ghReactionModel(null), [])
})

test('the picker carries all eight in GitHub order', () => {
  const picker = S.ghReactionPicker()

  assert.deepEqual(picker.map(item => item.content), S.GH_REACTION_ORDER)
  assert.equal(picker.length, 8)
  assert.ok(picker.every(item => item.glyph && item.label))
})

test('one toggle adds, increments, decrements and removes', () => {
  const start = [
    { content: 'heart', count: 2, mine: true },
    { content: 'eyes', count: 1, mine: false }
  ]

  assert.deepEqual(S.ghReactionToggle(start, 'heart'), [
    { content: 'heart', count: 1, mine: false },
    { content: 'eyes', count: 1, mine: false }
  ])
  assert.deepEqual(S.ghReactionToggle([{ content: 'heart', count: 1, mine: true }], 'heart'), [])
  assert.deepEqual(S.ghReactionToggle(start, 'eyes'), [
    { content: 'heart', count: 2, mine: true },
    { content: 'eyes', count: 2, mine: true }
  ])
  assert.deepEqual(S.ghReactionToggle(start, 'rocket'), [
    { content: 'heart', count: 2, mine: true },
    { content: 'eyes', count: 1, mine: false },
    { content: 'rocket', count: 1, mine: true }
  ])
  assert.equal(start[0].count, 2, 'the input rows are not mutated')
})

test('the draft names the repo, the number, the author and one quoted line', () => {
  const text = S.ghCommentDraftText({
    author: 'alice',
    body: 'The reaction row wraps under 720px.\n\nSecond paragraph stays out of the composer.',
    number: 42,
    owner: 'NyllRE',
    repo: 'project-command-center'
  })

  assert.equal(
    text,
    '#42 \u00b7 NyllRE/project-command-center\nalice wrote:\n> The reaction row wraps under 720px.\n\n'
  )
  assert.ok(text.endsWith('\n\n'), 'the caret lands on a blank last line')
  assert.ok(!text.includes('Second paragraph'), 'only the first line is quoted')
})

test('the quoted first line is bounded', () => {
  const body = `${'word '.repeat(50)}\nsecond line`
  const text = S.ghCommentDraftText({ author: 'bob', body, number: 7, owner: 'o', repo: 'r' })
  const quote = text.split('\n').find(line => line.startsWith('> '))

  assert.ok(quote, 'a quote line exists')
  assert.ok(quote.length <= S.GH_DRAFT_QUOTE_MAX + 3, `quote is ${quote.length} chars`)
  assert.ok(quote.length >= S.GH_DRAFT_QUOTE_MAX + 1, `quote is ${quote.length} chars`)
  assert.ok(quote.endsWith('\u2026'), 'a truncated line is marked')
  assert.ok(!text.includes('second line'))
})

test('a body-less thread still ends with the blank line', () => {
  const text = S.ghCommentDraftText({ author: '', body: '', number: 3, owner: 'o', repo: 'r' })

  assert.equal(text, '#3 \u00b7 o/r\n\n')
})

test('linked sessions match the number on the right column', () => {
  const links = [
    { session_id: 's1', issue: 42, pr: null, title: 'the issue work' },
    { session_id: 's2', issue: null, pr: 42, title: 'the PR work' },
    { session_id: 's3', issue: 42, pr: null, title: 'a second issue session' },
    { session_id: 's4', issue: 43, pr: null, title: 'other issue' },
    { session_id: '', issue: 42, pr: null, title: 'no id' }
  ]

  assert.deepEqual(S.ghLinkedSessions(links, 'issue', 42).map(link => link.session_id), ['s1', 's3'])
  assert.deepEqual(S.ghLinkedSessions(links, 'pr', 42).map(link => link.session_id), ['s2'])
  assert.deepEqual(S.ghLinkedSessions(links, 'issue', 0), [])
  assert.deepEqual(S.ghLinkedSessions(null, 'issue', 42), [])
})

// A comment is not the thread body: the route refuses `{kind:'issue',
// comment_id}` with bad_kind, so the request body has to name the comment.
test('a reaction on a comment targets the comment, whatever thread it sits on', () => {
  const onPrComment = S.ghReactionRequest({
    commentId: 900,
    content: '+1',
    folder: '/home/nyll/dev/alpha',
    kind: 'pr',
    number: 42
  })

  assert.deepEqual(onPrComment, {
    kind: 'issue_comment',
    content: '+1',
    comment_id: 900,
    path: '/home/nyll/dev/alpha'
  })

  const onIssueComment = S.ghReactionRequest({
    commentId: '901',
    content: 'heart',
    folder: '/home/nyll/dev/alpha',
    kind: 'issue',
    number: 7
  })

  assert.deepEqual(onIssueComment, {
    kind: 'issue_comment',
    content: 'heart',
    comment_id: 901,
    path: '/home/nyll/dev/alpha'
  })

  // A review comment is already the right kind and must not become an issue one.
  assert.equal(
    S.ghReactionTarget('review_comment', 902),
    'review_comment'
  )
  assert.equal(
    S.ghReactionTarget('review_comment', 0),
    'issue',
    'no comment id means the thread body, never a review comment'
  )
})

test('a reaction on the thread body names the PR or the issue itself', () => {
  assert.deepEqual(
    S.ghReactionRequest({ content: 'eyes', folder: '/r', kind: 'pr', number: 42 }),
    { kind: 'pr', content: 'eyes', number: 42, path: '/r' }
  )
  assert.deepEqual(
    S.ghReactionRequest({ content: 'eyes', folder: '/r', kind: 'issue', number: 7 }),
    { kind: 'issue', content: 'eyes', number: 7, path: '/r' }
  )
  // An unknown kind with no comment id is an issue body, the safe default.
  assert.equal(S.ghReactionTarget('', null), 'issue')
  assert.equal(S.ghReactionTarget('pr', 0), 'pr')
})
