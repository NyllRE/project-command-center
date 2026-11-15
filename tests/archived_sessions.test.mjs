// Proof for the three board fixes that are about STATE, not layout: the running-session
// ring on a Status card, the drop target a drag is actually over, and the archived
// sessions the gateway will not list.
//
// There is no renderer harness in this repo, so the layout claims are pinned as source
// invariants that FAIL when the defect comes back. Each assertion here exists because
// the defect shipped once:
//   - the ring was on every Projects card and on no Status card;
//   - the column's border was inline, so `.pcc-kcol[data-drop="true"]` could never win
//     and the drop target was invisible while the folded rail beside it lit up;
//   - archived sessions were absent from every read the board had.
//
// Run with:
//   node --test tests/archived_sessions.test.mjs

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, '..')
const PLUGIN = join(ROOT, 'desktop', 'plugin.js')
const API = join(ROOT, 'dashboard', 'plugin_api.py')

const source = readFileSync(PLUGIN, 'utf8')
const api = readFileSync(API, 'utf8')

const cardSource = () => source.slice(source.indexOf('function KanbanCard('), source.indexOf('function KanbanColumn('))
const columnSource = () => source.slice(source.indexOf('function KanbanColumn('), source.indexOf('function KanbanTable('))

// --- the running ring ---------------------------------------------------------

