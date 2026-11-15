// Proof for the final gate round: the two blocking findings (a pending lane
// move that only the Status view cleared, and a failed project read printed as
// an empty project) plus the follow-ups around them.
//
// Where the behaviour lives in a pure function or a handler the page really
// runs, the test executes it. A React render has no root in this harness, so
// the two call-site checks in H mount the component with a stub `jsx` and call
// the handler it produced, which is the component's own path and fails when the
// prop forwarding is dropped. Hooks (`useBoard`) need a React root; those are
// asserted as the source of the hook that owns the call, and the test says so.
//
// Run with:
//   node --test tests/gate_fixes.test.mjs

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(resolve(HERE, '..'), 'desktop', 'plugin.js')
const source = readFileSync(PLUGIN, 'utf8')

// One whole function, from its `function` line to its top-level closing brace.
function fn(name) {
  const start = source.indexOf(`function ${name}(`)

  assert.ok(start >= 0, `${name} missing from desktop/plugin.js`)

  const end = source.indexOf('\n}\n', start)

  assert.ok(end > start, `${name} has no closing brace`)

  return source.slice(start, end + 3)
}

function slice(begin, end) {
  const at = source.indexOf(begin)
  const to = source.indexOf(end)

  assert.ok(at >= 0, `${begin} missing from desktop/plugin.js`)
  assert.ok(to > at, `${end} missing or before the begin marker`)

  return source.slice(at, to + end.length)
}

// The pure lane and kanban halves, loaded the way the other suites load them:
// the real helpers, not copies.
const laneBlock = slice('// session-lane:begin', '// session-lane:end')
const kanbanBlock = slice('// session-kanban:begin', '// session-kanban:end')

vm.runInThisContext(`${laneBlock}\n${kanbanBlock}`, { filename: PLUGIN })

// ── A: the pending move clears wherever the board is read ────────────────────
// `reconcileSessionLanes` is the rule; run it against a stand-in atom.
function reconciler() {
  const context = vm.createContext({
    SESSION_POST_TTL_MS: 30000,
    rows: {},
    $sessionStatus: {
      get: () => context.rows,
      set: next => {
        context.rows = next
      }
    }
  })

  vm.runInContext(`${fn('reconcileSessionLanes')}\n;globalThis.__reconcile = reconcileSessionLanes`, context, {
    filename: PLUGIN
  })

  return context
}

test('A: the pending move is dropped when the board echoes it, kept until then', () => {
  const context = reconciler()
  const now = Date.now()

  context.rows = { s1: { project_id: 'p1', status: 'done', at: now } }
  context.__reconcile([{ session_id: 's1', status: 'review' }])
  assert.ok(context.rows.s1, 'a board that has not echoed the move keeps the pending lane')

  context.__reconcile([{ session_id: 's1', status: 'done' }])
  assert.deepEqual({ ...context.rows }, {}, 'the echo ends the pending move')
})

test('A: useBoard owns the reconcile, so every page clears the pending move', () => {
  // Hooks need a React root this harness does not have, so this half is the
  // source of the one hook that mounts on the project page, the Projects view
  // and the Status view alike.
  assert.match(fn('useBoard'), /reconcileSessionLanes\(links\)/, 'useBoard is the one place every page reads the board')
  assert.doesNotMatch(fn('useSessionCards'), /reconcileSessionLanes/, 'the Status view no longer owns the clear')
  assert.match(fn('useBoard'), /useEffect\(\(\) => \{\s*reconcileSessionLanes\(links\)\s*\}, \[links\]\)/)
})

// ── B: a failed project read is its own sentence ─────────────────────────────
const projectSessionsFailureText = () =>
  new Function(`${fn('projectSessionsFailureText')}\nreturn projectSessionsFailureText`)()

