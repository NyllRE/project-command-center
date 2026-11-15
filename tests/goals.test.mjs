// Proof for the active-goal line on every command-center surface.
//
// The pure half is sliced out of desktop/plugin.js and executed here, so the line model a card
// renders is the object the page really builds, not a hand-written fixture. The parts that only
// exist inside a React render (the hook, the components, the click guard) are asserted as source
// slices, and each such test says why.
//
// Run with:
//   node --test tests/goals.test.mjs

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

const goalsBlock = slice('// session-goals:begin', '// session-goals:end')
// The card builder resolves a lane and reads stamps, so the two pure blocks it depends on load
// here too, exactly as the session kanban suite does.
const laneBlock = slice('// session-lane:begin', '// session-lane:end')
const kanbanBlock = slice('// session-kanban:begin', '// session-kanban:end')

vm.runInThisContext(
  `${laneBlock}\n${kanbanBlock}\n${goalsBlock}\n${fn('toMs')}\n${fn('stampMs')}\n${fn('sessionStamp')}\n${fn('buildKanbanCard')}
;globalThis.__PCC_GOALS__ = {
    GOAL_IDS_LIMIT,
    goalCounts,
    goalGateState,
    goalModel,
    goalReadKey,
    goalTitleShort,
    buildKanbanCard
  }`,
  { filename: PLUGIN }
)
const G = globalThis.__PCC_GOALS__

// The payload GET /goals answers for an active goal. `subgoals_done` is 0 while the goal is active,
// because Hermes records completion for the goal, not per criterion; the fixture below raises it
// once to prove the model clamps and renders whatever the route sends.
const goalRow = (over = {}) => ({
  title: 'Fix the login redirect loop',
  status: 'active',
  turns_used: 4,
  max_turns: 20,
  subgoals: ['sign in as a fresh user', 'land on /dashboard'],
  subgoals_done: 0,
  gates: [{ command: 'npm test', attempts: 1, last_exit_code: 0 }],
  updated_at: 1_700_000_000,
  ...over
})

const LANES = [
  { id: 'unfiled', label: 'Unfiled', order: 0 },
  { id: 'backlog', label: 'Backlog', order: 1 },
  { id: 'active', label: 'Active', order: 2 },
  { id: 'review', label: 'Review', order: 3 },
  { id: 'blocked', label: 'Blocked', order: 4 },
  { id: 'done', label: 'Done', order: 5 },
  { id: 'canceled', label: 'Canceled', order: 6 }
]

const cardInput = over => ({
  session: { id: '20261113_212605_66fa65', preview: 'Fix the login redirect loop' },
  projectId: 'p1',
  lane: 'active',
  lanes: LANES,
  now: 1_700_000_100_000,
  ...over
})

test('an active goal becomes the card line model: title, turns, criteria', () => {
  const card = G.buildKanbanCard(cardInput({ goal: goalRow() }))

  assert.ok(card.goal, 'a card with an active goal carries a goal model')
  assert.equal(card.goal.title, 'Fix the login redirect loop')
  assert.equal(card.goal.short, 'Fix the login redirect loop')
  assert.equal(card.goal.turnsText, '4/20')
  assert.equal(card.goal.subgoals, 2)
  assert.equal(card.goal.subgoalsText, '0/2')
  assert.equal(G.goalCounts(card.goal), '4/20 · 0/2')
  assert.equal(card.goal.gateText, '1/1 gates passing')
  // The exact hover: the full title, the budget, the criteria and every gate, one per line.
  assert.equal(
    card.goal.tip,
    'Fix the login redirect loop\n4/20 turns\n0/2 criteria\n1/1 gates passing\npassed: npm test'
  )
})

test('subgoals_done counts, and is clamped to the criteria it is shown against', () => {
  const two = G.goalModel(goalRow({ subgoals_done: 2 }))

  assert.equal(two.subgoalsText, '2/2')
  assert.equal(G.goalCounts(two), '4/20 · 2/2')
  // A bad payload (more done than listed) never renders 7/2.
  const clamped = G.goalModel(goalRow({ subgoals_done: 7 }))
  assert.equal(clamped.subgoalsText, '2/2')
  assert.equal(clamped.done, 2)
  // No subgoals: the count half is the turn budget alone, never an empty "0/0".
  const none = G.goalModel(goalRow({ subgoals: [], subgoals_done: 0 }))
  assert.equal(none.subgoalsText, '')
  assert.equal(G.goalCounts(none), '4/20')
})

