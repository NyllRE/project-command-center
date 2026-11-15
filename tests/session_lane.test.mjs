// Proof for the session lane block embedded in desktop/plugin.js. The functions
// are not copied here: the marker block is sliced out of the live file, so a
// change in the plugin that is not self contained or not compilable fails this
// test.
//
// The cases follow the resolution order the badge promises: an explicit session
// status, then the linked pull request, then the project lane, then nothing. A
// /board row from a backend that predates the per-session lane carries no status
// fields, and that case is asserted here as a normal fallback rather than an
// error.
//
// Run with:
//   node --test tests/session_lane.test.mjs

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(resolve(HERE, '..'), 'desktop', 'plugin.js')
const BEGIN = '// session-lane:begin'
const END = '// session-lane:end'

const source = readFileSync(PLUGIN, 'utf8')
const beginAt = source.indexOf(BEGIN)
const endAt = source.indexOf(END)

assert.ok(beginAt >= 0, `${BEGIN} missing from desktop/plugin.js`)
assert.ok(endAt > beginAt, `${END} missing or before the begin marker`)
assert.equal(source.indexOf(BEGIN, beginAt + 1), -1, `${BEGIN} appears more than once`)

const block = source.slice(beginAt, endAt + END.length)
assert.doesNotMatch(block, /^\s*import\s/m, 'lane block must not import anything')
assert.doesNotMatch(block, /^\s*export\s/m, 'lane block must not export anything')
assert.doesNotMatch(block, /(?<![\w$])jsx\(/, 'lane block must not render')
assert.doesNotMatch(block, /\u2014/, 'lane block must not carry an em dash')

vm.runInThisContext(
  `${block}\n;globalThis.__PCC_LANE__ = { SESSION_LANE_LABELS, SESSION_LANE_VARS, SESSION_LANE_SOURCE_TEXT, sessionLaneId, sessionPrLane, sessionIssueLane, resolveSessionLane, sessionLaneTitle, laneForLink, sessionLaneTally, sessionLaneSummary }`,
  { filename: PLUGIN }
)
const L = globalThis.__PCC_LANE__

const refsOf = rows => new Map(Object.entries(rows).map(([number, row]) => [Number(number), row]))
const openPr = { number: 6, kind: 'pr', state: 'open', draft: false, merged: false }
const draftPr = { number: 7, kind: 'pr', state: 'open', draft: true, merged: false }
const mergedPr = { number: 8, kind: 'pr', state: 'closed', draft: false, merged: true }
const closedPr = { number: 9, kind: 'pr', state: 'closed', draft: false, merged: false }

test('every lane has a label and a theme variable', () => {
  assert.deepEqual(
    Object.keys(L.SESSION_LANE_LABELS),
    ['unfiled', 'backlog', 'active', 'review', 'blocked', 'done', 'canceled']
  )
  assert.deepEqual({ ...L.SESSION_LANE_VARS }, {
    unfiled: 'var(--ui-text-quaternary)',
    backlog: 'var(--ui-text-quaternary)',
    active: 'var(--ui-accent)',
    review: 'var(--ui-purple)',
    blocked: 'var(--ui-red)',
    done: 'var(--ui-green)',
    canceled: 'var(--ui-text-tertiary)'
  })
  for (const lane of Object.keys(L.SESSION_LANE_LABELS)) {
    assert.match(L.SESSION_LANE_VARS[lane], /^var\(--ui-[a-z-]+\)$/)
  }
})

test('the source words name every provenance the badge can show', () => {
  assert.equal(L.SESSION_LANE_SOURCE_TEXT.user, 'set by you')
  assert.equal(L.SESSION_LANE_SOURCE_TEXT.agent, 'set by the agent')
  assert.equal(L.SESSION_LANE_SOURCE_TEXT.pr, 'from its pull request')
  assert.equal(L.SESSION_LANE_SOURCE_TEXT.issue, 'from its issue')
  assert.equal(L.SESSION_LANE_SOURCE_TEXT.project, 'from the project')
  assert.equal(L.SESSION_LANE_SOURCE_TEXT.unset, 'nothing set it')
  assert.equal(L.SESSION_LANE_SOURCE_TEXT.auto, 'set automatically')
})

test('an explicit session status wins over the PR and the project', () => {
  for (const lane of Object.keys(L.SESSION_LANE_LABELS)) {
    const link = { status: lane, status_source: 'user', pr: 6 }
    const got = L.laneForLink(link, refsOf({ 6: openPr }), 'blocked')

    assert.equal(got.lane, lane)
    assert.equal(got.source, 'user')
  }
})

test('the status source survives to the tooltip', () => {
  assert.equal(L.laneForLink({ status: 'review', status_source: 'agent' }, null, '').source, 'agent')
  assert.equal(L.laneForLink({ status: 'done', status_source: 'auto' }, null, '').source, 'auto')
  // A row with a status but no source is a user move, never a crash.
  assert.equal(L.laneForLink({ status: 'review' }, null, '').source, 'user')
  // Case and padding do not change the lane.
  assert.equal(L.resolveSessionLane({ status: ' REVIEW ' }, null, '').lane, 'review')
})

test('an unknown or missing status falls through to Unfiled, never Backlog', () => {
  // A lane nobody set is Unfiled: Backlog only exists when somebody put a card there.
  assert.deepEqual({ ...L.resolveSessionLane({ status: 'shipping' }, null, '') }, { lane: 'unfiled', note: '', source: 'unset' })
  assert.deepEqual({ ...L.resolveSessionLane({ status: null }, null, '') }, { lane: 'unfiled', note: '', source: 'unset' })
  assert.deepEqual({ ...L.resolveSessionLane(null, null, '') }, { lane: 'unfiled', note: '', source: 'unset' })
  assert.deepEqual({ ...L.resolveSessionLane({}, {}, undefined) }, { lane: 'unfiled', note: '', source: 'unset' })
  // A project lane still answers before Unfiled does.
  assert.deepEqual({ ...L.resolveSessionLane({ status: 'shipping' }, null, 'backlog') }, { lane: 'backlog', note: '', source: 'project' })
})

test('the PR derivation: merged is done, closed without a merge is canceled, open proves nothing', () => {
  assert.equal(L.sessionPrLane(mergedPr), 'done')
  assert.equal(L.sessionPrLane({ state: 'merged', merged: true }), 'done')
  assert.equal(L.sessionPrLane(closedPr), 'canceled')
  // An open item never moves a card to review on its own.
  assert.equal(L.sessionPrLane(openPr), '')
  assert.equal(L.sessionPrLane(draftPr), '')
  assert.equal(L.sessionPrLane(null), '')
  assert.equal(L.sessionPrLane('not a row'), '')

  const merged = L.laneForLink({ session_id: 's1', pr: 8 }, refsOf({ 8: mergedPr }), 'active')

  assert.deepEqual({ ...merged }, { lane: 'done', note: '', source: 'pr' })
  assert.deepEqual(
    { ...L.laneForLink({ session_id: 's2', pr: 9 }, refsOf({ 9: closedPr }), 'active') },
    { lane: 'canceled', note: '', source: 'pr' }
  )
  // Open and draft fall through to the project lane, unchanged.
  assert.equal(L.laneForLink({ session_id: 's3', pr: 6 }, refsOf({ 6: openPr }), 'active').lane, 'active')
  assert.equal(L.laneForLink({ session_id: 's4', pr: 7 }, refsOf({ 7: draftPr }), 'active').lane, 'active')
})

test('the project lane answers when the session has no status and no terminal item', () => {
  assert.deepEqual({ ...L.resolveSessionLane(null, closedPr, 'backlog') }, { lane: 'canceled', note: '', source: 'pr' })
  // A linked PR the refs read never resolved falls back the same way.
  assert.equal(L.laneForLink({ session_id: 's1', pr: 77 }, refsOf({ 6: openPr }), 'blocked').lane, 'blocked')
  // No refs map at all is the old backend and a failed read alike.
  assert.equal(L.laneForLink({ session_id: 's1', pr: 6 }, undefined, 'done').lane, 'done')
  // With no project lane either, the answer is Unfiled.
  assert.equal(L.resolveSessionLane(null, openPr, '').lane, 'unfiled')
})

test('the issue derivation: closed as completed is done, not planned or duplicate is canceled', () => {
  const done = { number: 4, kind: 'issue', state: 'closed', state_reason: 'completed' }
  const doneNoReason = { number: 5, kind: 'issue', state: 'closed', state_reason: null }
  const dropped = { number: 6, kind: 'issue', state: 'closed', state_reason: 'not_planned' }
  const dupe = { number: 7, kind: 'issue', state: 'closed', state_reason: 'duplicate' }
  const open = { number: 8, kind: 'issue', state: 'open', state_reason: null }

  assert.equal(L.sessionIssueLane(done), 'done')
  assert.equal(L.sessionIssueLane(doneNoReason), 'done')
  assert.equal(L.sessionIssueLane(dropped), 'canceled')
  assert.equal(L.sessionIssueLane(dupe), 'canceled')
  assert.equal(L.sessionIssueLane(open), '')
  assert.equal(L.sessionIssueLane(null), '')

  assert.deepEqual(
    { ...L.laneForLink({ session_id: 'i1', issue: 4 }, refsOf({ 4: done }), 'active') },
    { lane: 'done', note: '', source: 'issue' }
  )
  assert.deepEqual(
    { ...L.laneForLink({ session_id: 'i2', issue: 6 }, refsOf({ 6: dropped }), 'active') },
    { lane: 'canceled', note: '', source: 'issue' }
  )
  // An open issue leaves the lane alone: the project lane answers.
  assert.equal(L.laneForLink({ session_id: 'i3', issue: 8 }, refsOf({ 8: open }), 'active').lane, 'active')
  // An explicit lane still wins over both derivations.
  assert.equal(
    L.laneForLink({ session_id: 'i4', issue: 4, status: 'review', status_source: 'user' }, refsOf({ 4: done }), 'active').lane,
    'review'
  )
})

test('the current un-restarted /board row renders the project lane, not an error', () => {
  const oldRow = { session_id: 's-1', project_id: 'p1', repo: 'acme/widgets', issue: 12, pr: null, pr_url: null, title: 'x' }

  assert.equal('status' in oldRow, false)
  // Missing fields read as unset: no lane of their own, so the project answers.
  assert.equal(L.resolveSessionLane(oldRow.status, oldRow.status_source, '').lane, 'unfiled')
  assert.deepEqual({ ...L.laneForLink(oldRow, null, 'review') }, { lane: 'review', note: '', source: 'project' })
})

test('the tooltip names the lane and where it came from', () => {
  assert.equal(L.sessionLaneTitle(L.resolveSessionLane({ status: 'review', status_source: 'agent' }, null, '')), 'Review lane, set by the agent')
  assert.equal(L.sessionLaneTitle(L.resolveSessionLane({ status: 'active', status_source: 'auto' }, null, '')), 'Active lane, set automatically')
  assert.equal(L.sessionLaneTitle(L.resolveSessionLane(null, mergedPr, '')), 'Done lane, from its pull request')
  assert.equal(L.sessionLaneTitle(L.resolveSessionLane(null, null, 'backlog')), 'Backlog lane, from the project')
  assert.equal(L.sessionLaneTitle(null), '')
  assert.equal(
    L.sessionLaneTitle(L.resolveSessionLane({ status: 'blocked', status_source: 'user', note: 'waiting on acme' }, null, '')),
    'Blocked lane, set by you: waiting on acme'
  )
})

test('the tally covers every session in the project, not only the linked rows', () => {
  const links = [
    { session_id: 's1', project_id: 'p1', pr: 8 },
    { session_id: 's2', project_id: 'p1', pr: 6 },
    { session_id: 's3', project_id: 'p1', status: 'blocked', status_source: 'user' },
    { session_id: 's4', project_id: 'p1' }
  ]
  const counts = L.sessionLaneTally(6, links, refsOf({ 6: openPr, 8: mergedPr }), 'active')

  // done (s1), s2's open PR derives nothing so it stays active, blocked (s3),
  // s4 and the two unnamed sessions: active.
  assert.deepEqual(
    { ...counts },
    { unfiled: 0, backlog: 0, active: 4, review: 0, blocked: 1, done: 1, canceled: 0 }
  )
  // A row counts once even when the total says there are fewer sessions.
  assert.deepEqual(
    { ...L.sessionLaneTally(1, links, refsOf({}), 'active') },
    { unfiled: 0, backlog: 0, active: 3, review: 0, blocked: 1, done: 0, canceled: 0 }
  )
})

test('the column summary is one short line', () => {
  // The project lane is left out, so a review stands out on an active column.
  assert.equal(L.sessionLaneSummary(3, { backlog: 0, active: 2, review: 1, blocked: 0, done: 0 }, 'active'), '3 sessions \u00b7 1 review')
  assert.equal(L.sessionLaneSummary(1, { backlog: 0, active: 0, review: 0, blocked: 1, done: 0 }, ''), '1 session \u00b7 1 blocked')
  // Signal order, and no more than two lane counts.
  assert.equal(
    L.sessionLaneSummary(9, { backlog: 1, active: 2, review: 3, blocked: 2, done: 1 }, 'backlog'),
    '9 sessions \u00b7 2 blocked \u00b7 3 review'
  )
  // A column whose project lane is review counts the lanes that differ.
  assert.equal(L.sessionLaneSummary(3, { backlog: 0, active: 1, review: 2, blocked: 0, done: 0 }, 'review'), '3 sessions \u00b7 1 active')
  assert.equal(L.sessionLaneSummary(0, {}, ''), '')
  assert.equal(L.sessionLaneSummary(4, {}, 'active'), '4 sessions')
  assert.equal(L.sessionLaneSummary(2, null), '2 sessions')
})

// The lane table travels with the line. Without it a custom project lane is not a
// lane at all: sessionLaneId drops it, so the column repeated the lane it already
// names AND printed the raw id instead of the label its author typed.
test('a custom project lane is left out of its own header and named by its label', () => {
  const lanes = [
    { id: 'unfiled', label: 'Unfiled', order: 0 },
    { id: 'active', label: 'Active', order: 1 },
    { id: 'triage', label: 'Waiting on Ali', order: 2 }
  ]
  const dot = '\u00b7'

  // The project's own lane is dropped from the line, custom or not.
  assert.equal(L.sessionLaneSummary(4, { triage: 2, active: 1 }, 'triage', lanes), `4 sessions ${dot} 1 active`)
  // A custom lane's count is still surfaced, by its label, never its id.
  assert.equal(L.sessionLaneSummary(4, { triage: 2, active: 2 }, 'active', lanes), `4 sessions ${dot} 2 waiting on ali`)
  // The system words do not change, and a table-less caller keeps the old answer.
  assert.equal(L.sessionLaneSummary(3, { active: 2, review: 1 }, 'active', lanes), `3 sessions ${dot} 1 review`)
  assert.equal(L.sessionLaneSummary(3, { active: 2, review: 1 }, 'active'), `3 sessions ${dot} 1 review`)
})

// A system lane keeps its id when it is renamed, so the seeded word is no longer
// the lane's name: the summary has to read the label the table carries now.
test('a renamed system lane prints the label it carries now', () => {
  const lanes = [
    { id: 'active', label: 'Active', order: 1 },
    { id: 'review', label: 'QA check', order: 2 }
  ]

  assert.equal(
    L.sessionLaneSummary(2, { active: 0, review: 2 }, 'active', lanes),
    '2 sessions \u00b7 2 qa check'
  )
  // No table, no label: the id is the only honest word left.
  assert.equal(L.sessionLaneSummary(2, { review: 2 }, 'active'), '2 sessions \u00b7 2 review')
})
