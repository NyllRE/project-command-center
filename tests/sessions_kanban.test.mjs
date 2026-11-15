// Proof for the session kanban block embedded in desktop/plugin.js. The block is
// not copied here: it is sliced out of the live file, so an edit that leaves it
// dependent on JSX, an import or a renderer global fails this suite.
//
// The block defers lane resolution to the session-lane block above it, which is
// why both are loaded into the same context: the point of the test is that the
// kanban page has no second resolution order of its own.
//
// Run with:
//   node --test tests/sessions_kanban.test.mjs

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(resolve(HERE, '..'), 'desktop', 'plugin.js')
const source = readFileSync(PLUGIN, 'utf8')

function slice(begin, end) {
  const at = source.indexOf(begin)
  const to = source.indexOf(end)

  assert.ok(at >= 0, `${begin} missing from desktop/plugin.js`)
  assert.ok(to > at, `${end} missing or before the begin marker`)
  assert.equal(source.indexOf(begin, at + 1), -1, `${begin} appears more than once`)

  const block = source.slice(at, to + end.length)

  assert.doesNotMatch(block, /^\s*import\s/m, 'the block must not import anything')
  assert.doesNotMatch(block, /^\s*export\s/m, 'the block must not export anything')
  assert.doesNotMatch(block, /(?<![\w$])jsx\(/, 'the block must not render')
  assert.doesNotMatch(block, /\u2014/, 'the block must not carry an em dash')

  return block
}

const laneBlock = slice('// session-lane:begin', '// session-lane:end')
const kanbanBlock = slice('// session-kanban:begin', '// session-kanban:end')
// The view table lives in its own block: the suite loads it too, because the
// view migration and the lane filter are the two page-level rules this suite
// pins alongside the pure kanban helpers.
const viewsBlock = slice('// merged-views:begin', '// merged-views:end')
// The card builder calls the goal line model, so the pure goal block loads into the same context;
// its own suite is tests/goals.test.mjs.
const goalsBlock = slice('// session-goals:begin', '// session-goals:end')

vm.runInThisContext(
  `${laneBlock}\n${kanbanBlock}\n${viewsBlock}\n;globalThis.__PCC_KANBAN__ = {
    KANBAN_LANES,
    KANBAN_LANE_IDS,
    KANBAN_STALE_MS,
    KANBAN_SOURCE_TEXT,
    normalizeKanbanLanes,
    renameKanbanLane,
    moveKanbanLane,
    kanbanLaneFor,
    kanbanFilterCards,
    kanbanCounts,
    sortKanbanCards,
    kanbanMoveBody,
    sessionIndex,
    sessionProjectId,
    sessionBranch,
    movedLane,
    laneIdIn,
    normalizeLaneTable,
    normalizeLaneFilter,
    laneFilterAllows,
    VIEWS,
    VIEW_IDS,
    DEFAULT_VIEW,
    normalizeView
  }`,
  { filename: PLUGIN }
)
const K = globalThis.__PCC_KANBAN__

// The card builder is not in the sliced block (it is a renderer helper), but a
// filter test that hand-writes card objects proves nothing: it would have missed
// a field the builder never emits. Extract the builder and the stamp readers it
// calls, so every card in this suite is the object the page really renders.
function fn(name) {
  const start = source.indexOf(`function ${name}(`)

  assert.ok(start >= 0, `${name} missing from desktop/plugin.js`)

  const end = source.indexOf('\n}\n', start)

  assert.ok(end > start, `${name} has no closing brace`)

  return source.slice(start, end + 3)
}

vm.runInThisContext(
  `${goalsBlock}\n${fn('toMs')}\n${fn('stampMs')}\n${fn('sessionStamp')}\n${fn('buildKanbanCard')}
;globalThis.__PCC_CARD__ = { buildKanbanCard, sessionStamp, stampMs }`,
  { filename: PLUGIN }
)
const C = globalThis.__PCC_CARD__

// The Status view's own card list, executed with the shapes the hook really
// receives: session.list rows, the tree join, link rows, project rows and the
// board's project lanes. Every card assertion in this suite goes through it, so a
// field the call site forgets to pass fails here instead of in the renderer.
vm.runInThisContext(
  `${fn('primaryPath')}\n${fn('sessionProjectId')}\n${fn('sessionBranch')}\n${fn('buildKanbanCard')}\n${fn('kanbanCardsFrom')}\n${fn('projectPanelCard')}
;globalThis.__PCC_CARDS__ = { kanbanCardsFrom, projectPanelCard }`,
  { filename: PLUGIN }
)
const CARDS = globalThis.__PCC_CARDS__

const DEFAULT_IDS = ['unfiled', 'backlog', 'active', 'review', 'blocked', 'done', 'canceled']
const ids = lanes => lanes.map(lane => lane.id)

const mergedPr = { number: 8, state: 'closed', merged: true, draft: false }
const openPr = { number: 6, state: 'open', merged: false, draft: false }
const draftPr = { number: 7, state: 'open', merged: false, draft: true }
const closedPr = { number: 9, state: 'closed', merged: false, draft: false }
const doneIssue = { number: 4, kind: 'issue', state: 'closed', state_reason: 'completed' }
const canceledIssue = { number: 5, kind: 'issue', state: 'closed', state_reason: 'not_planned' }
const openIssue = { number: 3, kind: 'issue', state: 'open', state_reason: null }

const card = (over = {}) => ({
  title: 'Fix the thing',
  projectId: 'p1',
  projectLabel: 'Alpha',
  branch: 'main',
  issue: 0,
  pr: 0,
  lane: 'active',
  source: 'user',
  stale: false,
  lastActivity: 1000,
  ...over
})

test('the default lanes are the seven system ids in order, with labels and an order each', () => {
  const lanes = K.normalizeKanbanLanes([])

  assert.deepEqual(ids(lanes), DEFAULT_IDS)
  assert.deepEqual(
    lanes.map(lane => lane.label),
    ['Unfiled', 'Backlog', 'Active', 'Review', 'Blocked', 'Done', 'Canceled']
  )
  assert.deepEqual(lanes.map(lane => lane.order), [0, 1, 2, 3, 4, 5, 6])
  assert.deepEqual(K.KANBAN_LANE_IDS, DEFAULT_IDS)
  // The fallback table marks every one of them system, so the renderer can refuse
  // to offer a delete for a lane it fell back to.
  assert.ok(K.KANBAN_LANES.every(lane => lane.system === true))
})

test('lane resolution matches the store: session, then linked item, then project, then unfiled', () => {
  const explicit = K.kanbanLaneFor({ status: 'blocked', status_source: 'agent', note: 'waiting' }, openPr, 'done')

  assert.equal(explicit.lane, 'blocked')
  assert.equal(explicit.source, 'agent')
  assert.equal(explicit.note, 'waiting')

  const merged = K.kanbanLaneFor({}, mergedPr, 'active')

  assert.equal(merged.lane, 'done')
  assert.equal(merged.source, 'pr')

  // A closed PR derives canceled; an open or draft PR derives nothing and the
  // next rule answers (here: unfiled, because no project lane is set).
  assert.equal(K.kanbanLaneFor({}, closedPr, '').lane, 'canceled')
  assert.equal(K.kanbanLaneFor({}, openPr, '').lane, 'unfiled')
  assert.equal(K.kanbanLaneFor({}, draftPr, '').lane, 'unfiled')

  const project = K.kanbanLaneFor(null, null, 'review')

  assert.equal(project.lane, 'review')
  assert.equal(project.source, 'project')

  const empty = K.kanbanLaneFor(null, null, '')

  assert.equal(empty.lane, 'unfiled')
  assert.equal(empty.source, 'unset')

  // An unknown explicit value is not a lane: the next rule answers instead.
  assert.equal(K.kanbanLaneFor({ status: 'shipped' }, null, 'blocked').lane, 'blocked')
  // A project id that is not a live lane falls through to unfiled, never backlog.
  assert.equal(K.kanbanLaneFor(null, null, 'someday').lane, 'unfiled')
})

test('the provenance words are the four the card promises', () => {
  assert.equal(K.KANBAN_SOURCE_TEXT.user, 'you')
  assert.equal(K.KANBAN_SOURCE_TEXT.agent, 'agent')
  assert.equal(K.KANBAN_SOURCE_TEXT.pr, 'from PR')
  assert.equal(K.KANBAN_SOURCE_TEXT.project, 'unset')
  assert.equal(K.KANBAN_SOURCE_TEXT.unset, 'unset')
})

test('a filter narrows by project, source, staleness and search, and they compose', () => {
  // Real gateway shapes: a session.list row has no cwd, branch or last-active,
  // a link row names the lane that won, and only the lane's source can answer
  // the source filter.
  const now = 1_700_000_000_000
  const lanes = K.normalizeKanbanLanes([])
  const alpha = { id: 'p1', label: 'Alpha', path: '/home/nyll/dev/alpha' }
  const beta = { id: 'p2', label: 'Beta', path: '/home/nyll/dev/beta' }
  const sessions = [
    { id: 's-auth', title: 'Auth token refresh', preview: '', started_at: 1_699_000_000, message_count: 6, source: 'gui', _lineage_root_id: null },
    { id: 's-bill', title: 'Billing retries', preview: '', started_at: 1_698_000_000, message_count: 3, source: 'agent', _lineage_root_id: null },
    { id: 's-docs', title: 'Docs sweep', preview: '', started_at: 1_697_000_000, message_count: 2, source: 'gui', _lineage_root_id: null }
  ]
  const links = [
    { session_id: 's-auth', project_id: 'p1', status: 'active', status_source: 'user', note: '', title: '', issue: 12, pr: 0, status_updated_at: 1_699_500_000 },
    { session_id: 's-bill', project_id: 'p2', status: 'review', status_source: 'agent', note: '', title: '', issue: 0, pr: 34, status_updated_at: 1_699_400_000 },
    { session_id: 's-docs', project_id: 'p1', status: 'done', status_source: 'auto', note: '', title: '', issue: 0, pr: 0, status_updated_at: 1_699_300_000 }
  ]
  const projectOf = id => (id === 'p2' ? beta : alpha)
  const rows = sessions.map(session => {
    const link = links.find(row => row.session_id === session.id) || null
    const project = projectOf(session.id === 's-bill' ? 'p2' : 'p1')

    return C.buildKanbanCard({
      session,
      link,
      project,
      projectId: project.id,
      folder: project.path,
      branch: session.id === 's-auth' ? 'feat/auth' : session.id === 's-docs' ? 'docs' : '',
      now,
      lanes
    })
  })

  // The builder emits the winning source as `laneSource`; there is no `source`
  // field on a card, which is what made the old filter blank the board.
  assert.deepEqual(rows.map(row => row.laneSource), ['user', 'agent', 'auto'])
  assert.ok(rows.every(row => row.source === undefined))

  assert.equal(K.kanbanFilterCards(rows, {}).length, 3)
  assert.equal(K.kanbanFilterCards(rows, { projectId: 'p1' }).length, 2)
  assert.deepEqual(K.kanbanFilterCards(rows, { source: 'user' }).map(row => row.id), ['s-auth'])
  // The agent filter is the store's own rule: an auto lane is agent work too.
  assert.deepEqual(K.kanbanFilterCards(rows, { source: 'agent' }).map(row => row.id), ['s-bill', 's-docs'])
  assert.deepEqual(K.kanbanFilterCards(rows, { source: 'agent' }).map(row => row.laneSource), ['agent', 'auto'])

  // Search: case-insensitive substring over title, project, branch and numbers.
  assert.equal(K.kanbanFilterCards(rows, { search: 'AUTH' }).length, 1)
  assert.equal(K.kanbanFilterCards(rows, { search: 'beta' }).length, 1)
  assert.equal(K.kanbanFilterCards(rows, { search: 'feat/auth' }).length, 1)
  assert.equal(K.kanbanFilterCards(rows, { search: '34' }).length, 1)
  assert.equal(K.kanbanFilterCards(rows, { search: '#12' }).length, 1)
  assert.equal(K.kanbanFilterCards(rows, { search: 'nothing' }).length, 0)

  // Two filters that cannot both hold empty the list rather than OR together.
  assert.equal(K.kanbanFilterCards(rows, { projectId: 'p2', source: 'user' }).length, 0)
  assert.equal(K.kanbanFilterCards(rows, { projectId: 'p2', source: 'agent' }).length, 1)
  assert.equal(K.kanbanFilterCards(rows, { projectId: 'p1', source: 'user', search: '12' }).length, 1)
})

test('a session with no link row is placed by the tree sessionIds claim, with its preview branch and stamp', () => {
  // The gateway truth: a session.list row is id/title/preview/started_at/
  // message_count/source/_lineage_root_id. No cwd, no branch, no last_active.
  // projects.tree is what carries sessionIds and the preview rows.
  const now = 1_700_100_000_000
  const lanes = K.normalizeKanbanLanes([])
  const treeProjects = [
    {
      id: 'p1',
      label: 'Alpha',
      path: '/home/nyll/dev/alpha',
      sessionIds: ['s1', 's2'],
      previewSessions: [
        { id: 's1', title: 'Kanban work', git_branch: 'feat/session-kanban', started_at: 1_700_000_000, ended_at: 1_700_050_000, last_active: 1_700_060_000 },
        { id: 's2', title: 'No checkout', started_at: 1_700_000_000 }
      ]
    },
    {
      id: '__no_project__',
      label: 'Home',
      isNoProject: true,
      sessionIds: ['s9'],
      previewSessions: [{ id: 's9', started_at: 1_700_000_000 }]
    }
  ]
  // What useSessionCards indexes: the Home bucket claims every unowned session
  // and is not a project, so it is filtered before the index is built.
  const projects = treeProjects.filter(project => project.isNoProject !== true)
  const join = K.sessionIndex(projects)
  const project = projects[0]
  const session = { id: 's1', title: 'Kanban work', preview: '', started_at: 1_699_000_000, message_count: 12, source: 'gui', _lineage_root_id: null }
  const projectId = K.sessionProjectId(session.id, null, join)
  const preview = join.previewById.get(session.id)
  // Through the hook's own builder with NO projectId in the input: that field is
  // what the call site forgot, and it is why an unlinked session on the Status
  // view read "No project", could not be moved and was skipped by the filter.
  const built = CARDS.kanbanCardsFrom([session], {
    branches: {},
    join,
    lanes,
    linkBySession: new Map(),
    now,
    overrides: {},
    projectById: new Map(projects.map(row => [row.id, row])),
    refsByProject: new Map(),
    statuses: { p1: 'active' }
  })[0]

  assert.equal(projectId, 'p1')
  assert.equal(built.projectId, 'p1', 'the hook hands the tree-resolved project id to the builder')
  assert.equal(built.projectLabel, 'Alpha')
  assert.equal(built.folder, '/home/nyll/dev/alpha')
  assert.equal(built.linked, false, 'no link row, and still placed')
  assert.equal(built.lane, 'active', 'the project lane places the card, so it is movable')
  assert.equal(built.branch, 'feat/session-kanban')
  // Last activity is the preview's newest stamp, not the list row's start time.
  assert.equal(built.lastActivity, 1_700_060_000_000)
  assert.ok(built.lastActivity > C.stampMs(session.started_at))
  // The project filter sees it, because the card names the project.
  assert.deepEqual(K.kanbanFilterCards([built], { projectId: 'p1' }).map(row => row.id), ['s1'])
  assert.equal(K.kanbanFilterCards([built], { projectId: 'p2' }).length, 0)

  // The link row still wins, and a session nothing claims stays unplaced.
  assert.equal(K.sessionProjectId('s1', { project_id: 'p2' }, join), 'p2')
  assert.equal(K.sessionProjectId('s9', null, join), '')

  // Branch: the preview's own checkout, then the project folder's branch, then
  // nothing (the renderer draws no chip rather than a fake 'main').
  assert.equal(K.sessionBranch(join.previewById.get('s2'), 'trunk'), 'trunk')
  assert.equal(K.sessionBranch(join.previewById.get('s2'), ''), '')
  assert.equal(K.sessionBranch(null, ''), '')
  assert.equal(built.lastActivity !== C.stampMs(session.started_at), true)

  // A move this client posted wins over the lane the last board read carries,
  // and an id with no pending move keeps its resolved lane untouched.
  const resolved = { lane: 'active', note: '', source: 'project' }

  assert.deepEqual(K.movedLane({ s1: { status: 'done' } }, 's1', resolved),
    { lane: 'done', note: '', source: 'user' })
  assert.equal(K.movedLane({ s1: { status: 'done' } }, 's2', resolved), resolved)
  assert.equal(K.movedLane({}, 's1', resolved), resolved)
  assert.equal(K.movedLane(null, 's1', resolved), resolved)
})

test('a card carries the folder its project mark reads, and the mark has one chain', () => {
  const lanes = K.normalizeKanbanLanes([])
  const project = { id: 'p1', label: 'Alpha', path: '/home/nyll/dev/alpha' }
  const session = { id: 's1', title: 'Work', preview: '', started_at: 1_700_000_000, message_count: 1, source: 'gui', _lineage_root_id: null }

  const withFolder = C.buildKanbanCard({ session, project, projectId: 'p1', folder: project.path, now: 1_700_000_100_000, lanes })
  const without = C.buildKanbanCard({ session, now: 1_700_000_100_000, lanes })

  assert.equal(withFolder.folder, '/home/nyll/dev/alpha')
  assert.equal(without.folder, '')
  assert.equal(withFolder.source, undefined, 'a card emits laneSource, never source')

  // Both call sites must hand the folder over: the builder only forwards it.
  const calls = source.split('buildKanbanCard({').slice(1)

  assert.equal(calls.length, 2)
  for (const call of calls) {
    const head = call.slice(0, call.indexOf('})'))
    const folderLine = head.split('\n').find(line => line.trim().startsWith('folder'))

    assert.ok(folderLine, 'every buildKanbanCard call site passes folder')
  }

  // One icon fallback chain in the file: the tag delegates to Avatar, which is
  // the component that calls useProjectIcon(primaryPath(project)).
  const tag = fn('KanbanProjectTag')

  assert.match(tag, /jsx\(Avatar, \{ project: \{ label: card\.projectLabel, path: card\.folder \}, size: 14 \}\)/)
  assert.doesNotMatch(tag, /useProjectIcon|STACK_GLYPH/)
  assert.match(fn('Avatar'), /useProjectIcon\(primaryPath\(project\)\)/)
})

test('a stale card is old and not done; an undated card is never stale', () => {
  const now = 10 * K.KANBAN_STALE_MS
  const lanes = K.normalizeKanbanLanes([])
  const project = { id: 'p1', label: 'Alpha', path: '/home/nyll/dev/alpha' }
  const oldSeconds = (now - K.KANBAN_STALE_MS - 1) / 1000

  // Built, not hand-written: `stale` is the builder's derivation from the lane
  // and the newest stamp, so a fixture that sets it would prove nothing.
  const build = (id, status, startedAt) =>
    C.buildKanbanCard({
      session: { id, title: id, preview: '', started_at: startedAt, message_count: 1, source: 'gui', _lineage_root_id: null },
      link: { session_id: id, project_id: 'p1', status, status_source: 'user', note: '', title: '', issue: 0, pr: 0, status_updated_at: startedAt },
      project,
      projectId: 'p1',
      folder: project.path,
      now,
      lanes
    })

  const oldActive = build('s-old-active', 'active', oldSeconds)
  const oldDone = build('s-old-done', 'done', oldSeconds)
  const fresh = build('s-fresh', 'active', now / 1000 - 10)
  const undated = C.buildKanbanCard({ session: { id: 's-undated', preview: '' }, now, lanes })

  assert.equal(oldActive.stale, true)
  assert.equal(oldDone.stale, false, 'done is never stale')
  assert.equal(fresh.stale, false)
  assert.equal(undated.stale, false, 'no stamp is not old')

  assert.deepEqual(K.kanbanFilterCards([oldActive, oldDone, fresh], { staleOnly: true }), [oldActive])
})

test('column counts follow the filtered list, not the raw one', () => {
  const rows = [
    card({ lane: 'active' }),
    card({ lane: 'active', projectId: 'p2' }),
    card({ lane: 'review' })
  ]
  const lanes = K.normalizeKanbanLanes([])

  const zero = { unfiled: 0, backlog: 0, active: 0, review: 0, blocked: 0, done: 0, canceled: 0 }

  assert.deepEqual(K.kanbanCounts(rows, lanes), { ...zero, active: 2, review: 1 })
  assert.deepEqual(K.kanbanCounts(K.kanbanFilterCards(rows, { projectId: 'p2' }), lanes), { ...zero, active: 1 })
})

test('a rename keeps the order and the ids, and an empty label is refused', () => {
  const lanes = K.normalizeKanbanLanes([])
  const renamed = K.renameKanbanLane(lanes, 'review', 'In review')

  assert.deepEqual(ids(renamed), DEFAULT_IDS)
  assert.equal(renamed[3].label, 'In review')
  assert.deepEqual(renamed.map(lane => lane.order), [0, 1, 2, 3, 4, 5, 6])

  assert.equal(K.renameKanbanLane(renamed, 'review', '   ')[3].label, 'In review')
  assert.equal(K.renameKanbanLane(renamed, 'someday', 'Nope')[3].label, 'In review')
  assert.equal(K.renameKanbanLane(renamed, 'review', '  Shipped  ')[3].label, 'Shipped')
})

test('a move reorders and renumbers without changing an id, and the ends are no-ops', () => {
  const lanes = K.normalizeKanbanLanes([])

  const up = K.moveKanbanLane(lanes, 'review', -1)

  assert.deepEqual(ids(up), ['unfiled', 'backlog', 'review', 'active', 'blocked', 'done', 'canceled'])
  assert.deepEqual(up.map(lane => lane.order), [0, 1, 2, 3, 4, 5, 6])
  assert.deepEqual(K.moveKanbanLane(lanes, 'unfiled', -1).map(lane => lane.id), DEFAULT_IDS)
  assert.deepEqual(K.moveKanbanLane(lanes, 'canceled', 1).map(lane => lane.id), DEFAULT_IDS)

  const twice = K.moveKanbanLane(K.moveKanbanLane(lanes, 'blocked', -1), 'blocked', -1)

  assert.deepEqual(ids(twice), ['unfiled', 'backlog', 'blocked', 'active', 'review', 'done', 'canceled'])
  // The stored shape stays {id, label, order}, 0..6, labels carried along.
  assert.deepEqual(Object.keys(twice[2]).sort(), ['id', 'label', 'order'])
  assert.equal(twice[2].label, 'Blocked')
})

test('malformed storage falls back to the seven default lanes', () => {
  for (const bad of [null, undefined, 'lanes', 42, {}, [{ id: 'review' }], [{ id: 'nope', label: 'X', order: 0 }]]) {
    const lanes = K.normalizeKanbanLanes(bad)

    assert.deepEqual(ids(lanes), DEFAULT_IDS, `input ${JSON.stringify(bad)}`)
    assert.equal(lanes.length, 7)
  }

  // Duplicate orders keep every id and resolve by the default order.
  const duplicate = K.normalizeKanbanLanes([
    { id: 'done', label: 'Shipped', order: 0 },
    { id: 'backlog', label: 'Ideas', order: 0 }
  ])

  assert.deepEqual(ids(duplicate), DEFAULT_IDS)
  assert.equal(duplicate[5].label, 'Shipped')
  assert.deepEqual(duplicate.map(lane => lane.order), [0, 1, 2, 3, 4, 5, 6])
})

test('the table sorts by the edited lane order, newest first inside a lane', () => {
  // Blocked moves one step up, past review: backlog, active, blocked, review, done.
  const lanes = K.moveKanbanLane(K.normalizeKanbanLanes([]), 'blocked', -1)
  const rows = [
    card({ lane: 'active', title: 'old', lastActivity: 10 }),
    card({ lane: 'active', title: 'new', lastActivity: 30 }),
    card({ lane: 'blocked', title: 'stuck', lastActivity: 20 }),
    card({ lane: 'backlog', title: 'later', lastActivity: 40 })
  ]
  const sorted = K.sortKanbanCards(rows, lanes, 1)

  assert.deepEqual(ids(lanes), ['unfiled', 'backlog', 'active', 'blocked', 'review', 'done', 'canceled'])
  assert.deepEqual(sorted.map(row => row.title), ['later', 'new', 'old', 'stuck'])
  assert.deepEqual(
    K.sortKanbanCards(rows, lanes, -1).map(row => row.title),
    ['stuck', 'new', 'old', 'later']
  )
  // The input is not mutated.
  assert.equal(rows[0].title, 'old')
})

test('the move itself posts that body through the existing board REST door', () => {
  const start = source.indexOf('function writeSessionLane(')

  assert.ok(start >= 0, 'writeSessionLane missing from desktop/plugin.js')
  const end = source.indexOf('\n}\n', start)

  assert.ok(end > start, 'writeSessionLane has no closing brace')
  const block = source.slice(start, end + 3)

  assert.ok(
    block.includes("boardRest('/board/status', { method: 'POST', body: kanbanMoveBody(projectId, sessionId, lane) })"),
    'the move must post the session body through boardRest'
  )
  assert.match(block, /\$sessionStatus\.set\(/, 'a move must update the local mirror first')
  assert.match(block, /queryClient\.invalidateQueries\(\{ queryKey: \[QUERY_ROOT, 'board'\] \}\)/)
  assert.match(block, /host\.notify\(\{/, 'a refused move must say so')
})

test('a move builds the session-scoped body the store expects', () => {
  assert.deepEqual(K.kanbanMoveBody('p1', 's1', 'review'), {
    project_id: 'p1',
    session_id: 's1',
    status: 'review',
    scope: 'session'
  })
  assert.deepEqual(Object.keys(K.kanbanMoveBody('p1', 's1', 'done')), [
    'project_id',
    'session_id',
    'status',
    'scope'
  ])
})

test('a linked item derives done or canceled, and an open one derives nothing', () => {
  // PR: merged is done, closed without a merge is canceled.
  assert.equal(K.kanbanLaneFor({}, mergedPr, '', null, null).lane, 'done')
  assert.equal(K.kanbanLaneFor({}, closedPr, '', null, null).lane, 'canceled')
  // Issue: completed is done, not planned is canceled.
  assert.equal(K.kanbanLaneFor({}, null, '', null, doneIssue).lane, 'done')
  assert.equal(K.kanbanLaneFor({}, null, '', null, canceledIssue).lane, 'canceled')
  // Open PR and open issue prove nothing: the project lane (then unfiled) answers.
  assert.equal(K.kanbanLaneFor({}, openPr, 'active', null, null).lane, 'active')
  assert.equal(K.kanbanLaneFor({}, null, 'active', null, openIssue).lane, 'active')
  assert.equal(K.kanbanLaneFor({}, null, '', null, openIssue).lane, 'unfiled')
  // An explicit lane the user set still wins over every derivation.
  const explicit = K.kanbanLaneFor(
    { status: 'review', status_source: 'user' },
    mergedPr,
    'active',
    null,
    doneIssue
  )

  assert.equal(explicit.lane, 'review')
  assert.equal(explicit.source, 'user')
})

test('a server lane row carries its system flag through the reader', () => {
  const rows = K.normalizeLaneTable([
    { id: 'unfiled', label: 'Unfiled', order: 0, system: 1 },
    { id: 'triage', label: 'Triage', order: 1, custom: true }
  ])

  assert.equal(rows[0].system, true)
  assert.equal(rows[1].system, undefined)
  assert.equal(rows[1].custom, true)
})

test('the page has two views and a stored Branches or Issues value lands on Projects', () => {
  assert.deepEqual(K.VIEW_IDS, ['status', 'projects'])
  assert.deepEqual(K.VIEWS.map(view => view.label), ['Status', 'Projects'])
  assert.equal(K.DEFAULT_VIEW, 'projects')
  assert.equal(K.normalizeView('projects'), 'projects')
  assert.equal(K.normalizeView('status'), 'status')
  // The two removed views migrate rather than opening an empty page.
  assert.equal(K.normalizeView('branches'), 'projects')
  assert.equal(K.normalizeView(' ISSUES '), 'projects')
  // A missing or unknown value opens Projects, the default board.
  assert.equal(K.normalizeView(''), 'projects')
  assert.equal(K.normalizeView(null), 'projects')
  assert.equal(K.normalizeView(42), 'projects')
  assert.equal(K.normalizeView('sessions'), 'projects')
})

test('the lane filter defaults to every lane checked and is intersected with the live table', () => {
  const lanes = K.normalizeKanbanLanes([])

  // No stored value means everything is checked.
  assert.deepEqual(K.normalizeLaneFilter(null, lanes), DEFAULT_IDS)
  assert.deepEqual(K.normalizeLaneFilter(undefined, lanes), DEFAULT_IDS)
  // A stored list keeps only live ids, in table order.
  assert.deepEqual(K.normalizeLaneFilter(['done', 'ghost', 'active'], lanes), ['active', 'done'])
  // An explicitly empty list is the user's answer: nothing is checked.
  assert.deepEqual(K.normalizeLaneFilter([], lanes), [])
  // A lane added after the filter was stored starts checked only when the stored
  // list names it, so the intersection is the whole rule.
  assert.deepEqual(K.normalizeLaneFilter(['unfiled', 'canceled'], lanes), ['unfiled', 'canceled'])
})

test('the filter predicate hides every unchecked lane and nothing else', () => {
  const selected = ['backlog', 'active']

  assert.equal(K.laneFilterAllows(selected, 'active'), true)
  assert.equal(K.laneFilterAllows(selected, 'backlog'), true)
  assert.equal(K.laneFilterAllows(selected, 'review'), false)
  assert.equal(K.laneFilterAllows(selected, 'canceled'), false)
  // Case and padding do not change the answer.
  assert.equal(K.laneFilterAllows(['done'], ' DONE '), true)
  // A missing or non-list selection hides everything rather than showing all.
  assert.equal(K.laneFilterAllows(null, 'active'), false)
  assert.equal(K.laneFilterAllows([], 'active'), false)
})

// A card is a click target for opening its session, and the menu lives inside
// it. Regression: a lane row in that menu opened the session instead of moving
// the card, so the card must ignore clicks that start on a nested control.
// A second regression: the lane row carried BOTH onClick and onSelect, so Radix
// ran both and one pick posted twice. Exactly one path fires it now, and
// writeSessionLane drops a repeat of a move already pending to the same lane.
test('a click on a nested control never opens the card session, and one pick moves once', () => {
  const source = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8')
  const card = source.slice(source.indexOf('function KanbanCard('), source.indexOf('function KanbanColumn('))
  const menu = source.slice(source.indexOf('function KanbanCardMenu('), source.indexOf('function KanbanCard('))
  const laneRow = menu.slice(menu.indexOf('...lanes.map(lane =>'), menu.indexOf('jsx(Separator'))

  assert.match(card, /target\.closest/, 'the card tests where the click started')
  assert.match(card, /role="menuitem"/, 'menu rows are excluded')
  assert.match(card, /data-pcc-noclick/, 'the trigger is excluded')
  assert.equal((laneRow.match(/onMove\(card, lane\.id\)/g) || []).length, 1, 'one path per pick')
  assert.match(laneRow, /onSelect: \(\) => onMove\(card, lane\.id\)/, 'the menu callback is the path')
  assert.doesNotMatch(laneRow, /onClick:/, 'a click handler beside onSelect fires the move twice')
})

test('column folding: empty lanes fold, unfiled and backlog start folded', () => {
  const source = readFileSync(PLUGIN, 'utf8')
  const start = source.indexOf('function laneCollapsed(')
  const end = source.indexOf('\n}\n', start)
  const body = source.slice(start, end + 3)
  const defaults = source.match(/const KANBAN_COLLAPSED_DEFAULT = \[([^\]]+)\]/)[1]
  const list = defaults.split(',').map(s => s.trim().replace(/'/g, '')).filter(Boolean)
  const laneCollapsed = new Function('KANBAN_COLLAPSED_DEFAULT', `${body}; return laneCollapsed`)(list)

  assert.deepEqual(list, ['unfiled', 'backlog'], 'the two idle lanes start folded')
  assert.equal(laneCollapsed('unfiled', {}, true), true, 'unfiled starts folded even with cards')
  assert.equal(laneCollapsed('backlog', {}, false), true, 'an empty lane folds')
  assert.equal(laneCollapsed('active', {}, true), false, 'a lane with cards is open')
  assert.equal(laneCollapsed('active', {}, false), true, 'empty folds regardless of lane')
  assert.equal(laneCollapsed('active', { active: false }, false), false, 'an explicit choice wins')
  assert.equal(laneCollapsed('active', { active: true }, true), true, 'including folding a full lane')
  assert.equal(laneCollapsed('unfiled', { unfiled: false }, true), false, 'unfiled can be opened')
})

// A folded lane is a rail, not an empty column: it keeps a name, a dot and a
// count in a few dozen pixels, and it is still a click and a drop target.
test('a folded lane renders as a rail and unfolds when a card lands on it', () => {
  const source = readFileSync(PLUGIN, 'utf8')
  const railWidth = Number(source.match(/const RAIL_WIDTH = (\d+)/)[1])
  const railStyle = source.slice(source.indexOf('  columnRail: {'), source.indexOf('  railLabel: {'))
  const labelStyle = source.slice(source.indexOf('  railLabel: {'), source.indexOf('  railCount: {'))
  const column = source.slice(source.indexOf('function KanbanColumn('), source.indexOf('function KanbanTable('))
  const folded = column.slice(column.indexOf('if (collapsed) {'), column.indexOf("  return jsxs('div', {\n    className: 'pcc-kcol'"))
  const dropAt = source.indexOf('  const drop = laneId => {')
  const drop = source.slice(dropAt, source.indexOf('  const refresh = () => {', dropAt))
  const actionsAt = source.indexOf('function laneActions(')
  const actions = source.slice(actionsAt, source.indexOf('\n}\n', actionsAt))

  assert.ok(railWidth >= 36 && railWidth <= 44, `the rail is a rail: ${railWidth}px`)
  assert.match(railStyle, /flex: `0 0 \$\{RAIL_WIDTH\}px`/, 'the rail takes its own flex basis, not a column')
  assert.match(labelStyle, /writingMode: 'vertical-rl'/, 'the label is laid out vertically, not resampled')
  assert.match(labelStyle, /textOverflow: 'ellipsis'/, 'a long label truncates instead of overflowing')
  assert.match(folded, /className: 'pcc-kcol pcc-krail'/, 'the folded column carries the rail class')
  assert.match(folded, /style: S\.columnRail/, 'and the rail style')
  assert.doesNotMatch(folded, /Badge/, 'the rail does not carry the system/custom badge')
  assert.match(folded, /onDrop: event => \{[\s\S]*onDrop\(lane\.id\)/, 'the rail is still a drop target')
  assert.match(folded, /onClick: toggle/, 'and a click target that unfolds it')
  assert.match(drop, /dropCard\(card, laneId\)/, 'the drop hands the card to the shared lane actions')
  assert.match(
    actions,
    /laneCollapsed\(laneId, collapsed, true\)[\s\S]*writeCollapsed\(next\)/,
    'a drop unfolds the lane it landed in'
  )
})

// A move is a WRITE, so it refuses an id the live table does not list instead of
// relocating the card to whatever lane happens to be first. The helper that says
// so is what writeSessionLane calls, and it has to exist: this suite loads the
// block, so a reference to a helper nobody defined fails here rather than in the
// renderer (the reported "clicking a lane does nothing" bug).
test('a move names a lane the table lists, and nothing for one it does not', () => {
  const lanes = K.normalizeKanbanLanes(K.KANBAN_LANES)

  assert.equal(K.laneIdIn(lanes, 'review'), 'review')
  assert.equal(K.laneIdIn(lanes, ' REVIEW '), 'review', 'case and space are the table\'s, not the caller\'s')
  assert.equal(K.laneIdIn(lanes, 'nope'), '', 'a lane the table dropped is a refusal, not a fallback')
  assert.equal(K.laneIdIn(lanes, ''), '')
  assert.equal(K.laneIdIn(lanes, null), '')
  assert.equal(K.laneIdIn([{ id: 'shipping', label: 'Shipping' }], 'shipping'), 'shipping', 'a custom lane is a lane')
})

test('the move guard, not the caller, is the one that explains a refusal', () => {
  const start = source.indexOf('function writeSessionLane(')

  assert.ok(start >= 0, 'writeSessionLane missing from desktop/plugin.js')
  const block = source.slice(start, source.indexOf('\n}\n', start) + 3)

  assert.doesNotMatch(block, /if \(!projectId \|\| !sessionId \|\| !lane\) return/, 'the silent guard is the bug')
  assert.match(block, /if \(!projectId\) \{[\s\S]*?host\.notify\([\s\S]*?not linked to a project/, 'an unlinked session says so')
  assert.match(block, /if \(!lane\) \{[\s\S]*?host\.notify\(/, 'a dropped lane says so')
  assert.match(block, /laneFailure\(res\)/, 'a refusal surfaces the store\'s own error string')
})

// The folder rule the store already applies to an unlinked session, applied in
// the page so the card can be moved before a link row exists.
test('an unlinked session is placed by the nearest project folder that contains it', () => {
  const start = source.indexOf('function projectForPath(')

  assert.ok(start >= 0, 'projectForPath missing from desktop/plugin.js')
  const block = source.slice(start, source.indexOf('\n}\n', start) + 3)
  const projectForPath = vm.runInThisContext(`(() => { ${block}; return projectForPath })()`, { filename: PLUGIN })
  const folders = [
    { id: 'p_near', path: '/home/nyll/dev/sub' },
    { id: 'p_far', path: '/home/nyll/dev' },
    { id: 'p_other', path: '/home/nyll/other' }
  ]

  assert.equal(projectForPath(folders, '/home/nyll/dev/sub'), 'p_near')
  assert.equal(projectForPath(folders, '/home/nyll/dev/sub/app/src'), 'p_near', 'the longest folder wins')
  assert.equal(projectForPath(folders, '/home/nyll/dev/another'), 'p_far')
  assert.equal(
    projectForPath([{ id: 'p_near', path: '/home/nyll/dev/sub' }], '/home/nyll/dev/subway'),
    '',
    'a sibling name is not inside the folder'
  )
  assert.equal(projectForPath(folders, ''), '')
  assert.equal(projectForPath(folders, null), '')
})

// The log has to name the build: without this line a stale copy in
// desktop-plugins/ and a live one are indistinguishable in desktop.log.
test('register logs the build stamp first, and the stamp sits with the constants', () => {
  const build = source.match(/^const PLUGIN_BUILD = '([^']+)'$/m)

  assert.ok(build, 'PLUGIN_BUILD is a plain constant')
  assert.ok(build[1].length > 0)
  const registerAt = source.indexOf('  register(ctx) {')

  assert.ok(registerAt > 0, 'register missing')
  const registerBody = source.slice(registerAt, registerAt + 1200)

  assert.match(registerBody, /console\.error\('\[pcc\] build', PLUGIN_BUILD/, 'register names the build it is')
})

// The move itself, executed: the real writeSessionLane body with the REST door
// stubbed. This is the path the card menu takes, so it pins both halves of the
// fix: a move POSTs the session-scoped body, and every case that cannot post
// says why instead of closing the menu onto nothing.
test('a lane change posts, and a change that cannot post tells the user why', async () => {
  const grab = name => {
    const start = source.indexOf(`function ${name}(`)

    assert.ok(start >= 0, `${name} missing from desktop/plugin.js`)

    return source.slice(start, source.indexOf('\n}\n', start) + 3)
  }

  const harness = new Function(
    '$lanes', '$sessionStatus', 'boardRest', 'kanbanMoveBody', 'laneIdIn', 'queryClient',
    'host', 'laneLabel', 'QUERY_ROOT',
    `${grab('laneFailure')}\n${grab('writeSessionLane')}\nreturn writeSessionLane`
  )
  const run = ({ projectId = 'p1', sessionId = 's1', status = 'review', lane = 'review', answer = { ok: true } } = {}) => {
    const posted = []
    const notices = []
    const store = { value: {}, get() { return this.value }, set(v) { this.value = v } }

    harness(
      { get: () => K.KANBAN_LANES },
      store,
      (path, opts) => {
        posted.push({ path, body: opts.body })

        return Promise.resolve(answer)
      },
      K.kanbanMoveBody,
      () => lane,
      { invalidateQueries() {} },
      { notify: message => notices.push(message) },
      id => id,
      'root'
    )(projectId, sessionId, status)

    return { notices, posted, store }
  }

  const ok = run()

  assert.equal(ok.posted.length, 1, 'the move posts')
  assert.deepEqual(ok.posted[0], {
    path: '/board/status',
    body: { project_id: 'p1', session_id: 's1', status: 'review', scope: 'session' }
  })
  assert.equal(ok.store.value.s1.status, 'review', 'the card moves before the answer lands')
  assert.equal(ok.notices.length, 0)

  const unlinked = run({ projectId: '' })

  assert.equal(unlinked.posted.length, 0, 'an unlinked session cannot be posted')
  assert.equal(unlinked.notices.length, 1, 'and the user is told')
  assert.match(unlinked.notices[0].message, /not linked to a project yet/)

  const dropped = run({ lane: '' })

  assert.equal(dropped.posted.length, 0, 'a lane the table dropped is not posted')
  assert.match(dropped.notices[0].message, /no "review" lane/)

  const refused = run({ answer: { ok: false, code: 'project_mismatch', error: 'session s1 is carded under p2' } })

  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(refused.notices.length, 1, 'a refusal is not silent')
  assert.match(refused.notices[0].message, /carded under p2/, 'the store\'s own words reach the user')
  assert.equal(refused.store.value.s1, undefined, 'and the card goes back')
})

// One pick is one write. The menu row used to carry onClick AND onSelect, and
// Radix runs both for a single press: two POSTs, and two toasts on a refusal.
// The row now has one path, and the write itself drops a repeat of a move that is
// already pending to the same lane, so a double fire from any source cannot post
// twice. Both halves are asserted here: the guard by calling it twice, the menu by
// reading the row's handlers (a menu cannot render without a React root).
test('a move already pending to the same lane is not posted again', async () => {
  const grab = name => {
    const start = source.indexOf(`function ${name}(`)

    assert.ok(start >= 0, `${name} missing from desktop/plugin.js`)

    return source.slice(start, source.indexOf('\n}\n', start) + 3)
  }

  const writeSessionLane = new Function(
    '$lanes', '$sessionStatus', 'boardRest', 'kanbanMoveBody', 'laneIdIn', 'queryClient',
    'host', 'laneLabel', 'QUERY_ROOT',
    `${grab('laneFailure')}\n${grab('writeSessionLane')}\nreturn writeSessionLane`
  )
  const posted = []
  const notices = []
  const store = { value: {}, get() { return this.value }, set(v) { this.value = v } }
  const move = writeSessionLane(
    { get: () => K.KANBAN_LANES },
    store,
    (path, opts) => {
      posted.push(opts.body)

      return Promise.resolve({ ok: true })
    },
    K.kanbanMoveBody,
    (lanes, status) => status,
    { invalidateQueries() {} },
    { notify: message => notices.push(message) },
    id => id,
    'root'
  )

  move('p1', 's1', 'review')
  move('p1', 's1', 'review')

  assert.equal(posted.length, 1, 'one pending move, one POST')
  assert.equal(notices.length, 0)

  // A different lane is a different write: the guard drops only the repeat.
  move('p1', 's1', 'done')

  assert.equal(posted.length, 2, 'a second, different move still posts')
  assert.deepEqual(posted[1], { project_id: 'p1', session_id: 's1', status: 'done', scope: 'session' })
})

// The fold map is a stored choice like the sort and the view. Its atom is created
// at module load, before ctx exists, so its initializer reads no storage: without
// the restore in register() a fold choice is forgotten on the next reload.
test('every stored choice, the fold map included, is restored when the plugin registers', () => {
  const grab = name => {
    const start = source.indexOf(`function ${name}(`)

    assert.ok(start >= 0, `${name} missing from desktop/plugin.js`)

    return source.slice(start, source.indexOf('\n}\n', start) + 3)
  }
  const mkAtom = () => {
    let value

    return { get: () => value, set: next => { value = next } }
  }
  const atoms = {
    $collapsed: mkAtom(),
    $ghPreset: mkAtom(),
    $lanes: mkAtom(),
    $openProject: mkAtom(),
    $showArchived: mkAtom(),
    $sort: mkAtom(),
    $statuses: mkAtom(),
    $view: mkAtom()
  }
  const restoreStoredState = new Function(
    ...Object.keys(atoms),
    'readLanes', 'readStatuses', 'readSort', 'readView', 'readGhPreset', 'readOpenProject', 'readCollapsed',
    'readShowArchived',
    `${grab('restoreStoredState')}\nreturn restoreStoredState`
  )
  const stored = { lanes: [{ id: 'active' }], collapsed: { active: true, backlog: false }, preset: 'prs', project: 'p1', sort: 'activity', statuses: { p1: 'active' }, view: 'status', archived: true }

  restoreStoredState(
    ...Object.values(atoms),
    () => stored.lanes,
    () => stored.statuses,
    () => stored.sort,
    () => stored.view,
    () => stored.preset,
    () => stored.project,
    () => stored.collapsed,
    () => stored.archived
  )()

  assert.deepEqual(atoms.$collapsed.get(), stored.collapsed, 'a folded lane is still folded after a reload')
  assert.equal(atoms.$view.get(), 'status')
  // The archived switch is a stored choice like the rest: a filter that silently
  // forgets itself on every reload is not a filter.
  assert.equal(atoms.$showArchived.get(), stored.archived, 'the archived filter is restored too')

  // And register is what calls it: without this line the restores never run.
  const register = source.slice(source.indexOf('  register(ctx) {'), source.indexOf('    ctx.registerMany(['))
  assert.match(register, /restoreStoredState\(\)/, 'register seeds every stored choice')
})

// The project page's lane panel builds its cards from projects.project_sessions
// rows, which carry last_active. Leaving that out made the builder fall back to
// started_at, so a session busy an hour ago but started last week read stale and
// sorted last in its lane.
test('a project panel card carries its row\'s newest activity, not its start time', () => {
  const now = 1_700_100_000_000
  const lanes = K.normalizeKanbanLanes([])
  const project = { id: 'p1', label: 'Alpha', path: '/home/nyll/dev/alpha' }
  const startedAt = Math.floor((now - 10 * 24 * 60 * 60 * 1000) / 1000)
  const activeAt = Math.floor((now - 60 * 60 * 1000) / 1000)
  const build = row =>
    CARDS.projectPanelCard(row, {
      folder: project.path,
      goals: {},
      lanes,
      link: null,
      now,
      project,
      projectId: 'p1',
      projectLane: 'active',
      refs: null
    })

  const busy = build({ id: 's1', title: 'Busy', started_at: startedAt, last_active: activeAt })
  const idle = build({ id: 's2', title: 'Idle', started_at: startedAt })

  assert.equal(busy.lastActivity, activeAt * 1000, 'the row last_active is the card stamp')
  assert.equal(busy.stale, false, 'active an hour ago is not stale, whatever its start time')
  assert.equal(idle.lastActivity, startedAt * 1000)
  assert.equal(idle.stale, true, 'a row with no activity since its start is stale')
  // The lane panel sorts newest first, so the two rows keep their real order.
  assert.deepEqual(
    K.sortKanbanCards([idle, busy], lanes, 1).map(card => card.id),
    ['s1', 's2']
  )
})
