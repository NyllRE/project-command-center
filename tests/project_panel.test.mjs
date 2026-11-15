// Proof that the project page's By-lane panel behaves like the Status board.
//
// This one is source-level and that is deliberate: the defect lives in the
// props a React element is mounted with. KanbanViews calls useValue/useState and
// DragEvents, and the node harness has no React root, no plugin host and no
// drag plumbing, so the only honest assertion is on the mount itself: the fold
// map, the toggle and the drop handler the panel is given are the Status board's
// own. What the toggle computes is already covered behaviourally by
// sessions_kanban.test.mjs (laneCollapsed and the rail render).
//
// Run with:
//   node --test tests/project_panel.test.mjs

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(resolve(HERE, '..'), 'desktop', 'plugin.js')
const source = readFileSync(PLUGIN, 'utf8')

function block(name, end) {
  const start = source.indexOf(`function ${name}(`)

  assert.ok(start >= 0, `${name} missing from desktop/plugin.js`)

  const stop = source.indexOf(end, start)

  assert.ok(stop > start, `${name}: ${end} missing`)

  return source.slice(start, stop)
}

const detail = block('ProjectDetailPage', 'function ProjectCommandCenterPage(')
const status = block('SessionsKanbanPage', 'function ProjectCommandCenterPage(')

// Both pages mount KanbanViews through `jsx`; the whole prop list sits well
// inside a 1400-character window from the mount, and asserting on that window
// keeps the check to the element the panel is handed.
function mountProps(text) {
  const at = text.indexOf('jsx(KanbanViews, {')

  assert.ok(at >= 0, 'KanbanViews is mounted')

  return text.slice(at, at + 1400)
}

const panelProps = mountProps(detail)
const statusProps = mountProps(status)

test('the project panel gets the same fold map and toggle as the status board', () => {
  assert.match(detail, /const collapsedLanes = useValue\(\$collapsed\)/)
  assert.match(panelProps, /collapsed: collapsedLanes/)
  assert.match(panelProps, /onToggleCollapse: panelToggleLane/)
  assert.match(statusProps, /collapsed: collapsedLanes/)
  assert.match(statusProps, /onToggleCollapse: toggleLane/)
})

test('both boards take their move, fold and drop from the one laneActions helper', () => {
  assert.match(detail, /const \{ drop: dropCard, move: panelMove, toggle: panelToggleLane \} = laneActions\(panelByLane, collapsedLanes\)/)
  assert.match(status, /const \{ drop: dropCard, move, toggle: toggleLane \} = laneActions\(byLane, collapsedLanes\)/)
  // The panel's drop is its own drag state, then the shared rule.
  const at = detail.indexOf('const panelDrop =')
  const stop = detail.indexOf('const repoInfo =', at)

  assert.ok(at >= 0 && stop > at, 'panelDrop exists')

  const drop = detail.slice(at, stop)

  assert.match(drop, /setPanelDrag\(null\)/)
  assert.match(drop, /dropCard\(card, laneId\)/, 'the panel moves through the shared drop')
})

test('a drop on a folded rail unfolds the lane before it moves the card', () => {
  const helper = block('laneActions', 'function reconcileSessionLanes(')
  const unfold = helper.indexOf('laneCollapsed(laneId, collapsed, true)')
  const move = helper.indexOf('move(card, laneId)')

  assert.ok(unfold >= 0, 'the shared drop checks the fold state')
  assert.ok(move > unfold, 'the unfold happens before the move')
  assert.match(helper, /\$collapsed\.set\(next\)/)
  assert.match(helper, /writeCollapsed\(next\)/)
  assert.match(helper, /\[laneId\]: false/)
})
