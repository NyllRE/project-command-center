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
  `${block}\n;globalThis.__PCC_LANE__ = { SESSION_LANE_LABELS, SESSION_LANE_VARS, SESSION_LANE_SOURCE_TEXT, sessionLaneId, sessionPrLane, resolveSessionLane, sessionLaneTitle, laneForLink, sessionLaneTally, sessionLaneSummary }`,
  { filename: PLUGIN }
)
const L = globalThis.__PCC_LANE__

const refsOf = rows => new Map(Object.entries(rows).map(([number, row]) => [Number(number), row]))
const openPr = { number: 6, kind: 'pr', state: 'open', draft: false, merged: false }
const draftPr = { number: 7, kind: 'pr', state: 'open', draft: true, merged: false }
const mergedPr = { number: 8, kind: 'pr', state: 'closed', draft: false, merged: true }
const closedPr = { number: 9, kind: 'pr', state: 'closed', draft: false, merged: false }

test('every lane has a label and a theme variable', () => {
  assert.deepEqual(Object.keys(L.SESSION_LANE_LABELS), ['backlog', 'active', 'review', 'blocked', 'done'])
  assert.deepEqual({ ...L.SESSION_LANE_VARS }, {
    backlog: 'var(--ui-text-quaternary)',
    active: 'var(--ui-accent)',
    review: 'var(--ui-purple)',
    blocked: 'var(--ui-red)',
    done: 'var(--ui-green)'
  })
  for (const lane of Object.keys(L.SESSION_LANE_LABELS)) {
    assert.match(L.SESSION_LANE_VARS[lane], /^var\(--ui-[a-z-]+\)$/)
  }
})

test('the source words are the four the tooltip promises', () => {
  assert.equal(L.SESSION_LANE_SOURCE_TEXT.user, 'set by you')
  assert.equal(L.SESSION_LANE_SOURCE_TEXT.agent, 'set by the agent')
  assert.equal(L.SESSION_LANE_SOURCE_TEXT.pr, 'from its pull request')
  assert.equal(L.SESSION_LANE_SOURCE_TEXT.project, 'from the project')
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

test('an unknown or missing status falls through instead of inventing a lane', () => {
  assert.equal(L.resolveSessionLane({ status: 'shipping' }, null, ''), null)
  assert.equal(L.resolveSessionLane({ status: null }, null, ''), null)
  assert.equal(L.resolveSessionLane(null, null, ''), null)
  assert.equal(L.resolveSessionLane({}, {}, undefined), null)
})

test('the PR refinement: merged is done, open or draft is review', () => {
  assert.equal(L.sessionPrLane(mergedPr), 'done')
  assert.equal(L.sessionPrLane({ state: 'merged', merged: true }), 'done')
  assert.equal(L.sessionPrLane(openPr), 'review')
  assert.equal(L.sessionPrLane(draftPr), 'review')
  // A PR closed without a merge proves nothing about the work.
  assert.equal(L.sessionPrLane(closedPr), '')
  assert.equal(L.sessionPrLane(null), '')
  assert.equal(L.sessionPrLane('not a row'), '')

  const merged = L.laneForLink({ session_id: 's1', pr: 8 }, refsOf({ 8: mergedPr }), 'active')

  assert.deepEqual({ ...merged }, { lane: 'done', note: '', source: 'pr' })
  assert.equal(L.laneForLink({ session_id: 's2', pr: 6 }, refsOf({ 6: openPr }), 'active').lane, 'review')
  assert.equal(L.laneForLink({ session_id: 's3', pr: 7 }, refsOf({ 7: draftPr }), 'active').lane, 'review')
})

test('the project lane answers when the session has no status and no usable PR', () => {
  assert.deepEqual({ ...L.resolveSessionLane(null, closedPr, 'backlog') }, { lane: 'backlog', note: '', source: 'project' })
  // A linked PR the refs read never resolved falls back the same way.
  assert.equal(L.laneForLink({ session_id: 's1', pr: 77 }, refsOf({ 6: openPr }), 'blocked').lane, 'blocked')
  // No refs map at all is the old backend and a failed read alike.
  assert.equal(L.laneForLink({ session_id: 's1', pr: 6 }, undefined, 'done').lane, 'done')
})

test('the current un-restarted /board row renders the project lane, not an error', () => {
  const oldRow = { session_id: 's-1', project_id: 'p1', repo: 'acme/widgets', issue: 12, pr: null, pr_url: null, title: 'x' }

  assert.equal('status' in oldRow, false)
  // Missing fields read as unset: no lane of their own, so the project answers.
  assert.equal(L.resolveSessionLane(oldRow.status, oldRow.status_source, ''), null)
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

  // done (s1), review (s2), blocked (s3), s4 and the two unnamed sessions: active.
  assert.deepEqual({ ...counts }, { backlog: 0, active: 3, review: 1, blocked: 1, done: 1 })
  // A row counts once even when the total says there are fewer sessions.
  assert.deepEqual({ ...L.sessionLaneTally(1, links, refsOf({}), 'active') }, { backlog: 0, active: 3, review: 0, blocked: 1, done: 0 })
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
