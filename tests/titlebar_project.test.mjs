// Proof for the titlebar tool's click. It cannot be a render test: the tool is
// registered into the Desktop titlebar, and the node harness has no React root
// or plugin host. What it CAN do is run the real resolution functions in a vm
// with the same inputs the app has at click time (a focused stored id, /board's
// link rows, projects.tree, host.state.cwd), so the order is behaviour, not
// source text.
//
// Run with:
//   node --test tests/titlebar_project.test.mjs

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(resolve(HERE, '..'), 'desktop', 'plugin.js')
const source = readFileSync(PLUGIN, 'utf8')

function fn(name) {
  const start = source.indexOf(`function ${name}(`)

  assert.ok(start >= 0, `${name} missing from desktop/plugin.js`)

  const end = source.indexOf('\n}\n', start)

  assert.ok(end > start, `${name} has no closing brace`)

  return source.slice(start, end + 3)
}

// The state the click reads, plus the two doors it writes through. Reset per
// test so one case cannot leak a link row into the next.
let board
let tree
let focused
let cwd
let opened
let viewed
let notified

const host = {
  state: {
    focusedStoredSessionId: { get: () => focused },
    cwd: { get: () => cwd },
    profile: { get: () => '' }
  },
  notify: message => notified.push(message),
  request: method => Promise.resolve(method === 'projects.tree' ? tree : null)
}

const context = vm.createContext({
  TREE_PREVIEW_LIMIT: 500,
  boardRest: () => Promise.resolve(board),
  host,
  jsx: () => ({ icon: true }),
  Codicon: () => null,
  openProject: id => opened.push(id),
  openView: id => viewed.push(id),
  profileParam: () => ({})
})

vm.runInContext(
  `${fn('primaryPath')}
${fn('projectForPath')}
${fn('projectFoldersOf')}
${fn('linkProjectForSession')}
${fn('focusedSessionId')}
${fn('focusedCwd')}
${fn('focusedProjectId')}
${fn('openFocusedSessionProject')}
${fn('titlebarTool')}
;globalThis.__PCC_TITLEBAR__ = { titlebarTool, focusedProjectId, openFocusedSessionProject }`,
  context,
  { filename: PLUGIN }
)

const T = context.__PCC_TITLEBAR__

function reset({ focusedId = '', cwdPath = '', links = [], projects = [] } = {}) {
  board = { links, projects: [] }
  tree = { projects }
  focused = focusedId
  cwd = cwdPath
  opened = []
  viewed = []
  notified = []
}

test('the tool runs onSelect and never leans on a route change', () => {
  reset()

  const tool = T.titlebarTool()

  assert.equal(tool.id, 'pcc-open')
  assert.equal(tool.to, undefined, 'a `to` no-ops when the session tab already sits on this route')
  assert.equal(typeof tool.onSelect, 'function')
  assert.equal(tool.label, "Open this session's project")
  assert.equal(tool.title, "Open this session's project")
})

test('the board link row answers first, even when the tree claims the session elsewhere', async () => {
  reset({
    focusedId: 's1',
    links: [{ session_id: 's1', project_id: 'p9', status: 'active' }],
    projects: [
      { id: 'p1', label: 'Alpha', path: '/home/nyll/dev/alpha', sessionIds: ['s1'], previewSessions: [] }
    ]
  })

  await T.openFocusedSessionProject()

  assert.deepEqual(opened, ['p9'])
  assert.deepEqual(viewed, [])
  assert.deepEqual(notified, [])
})

test('an unlinked session is placed by the tree sessionIds claim, not a path guess', async () => {
  reset({
    focusedId: 's2',
    projects: [
      { id: 'p1', label: 'Alpha', path: '/home/nyll/dev/alpha', sessionIds: ['s1'], previewSessions: [] },
      { id: 'p2', label: 'Beta', path: '/home/nyll/dev/beta', sessionIds: ['s2'], previewSessions: [] }
    ]
  })

  await T.openFocusedSessionProject()

  assert.deepEqual(opened, ['p2'])
  assert.deepEqual(viewed, [])
})

test('with no link and no claim, the longest project folder containing cwd wins', async () => {
  reset({
    focusedId: 's3',
    cwdPath: '/home/nyll/dev/alpha/packages/board',
    projects: [
      { id: 'p1', label: 'Alpha', path: '/home/nyll/dev/alpha', sessionIds: [], previewSessions: [] },
      { id: 'p2', label: 'Board', path: '/home/nyll/dev/alpha/packages/board', sessionIds: [], previewSessions: [] }
    ]
  })

  await T.openFocusedSessionProject()

  assert.deepEqual(opened, ['p2'])
})

test('no project opens the board and says so instead of doing nothing', async () => {
  reset({
    focusedId: 's4',
    projects: [
      { id: '__no_project__', label: 'Home', isNoProject: true, sessionIds: ['s4'], previewSessions: [] }
    ]
  })

  await T.openFocusedSessionProject()

  assert.deepEqual(opened, [], 'the Home bucket is not a project')
  assert.deepEqual(viewed, ['projects'])
  // The notify payload is built inside the vm realm, so compare its fields.
  assert.deepEqual(
    notified.map(message => `${message.kind}:${message.message}`),
    ['info:This session is not in a project yet, so the board opened instead.']
  )
})

// Two different "no project" cases, two different sentences. With nothing focused
// the old line claimed a session was open and missing a project, which is not what
// the click found.
test('no focused session is not the same message as a session without a project', async () => {
  reset({ focusedId: '', projects: [] })

  await T.openFocusedSessionProject()

  assert.deepEqual(opened, [])
  assert.deepEqual(viewed, ['projects'], 'the board still opens')
  assert.deepEqual(
    notified.map(message => `${message.kind}:${message.message}`),
    ['info:No session is focused yet, so the board opened instead.']
  )

  // A focused session with no project of its own keeps its own wording.
  reset({
    focusedId: 's7',
    projects: [{ id: 'p1', label: 'Alpha', path: '/home/nyll/dev/alpha', sessionIds: [], previewSessions: [] }]
  })

  await T.openFocusedSessionProject()

  assert.deepEqual(
    notified.map(message => `${message.kind}:${message.message}`),
    ['info:This session is not in a project yet, so the board opened instead.']
  )
})