test('B: a failed project read says so instead of reporting an empty project', () => {
  const text = projectSessionsFailureText()

  assert.equal(
    text(new Error('REST 502: the gateway did not answer.')),
    "Could not load this project's sessions: REST 502: the gateway did not answer. Retrying on the next refresh."
  )
  assert.equal(
    text(null),
    "Could not load this project's sessions: the request failed. Retrying on the next refresh."
  )

  const page = fn('ProjectDetailPage')

  assert.match(page, /const detailFailure = detail\.isError && !detail\.data \? projectSessionsFailureText\(detail\.error\) : ''/)
  assert.match(page, /const panelBody = projectPanelBody\(/, 'the panel takes its body from the one decision')
})

// A `projects.tree` entry as `build_tree(hydrate=False)` really shapes it: the
// project keeps its repo and branch groups, with every session list empty. That
// is the payload the old panel read as "one lane, no sessions".
const TREE_PROJECT = {
  id: 'p1',
  label: 'Alpha',
  path: '/alpha',
  sessionCount: 2,
  repos: [
    { id: 'main', label: 'alpha', path: '/alpha', groups: [{ id: 'main', label: 'main', isGit: true, sessions: [] }] }
  ]
}

// The panel decision as the page runs it: the sliced block, not a copy, with the
// one dependency `laneGroups` sorts by stubbed (every list here is empty, so the
// sort cannot change the result).
function panel() {
  const block = slice('// project-panel:begin', '// project-panel:end')
  const context = vm.createContext({ sessionStamp: () => 0 })

  vm.runInContext(
    `${fn('laneGroups')}\n${block}\n;globalThis.__panel = { laneGroups, projectPanelBody }`,
    context,
    { filename: PLUGIN }
  )

  return context.__panel
}

test('B: a failed read outranks the tree-shaped lane, in both view modes', () => {
  const { laneGroups, projectPanelBody } = panel()
  const lanes = laneGroups(TREE_PROJECT)
  const failure = "Could not load this project's sessions: REST 502: the gateway did not answer. Retrying on the next refresh."

  assert.equal(lanes.length, 1, 'the tree hands the panel one branch')
  assert.equal(lanes[0].sessions.length, 0, 'with its session list empty')

  for (const panelMode of ['branch', 'lane']) {
    const body = projectPanelBody({ detailFailure: failure, isLoading: false, lanes, panelMode })

    assert.equal(body.kind, 'notice', `${panelMode}: a failed read is a notice, not a branch or a board`)
    assert.equal(body.text, failure, `${panelMode}: the failure sentence wins over the empty lane`)
  }

  // And the call site one level up honours the notice before either view mounts,
  // so the decision cannot be right while the page still renders the lane.
  const page = fn('ProjectDetailPage')
  const notice = page.indexOf("panelBody.kind === 'notice'")
  const kanban = page.indexOf('jsx(KanbanViews')

  assert.ok(notice >= 0, 'the panel renders the notice branch')
  assert.ok(kanban > notice, 'the notice is checked before the lane board is mounted')
})

test('B: first load says it is loading, and an empty answer still says the project is empty', () => {
  const { laneGroups, projectPanelBody } = panel()
  const empty = laneGroups({ id: 'p2', label: 'Empty', repos: [] })
  const lanes = laneGroups(TREE_PROJECT)

  // The helper runs inside the vm realm, so compare its fields, not its
  // prototype.
  const body = args => ({ ...projectPanelBody(args) })

  assert.deepEqual(body({ detailFailure: '', isLoading: false, lanes: empty, panelMode: 'branch' }), {
    kind: 'notice',
    text: 'No sessions in this project yet'
  })
  assert.deepEqual(body({ detailFailure: '', isLoading: true, lanes, panelMode: 'branch' }), {
    kind: 'notice',
    text: 'Loading sessions...'
  })
  assert.deepEqual(body({ detailFailure: '', isLoading: false, lanes, panelMode: 'branch' }), {
    kind: 'branch',
    text: ''
  })
  assert.deepEqual(body({ detailFailure: '', isLoading: false, lanes, panelMode: 'lane' }), {
    kind: 'lane',
    text: ''
  })
})

// ── C: a failed tree read is not a missing project ───────────────────────────
test('C: a failed tree read says the tree could not be read, not that the project is gone', () => {
  const page = fn('ProjectDetailPage')
  const failed = page.indexOf('if (tree.isError && !tree.data) {')
  const gone = page.indexOf("title: 'That project is gone'")

  assert.ok(failed >= 0, 'the page branches on a failed tree read')
  assert.ok(gone > failed, 'the failure branch comes before the gone branch')
  assert.match(
    page.slice(failed, gone),
    /Could not read the project tree, so this project can't be shown right now\./
  )
})

// ── D: the titlebar reports a failed read as a failed read ──────────────────
function titlebar() {
  const state = { board: null, tree: null, opened: [], viewed: [], notified: [], boardAnswer: { links: [] } }
  const context = vm.createContext({
    TREE_PREVIEW_LIMIT: 500,
    boardRest: () => (state.board ? Promise.resolve(state.boardAnswer) : Promise.reject(new Error('REST 503'))),
    host: {
      state: {
        focusedStoredSessionId: { get: () => 's1' },
        cwd: { get: () => '' }
      },
      notify: message => state.notified.push(message),
      request: () => (state.tree ? Promise.resolve({ projects: [] }) : Promise.reject(new Error('no tree')))
    },
    jsx: () => ({}),
    Codicon: () => null,
    openProject: id => state.opened.push(id),
    openView: id => state.viewed.push(id),
    profileParam: () => ({})
  })

  vm.runInContext(
    `${fn('primaryPath')}\n${fn('projectForPath')}\n${fn('projectFoldersOf')}\n${fn('linkProjectForSession')}\n${fn('focusedSessionId')}\n${fn('focusedCwd')}\n${fn('focusedProjectId')}\n${fn('openFocusedSessionProject')}
;globalThis.__open = openFocusedSessionProject`,
    context,
    { filename: PLUGIN }
  )

  return { context, state }
}

test('D: an unreachable read opens the board and says the read failed', async () => {
  const { context, state } = titlebar()

  await context.__open()

  assert.deepEqual(state.opened, [])
  assert.deepEqual(state.viewed, ['projects'], 'the board still opens')
  assert.deepEqual(
    state.notified.map(message => `${message.kind}:${message.message}`),
    ["error:Could not reach the board to find this session's project, so the board opened instead."]
  )
})

test('D: a read that answered "no project" keeps the project-less sentence', async () => {
  const { context, state } = titlebar()

  state.board = true
  state.tree = true

  await context.__open()

  assert.deepEqual(state.viewed, ['projects'])
  assert.deepEqual(
    state.notified.map(message => `${message.kind}:${message.message}`),
    ['info:This session is not in a project yet, so the board opened instead.']
  )
})

test('D: a board that answered ok:false is a failed read, not a project-less session', async () => {
  const { context, state } = titlebar()

  state.board = true
  state.tree = true
  // The store failure shape: HTTP 200, ok:false, and links empty. Truthy, so the
  // old check called it an answer and the click blamed the session.
  state.boardAnswer = {
    ok: false,
    code: 'store_error',
    error: 'the board database could not be reached: OperationalError',
    links: [],
    statuses: []
  }

  await context.__open()

  assert.deepEqual(state.opened, [])
  assert.deepEqual(state.viewed, ['projects'], 'the board still opens')
  assert.deepEqual(
    state.notified.map(message => `${message.kind}:${message.message}`),
    ["error:Could not reach the board to find this session's project, so the board opened instead."]
  )
})

// ── F: a project lane pick that names a dead lane is refused out loud ───────
test('F: picking a lane the board no longer has says so instead of returning', () => {
  const state = { notices: [], posts: [] }
  const context = vm.createContext({
    laneIds: rows => rows.map(row => row.id),
    $lanes: { get: () => [{ id: 'active' }, { id: 'review' }] },
    $statuses: { get: () => ({}) },
    saveStatuses: () => {},
    statusPostedAt: new Map(),
    boardRest: () => state.posts.push('POST'),
    queryClient: { invalidateQueries: () => {} },
    QUERY_ROOT: 'pcc',
    statusFailure: () => new Error('no'),
    host: { notify: message => state.notices.push(message) }
  })

  vm.runInContext(`${fn('writeStatus')}\n;globalThis.__writeStatus = writeStatus`, context, { filename: PLUGIN })
  context.__writeStatus('p1', 'qa-check')

  assert.deepEqual(state.posts, [], 'a dead lane is never posted')
  assert.deepEqual(
    state.notices.map(message => `${message.kind}:${message.message}`),
    ['warning:The board has no "qa-check" lane any more, so the project lane was not changed.']
  )
})

// ── G: a failed project sync is reported, once per distinct reason ──────────
function sync() {
  const state = { notices: [], posts: 0, fail: null, answer: { ok: true } }
  const context = vm.createContext({
    projectSyncSig: '',
    projectSyncError: '',
    boardRest: () => {
      state.posts += 1

      return state.fail ? Promise.reject(state.fail) : Promise.resolve(state.answer)
    },
    host: { notify: message => state.notices.push(message) }
  })

  vm.runInContext(`${fn('syncProjects')}\n;globalThis.__sync = syncProjects`, context, { filename: PLUGIN })

  return { context, state }
}

test('G: a failed project sync says why, and does not repeat itself', async () => {
  const { context, state } = sync()

  state.fail = new Error('REST 500: board unavailable.')
  await context.__sync([{ id: 'p1', label: 'Alpha', paths: ['/alpha'] }], 'sig-1')

  assert.equal(context.projectSyncSig, '', 'a failed sync is not recorded as done')
  assert.deepEqual(
    state.notices.map(message => `${message.kind}:${message.message}`),
    ['warning:Could not register this project with the board: REST 500: board unavailable.']
  )

  await context.__sync([{ id: 'p1', label: 'Alpha', paths: ['/alpha'] }], 'sig-1')
  assert.equal(state.notices.length, 1, 'the same failure on the next poll is not a second toast')

  state.fail = null
  await context.__sync([{ id: 'p1', label: 'Alpha', paths: ['/alpha'] }], 'sig-1')
  assert.equal(context.projectSyncSig, 'sig-1', 'a sync that landed is remembered')
  assert.equal(state.notices.length, 1)
})

test('G: an HTTP 200 that says ok:false is a failed sync, so it says so and retries', async () => {
  const { context, state } = sync()
  const rows = [{ id: 'p1', label: 'Alpha', paths: ['/alpha'] }]

  // The route answers 200 with the store's own refusal, which used to be read
  // as a landed sync: recorded, silent, and never retried until the tree moved.
  state.answer = {
    ok: false,
    code: 'store_error',
    error: 'the board database could not be reached: OperationalError'
  }
  await context.__sync(rows, 'sig-1')

  assert.equal(context.projectSyncSig, '', 'a refused sync is not recorded as done')
  assert.deepEqual(
    state.notices.map(message => `${message.kind}:${message.message}`),
    ['warning:Could not register this project with the board: the board database could not be reached: OperationalError.']
  )

  await context.__sync(rows, 'sig-1')
  assert.equal(state.posts, 2, 'the next board tick retries, instead of waiting for the tree to change')

  state.answer = { ok: true }
  await context.__sync(rows, 'sig-1')
  assert.equal(context.projectSyncSig, 'sig-1', 'an ok answer is the one that is remembered')
})

// ── H1: StartSessionButton forwards `kind` to openIssueDraft ────────────────
function startButton() {
  const state = { drafts: [], notices: [] }
  const element = (type, props) => ({ type, props })
  const StartSessionButton = new Function(
    'useState',
    'jsx',
    'jsxs',
    'Button',
    'Codicon',
    'openIssueDraft',
    'host',
    `${fn('StartSessionButton')}\nreturn StartSessionButton`
  )(
    () => [false, () => {}],
    element,
    element,
    'Button',
    'Codicon',
    args => {
      state.drafts.push(args)

      return Promise.resolve(true)
    },
    { notify: message => state.notices.push(message) }
  )

  return { StartSessionButton, state }
}

test('H: the start-session button hands the issue kind to the draft opener', async () => {
  const { StartSessionButton, state } = startButton()
  const mounted = StartSessionButton({
    folder: '/home/nyll/dev/alpha',
    issue: { number: 7, title: 'A bug', state: 'open' },
    kind: 'pr',
    owner: 'nyll',
    projectId: 'p1',
    repo: 'alpha'
  })

  assert.equal(mounted.type, 'Button')
  await mounted.props.onClick({ stopPropagation() {} })
  await new Promise(done => setTimeout(done, 0))

  assert.equal(state.drafts.length, 1, 'the click opened exactly one draft')
  assert.equal(state.drafts[0].kind, 'pr', 'without `kind` the draft is written as an issue, not a pull request')
  assert.equal(state.drafts[0].issue.number, 7)
  assert.equal(state.drafts[0].folder, '/home/nyll/dev/alpha')
})

// ── H2: ProjectColumn gives the summary the live lane table ────────────────
// The component is mounted with a stub `jsx`, so its own render path runs and
// the summary string it builds is the one a column shows.
function projectColumn() {
  const state = { rendered: null }
  const element = (type, props) => ({ type, props })
  const stub = () => ''
  const ProjectColumn = new Function(
    'useMemo',
    'useCallback',
    'recentSessions',
    'formatCost',
    'formatTokens',
    'linksBySession',
    'primaryPath',
    'RUNNING',
    'jsx',
    'jsxs',
    'S',
    'ArcRing',
    'Avatar',
    'PathLine',
    'ProjectIdRow',
    'Tip',
    'Button',
    'Codicon',
    'StatusPicker',
    'BoardLinkChips',
    'StatusDot',
    'ColumnBody',
    `${fn('ProjectColumn')}\nreturn ProjectColumn`
  )(
    run => run(),
    run => run,
    () => [{ id: 's1' }, { id: 's2' }],
    stub,
    stub,
    rows => new Map(rows.map(row => [row.session_id, row])),
    stub,
    new Set(),
    element,
    element,
    {},
    stub,
    stub,
    stub,
    stub,
    stub,
    stub,
    stub,
    stub,
    stub,
    stub,
    stub
  )

  const lanes = [
    { id: 'unfiled', label: 'Unfiled' },
    { id: 'backlog', label: 'Backlog' },
    { id: 'active', label: 'Active' },
    { id: 'review', label: 'QA check' },
    { id: 'blocked', label: 'Blocked' },
    { id: 'done', label: 'Done' },
    { id: 'canceled', label: 'Canceled' }
  ]

  state.rendered = ProjectColumn({
    project: { id: 'p1', label: 'Alpha', sessionCount: 2, sessionIds: [], totalTokens: 0, totalCostUsd: 0 },
    index: 0,
    status: 'active',
    lane: 'active',
    laneRows: lanes,
    linkRows: [
      { session_id: 's1', project_id: 'p1', status: 'review' },
      { session_id: 's2', project_id: 'p1', status: 'review' }
    ],
    refs: new Map(),
    liveByStored: new Map(),
    moved: {},
    onOpenProject: stub,
    onOpenSession: stub,
    links: [],
    counts: {},
    laneFilter: null,
    goalById: {}
  })

  return state.rendered
}

function strings(node, out = []) {
  if (typeof node === 'string') {
    out.push(node)

    return out
  }

  if (Array.isArray(node)) {
    for (const item of node) strings(item, out)

    return out
  }

  if (node && node.props) strings(node.props.children, out)

  return out
}

test('H: the project column summary names the live lane, so laneRows reaches it', () => {
  const rendered = projectColumn()
  const summary = strings(rendered).find(text => text.includes('sessions'))

  assert.equal(summary, '2 sessions \u00b7 2 qa check', 'the renamed system lane prints its live label, not the seeded word')
})

// ── L: the dead readers are gone ────────────────────────────────────────────
test('L: the unused colour helper and the unused group list style are removed', () => {
  assert.doesNotMatch(source, /function ghStatusColor\(/, 'ghStatusColor has no caller outside the tests')
  assert.doesNotMatch(source, /ghStatusColor/, 'no call site left either')
  assert.doesNotMatch(source, /groupList/, 'the group list style had no reader')
})
