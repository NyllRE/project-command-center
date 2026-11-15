// Proof for the merged page and the server-owned lane table. The blocks are not
// copied here: they are sliced out of desktop/plugin.js, so a change that leaves
// them dependent on JSX, an import or a renderer global fails this suite.
//
// Three blocks are loaded into one context, in file order: the lane resolution
// order, the kanban table helpers, then the merged-view helpers. The kanban
// block defers to the lane block, and the merged-view helpers are pure.
//
// The page has two views now. Branches and Issues were folded into Projects, so
// this suite proves the removed components are gone and a stored legacy value
// opens Projects instead.
//
// Run with:
//   node --test tests/merged_views.test.mjs

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

// One whole function, from its `function` line to its top-level closing brace.
function fn(name) {
  const start = source.indexOf(`function ${name}(`)

  assert.ok(start >= 0, `${name} missing from desktop/plugin.js`)
  const end = source.indexOf('\n}\n', start)

  assert.ok(end > start, `${name} has no closing brace`)

  return source.slice(start, end + 3)
}

const laneBlock = slice('// session-lane:begin', '// session-lane:end')
const kanbanBlock = slice('// session-kanban:begin', '// session-kanban:end')
const viewsBlock = slice('// merged-views:begin', '// merged-views:end')

// sessionCountLabel reads the module's read cap, so the vm gets the same number
// the file declares rather than a second copy of 200.
const LIST_LIMIT = Number(source.match(/const SESSION_LIST_LIMIT = (\d+)/)[1])

vm.runInThisContext(
  `${laneBlock}\n${kanbanBlock}\n${viewsBlock}\nconst SESSION_LIST_LIMIT = ${LIST_LIMIT}\n;globalThis.__PCC_MERGED__ = {
    VIEWS,
    DEFAULT_VIEW,
    VIEW_IDS,
    normalizeView,
    KANBAN_LANES,
    normalizeKanbanLanes,
    normalizeLaneTable,
    laneTableOrDefaults,
    laneIds,
    lanePlace,
    kanbanPlaceCards,
    laneSlug,
    laneCreateBody,
    laneRenameBody,
    laneDeleteBody,
    laneOrderBody,
    addKanbanLane,
    laneColor,
    laneLabel,
    laneUseCounts,
    laneUseText,
    laneRowsFromResponse,
    kanbanLaneFor,
    kanbanCounts,
    sessionBranch,
    sessionCountLabel,
    movedLane,
    LIST_LIMIT: SESSION_LIST_LIMIT
  }`,
  { filename: PLUGIN }
)
const M = globalThis.__PCC_MERGED__

const DEFAULT_IDS = ['unfiled', 'backlog', 'active', 'review', 'blocked', 'done', 'canceled']

const card = (over = {}) => ({
  id: 's1',
  sessionId: 's1',
  title: 'Fix the thing',
  projectId: 'p1',
  projectLabel: 'Alpha',
  branch: 'main',
  issue: 0,
  pr: 0,
  prState: null,
  lane: 'active',
  laneSource: 'user',
  laneNote: '',
  lastActivity: 1000,
  ...over
})

// ── view switching state ────────────────────────────────────────────────────

test('the one page has two views, Projects first, and a legacy value migrates', () => {
  assert.deepEqual(M.VIEW_IDS, ['status', 'projects'])
  assert.deepEqual(M.VIEWS.map(view => view.label), ['Status', 'Projects'])
  assert.equal(M.DEFAULT_VIEW, 'projects')

  assert.equal(M.normalizeView(' STATUS '), 'status')
  assert.equal(M.normalizeView('Projects'), 'projects')
  assert.equal(M.normalizeView('branches'), 'projects')
  assert.equal(M.normalizeView('issues'), 'projects')
  assert.equal(M.normalizeView('sessions'), 'projects')
  assert.equal(M.normalizeView(''), 'projects')
  assert.equal(M.normalizeView(null), 'projects')
  assert.equal(M.normalizeView(42), 'projects')
})