test('no goal renders nothing: null, an empty row and a blank title are all no line', () => {
  assert.equal(G.goalModel(null), null)
  assert.equal(G.goalModel(undefined), null)
  assert.equal(G.goalModel({}), null)
  assert.equal(G.goalModel(goalRow({ title: '   ' })), null)
  // The card's field is null, not undefined or a placeholder, and every other field still builds.
  const card = G.buildKanbanCard(cardInput())

  assert.equal(card.goal, null)
  assert.equal(card.title, 'Fix the login redirect loop')
})

test('long titles truncate on a word boundary and short ones are untouched', () => {
  const short = 'Ship the sidebar'
  const long = 'Make the session kanban show the active goal on every card and every row'
  const title = G.goalTitleShort(long)

  assert.equal(G.goalTitleShort(short), short)
  assert.ok(title.length <= 44, `truncated to ${title.length} chars`)
  assert.ok(title.endsWith('…'), 'one ellipsis marks the cut')
  assert.doesNotMatch(title, /\s…$/, 'no space is left before the ellipsis')
  assert.ok(long.startsWith(title.slice(0, -1)), 'the kept text is the original prefix')
  // The model carries both: the truncated form for the line, the full one for the hover.
  const model = G.goalModel(goalRow({ title: long }))

  assert.equal(model.short, title)
  assert.equal(model.title, long)
  assert.ok(model.tip.startsWith(long), 'the hover gives the whole title')
  // A single word longer than the budget still truncates instead of vanishing.
  const word = G.goalTitleShort('a'.repeat(80))
  assert.equal(word.length, 44)
  assert.ok(word.endsWith('…'))
})

test('gate state is read from the exit code, never guessed', () => {
  assert.equal(G.goalGateState({ last_exit_code: 0 }), 'passed')
  assert.equal(G.goalGateState({ last_exit_code: 1 }), 'failed')
  assert.equal(G.goalGateState({ last_exit_code: null }), 'pending')
  assert.equal(G.goalGateState({}), 'pending')
  const model = G.goalModel(goalRow({
    gates: [
      { command: 'npm test', last_exit_code: 0 },
      { command: 'npm run lint', last_exit_code: 2 },
      { command: 'make build', last_exit_code: null }
    ]
  }))

  assert.equal(model.gateText, '1/3 gates passing')
  assert.deepEqual(model.gates.map(gate => gate.state), ['passed', 'failed', 'pending'])
  assert.match(model.tip, /failed: npm run lint/)
  assert.match(model.tip, /not run: make build/)
})

test('one read key: de-duplicated, capped at 60, and order-insensitive', () => {
  const ids = ['b2', 'a1', 'a1', '', ' c3 ', null]
  assert.equal(G.goalReadKey(ids), 'a1,b2,c3')
  // The key is the SET of sessions. Typing in search or reordering the cards
  // changes their order and must not change the key, or every goal line blinks
  // off until the new read lands.
  assert.equal(G.goalReadKey(['a1', 'b2', 'c3']), G.goalReadKey(['c3', 'b2', 'a1']))
  assert.equal(G.goalReadKey(['s9', 's1']), G.goalReadKey(['s1', 's9']))

  // The cap keeps the caller's newest-first head (the 60 the backend answers
  // anyway), and the emitted list is sorted.
  const many = Array.from({ length: 90 }, (_, i) => `s${i}`)
  const parts = G.goalReadKey(many).split(',')

  assert.equal(G.GOAL_IDS_LIMIT, 60)
  assert.equal(parts.length, 60)
  assert.deepEqual([...parts].sort(), parts, 'the key is sorted')
  assert.deepEqual(parts, many.slice(0, 60).sort(), 'the cap keeps the newest 60 the view passed')
  assert.equal(G.goalReadKey([]), '')
})

// The keep-previous half lives in the hook, which needs a renderer and a query
// cache: a behaviour test would be testing react-query, not this plugin. The
// source slice pins the option name the hook passes.
test('the goals read keeps the previous answer while a new key loads', () => {
  const hook = fn('useSessionGoals')

  assert.match(hook, /placeholderData: previous => previous/, 'a new id set must not blank the goal lines')
})

