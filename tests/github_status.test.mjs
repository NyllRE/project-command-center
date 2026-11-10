// Proof for the GitHub status table and the cumulative filter union embedded in
// desktop/plugin.js. The functions are not copied here: the marker block is
// sliced out of the live file, so a change in the plugin that is not compilable
// or not self contained fails this test.
//
// Run with:
//   node --test tests/github_status.test.mjs

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(resolve(HERE, '..'), 'desktop', 'plugin.js')
const BEGIN = '// github-status:begin'
const END = '// github-status:end'

const source = readFileSync(PLUGIN, 'utf8')
const beginAt = source.indexOf(BEGIN)
const endAt = source.indexOf(END)

assert.ok(beginAt >= 0, `${BEGIN} missing from desktop/plugin.js`)
assert.ok(endAt > beginAt, `${END} missing or before the begin marker`)
assert.equal(source.indexOf(BEGIN, beginAt + 1), -1, `${BEGIN} appears more than once`)

const block = source.slice(beginAt, endAt + END.length)
assert.doesNotMatch(block, /^\s*import\s/m, 'status block must not import anything')
assert.doesNotMatch(block, /^\s*export\s/m, 'status block must not export anything')
assert.doesNotMatch(block, /(?<![\w$])jsx\(/, 'status block must not render')

vm.runInThisContext(
  `${block}\n;globalThis.__PCC_STATUS__ = { GH_STATUS_COLORS, GH_STATUS_LABELS, GH_STATUS_FILTERS, ghStatusKey, ghStatusColor, ghStatusLabel, ghStatusVariant, ghStatusLook, ghStatusBuckets, ghFilterRows }`,
  { filename: PLUGIN }
)
const S = globalThis.__PCC_STATUS__

const openIssue = { number: 1, state: 'open' }
const doneIssue = { number: 2, state: 'closed', state_reason: 'completed' }
const notPlannedIssue = { number: 3, state: 'closed', state_reason: 'not_planned' }
const duplicateIssue = { number: 4, state: 'closed', state_reason: 'duplicate' }
const bareClosedIssue = { number: 5, state: 'closed' }
const openPr = { number: 6, state: 'open', draft: false, merged: false }
const mergedPr = { number: 7, state: 'closed', draft: false, merged: true }
const closedPr = { number: 8, state: 'closed', draft: false, merged: false }
const draftPr = { number: 9, state: 'open', draft: true, merged: false }
const unknownRow = { number: 10 }
const hostileRow = { number: 11, state: 'on hold' }

test('the palette is GitHub dark, named once', () => {
  assert.deepEqual(
    { ...S.GH_STATUS_COLORS },
    {
      open: '#3fb950',
      merged: '#a371f7',
      closed: '#a371f7',
      not_planned: '#f85149',
      duplicate: '#f85149',
      draft: '#8b949e',
      unknown: '#8b949e'
    }
  )
})

test('issues colour by close reason', () => {
  assert.equal(S.ghStatusColor('issue', openIssue), '#3fb950')
  assert.equal(S.ghStatusColor('issue', doneIssue), '#a371f7')
  assert.equal(S.ghStatusColor('issue', notPlannedIssue), '#f85149')
  assert.equal(S.ghStatusColor('issue', duplicateIssue), '#f85149')
  // Closed with no reason is the ordinary close, so purple, never red.
  assert.equal(S.ghStatusColor('issue', bareClosedIssue), '#a371f7')
  assert.equal(S.ghStatusColor('issue', unknownRow), '#8b949e')
})

test('a reason written any way still reads', () => {
  assert.equal(S.ghStatusKey('issue', { state: 'closed', state_reason: 'NOT PLANNED' }), 'not_planned')
  assert.equal(S.ghStatusKey('issue', { state: 'closed', state_reason: 'not-planned' }), 'not_planned')
  assert.equal(S.ghStatusKey('issue', { state: 'closed', state_reason: 'Duplicate' }), 'duplicate')
  assert.equal(S.ghStatusKey('issue', { state: 'Closed', state_reason: 'completed' }), 'closed')
})

test('pull requests colour by merge, close and draft', () => {
  assert.equal(S.ghStatusColor('pr', openPr), '#3fb950')
  assert.equal(S.ghStatusColor('pr', mergedPr), '#a371f7')
  assert.equal(S.ghStatusColor('pr', closedPr), '#a371f7')
  assert.equal(S.ghStatusColor('pr', draftPr), '#8b949e')
  assert.equal(S.ghStatusColor('pr', unknownRow), '#8b949e')
})

test('glyphs follow the state, issues keep one glyph', () => {
  assert.equal(S.ghStatusLook('issue', openIssue).name, 'issues')
  assert.equal(S.ghStatusLook('issue', notPlannedIssue).name, 'issues')
  assert.equal(S.ghStatusLook('pr', openPr).name, 'git-pull-request')
  assert.equal(S.ghStatusLook('pr', draftPr).name, 'git-pull-request-draft')
  assert.equal(S.ghStatusLook('pr', closedPr).name, 'git-pull-request-closed')
  assert.equal(S.ghStatusLook('pr', mergedPr).name, 'git-merge')
  assert.equal(S.ghStatusLook('pr', mergedPr).color, '#a371f7')
})

test('labels and badge variants ride the same key', () => {
  assert.equal(S.ghStatusLabel('issue', notPlannedIssue), 'Closed as not planned')
  assert.equal(S.ghStatusLabel('pr', mergedPr), 'Merged')
  assert.equal(S.ghStatusVariant('pr', openPr), 'success')
  assert.equal(S.ghStatusVariant('pr', draftPr), 'muted')
  assert.equal(S.ghStatusVariant('issue', notPlannedIssue), 'destructive')
  assert.equal(S.ghStatusVariant('pr', mergedPr), 'muted')
})

test('a merged PR answers Merged and Closed', () => {
  assert.deepEqual(S.ghStatusBuckets('pr', mergedPr), ['merged', 'closed'])
  assert.deepEqual(S.ghStatusBuckets('pr', closedPr), ['closed'])
  assert.deepEqual(S.ghStatusBuckets('pr', draftPr), ['open', 'draft'])
  assert.deepEqual(S.ghStatusBuckets('pr', openPr), ['open'])
  assert.deepEqual(S.ghStatusBuckets('issue', doneIssue), ['closed'])
  assert.deepEqual(S.ghStatusBuckets('issue', openIssue), ['open'])
})

test('an unreadable row lands under Open, never in the void', () => {
  assert.deepEqual(S.ghStatusBuckets('pr', unknownRow), ['open'])
  assert.deepEqual(S.ghStatusBuckets('pr', hostileRow), ['open'])
  assert.deepEqual(S.ghStatusBuckets('issue', unknownRow), ['open'])
  assert.equal(S.ghFilterRows('pr', [unknownRow], ['open']).length, 1)
})

test('Open only is the default and it keeps drafts', () => {
  const rows = [openPr, mergedPr, closedPr, draftPr]
  const shown = S.ghFilterRows('pr', rows, ['open'])
  assert.deepEqual(shown.map(r => r.number), [6, 9])
})

test('ticks are cumulative: Closed adds to Open, not instead of it', () => {
  const rows = [openPr, mergedPr, closedPr, draftPr]
  assert.deepEqual(S.ghFilterRows('pr', rows, ['open', 'closed']).map(r => r.number), [6, 7, 8, 9])
  // Merged alone is the merged PR, and nothing else.
  assert.deepEqual(S.ghFilterRows('pr', rows, ['merged']).map(r => r.number), [7])
  // Draft alone is the open draft, and nothing else.
  assert.deepEqual(S.ghFilterRows('pr', rows, ['draft']).map(r => r.number), [9])
})

test('every tick is everything, and no tick is everything too', () => {
  const rows = [openPr, mergedPr, closedPr, draftPr]
  for (const selection of [['open', 'merged', 'closed', 'draft'], []]) {
    assert.deepEqual(S.ghFilterRows('pr', rows, selection).map(r => r.number).sort(), [6, 7, 8, 9])
  }
})

test('issues union by state', () => {
  const rows = [openIssue, doneIssue, notPlannedIssue, duplicateIssue, bareClosedIssue]
  assert.deepEqual(S.ghFilterRows('issue', rows, ['open']).map(r => r.number), [1])
  assert.deepEqual(S.ghFilterRows('issue', rows, ['open', 'closed']).map(r => r.number), [1, 2, 3, 4, 5])
  assert.deepEqual(S.ghFilterRows('issue', rows, ['closed']).map(r => r.number), [2, 3, 4, 5])
})

test('filter rows name the statuses the brief asks for', () => {
  assert.deepEqual(S.GH_STATUS_FILTERS.issue.map(o => o.label), ['Open', 'Closed'])
  assert.deepEqual(S.GH_STATUS_FILTERS.pr.map(o => o.label), ['Open', 'Merged', 'Closed', 'Draft'])
})

test('junk in, empty list out, never a crash', () => {
  assert.deepEqual(S.ghFilterRows('pr', null, ['open']), [])
  assert.deepEqual(S.ghFilterRows('pr', undefined, null), [])
  assert.deepEqual(S.ghFilterRows('pr', [null], ['open']), [null])
  assert.equal(S.ghStatusColor('pr', null), S.GH_STATUS_COLORS.unknown)
  assert.equal(S.ghStatusColor('nonsense', { state: 'open' }), S.GH_STATUS_COLORS.open)
  assert.equal(S.ghStatusKey('issue', { state: 'closed', state_reason: 42 }), 'closed')
})