test('the Status card draws the running-session ring, gated on RUNNING', () => {
  const card = cardSource()

  // Same widget the Projects lane mounts, and the same gate: a card whose session is
  // merely 'waiting' is not running and must not shimmer.
  assert.match(card, /RUNNING\.has\(state\) \? jsx\(ArcRing, \{ radius: '8px' \}\)/, 'the ring must be on the card')
  // `.arc-border` is absolutely positioned and reads `--arc-radius`: a host without
  // positioning puts the ring somewhere else on the page.
  assert.match(source, /  kcard: \{\n    \/\/ Host for the app's own[\s\S]{0,240}?position: 'relative',/, 'the card hosts the ring')
  assert.match(source, /  kcard: \{[\s\S]{0,400}?'--arc-radius': '8px'/, 'the card declares the ring radius')
})

// --- the drop target ----------------------------------------------------------

test('the kanban column has no inline border, so the drop state can win', () => {
  const start = source.indexOf('  kcol: {')
  const end = source.indexOf('\n  },', start)
  const block = source.slice(start, end)

  assert.ok(block.length > 0, 'S.kcol must exist')
  // Inline styles outrank any selector. A border or background here is the exact bug
  // that made the hovered column look identical to every other column.
  assert.doesNotMatch(block, /\n    border:/, 'no inline border on the column')
  assert.doesNotMatch(block, /\n    background:/, 'no inline background on the column')
  // ...and the paint has to exist in CSS, before the drop rule so the drop rule wins.
  const base = source.indexOf('.pcc-kcol:not(.pcc-krail) {')
  const drop = source.indexOf('.pcc-kcol[data-drop="true"] {')

  assert.ok(base >= 0, '.pcc-kcol paint must live in CSS')
  assert.ok(drop > base, 'the drop rule must come after the base rule, not before it')
})

test('the hovered lane says the drop will land there, in words', () => {
  const column = columnSource()

  assert.match(column, /Release to drop in \$\{lane\.label\}/, 'the hint must name the lane')
  assert.match(column, /over\n\s+\? jsx\(/, 'the hint is conditional on being the hovered lane')
  assert.match(source, /  kdropHint: \{/, 'the hint style must exist')
})

test('the hint has air above it and arrives instead of snapping in', () => {
  // The header's border sat directly on the hint, and an instant block of text
  // appearing under the cursor read as a glitch rather than as feedback.
  assert.match(source, /  kdropHint: \{[\s\S]{0,400}?margin: '8px 10px 8px'/, 'the hint needs space above it')
  assert.match(source, /@keyframes pcc-hint-in \{/, 'the hint needs its own animation')
  assert.match(source, /\.pcc-kdrop-hint \{ animation: pcc-hint-in/, 'the animation must be applied')
  assert.match(columnSource(), /className: 'pcc-kdrop-hint'/, 'the hint element must carry the class')
  // And reduced motion wins: a drag is not a place to insist on an animation.
  assert.match(source, /\.pcc-kdrop-hint, \.pcc-kcard\[data-dropped="true"\] \{ animation: none !important; \}/, 'reduced motion must disable it')
})

test('only the card that landed animates, and only once', () => {
  assert.match(cardSource(), /'data-dropped': dropped === card\.id \? 'true' : 'false'/, 'the flag is per card')
  assert.match(source, /@keyframes pcc-drop-in \{/, 'the settle animation must exist')
  assert.match(source, /\.pcc-kcard\[data-dropped="true"\] \{ animation: pcc-drop-in/, 'and be applied')
  // Set on drop, cleared on a timer: a card that keeps the flag replays the
  // animation on every re-render (each poll), which looks like a nervous tick.
  assert.match(source, /if \(card && card\.id\) setDroppedId\(String\(card\.id\)\)/, 'a drop records the card')
  assert.match(source, /setTimeout\(\(\) => setDroppedId\(''\), 400\)/, 'and the flag is cleared')
})

test('the column is still the drop target it was, not a click-through', () => {
  const column = columnSource()

  assert.match(column, /onDragOver: event => \{\n\s+event\.preventDefault\(\)/, 'preventDefault is what makes a drop legal')
  assert.match(column, /onDrop: event => \{\n\s+event\.preventDefault\(\)/, 'the drop handler must preventDefault too')
})

// --- archived sessions --------------------------------------------------------

test('the archived read is the plugin route, because session.list cannot answer', () => {
  // The gateway method lists with `archived_only=False` and exposes no flag for it,
  // so a plugin-side read is the only honest source.
  assert.match(api, /@router\.get\("\/sessions\/archived"\)/, 'the route must exist')
  assert.match(api, /archived_only=True/, 'it must ask the store for archived rows')
  assert.match(api, /session_count\(archived_only=True\)/, 'the total must be the real count, not the page size')
  assert.match(api, /"code": "sessions_unavailable"/, 'an unreadable store is a sentence, not a 500')
  // The frontend's cap has to match the route's, or every request 422s.
  const cap = Number((api.match(/ARCHIVED_MAX = (\d+)/) || [])[1])

  assert.ok(cap > 0, 'ARCHIVED_MAX must be declared')
  assert.match(source, new RegExp(`const ARCHIVED_LIMIT = ${cap}\\b`), 'the two caps must be the same number')
})

test('the switch is in both toolbars and both footers, and is disabled while broken', () => {
  const footers = [...source.matchAll(/jsx\(ArchivedFooter, \{/g)]
  const toggles = [...source.matchAll(/jsx\(ArchivedToggle, \{/g)]

  assert.equal(footers.length, 2, 'Status and Projects must both carry the count')
  assert.equal(toggles.length, 2, 'and both must offer the button a hand goes looking for')
  // A filter that can be turned on while its source is unreadable shows an empty
  // board; this one explains itself and stays off until the read answers.
  assert.match(source, /disabled: archived\.failed/, 'the toggle must refuse while the read is broken')
  assert.match(source, /Archived sessions could not be read\. Use Retry under the board\./, 'and say where to fix it')
  assert.match(source, /function setArchivedFilter\(/, 'both views write the filter through one function')
  // An archived card that looks like a live one is a trap: the rows say so.
  assert.match(source, /card\.archived\s*\n?\s*\? jsx\('span', \{ style: \{ \.\.\.S\.chip, color: 'var\(--ui-orange\)' \}, children: 'Archived' \}/, 'the kanban card must be marked')
  assert.match(source, /session\.archived\s*\n?\s*\? jsx\('span', \{ style: \{ \.\.\.S\.chip, color: 'var\(--ui-orange\)' \}, children: 'Archived' \}/, 'the project row must be marked')
})

test('the archived rows ride the real card builder, with the folders to place them', () => {
  // A hand-built card would drift from the six rules the builder owns; feeding it the
  // rows is what makes an archived card draggable and lane-correct like any other.
  assert.match(source, /kanbanCardsFrom\(archived\.rows, \{/, 'the archived rows go through the builder')
  assert.match(source, /kanbanCardsFrom\(archivedState\.rows, \{|archivedState\.rows\)/, 'the projects view reads the same rows')
  // The cwd fallback is the only thing that names a project for a session in no tree.
  assert.match(source, /session\.cwd && Array\.isArray\(source\.folders\) \? projectForPath\(source\.folders, session\.cwd\)/, 'the builder needs the folders')
})

test('the archived filter survives a reload like every other stored choice', () => {
  assert.match(source, /\$showArchived\.set\(readShowArchived\(\)\)/, 'restoreStoredState must seed the filter')
  assert.match(source, /const ARCHIVED_KEY = 'kanbanShowArchived\.v1'/, 'the key is versioned like the others')
})

test('a failed archived read is not zero archived sessions', () => {
  // "0 archived" would read as "nothing is hidden" and quietly close the question.
  assert.match(source, /failed: Boolean\(query\.isFetched && !payload\)/, 'the hook must expose the failure')
  assert.match(source, /Archived sessions could not be read/, 'the footer must say it')
  assert.match(source, /archived\.query\.refetch\(\)/, 'and the same control must retry')
})

test('the code this pass added carries no em dash', () => {
  const column = columnSource()
  const added = column.slice(column.indexOf('Release now and it lands HERE'), column.indexOf('onDragLeave'))

  assert.doesNotMatch(cardSource(), /\u2014/)
  assert.doesNotMatch(added, /\u2014/)
})