test('the card renders the line under the meta, and the line never opens the session', () => {
  // A React component cannot run here, so the render is a source slice: the card must render
  // GoalMeta, and GoalMeta must carry the target glyph and the click guard the card's handler reads.
  const card = fn('KanbanCard')

  assert.match(card, /card\.goal \? jsx\(GoalMeta, \{ goal: card\.goal \}/, 'the card renders its goal line')
  assert.match(card, /data-pcc-noclick/, 'the card click handler still exempts the goal line')
  const goal = fn('GoalMeta')

  assert.match(goal, /name: 'target'/, 'a target-style codicon, not an invented mark')
  assert.match(goal, /'data-pcc-noclick': 'true'/, 'the line itself is exempt from the card click')
  assert.match(goal, /children: goal\.short/, 'the truncated title is what the line shows')
  assert.match(goal, /children: goalCounts\(goal\)/, 'the counts come from the one shared helper')
  assert.match(goal, /label: goal\.tip/, 'the hover carries the full text and the gate state')
  assert.doesNotMatch(goal, /onClick|onOpenSession|onOpen\(/, 'the goal line has no click of its own')
})

test('one batched read per view, never a gateway read per card', () => {
  // The whole point of the route: a per-card session.control.read is one gateway round trip per
  // session per poll. The renderer must not call one, and every view must use the same hook.
  assert.equal((source.match(/host\.request\(\s*['"]session\.control\.read/g) || []).length, 0,
    'no per-session gateway control read anywhere in the renderer')
  const hook = fn('useSessionGoals')

  assert.match(hook, /boardRest\(`\/goals\?session_ids=\$\{encodeURIComponent\(key\)\}`\)/)
  assert.match(hook, /refetchInterval: BOARD_MS/, 'the read rides the board poll')
  assert.match(hook, /payload\.ok !== true/, 'ok:false is a failure, not an empty answer')
  assert.equal((source.match(/function useSessionGoals\(/g) || []).length, 1)
  assert.equal((source.match(/useSessionGoals\(/g) || []).length, 4,
    'the definition plus one call per view (status, projects, project detail)')
})

test('a failed read leaves cards bare and says why once, in the view header', () => {
  assert.equal((source.match(/children: 'Goal status unavailable'/g) || []).length, 3,
    'one small note in each of the three view headers')
  assert.doesNotMatch(source, /Goal status unavailable.*notify|notify.*Goal status unavailable/,
    'no toast, so a failing poll cannot storm the user')
  // The note is the quiet style, not a banner and not per-card.
  assert.match(source, /goalNote: \{ fontSize: '0\.6875rem'/)
  // Rows and the table take the same info into their meta lines.
  assert.match(fn('DetailSessionRow'), /goal \? jsx\(GoalMeta, \{ goal \}/)
  assert.match(fn('SessionCard'), /goal \? jsx\(GoalMeta, \{ goal \}/)
  assert.match(fn('KanbanTable'), /card\.goal \? jsx\(GoalMeta, \{ goal: card\.goal \}\)/)
})

// Both row shapes must honour the marker the goal line carries. The kanban card
// has always tested where the click started; the RowButton rows (the Projects
// view card and the project page's session row) opened the session regardless, so
// clicking the goal line there was a second door into the session.
test('the goal line never opens its row, in either row shape', () => {
  const rowClickOpens = new Function(`${fn('rowClickOpens')}\nreturn rowClickOpens`)()
  const fake = matches => ({ target: { closest: selector => (matches.includes(selector) ? {} : null) } })

  assert.equal(rowClickOpens(fake(['[data-pcc-noclick]'])), false, 'a click on the goal line does not open')
  assert.equal(rowClickOpens(fake([])), true, 'a click anywhere else in the row does')
  assert.equal(rowClickOpens({}), true, 'a keyboard activation has no target to inspect')
  assert.equal(rowClickOpens(null), true)

  // The wiring, as a slice: a row open handler needs a React root, so the one
  // expression that consumes the predicate is what is pinned.
  for (const name of ['SessionCard', 'DetailSessionRow']) {
    assert.match(
      fn(name),
      /onClick: event => \{\s*if \(rowClickOpens\(event\)\) onOpen\(session\.id\)\s*\}/,
      `${name} refuses a click that started on the goal line`
    )
  }
})