test('the view is the one route state kept, in ctx.storage', () => {
  const begin = source.indexOf('function readView(')
  const end = source.indexOf('\n}\n', begin)

  assert.ok(begin >= 0, 'readView missing from desktop/plugin.js')
  const block = source.slice(begin, end + 3)

  assert.match(block, /storage\.get\(VIEW_KEY/, 'the view must read the stored key')
  assert.match(block, /normalizeView\(/, 'a stored view must be normalized')

  const write = source.slice(source.indexOf('function writeView('), source.indexOf('\n}\n', source.indexOf('function writeView(')) + 3)

  assert.match(write, /storage\.set\(VIEW_KEY/, 'the view must be written back to storage')
  assert.match(write, /\$view\.set\(/, 'the render atom must be set too')
  assert.match(source, /const VIEW_KEY = 'ccView\.v1'/, 'the view key must be stable')
  assert.match(source, /\$view\.set\(readView\(\)\)/, 'register must seed the atom from storage')
})

test('the merged page renders one switcher over the two views', () => {
  const begin = source.indexOf('function ProjectCommandCenterPage()')
  const end = source.indexOf('\n}\n', begin)

  assert.ok(begin >= 0, 'the merged page is missing')
  const block = source.slice(begin, end + 3)

  assert.match(block, /useValue\(\$view\)/, 'the page must read the view atom')
  assert.match(block, /jsx\(ViewSwitcher, \{\}\)/, 'the page must render the switcher')
  assert.match(block, /view === 'status'/, 'the page must branch to Status')
  assert.match(block, /jsx\(ProjectsPage, \{\}\)/, 'everything else is the Projects board')
  assert.doesNotMatch(block, /branches|issues/, 'no view may still branch on the removed ids')

  const switcher = source.slice(source.indexOf('function ViewSwitcher()'), source.indexOf('\n}\n', source.indexOf('function ViewSwitcher()')) + 3)

  assert.match(switcher, /options: VIEWS/, 'the switcher options are the constant list, so the width never moves')
  assert.match(switcher, /style: S\.viewSwitch/, 'the switcher sits in a fixed, non-growing wrapper')
  assert.match(source, /viewSwitch: \{ display: 'inline-flex', flex: 'none' \}/, 'the wrapper must not grow')
})

test('the removed grouped views and their helpers are gone', () => {
  for (const gone of [
    'SessionGroupView',
    'GroupedSessions',
    'GROUP_VIEWS',
    'kanbanBranchGroups',
    'kanbanIssueGroups',
    'kanbanIssueState',
    'ISSUE_STATES',
    'cardAsSession',
    'cardLane'
  ]) {
    assert.doesNotMatch(source, new RegExp(`\\b${gone}\\b`), `${gone} must be deleted, not left dead`)
  }
  // DetailSessionRow is still the project page's session row, so it stays.
  assert.match(source, /function DetailSessionRow\(/, 'the project detail page still renders its row')
})

test('the separate sessions route, page and nav entry are gone', () => {
  assert.doesNotMatch(source, /SESSIONS_ROUTE/, 'no page may still point at /sessions')
  assert.doesNotMatch(source, /sessions-page/, 'the second page registration must be gone')
  assert.doesNotMatch(source, /sessions-nav/, 'the second sidebar entry must be gone')
  assert.equal((source.match(/area: SIDEBAR_NAV_AREA/g) || []).length, 1, 'the sidebar keeps one entry')
})

test('the palette opens the two named views and no Issues view', () => {
  const begin = source.indexOf("label: 'Open Projects'")
  const end = source.indexOf(']\n    ])', begin)
  const block = source.slice(begin, end > begin ? end : begin + 2000)

  assert.match(block, /label: 'Open Projects'[\s\S]*run: \(\) => openView\('projects'\)/)
  assert.match(block, /label: 'Open Sessions board'[\s\S]*run: \(\) => openView\('status'\)/)
  assert.doesNotMatch(source, /label: 'Open Issues'/, 'the Issues palette entry is gone')
  assert.doesNotMatch(source, /openView\('issues'\)/, 'nothing may open the removed view')
})

// ── lanes: the server read and the fallback ────────────────────────────────

test('a server lane payload is kept as-is: custom ids, labels, order and system', () => {
  const rows = M.laneRowsFromResponse({
    ok: true,
    lanes: [
      { id: 'ideas', label: 'Ideas', order: 0, custom: true },
      { id: 'active', label: 'Building', order: 1, custom: false, system: 1 },
      { id: 'done', label: 'Shipped', order: 2, custom: false }
    ]
  })

  assert.deepEqual(M.laneIds(rows), ['ideas', 'active', 'done'])
  assert.deepEqual(rows.map(row => row.label), ['Ideas', 'Building', 'Shipped'])
  assert.equal(rows[0].custom, true)
  assert.equal(rows[1].custom, undefined)
  assert.equal(rows[1].system, true, 'a system lane keeps the flag the route sent')
  assert.equal(rows[0].system, undefined)

  // Anything that is not the documented shape reads as "no server table".
  assert.equal(M.laneRowsFromResponse(null), null)
  assert.equal(M.laneRowsFromResponse({ ok: false, code: 'unknown_lane' }), null)
  assert.equal(M.laneRowsFromResponse({ ok: true, lanes: [] }), null)
  assert.equal(M.laneRowsFromResponse({ ok: true }), null)
})

test('a missing lane route falls back to the seven default lanes', () => {
  // The page keeps rendering from ctx.storage, so the fallback table is the same
  // seven ids the store seeds, unfiled first and canceled last.
  const stored = M.laneTableOrDefaults(null)

  assert.deepEqual(M.laneIds(stored), DEFAULT_IDS)
  assert.deepEqual(M.laneIds(M.laneTableOrDefaults(M.KANBAN_LANES)), DEFAULT_IDS)
})

test('an unknown lane id renders in the first lane, not nowhere', () => {
  const table = M.laneRowsFromResponse({
    ok: true,
    lanes: [
      { id: 'triage', label: 'Triage', order: 0, custom: true },
      { id: 'active', label: 'Active', order: 1 }
    ]
  })

  assert.equal(M.lanePlace(table, 'done'), 'triage')
  assert.equal(M.lanePlace(table, 'active'), 'active')
  assert.equal(M.lanePlace(M.laneTableOrDefaults(null), 'someday'), 'unfiled')

  const placed = M.kanbanPlaceCards([card({ id: 'x', lane: 'done' })], table)

  assert.equal(placed[0].lane, 'triage')

  const resolved = M.kanbanLaneFor({ status: 'done' }, null, '', table)

  assert.equal(resolved.lane, 'triage', 'a session left in a deleted lane lands in the first column')
})

test('a custom lane is a real lane for resolution and counting', () => {
  const table = M.laneRowsFromResponse({
    ok: true,
    lanes: [
      { id: 'backlog', label: 'Backlog', order: 0 },
      { id: 'triage', label: 'Triage', order: 1, custom: true }
    ]
  })

  assert.equal(M.kanbanLaneFor({ status: 'triage', status_source: 'user' }, null, '', table).lane, 'triage')
  assert.deepEqual(M.kanbanCounts([card({ lane: 'triage' })], table), { backlog: 0, triage: 1 })
  assert.match(M.laneColor('triage', table), /^var\(--ui-/)
})

// ── lanes: the write bodies ────────────────────────────────────────────────

test('create, rename, order and delete build the bodies the routes document', () => {
  const table = M.laneTableOrDefaults(null)
  const renamed = M.addKanbanLane(table, 'Needs design')
  const id = renamed[renamed.length - 1].id

  assert.equal(id, 'needs-design', 'a label is slugged into an id')
  assert.equal(renamed[renamed.length - 1].custom, true, 'a created lane carries the custom marker')
  assert.deepEqual(M.laneCreateBody('  Needs design  '), { label: 'Needs design' })
  assert.deepEqual(M.laneRenameBody('review', ' In review '), { id: 'review', label: 'In review' })
  assert.deepEqual(M.laneOrderBody(renamed), { ids: M.laneIds(renamed) })
  assert.deepEqual(M.laneDeleteBody('blocked'), { id: 'blocked' })
})

test('the slug is the server rule: 24 characters, a leading letter, no suffix', () => {
  const long = M.addKanbanLane(M.laneTableOrDefaults(null), `aaaa ${'b'.repeat(40)}`)
  const id = long[long.length - 1].id

  assert.equal(id.length, 24, 'the id is the route\'s 24-character cut')
  assert.match(id, /^[a-z][a-z0-9-]*$/, 'the id starts with a letter')
  assert.equal(M.laneSlug('3d printing'), '', 'a digit-first label has no id the route would accept')
  assert.equal(M.laneSlug('---'), '', 'a label with no letters or digits has no id')
})

test('adding twice leaves one column: the route refuses the duplicate id', () => {
  const table = M.laneTableOrDefaults(null)
  const once = M.addKanbanLane(table, 'Needs design')
  const twice = M.addKanbanLane(once, 'Needs design')

  assert.deepEqual(M.laneIds(twice), M.laneIds(once), 'no suffixed id is invented client side')
  assert.equal(M.laneIds(twice).filter(lane => lane === 'needs-design').length, 1)
})

test('the reassign retry ride on the delete body', () => {
  assert.deepEqual(M.laneDeleteBody('done', 'backlog'), { id: 'done', reassign: 'backlog' })
  assert.deepEqual(M.laneDeleteBody('done', '  active '), { id: 'done', reassign: 'active' })
  // No target yet means the first delete attempt, which asks rather than moves.
  assert.deepEqual(Object.keys(M.laneDeleteBody('done', '')), ['id'])
})

test('the CRUD calls go through the documented routes and revert on failure', () => {
  const grab = name => {
    const begin = source.indexOf(`function ${name}(`)
    const end = source.indexOf('\n}\n', begin)

    assert.ok(begin >= 0, `${name} missing from desktop/plugin.js`)

    return source.slice(begin, end + 3)
  }

  const create = grab('createLane')

  assert.match(create, /lanePost\('\/board\/lanes', laneCreateBody\(text\)\)/)
  assert.match(create, /writeLanes\(snapshot\)/, 'a refused create reverts the optimistic table')
  assert.match(create, /laneToast\(/, 'a refused create says so')

  const reorder = grab('reorderLanes')

  assert.match(reorder, /lanePost\('\/board\/lanes\/order', laneOrderBody\(next\)\)/)
  assert.match(reorder, /writeLanes\(snapshot\)/)

  const remove = grab('deleteLane')

  assert.match(remove, /lanePost\('\/board\/lanes\/delete', laneDeleteBody\(laneId, target\)\)/)
  assert.match(remove, /laneRefreshBoard\(/, 'a delete invalidates the board, not only the lanes')
  assert.match(remove, /writeLanes\(snapshot\)/, 'a refused delete reverts')
  assert.match(remove, /err\.code !== 'lane_in_use'/, 'lane_in_use is asked, not toasted')
})

test('the popover asks lane_in_use for a target and retries with reassign', () => {
  const begin = source.indexOf('function LanesPopover(')
  const end = source.indexOf('\n}\n', begin)
  const block = source.slice(begin, end + 3)

  assert.match(block, /err\.code === 'lane_in_use'/, 'the refusal is recognised')
  assert.match(block, /setPending\(\{ counts, id: lane\.id, label: lane\.label \}\)/, 'the pending delete keeps the lane and its counts')
  assert.match(block, /deleteLane\(pending\.id, target\)/, 'the retry carries the chosen lane')
  assert.match(block, /ariaLabel: 'Move sessions to a lane'/, 'the target comes from a select')
  assert.match(block, /children: 'Add lane'/, 'the create control is in the popover')
  assert.match(block, /children: 'Move and remove'/, 'the reassign retry is one click')
})

test('a system lane offers no delete control, only rename and reorder', () => {
  const begin = source.indexOf('function LaneEditorRow(')
  const end = source.indexOf('\n}\n', begin)
  const block = source.slice(begin, end + 3)

  assert.match(block, /lane\.system/, 'the row knows which lanes are system')
  assert.match(block, /disabled: !live \|\| total <= 1 \|\| Boolean\(lane\.system\)/, 'a system lane cannot be removed')
  assert.match(block, /children: 'system'/, 'the row labels the system lanes')
})

test('lane_in_use counts read the route\'s own project_status and links fields', () => {
  assert.deepEqual(M.laneUseCounts({ project_status: 2, links: 3 }), { projects: 2, sessions: 3 })
  assert.deepEqual(M.laneUseCounts({ links: 4 }), { projects: 0, sessions: 4 })
  assert.deepEqual(M.laneUseCounts({ project_status: 1 }), { projects: 1, sessions: 0 })
  assert.deepEqual(M.laneUseCounts(null), { projects: 0, sessions: 0 })
  assert.equal(M.laneUseText('Review', { projects: 2, sessions: 3 }), '3 sessions and 2 projects use Review.')
  assert.equal(M.laneUseText('Review', { projects: 0, sessions: 1 }), '1 session uses Review.')
  assert.equal(M.laneUseText('Review', { projects: 1, sessions: 0 }), '1 project uses Review.')
  assert.equal(M.laneUseText('Review', { projects: 0, sessions: 0 }), 'Review still has sessions.')
})

// ── the projects view's session rendering ──────────────────────────────────

test('the projects view renders every session and no longer truncates a column', () => {
  assert.doesNotMatch(source, /BOARD_CARD_LIMIT/, 'the card limit constant is gone')
  assert.doesNotMatch(source, /S\.more\b/, 'the "+N more" button style is gone')
  const body = source.slice(source.indexOf('function ColumnBody('), source.indexOf('\n}\n', source.indexOf('function ColumnBody(')))
  assert.doesNotMatch(body, /hidden > 0|\+\$\{hidden\}|S\.more/, 'the column body has no truncation branch')
  assert.match(source, /preview_limit: TREE_PREVIEW_LIMIT/, 'the tree read asks for a window wide enough to show them all')
  assert.match(source, /overflowY: 'auto'/, 'the column body scrolls instead of truncating')
})

test('the projects view carries the lane filter button, the visible count and storage', () => {
  assert.match(source, /name: 'filter', size: '0\.875rem' \}\), 'Filter'/, 'the button is labelled Filter')
  assert.match(source, /function SessionLaneFilter\(/, 'the popover is its own component')
  assert.match(source, /type: 'checkbox'/, 'one checkbox per lane')
  assert.match(source, /const LANE_FILTER_KEY = 'projectLaneFilter\.v1'/, 'the filter has a stable storage key')
  assert.match(source, /storage\.get\(LANE_FILTER_KEY/, 'the filter is read from ctx.storage')
  assert.match(source, /storage\.set\(LANE_FILTER_KEY/, 'a toggle writes ctx.storage')
  assert.match(source, /of \$\{sessionCounts\.total\} sessions/, 'the page counts what is visible')
})

// ── row geometry ───────────────────────────────────────────────────────────

test('project rows and detail rows are 60px tall with 8px vertical padding', () => {
  const style = name => {
    const begin = source.indexOf(`\n  ${name}: {`)
    const end = source.indexOf('\n  },', begin)

    assert.ok(begin >= 0, `S.${name} missing`)
    const block = source.slice(begin, end)

    assert.match(block, /minHeight: 60/, `S.${name} must be 60px tall`)
    assert.match(block, /padding: '8px (10|12)px'/, `S.${name} needs 8px vertical padding`)
    assert.match(block, /boxSizing: 'border-box'/, `S.${name} must count the padding inside the 60px`)
  }

  style('card')
  style('sessionRow')

  // One line with an ellipsis, and a badge that cannot wrap.
  const title = source.slice(source.indexOf('\n  cardTitle: {'), source.indexOf('\n  },', source.indexOf('\n  cardTitle: {')))

  assert.match(title, /whiteSpace: 'nowrap'/)
  assert.match(title, /textOverflow: 'ellipsis'/)
  assert.doesNotMatch(title, /WebkitLineClamp/, 'the title is one line, not two')
  assert.match(source, /laneBadge: \{[^}]*whiteSpace: 'nowrap'/, 'the lane badge never wraps')
})

// ── the project page toggle ────────────────────────────────────────────────

test('the project page session panel offers By branch and By lane', () => {
  assert.deepEqual(source.match(/const PANEL_MODES = \[[\s\S]*?\]/)[0].match(/label: '[^']+'/g), ["label: 'By branch'", "label: 'By lane'"])

  const begin = source.indexOf("children: 'Sessions by lane'")
  const block = source.slice(begin, begin + 2600)

  assert.match(block, /SegmentedControl, \{ onChange: setPanelMode, options: PANEL_MODES/, 'the toggle is in the panel header')
  assert.match(block, /panelBody\.kind === 'lane'/, 'the panel branches on the body decision')
  assert.match(block, /jsx\(KanbanViews, \{/, 'the lane mode renders the kanban')
  assert.match(block, /lanes: laneRows/, 'the columns are the server lane table')
  assert.match(block, /onMove: panelMove/, 'a move in the panel is a real move')
})

// ── the low-severity review items, each pinned where it lives ──────────────
//
// Each case runs the code where the code CAN run in node. The remaining source
// assertions are the ones whose subject only exists inside a React render (a JSX
// prop, a mutation callback, the SDK open chain), and each names that reason.

test('D16: laneFailure maps the route\'s own project_status and links onto the error', () => {
  // Executed: the whole lane_in_use path, counts and sentence together.
  const { laneFailure, laneUseCounts, laneUseText } = new Function(
    `${fn('laneUseCounts')}\n${fn('laneUseText')}\n${fn('laneFailure')}\nreturn { laneFailure, laneUseCounts, laneUseText }`
  )()
  const refused = laneFailure({ ok: false, code: 'lane_in_use', project_status: 2, links: 3 })

  assert.deepEqual(refused.counts, { projects: 2, sessions: 3 }, 'the route shape is mapped, not read as res.counts')
  assert.equal(refused.code, 'lane_in_use')
  assert.equal(refused.message, 'That lane still has sessions.')
  // The popover's sentence is built from the SAME two numbers.
  assert.equal(laneUseText('Review', refused.counts), '3 sessions and 2 projects use Review.')
  assert.deepEqual(laneUseCounts({ project_status: 1, links: 0 }), { projects: 1, sessions: 0 })
  // The message key the route never sends is not read as a count.
  assert.deepEqual(laneFailure({ code: 'lane_in_use', counts: { projects: 9, sessions: 9 } }).counts, { projects: 0, sessions: 0 })
  // The route's own sentence wins, and an unknown code still says something.
  assert.equal(laneFailure({ code: 'lane_in_use', error: '  pick another lane  ' }).message, 'pick another lane')
  assert.equal(laneFailure({ code: 'nope' }).message, 'The board refused the lane change.')
})

// D17 - the slug is executed; the provisional-column half is a mutation callback
// inside a React component (`createLane`), which node cannot run: its three lines
// are pinned as source, and the reason is that there is no component to mount.
test('D17: a created lane keeps its provisional column, and only the route refuses a duplicate', () => {
  assert.equal(M.laneSlug('Shipping now'), 'shipping-now')
  assert.equal(M.laneSlug('  A really long lane label that runs past the cap  ').length, 24)
  const cut = M.laneSlug('a-'.repeat(20))

  assert.equal(cut.length, 23, 'the slug is cut to 24 and a trailing dash removed')
  assert.doesNotMatch(cut, /-$/, 'a dash the cut leaves is stripped')
  assert.match(cut, /^[a-z][a-z0-9-]*$/)
  assert.equal(M.laneSlug('3d printing'), '', 'a digit-first label has no id the route would accept')
  assert.equal(M.laneSlug('---'), '')

  assert.match(source, /provisionalLanes\.add\(added\.id\)/, 'the optimistic id is marked provisional')
  assert.match(source, /withProvisionalLanes\(serverRows\)/, 'a reread merges the provisional row back in')
  assert.match(source, /provisionalLanes\.delete\(added\.id\)/, 'the marker dies when the POST settles')
  assert.match(source, /laneSlug\(text\) \? 'lane_exists' : 'bad_label'/, 'a duplicate is the route\'s refusal')
})

test('D18: a session list at the read limit says newest, not the whole history', () => {
  // Executed: the label function the view renders.
  assert.equal(M.sessionCountLabel(0, 0), '')
  assert.equal(M.sessionCountLabel(1, 1), '1 session')
  assert.equal(M.sessionCountLabel(4, 4), '4 sessions')
  assert.equal(M.sessionCountLabel(4, 2), '2 of 4 sessions', 'a filter says how much it shows')
  assert.equal(M.sessionCountLabel(M.LIST_LIMIT, M.LIST_LIMIT), `newest ${M.LIST_LIMIT} sessions`, 'the cap is named')
  assert.equal(M.sessionCountLabel(M.LIST_LIMIT, 3), `3 of ${M.LIST_LIMIT} sessions`)
  // The read that produces a 200-row list uses the one constant.
  assert.match(source, /const SESSION_LIST_LIMIT = 200/, 'the read limit is one constant')
  assert.match(source, /limit: SESSION_LIST_LIMIT/, 'the read uses it')
})

// D19 - these are ABSENCES: deleted helpers and a duplicated style key that has no
// behaviour to observe, because the defect is code that should not exist.
test('D19: the dead helpers and the duplicate style keys are gone', () => {
  assert.doesNotMatch(source, /function kanbanLane\(/, 'kanbanLane had no caller')
  assert.doesNotMatch(source, /function mergeKanbanOrder\(/, 'mergeKanbanOrder had no caller')
  const styleBlock = source.slice(source.indexOf('const S = {'), source.indexOf('\n}\n', source.indexOf('const S = {')))
  const keys = [...styleBlock.matchAll(/^ {2}([A-Za-z0-9_]+):/gm)].map(match => match[1])

  assert.equal(new Set(keys).size, keys.length, 'no later S key silently overrides an earlier one')
  assert.doesNotMatch(source, /collapsed\s*\?\s*'chevron-right'/, 'the expanded column has no folded branch')
  assert.doesNotMatch(source, /catch\(err => \{\s*throw err\s*\}\)/, 'the empty rethrow catch is gone')
})

// D20 - the subject is a JSX prop pair on two controls, so there is no function to
// call: a disabled state and its title only exist when the component renders.
test('D20: a disabled lane control says why it cannot act', () => {
  assert.match(source, /title: card\.projectId \? undefined : 'A lane cannot be saved without a project/, 'the table select explains its disabled state')
  assert.match(source, /Lanes cannot be removed while the board backend is not answering/, 'a dead backend is the tip, not Remove X')
})

test('D21: an unknown branch draws no chip, never the invented main', () => {
  // Executed: the one reader every card uses.
  assert.equal(M.sessionBranch({ git_branch: 'feat/x' }, 'trunk'), 'feat/x')
  assert.equal(M.sessionBranch({}, 'trunk'), 'trunk', 'the folder branch is the fallback')
  assert.equal(M.sessionBranch({ git_branch: '   ' }, '  '), '', 'nothing known means no chip')
  assert.equal(M.sessionBranch(null, ''), '')
  // The absence of a literal 'main' default anywhere in the file.
  assert.doesNotMatch(source, /\|\| 'main'/, 'no branch falls back to main')
})

// D23 - movedLane is pure and executed; the two call sites are a map inside a
// component render, so their wiring is asserted next to the behaviour it uses.
test('D23: the projects view and the detail rows read the pending-move cache', () => {
  const resolved = { lane: 'active', note: 'a note', source: 'project' }

  assert.deepEqual(M.movedLane({ s1: { status: 'review' } }, 's1', resolved), { lane: 'review', note: '', source: 'user' })
  assert.equal(M.movedLane({ s1: { status: 'review' } }, 's2', resolved), resolved, 'an unrelated session keeps its lane')
  assert.equal(M.movedLane({}, 's1', resolved), resolved)
  assert.equal(M.movedLane(null, 's1', resolved), resolved)
  // A pending row with no status is not a move.
  assert.equal(M.movedLane({ s1: {} }, 's1', resolved), resolved)

  const column = source.slice(source.indexOf('function ProjectColumn('), source.indexOf('\n}\n', source.indexOf('function ProjectColumn(')))
  const detail = source.slice(source.indexOf('function ProjectDetailPage('), source.indexOf('\nconst panelCounts', source.indexOf('function ProjectDetailPage(')))

  assert.match(column, /movedLane\(moved, id, laneForLink/, 'a project column row applies a posted move')
  assert.match(detail, /movedLane\(overrides, id, laneForLink/, 'a detail row applies a posted move')
  assert.match(detail, /lane: move\.status, laneSource: 'user'/, 'the lane panel card applies the same move')
})

// D24 - the subject is the SDK open chain: `openSession` resolves inside the app,
// and the ordering it needs (never write the composer before the tab exists) is a
// promise chain with no pure half to hand an input to.
test('D24: openSession answers whether the tab opened, and the draft waits for it', () => {
  const begin = source.indexOf('function openLinkedSession(')
  const block = source.slice(begin, source.indexOf('\n}\n', begin) + 3)

  assert.match(source, /\.then\(\(\) => true\)[\s\S]{0,400}return false/, 'openSession resolves true or false')
  assert.match(block, /return openSession\(sessionId\)\.then\(opened => \{/, 'the draft is chained to the open')
  assert.match(block, /if \(!opened\) return false/, 'a failed open stops before the composer')
})

test('the pages are only ever rendered inside the merged page, so the flag is gone', () => {
  assert.doesNotMatch(source, /function ProjectsPage\(\{ embedded \}\)/)
  assert.doesNotMatch(source, /function SessionsKanbanPage\(\{ embedded \}\)/)
  assert.doesNotMatch(source, /embedded \?/)
  assert.match(source, /jsx\(SessionsKanbanPage, \{\}\)/)
})

test('one .pcc-link rule carries the button reset the table used to repeat inline', () => {
  assert.match(source, /\.pcc-link \{ border: 0; background: transparent; color: inherit; font: inherit; padding: 0; cursor: pointer; text-align: left;/, 'the class owns the reset')

  const table = source.slice(source.indexOf('function KanbanTable('), source.indexOf('\n}\n', source.indexOf('function KanbanTable(')))

  assert.doesNotMatch(table, /style: \{\s*border: 0,/, 'the table buttons carry the class, not the inline reset')
  assert.equal((table.match(/className: 'pcc-link'/g) || []).length, 2)
})
