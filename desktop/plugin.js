/**
 * Project Command Center.
 * A titlebar button, a board page (one horizontally scrolling column per
 * Hermes project with its recent sessions, and a live sessions strip below)
 * and a full project detail page at /project-command-center/project.
 * Everything is read from the gateway (projects.tree, projects.project_sessions,
 * session.active_list) plus the app's own busy atom. Project status is the only
 * state we own, and it lives behind readStatuses/writeStatus: ctx.storage is the
 * offline mirror, the backend's /board is the shared copy.
 *
 * Layout uses inline styles and one scoped <style> block with theme variables,
 * because a runtime plugin cannot rely on the app's compiled utility classes.
 */

import {
  Badge,
  Blobatar,
  Button,
  Codicon,
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  EmptyState,
  ErrorState,
  MessageTextContent,
  PALETTE_AREA,
  Popover,
  PopoverContent,
  PopoverTrigger,
  ROUTES_AREA,
  RowButton,
  SearchField,
  SegmentedControl,
  Separator,
  SIDEBAR_NAV_AREA,
  Skeleton,
  StatusDot,
  Textarea,
  Tip,
  atom,
  fmtDateTime,
  host,
  queryClient,
  relativeTime,
  useMutation,
  useQuery,
  useValue,
  writeClipboardText
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'project-command-center'
const ROUTE = '/project-command-center'
// The SDK routes carry no params, so the detail page is its own path and reads
// the selected project id out of an atom persisted in ctx.storage.
const PROJECT_ROUTE = `${ROUTE}/project`
const QUERY_ROOT = 'project-command-center'
const TREE_MS = 15000
const LIVE_MS = 5000
const STATUS_KEY = 'projectStatus.v1'
const SORT_KEY = 'projectSort.v1'
const OPEN_KEY = 'openProject.v1'
const BOARD_CARD_LIMIT = 5
const COLUMN_WIDTH = 340
const COLUMN_GAP = 12
// /board is the backend copy of the status map plus the session→issue/PR links.
// The poll is slow on purpose: writes are optimistic and an event invalidates it.
const BOARD_MS = 15000
const BOARD_SYNC_DEBOUNCE_MS = 900
// A status we just posted wins over a poll that started before it landed.
const BOARD_WRITE_GRACE_MS = 8000
// The draft is a set of entry points, not a summary: where the issue lives and
// one line of context, small enough that the agent reads the issue itself.
// The composer only answers once the new chat has painted, so the draft is
// written on this bounded retry schedule instead of once.
const DRAFT_RETRY_MS = [120, 300, 600, 1000, 2000]

const STATUSES = [
  { id: 'backlog', label: 'Backlog', variant: 'muted' },
  { id: 'active', label: 'Active', variant: 'default' },
  { id: 'review', label: 'Review', variant: 'warn' },
  { id: 'blocked', label: 'Blocked', variant: 'destructive' },
  { id: 'done', label: 'Done', variant: 'success' }
]
const DEFAULT_STATUS = 'active'

// Order matters: 'status' sorts by this list's index, so backlog sits left and
// done sits right, with the unfinished middle in between.
const SORTS = [
  { id: 'recent', label: 'Last active', icon: 'history' },
  { id: 'name', label: 'Name', icon: 'symbol-class' },
  { id: 'sessions', label: 'Most sessions', icon: 'comment-discussion' },
  { id: 'tokens', label: 'Most tokens', icon: 'pulse' },
  { id: 'status', label: 'Status', icon: 'circle-outline' }
]
const DEFAULT_SORT = 'recent'
const STATUS_RANK = { backlog: 0, active: 1, review: 2, blocked: 3, done: 4 }
const STATUS_BY_ID = Object.fromEntries(STATUSES.map(s => [s.id, s]))

const LIVE_LABELS = {
  idle: 'Idle',
  starting: 'Starting',
  waiting: 'Waiting',
  working: 'Working',
  streaming: 'Streaming',
  resuming: 'Resuming'
}

// A turn is running in these states; `waiting` is an open turn that wants the
// user (amber dot, no arc) and `idle` paints nothing at all.
const RUNNING = new Set(['working', 'streaming', 'starting', 'resuming'])

function liveTone(status) {
  if (RUNNING.has(status)) return 'good'
  if (status === 'waiting') return 'warn'

  return 'muted'
}

// The gateway poll lags the app's own busy flag by up to one tick, and a session
// running in another window only ever shows up in the poll. So busy wins when it
// says "mid-turn" and never overrides a running status or `waiting` with idle.
//
// Neither source is enough on its own: a turn that resumed after an approval can
// report `idle` in the poll while it streams, and the busy flag drops the moment
// a turn parks on a tool. So the third input is the gateway event stream itself:
// any event for that runtime id inside the watchdog window means the session is
// producing right now.
function liveState(item, busy, recentlyActive) {
  const status = (item && item.status) || 'idle'

  if (status === 'waiting') return status
  if (RUNNING.has(status)) return status
  if (busy || recentlyActive) return 'working'

  return 'idle'
}

// ── activity watchdog ────────────────────────────────────────────────────────
// Keyed by RUNTIME id (the envelope's session_id), because that is what the
// event stream carries; the hook maps it to stored ids once active_list answers.
// Writes are throttled per session so a token stream cannot re-render the board.

const ACTIVITY_TTL_MS = 90000
const ACTIVITY_WRITE_MS = 2000
const ACTIVITY_TYPES = new Set([
  'message.start',
  'message.delta',
  'message.interim',
  'reasoning.delta',
  'reasoning.available',
  'thinking.delta',
  'tool.start',
  'tool.complete',
  'tool.generating',
  'tool.progress',
  'status.update',
  'session.usage',
  'agent.terminal.output'
])

const $activity = atom({})
// Bumped on a timer so the TTL can expire without waiting for another event.
const $activityClock = atom(0)
const lastActivityWrite = new Map()

function noteActivity(runtimeId, at) {
  if (!runtimeId) return
  const now = Date.now()
  const previous = lastActivityWrite.get(runtimeId) || 0

  if (now - previous < ACTIVITY_WRITE_MS) return
  lastActivityWrite.set(runtimeId, now)
  $activity.set({ ...$activity.get(), [runtimeId]: at })
}

function clearActivity(runtimeId) {
  if (!runtimeId) return
  const current = $activity.get()

  if (!(runtimeId in current)) return
  lastActivityWrite.delete(runtimeId)
  const next = { ...current }

  delete next[runtimeId]
  $activity.set(next)
}

// ── status storage: one optimistic copy, one server copy ─────────────────────
// The backend owns the status map (/board, POST /board/status, /board/seed).
// ctx.storage is the offline mirror and the restart copy: every local write lands
// there first, every applied server value is written back, and a failing read
// leaves the storage copy in charge exactly as it was.

let pluginCtx = null
// ctx timers die with the plugin, but a composer verb or a REST call can settle
// after unload; a promise-chained ctx.setTimeout would then write into a
// lifetime set that is already retired. Scheduling goes through one gate, and
// register() arms it again for a reload.
let pluginDisposed = false
const $statuses = atom({})
// True when the last /board read answered with the board shape.
const $boardLive = atom(false)
// project id → { at, status } for the last status this client posted.
const statusPostedAt = new Map()

function readStatuses() {
  const raw = pluginCtx ? pluginCtx.storage.get(STATUS_KEY, {}) : {}
  const out = {}

  if (raw && typeof raw === 'object') {
    for (const [id, status] of Object.entries(raw)) {
      if (STATUS_BY_ID[status]) out[id] = status
    }
  }

  return out
}

// ctx.rest rejects when the gateway has no router for this plugin, which is the
// "backend not enabled yet" signal. Keeping it a rejected promise (rather than a
// throw at call time) means every caller can branch the same way ghRest does.
function boardRest(path, opts) {
  if (!pluginCtx || typeof pluginCtx.rest !== 'function') {
    return Promise.reject(new Error('This Desktop build has no plugin REST door.'))
  }

  return pluginCtx.rest(path, opts)
}

// The only door to a ctx timer after registration: a plugin that has been
// disposed (or not registered yet) drops the callback instead of scheduling
// into a retired lifetime.
function schedule(fn, ms) {
  if (pluginDisposed || !pluginCtx) return null

  return pluginCtx.setTimeout(fn, ms)
}

function saveStatuses(next) {
  if (!pluginDisposed && pluginCtx) pluginCtx.storage.set(STATUS_KEY, next)
}

function writeStatus(projectId, status) {
  if (!STATUS_BY_ID[status]) return
  const next = { ...$statuses.get(), [projectId]: status }

  $statuses.set(next)
  saveStatuses(next)
  // Optimistic on the server too. A 404 (old process) is swallowed: the storage
  // copy above already carries the change, so the picker never blocks on it.
  statusPostedAt.set(projectId, { at: Date.now(), status })
  boardRest('/board/status', { method: 'POST', body: { project_id: projectId, status } }).catch(() => {})
}

// A payload that is not the board shape (a 404 body, an older process) yields
// null, which every consumer reads as "no server statuses, use storage".
function boardStatusMap(payload) {
  const rows = payload && payload.statuses

  if (!rows || typeof rows !== 'object' || Array.isArray(rows)) return null
  const out = {}

  for (const [id, row] of Object.entries(rows)) {
    const value = typeof row === 'string' ? row : row && row.status

    if (STATUS_BY_ID[value]) out[id] = value
  }

  return out
}

// One read feeds the status picker, the board chips and the project sync. Both
// pages mount it; the queryKey is shared, so it stays one request.
let boardSeedTried = false

function useBoard() {
  const query = useQuery({
    queryFn: () => boardRest('/board'),
    queryKey: [QUERY_ROOT, 'board'],
    refetchInterval: BOARD_MS,
    retry: false,
    staleTime: 8000
  })

  const statuses = useMemo(() => boardStatusMap(query.data), [query.data])
  const data = statuses ? query.data : null
  const links = useMemo(() => (data && Array.isArray(data.links) ? data.links : []), [data])

  useEffect(() => {
    $boardLive.set(Boolean(data))
  }, [data])

  // The server wins, except for a project changed moments ago: the POST may
  // still be in flight, and reverting a picker under the cursor is worse than a
  // tick of stale data. react-query's structural sharing hands back the SAME
  // data object across identical polls, so the effect also depends on
  // dataUpdatedAt: a value skipped inside the grace window is re-examined on
  // every later read instead of waiting for some other board field to change.
  useEffect(() => {
    if (!statuses) return

    const now = Date.now()
    const next = { ...$statuses.get() }
    let changed = false

    for (const [id, value] of Object.entries(statuses)) {
      const posted = statusPostedAt.get(id)

      if (posted) {
        // The echo of what we posted ends the grace window, so a newer server
        // value for this project is applied the moment it lands.
        if (posted.status === value || now - posted.at >= BOARD_WRITE_GRACE_MS) statusPostedAt.delete(id)
        else continue
      }

      if (next[id] !== value) {
        next[id] = value
        changed = true
      }
    }

    if (changed) {
      $statuses.set(next)
      // Server truth is this machine's restart copy too, so a relaunch or an
      // offline read shows what the board showed.
      saveStatuses(next)
    }
  }, [statuses, query.dataUpdatedAt])

  // First live read against a fresh backend: it has no statuses but this machine
  // has some, so hand them over once. Once the server answers with ANY row it is
  // authoritative, and a project only this machine knows goes over one
  // POST /board/status at a time (there is no bulk seed for a non-empty map). A
  // failed seed clears the flag and the next board tick tries again.
  useEffect(() => {
    if (!statuses || boardSeedTried) return

    const local = readStatuses()

    boardSeedTried = true

    if (Object.keys(statuses).length === 0) {
      if (Object.keys(local).length === 0) return

      boardRest('/board/seed', { method: 'POST', body: { statuses: local } }).catch(() => {
        boardSeedTried = false
      })

      return
    }

    for (const [id, status] of Object.entries(local)) {
      if (statuses[id]) continue

      boardRest('/board/status', { method: 'POST', body: { project_id: id, status } }).catch(() => {})
    }
  }, [statuses, query.dataUpdatedAt])

  return { data, links, live: Boolean(data), query }
}

// project id → the issue and PR numbers a session is linked to, newest link last.
function linksByProject(links) {
  const map = new Map()

  for (const link of links || []) {
    if (!link || !link.project_id) continue

    const row = map.get(link.project_id) || { issues: [], prs: [] }
    const issue = Number(link.issue)
    const pr = Number(link.pr)

    if (Number.isFinite(issue) && issue > 0 && !row.issues.includes(issue)) row.issues.push(issue)
    if (Number.isFinite(pr) && pr > 0 && !row.prs.includes(pr)) row.prs.push(pr)
    map.set(link.project_id, row)
  }

  return map
}

// The tree changes rarely, so a debounce absorbs a burst of 15s polls. The
// signature is the payload itself: an identical id/label/path set posts once, and
// a failed post retries when the board comes back ($boardLive flips or the tree
// changes).
let projectSyncSig = ''

function projectSyncRows(projects) {
  return (projects || [])
    .filter(p => p && p.id)
    .map(p => ({ id: String(p.id), label: String(p.label || ''), paths: folderList(p) }))
    .sort((a, b) => a.id.localeCompare(b.id))
}

function useProjectSync(projects) {
  const live = useValue($boardLive)
  const rows = useMemo(() => projectSyncRows(projects), [projects])
  const sig = useMemo(() => JSON.stringify(rows), [rows])

  useEffect(() => {
    if (rows.length === 0 || projectSyncSig === sig) return undefined

    const timer = schedule(() => {
      boardRest('/board/projects', { method: 'POST', body: { projects: rows } })
        .then(() => {
          projectSyncSig = sig
        })
        .catch(() => {})
    }, BOARD_SYNC_DEBOUNCE_MS)

    return () => {
      if (typeof timer === 'function') timer()
    }
    // `rows` travels in the payload but not in the deps: `sig` is its
    // serialization, so a tree poll with the same set must not reset the timer.
  }, [sig, live])
}

// ── sort storage ─────────────────────────────────────────────────────────────

const $sort = atom(DEFAULT_SORT)

function readSort() {
  const raw = pluginCtx ? pluginCtx.storage.get(SORT_KEY, DEFAULT_SORT) : DEFAULT_SORT

  return SORTS.some(s => s.id === raw) ? raw : DEFAULT_SORT
}

function writeSort(sortId) {
  if (!SORTS.some(s => s.id === sortId)) return
  $sort.set(sortId)
  if (pluginCtx) pluginCtx.storage.set(SORT_KEY, sortId)
}

// ── open project storage ─────────────────────────────────────────────────────
// Same discipline as status/sort: the atom is the render path, storage is the
// durable copy. The detail route has no params, so it reads this on mount.

const $openProject = atom('')

function readOpenProject() {
  const raw = pluginCtx ? pluginCtx.storage.get(OPEN_KEY, '') : ''

  return typeof raw === 'string' ? raw : ''
}

function openProject(projectId) {
  if (!projectId) return
  $openProject.set(projectId)
  if (pluginCtx) pluginCtx.storage.set(OPEN_KEY, projectId)
  host.navigate(PROJECT_ROUTE)
}

// ── formatting helpers ───────────────────────────────────────────────────────

// Gateway timestamps are epoch seconds; tolerate milliseconds too.
function toMs(value) {
  if (!value || !Number.isFinite(Number(value))) return 0
  const n = Number(value)

  return n < 1e12 ? n * 1000 : n
}

function ago(value) {
  const ms = toMs(value)

  return ms ? relativeTime(ms) : ''
}

// `state.db` has no `last_active` column (started_at, ended_at,
// last_activity_at), and the gateway derives last_active from the newest of them
// with started_at as the floor. So a finished session arrives reading as its
// creation time; take the newest stamp the row actually carries.
function sessionStamp(s) {
  if (!s) return 0

  return Math.max(toMs(s.last_active), toMs(s.ended_at), toMs(s.started_at))
}

function stampTip(ms) {
  if (!ms) return 'No activity yet'

  return `Last updated ${fmtDateTime.format(new Date(ms))}`
}

function formatTokens(n) {
  if (!n || n < 0) return '0'
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, '')}m`
  if (n >= 1e4) return `${Math.round(n / 1e3)}k`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1).replace(/\.0$/, '')}k`

  return String(Math.round(n))
}

function formatCost(n) {
  if (!n || n <= 0) return ''
  if (n >= 10) return `$${n.toFixed(0)}`
  if (n >= 0.1) return `$${n.toFixed(2)}`

  return `$${n.toFixed(3)}`
}

// Keeps the head and the tail of a path, since the folder name is the part that matters.
function middleTruncate(path, max = 40) {
  const p = String(path || '').replace(/^\/home\/[^/]+/, '~').replace(/^\/Users\/[^/]+/, '~')

  if (p.length <= max) return p
  const tail = Math.ceil((max - 1) * 0.62)
  const head = max - 1 - tail

  return `${p.slice(0, head)}\u2026${p.slice(p.length - tail)}`
}

function sessionTitle(s) {
  return String(s.title || '').trim() || String(s.preview || '').trim() || 'Untitled session'
}

function shortModel(model) {
  return String(model || '').split('/').pop()
}

// `lastActive` on the tree node is not always populated, so take the newest
// session stamp we can see in the lanes and previews as well. A project with no
// sessions lands at 0 and sorts to the end of the board, which is the point.
function projectRecency(project) {
  let newest = toMs(project.lastActive)

  for (const repo of project.repos || []) {
    for (const lane of repo.groups || []) {
      for (const s of lane.sessions || []) newest = Math.max(newest, sessionStamp(s))
    }
  }

  for (const s of project.previewSessions || []) newest = Math.max(newest, sessionStamp(s))

  return newest
}

function sortProjects(list, sortId, statuses) {
  const byName = (a, b) => String(a.label || '').localeCompare(String(b.label || ''), undefined, { numeric: true })
  const ranked = [...list]

  if (sortId === 'name') {
    ranked.sort(byName)
  } else if (sortId === 'sessions') {
    ranked.sort((a, b) => (b.sessionCount || 0) - (a.sessionCount || 0) || byName(a, b))
  } else if (sortId === 'tokens') {
    ranked.sort((a, b) => (b.totalTokens || 0) - (a.totalTokens || 0) || byName(a, b))
  } else if (sortId === 'status') {
    const rank = p => STATUS_RANK[statuses[p.id] || DEFAULT_STATUS] ?? 99

    ranked.sort((a, b) => rank(a) - rank(b) || projectRecency(b) - projectRecency(a) || byName(a, b))
  } else {
    ranked.sort((a, b) => projectRecency(b) - projectRecency(a) || byName(a, b))
  }

  return ranked
}

function primaryPath(project) {
  return project.path || (project.repos && project.repos[0] && project.repos[0].path) || ''
}

function validColor(color) {
  if (!color || typeof color !== 'string') return null
  if (typeof CSS !== 'undefined' && CSS.supports && !CSS.supports('color', color)) return null

  return color
}

function prefersReducedMotion() {
  return typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

// Every project RPC is profile-scoped; under a multiplexed gateway the request
// must name the profile or it reads the wrong store.
function profileParam() {
  try {
    const name = host.state.profile.get()

    return name ? { profile: name } : {}
  } catch {
    return {}
  }
}

// The app's own session tab: an existing session (a board card, a live strip
// row) opens as a tab, and a failure is reported instead of vanishing.
function openSession(id) {
  Promise.resolve(host.openSession(id, { intent: 'tab' })).catch(err => {
    host.notify({ kind: 'error', message: `Could not open session: ${err && err.message ? err.message : err}` })
  })
}

// ── styles ───────────────────────────────────────────────────────────────────
const CSS_TEXT = `
.pcc-col { animation: pcc-in 260ms cubic-bezier(0.22, 1, 0.36, 1) both; animation-delay: calc(min(var(--pcc-i, 0), 8) * 28ms); }
@keyframes pcc-in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) {
  .pcc-col { animation: none !important; }
}
.pcc-scroller { scrollbar-width: thin; scroll-snap-type: x proximity; }
.pcc-col { scroll-snap-align: start; }
.pcc-card { transition: background-color 120ms ease, border-color 120ms ease; }
.pcc-card:hover, .pcc-card:focus-visible { background: var(--chrome-action-hover); border-color: var(--ui-stroke-secondary); outline: none; }
.pcc-link { transition: color 120ms ease; }
.pcc-link:hover, .pcc-link:focus-visible { color: var(--ui-accent); outline: none; }
.pcc-more:hover, .pcc-more:focus-visible { color: var(--ui-text-primary); background: var(--chrome-action-hover); outline: none; }
.pcc-live-row:hover, .pcc-live-row:focus-visible { background: var(--chrome-action-hover); outline: none; }
.pcc-status-btn:focus-visible { outline: 1px solid var(--ui-accent); outline-offset: 2px; }
.pcc-crumb { transition: color 120ms ease; }
.pcc-crumb:hover, .pcc-crumb:focus-visible { color: var(--ui-text-primary); outline: none; }
.pcc-detail-row { transition: background-color 120ms ease; }
.pcc-detail-row:hover, .pcc-detail-row:focus-visible { background: var(--chrome-action-hover); outline: none; }
.pcc-gh-row + .pcc-gh-row { border-top: 1px solid color-mix(in srgb, var(--ui-stroke-secondary) 30%, transparent); }
.pcc-gh-row:hover { background: color-mix(in srgb, var(--chrome-action-hover) 70%, transparent); }
.pcc-gh-title:focus-visible { outline: 1px solid var(--ui-accent); outline-offset: 2px; border-radius: 3px; }
.pcc-gh-chip { transition: color 120ms ease, background-color 120ms ease; }
.pcc-gh-chip:hover, .pcc-gh-chip:focus-visible { color: var(--ui-text-primary); background: var(--chrome-action-hover); outline: none; }
.pcc-gh-ref { transition: color 120ms ease, background-color 120ms ease; }
.pcc-gh-ref:hover, .pcc-gh-ref:focus-visible { color: var(--ui-text-primary); background: color-mix(in srgb, var(--ui-stroke-secondary) 65%, transparent); outline: 1px solid var(--ui-accent); outline-offset: 1px; }
.pcc-gh-back:focus-visible { outline: 1px solid var(--ui-accent); outline-offset: 2px; }
.pcc-gh-sub-btn:hover, .pcc-gh-sub-btn:focus-visible { background: var(--chrome-action-hover); outline: none; }
.pcc-gh-linkbtn:hover, .pcc-gh-linkbtn:focus-visible { color: var(--ui-text-secondary); outline: none; }
@keyframes pcc-gh-flash { from { background: color-mix(in srgb, var(--ui-accent) 14%, transparent); } to { background: transparent; } }
.pcc-gh-focus { animation: pcc-gh-flash 1400ms ease-out 1 both; }
@media (prefers-reduced-motion: reduce) { .pcc-gh-focus { animation: none !important; } }
.pcc-gh-error > div:first-child { gap: 6px; }
`

const S = {
  page: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, minWidth: 0, overflow: 'hidden' },
  header: { display: 'flex', alignItems: 'center', gap: 12, padding: '14px 20px 10px', flex: 'none', flexWrap: 'wrap' },
  title: { margin: 0, fontSize: '1rem', fontWeight: 600, color: 'var(--ui-text-primary)', lineHeight: 1.2 },
  count: { fontSize: '0.75rem', color: 'var(--ui-text-quaternary)', fontVariantNumeric: 'tabular-nums' },
  grow: { flex: '1 1 0', minWidth: 0 },
  searchWrap: { width: 300, maxWidth: '100%', flex: '0 1 300px' },
  boardWrap: { position: 'relative', flex: '1 1 0', minHeight: 0, display: 'flex' },
  scroller: {
    display: 'flex',
    gap: COLUMN_GAP,
    alignItems: 'stretch',
    overflowX: 'auto',
    overflowY: 'hidden',
    padding: '4px 20px 12px',
    width: '100%',
    height: '100%',
    boxSizing: 'border-box'
  },
  column: {
    position: 'relative',
    display: 'flex',
    flexDirection: 'column',
    width: COLUMN_WIDTH,
    flex: `0 0 ${COLUMN_WIDTH}px`,
    minHeight: 0,
    borderRadius: 12,
    border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 70%, transparent)',
    background: 'color-mix(in srgb, var(--ui-bg-elevated) 55%, transparent)',
    overflow: 'hidden'
  },
  colHeader: {
    position: 'sticky',
    top: 0,
    zIndex: 1,
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
    padding: '12px 14px 10px',
    borderBottom: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 70%, transparent)',
    background: 'color-mix(in srgb, var(--ui-bg-elevated) 90%, transparent)',
    flex: 'none'
  },
  row: { display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 },
  nameBtn: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    minWidth: 0,
    flex: '1 1 0',
    padding: 0,
    border: 0,
    background: 'transparent',
    cursor: 'pointer',
    textAlign: 'left',
    color: 'var(--ui-text-primary)'
  },
  name: { fontSize: '0.875rem', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 },
  path: { fontSize: '0.6875rem', color: 'var(--ui-text-quaternary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0, display: 'block' },
  metrics: { display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.6875rem', color: 'var(--ui-text-secondary)', minWidth: 0, flexWrap: 'wrap' },
  metricMono: { fontVariantNumeric: 'tabular-nums', color: 'var(--ui-text-secondary)' },
  // The identifier row under the path: icon + number pairs, no words. Same size and
  // muted tone as the chips; the pair is borderless so a column header stays sleek.
  idRow: { display: 'flex', alignItems: 'center', gap: 8, marginTop: 3, minWidth: 0, flexWrap: 'wrap' },
  idPair: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 3,
    fontSize: '0.6875rem',
    lineHeight: 1.4,
    color: 'var(--ui-text-secondary)',
    fontVariantNumeric: 'tabular-nums',
    whiteSpace: 'nowrap'
  },
  body: { display: 'flex', flexDirection: 'column', gap: 6, padding: 8 },
  card: {
    position: 'relative',
    overflow: 'hidden',
    '--arc-radius': '8px',
    display: 'flex',
    flexDirection: 'column',
    gap: 5,
    width: '100%',
    padding: '8px 10px',
    borderRadius: 8,
    border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 55%, transparent)',
    background: 'transparent',
    textAlign: 'left',
    cursor: 'pointer',
    color: 'var(--ui-text-primary)',
    minWidth: 0
  },
  cardTitle: {
    fontSize: '0.75rem',
    lineHeight: 1.35,
    fontWeight: 500,
    display: '-webkit-box',
    WebkitLineClamp: 2,
    WebkitBoxOrient: 'vertical',
    overflow: 'hidden',
    wordBreak: 'break-word',
    minWidth: 0
  },
  cardMeta: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: '0.6875rem', color: 'var(--ui-text-quaternary)', minWidth: 0 },
  chip: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 3,
    maxWidth: 150,
    padding: '1px 6px',
    borderRadius: 999,
    border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 70%, transparent)',
    color: 'var(--ui-text-secondary)',
    fontSize: '0.6875rem',
    lineHeight: 1.4,
    minWidth: 0
  },
  ellipsis: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 },
  more: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 4,
    padding: '5px 8px',
    border: 0,
    borderRadius: 8,
    background: 'transparent',
    color: 'var(--ui-text-tertiary)',
    fontSize: '0.6875rem',
    cursor: 'pointer'
  },
  quiet: { fontSize: '0.75rem', color: 'var(--ui-text-quaternary)', padding: '10px 8px', textAlign: 'center' },
  strip: { flex: 'none', padding: '0 20px 14px', display: 'flex', flexDirection: 'column', gap: 6 },
  stripHead: { display: 'flex', alignItems: 'center', gap: 8, paddingTop: 10 },
  stripTitle: { fontSize: '0.75rem', fontWeight: 600, color: 'var(--ui-text-secondary)', letterSpacing: '0.02em' },
  stripList: { display: 'flex', flexDirection: 'column', maxHeight: 156, overflowY: 'auto' },
  liveRow: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    width: '100%',
    padding: '5px 8px',
    border: 0,
    borderRadius: 6,
    background: 'transparent',
    textAlign: 'left',
    cursor: 'pointer',
    color: 'var(--ui-text-primary)',
    fontSize: '0.75rem',
    minWidth: 0
  },
  edge: { position: 'absolute', top: 4, bottom: 12, width: 28, pointerEvents: 'none', zIndex: 2 },
  edgeBtn: { position: 'absolute', top: '50%', transform: 'translateY(-50%)', zIndex: 3, borderRadius: 999 },
  crumbs: {
    display: 'flex',
    alignItems: 'center',
    gap: 4,
    padding: '12px 20px 0',
    flex: 'none',
    fontSize: '0.75rem',
    color: 'var(--ui-text-quaternary)'
  },
  crumbBtn: {
    border: 0,
    padding: 0,
    background: 'transparent',
    color: 'var(--ui-text-tertiary)',
    fontSize: 'inherit',
    cursor: 'pointer'
  },
  detailHead: { display: 'flex', alignItems: 'center', gap: 10, padding: '10px 20px 12px', flex: 'none', flexWrap: 'wrap' },
  detailTitle: {
    margin: 0,
    fontSize: '1.25rem',
    fontWeight: 600,
    lineHeight: 1.15,
    color: 'var(--ui-text-primary)',
    minWidth: 0,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap'
  },
  detailBody: {
    flex: '1 1 0',
    minHeight: 0,
    overflowY: 'auto',
    overflowX: 'hidden',
    scrollbarWidth: 'thin',
    padding: '0 20px 24px',
    display: 'flex',
    flexDirection: 'column',
    gap: 16
  },
  stats: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(118px, 1fr))',
    gap: 1,
    borderRadius: 10,
    overflow: 'hidden',
    border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 70%, transparent)',
    background: 'color-mix(in srgb, var(--ui-stroke-secondary) 70%, transparent)'
  },
  statCell: {
    display: 'flex',
    flexDirection: 'column',
    gap: 3,
    minWidth: 0,
    padding: '10px 14px',
    background: 'var(--ui-bg-elevated)'
  },
  lane: {
    display: 'flex',
    flexDirection: 'column',
    minWidth: 0,
    borderRadius: 10,
    border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 55%, transparent)',
    overflow: 'hidden'
  },
  laneHead: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    minWidth: 0,
    padding: '8px 12px',
    background: 'color-mix(in srgb, var(--ui-bg-elevated) 70%, transparent)',
    fontSize: '0.75rem',
    color: 'var(--ui-text-secondary)'
  },
  laneRow: {
    position: 'relative',
    overflow: 'hidden',
    '--arc-radius': '0',
    display: 'flex',
    flexDirection: 'column',
    gap: 3,
    width: '100%',
    minWidth: 0,
    padding: '8px 12px',
    border: 0,
    borderTop: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 35%, transparent)',
    background: 'transparent',
    textAlign: 'left',
    color: 'var(--ui-text-primary)',
    cursor: 'pointer'
  },
  laneRowTitle: { fontSize: '0.8125rem', lineHeight: 1.35, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 },
  laneRowMeta: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    flexWrap: 'wrap',
    fontSize: '0.6875rem',
    color: 'var(--ui-text-quaternary)',
    minWidth: 0
  },
  laneQuiet: { padding: '8px 12px', fontSize: '0.75rem', color: 'var(--ui-text-quaternary)' },
  // The GitHub surface: one accent at most per row, separators between rows and
  // nothing else. Everything here is small type over tabular numerals.
  ghSection: {
    display: 'flex',
    flexDirection: 'column',
    gap: 0,
    minWidth: 0,
    paddingTop: 12,
    borderTop: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 45%, transparent)'
  },
  ghHead: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', minWidth: 0 },
  ghToolbar: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', padding: '9px 0 2px', minWidth: 0 },
  ghTotals: {
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    flexWrap: 'wrap',
    fontSize: '0.6875rem',
    color: 'var(--ui-text-quaternary)',
    fontVariantNumeric: 'tabular-nums'
  },
  ghTotal: { display: 'inline-flex', alignItems: 'baseline', gap: 4, whiteSpace: 'nowrap' },
  ghTotalValue: {
    fontSize: '0.75rem',
    fontWeight: 600,
    color: 'var(--ui-text-secondary)',
    fontVariantNumeric: 'tabular-nums'
  },
  ghList: { display: 'flex', flexDirection: 'column', minWidth: 0, paddingTop: 4 },
  ghRow: {
    position: 'relative',
    overflow: 'hidden',
    '--arc-radius': '0',
    display: 'flex',
    flexDirection: 'column',
    gap: 3,
    minWidth: 0,
    padding: '7px 4px 7px 4px'
  },
  ghLine: { display: 'flex', alignItems: 'flex-start', gap: 8, minWidth: 0 },
  ghTitleBtn: {
    display: 'flex',
    alignItems: 'flex-start',
    gap: 7,
    flex: '1 1 0',
    minWidth: 0,
    padding: 0,
    border: 0,
    borderRadius: 3,
    background: 'transparent',
    color: 'var(--ui-text-primary)',
    textAlign: 'left',
    cursor: 'pointer',
    font: 'inherit'
  },
  ghTitle: {
    flex: '1 1 auto',
    minWidth: 0,
    fontSize: '0.75rem',
    lineHeight: 1.35,
    fontWeight: 500,
    color: 'var(--ui-text-primary)',
    display: '-webkit-box',
    WebkitLineClamp: 2,
    WebkitBoxOrient: 'vertical',
    overflow: 'hidden',
    wordBreak: 'break-word'
  },
  ghNum: {
    flex: 'none',
    fontSize: '0.6875rem',
    lineHeight: 1.6,
    color: 'var(--ui-text-quaternary)',
    fontVariantNumeric: 'tabular-nums',
    fontFamily: 'var(--font-mono, ui-monospace, monospace)'
  },
  ghAge: {
    flex: 'none',
    fontSize: '0.6875rem',
    lineHeight: 1.6,
    color: 'var(--ui-text-quaternary)',
    fontVariantNumeric: 'tabular-nums',
    whiteSpace: 'nowrap'
  },
  ghMeta: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    flexWrap: 'wrap',
    minWidth: 0,
    paddingLeft: 21,
    fontSize: '0.6875rem',
    color: 'var(--ui-text-quaternary)'
  },
  ghChipBtn: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 3,
    flex: 'none',
    height: 16,
    padding: '0 5px',
    border: 0,
    borderRadius: 3,
    background: 'color-mix(in srgb, var(--ui-stroke-secondary) 40%, transparent)',
    color: 'var(--ui-text-tertiary)',
    fontSize: '0.625rem',
    lineHeight: 1,
    fontVariantNumeric: 'tabular-nums',
    fontFamily: 'var(--font-mono, ui-monospace, monospace)',
    cursor: 'pointer'
  },
  ghChipTag: {
    display: 'inline-flex',
    alignItems: 'center',
    flex: 'none',
    height: 16,
    padding: '0 5px',
    borderRadius: 3,
    background: 'color-mix(in srgb, var(--ui-stroke-secondary) 30%, transparent)',
    color: 'var(--ui-text-quaternary)',
    fontSize: '0.625rem',
    lineHeight: 1,
    fontVariantNumeric: 'tabular-nums',
    fontFamily: 'var(--font-mono, ui-monospace, monospace)'
  },
  ghLabel: {
    display: 'inline-flex',
    alignItems: 'center',
    maxWidth: 110,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    padding: '0 5px',
    borderRadius: 3,
    border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 55%, transparent)',
    color: 'var(--ui-text-tertiary)',
    fontSize: '0.625rem',
    lineHeight: '14px'
  },
  ghInitials: {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 16,
    height: 16,
    borderRadius: 999,
    background: 'color-mix(in srgb, var(--ui-stroke-secondary) 45%, transparent)',
    color: 'var(--ui-text-tertiary)',
    fontSize: '0.5625rem',
    fontWeight: 600,
    letterSpacing: '0.02em'
  },
  ghTiny: { fontSize: '0.625rem', color: 'var(--ui-text-quaternary)' },
  ghBranchName: {
    flex: '1 1 0',
    minWidth: 0,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    fontSize: '0.75rem',
    fontWeight: 500,
    color: 'var(--ui-text-primary)',
    fontFamily: 'var(--font-mono, ui-monospace, monospace)'
  },
  ghSubject: {
    flex: '0 1 320px',
    minWidth: 0,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    color: 'var(--ui-text-quaternary)'
  },
  ghSubBtn: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    width: '100%',
    minWidth: 0,
    padding: '3px 4px 3px 21px',
    border: 0,
    borderRadius: 3,
    background: 'transparent',
    color: 'var(--ui-text-primary)',
    textAlign: 'left',
    cursor: 'pointer',
    fontSize: '0.6875rem'
  },
  ghSubTitle: {
    flex: '1 1 0',
    minWidth: 0,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    color: 'var(--ui-text-secondary)'
  },
  // Issue/PR chips on a board column header, sourced from /board's link rows.
  boardChips: { display: 'inline-flex', alignItems: 'center', gap: 4, minWidth: 0, flexWrap: 'wrap' },
  ghFiles: { display: 'inline-flex', alignItems: 'center', gap: 4, flexWrap: 'wrap', minWidth: 0 },
  ghFile: {
    maxWidth: 180,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    padding: '0 4px',
    borderRadius: 3,
    background: 'color-mix(in srgb, var(--ui-stroke-secondary) 28%, transparent)',
    color: 'var(--ui-text-tertiary)',
    fontSize: '0.625rem',
    fontFamily: 'var(--font-mono, ui-monospace, monospace)'
  },
  // ── detail modal ───────────────────────────────────────────────────────────
  // Same vocabulary as the rows, one step roomier: the dialog already supplies
  // the surface, the padding and the scroll, so these only lay out content.
  ghModalHead: { display: 'flex', flexDirection: 'column', gap: 6, minWidth: 0, textAlign: 'left' },
  ghModalTitleRow: { display: 'flex', alignItems: 'flex-start', gap: 7, minWidth: 0 },
  ghModalNum: {
    flex: 'none',
    fontSize: '0.75rem',
    lineHeight: 1.5,
    color: 'var(--ui-text-quaternary)',
    fontVariantNumeric: 'tabular-nums',
    fontFamily: 'var(--font-mono, ui-monospace, monospace)'
  },
  ghModalTitle: {
    flex: '1 1 0',
    minWidth: 0,
    fontSize: '0.875rem',
    fontWeight: 600,
    lineHeight: 1.35,
    color: 'var(--ui-text-primary)',
    wordBreak: 'break-word'
  },
  ghModalMeta: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    flexWrap: 'wrap',
    minWidth: 0,
    fontSize: '0.6875rem',
    color: 'var(--ui-text-tertiary)'
  },
  ghBackBtn: {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    flex: 'none',
    width: 18,
    height: 18,
    padding: 0,
    border: 0,
    borderRadius: 4,
    background: 'transparent',
    color: 'var(--ui-text-secondary)',
    cursor: 'pointer'
  },
  ghActorRow: { display: 'inline-flex', alignItems: 'center', gap: 4, flex: 'none', minWidth: 0 },
  ghActor: {
    minWidth: 0,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    fontWeight: 500,
    color: 'var(--ui-text-secondary)'
  },
  ghDiff: { display: 'inline-flex', alignItems: 'center', gap: 5, flex: 'none', fontVariantNumeric: 'tabular-nums' },
  ghThread: { display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 },
  ghSection: { display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 },
  ghSectionHead: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    fontSize: '0.6875rem',
    fontWeight: 600,
    letterSpacing: '0.02em',
    color: 'var(--ui-text-tertiary)'
  },
  // MessageTextContent owns the prose; this only bounds and scales the box.
  ghProse: { minWidth: 0, fontSize: '0.8125rem' },
  ghComment: {
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    minWidth: 0,
    padding: '8px 10px',
    borderRadius: 8,
    background: 'color-mix(in srgb, var(--ui-stroke-secondary) 16%, transparent)'
  },
  ghCommentHead: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    flexWrap: 'wrap',
    minWidth: 0,
    fontSize: '0.6875rem',
    color: 'var(--ui-text-tertiary)'
  },
  ghComposer: {
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
    minWidth: 0,
    paddingTop: 10,
    borderTop: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 45%, transparent)'
  },
  ghComposerRow: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', minWidth: 0 },
  ghComposerError: { fontSize: '0.75rem', lineHeight: 1.45, color: 'var(--dt-destructive)' },
  ghRefChip: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 3,
    maxWidth: 280,
    padding: '0 4px',
    border: 0,
    borderRadius: 3,
    background: 'color-mix(in srgb, var(--ui-stroke-secondary) 40%, transparent)',
    color: 'var(--ui-text-secondary)',
    lineHeight: 1.4,
    verticalAlign: 'baseline',
    cursor: 'pointer',
    font: 'inherit'
  },
  ghRefText: { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  ghPop: { display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0, fontSize: '0.75rem', color: 'var(--ui-text-secondary)' },
  ghPopHead: { display: 'flex', alignItems: 'center', gap: 5, minWidth: 0, color: 'var(--ui-text-tertiary)' },
  ghPopKind: { textTransform: 'capitalize' },
  ghPopNum: { fontFamily: 'var(--font-mono, ui-monospace, monospace)', fontVariantNumeric: 'tabular-nums' },
  ghPopTitle: { fontWeight: 600, color: 'var(--ui-text-primary)', wordBreak: 'break-word' },
  ghPopMeta: { display: 'flex', gap: 6, flexWrap: 'wrap', fontSize: '0.6875rem', color: 'var(--ui-text-quaternary)' },
  ghPopBody: { color: 'var(--ui-text-tertiary)', lineHeight: 1.45, wordBreak: 'break-word' },
  ghGroup: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    padding: '10px 0 3px',
    fontSize: '0.6875rem',
    fontWeight: 600,
    letterSpacing: '0.02em',
    color: 'var(--ui-text-tertiary)'
  },
  ghQuiet: { padding: '8px 0', fontSize: '0.75rem', color: 'var(--ui-text-quaternary)' },
  ghErrorWrap: { display: 'flex', justifyContent: 'center', padding: '16px 0 8px' },
  // The diagnose panel is inline and quiet: key/value rows in small type, no
  // card chrome beyond one hairline.
  ghDiag: {
    display: 'flex',
    flexDirection: 'column',
    gap: 3,
    margin: '2px 0 8px',
    padding: '8px 10px',
    border: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 45%, transparent)',
    borderRadius: 6,
    fontSize: '0.6875rem',
    minWidth: 0
  },
  ghDiagHead: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    paddingBottom: 4,
    fontWeight: 600,
    color: 'var(--ui-text-tertiary)'
  },
  ghDiagRow: {
    display: 'grid',
    gridTemplateColumns: '128px minmax(0, 1fr)',
    gap: 10,
    alignItems: 'baseline',
    minWidth: 0
  },
  ghDiagKey: { color: 'var(--ui-text-quaternary)' },
  ghDiagValue: {
    color: 'var(--ui-text-secondary)',
    fontFamily: 'var(--font-mono, ui-monospace, monospace)',
    fontSize: '0.625rem',
    overflowWrap: 'anywhere'
  },
  ghDiagNote: { color: 'var(--ui-text-quaternary)', lineHeight: 1.5, paddingTop: 3 },
  ghLinkBtn: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    padding: 0,
    border: 0,
    background: 'transparent',
    color: 'var(--ui-text-quaternary)',
    fontSize: '0.75rem',
    cursor: 'pointer'
  },
  ghRepoBtn: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    maxWidth: 260,
    padding: '1px 6px',
    border: 0,
    borderRadius: 999,
    background: 'color-mix(in srgb, var(--ui-stroke-secondary) 35%, transparent)',
    color: 'var(--ui-text-tertiary)',
    fontSize: '0.6875rem',
    lineHeight: 1.5,
    cursor: 'pointer'
  },
  ghRepoText: {
    maxWidth: 260,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    fontSize: '0.6875rem',
    color: 'var(--ui-text-tertiary)'
  }
}

// ── small pieces ─────────────────────────────────────────────────────────────

// The best mark the backend can find for a project folder, asked once per folder
// and shared between every card that shows it. The stack keys are the backend's
// contract; the glyph names live here so a wrong name can never break a render.
const STACK_GLYPH = {
  laravel: 'server-process',
  nuxt: 'package',
  node: 'package',
  python: 'beaker',
  rust: 'gear',
  go: 'rocket',
  nix: 'file-code',
  obsidian: 'book',
  docker: 'layers',
  docs: 'book',
  git: 'git-branch'
}

function useProjectIcon(folder) {
  const query = useQuery({
    enabled: Boolean(folder),
    queryFn: () => ghRest(`/icon?path=${encodeURIComponent(folder)}`).catch(() => null),
    queryKey: [QUERY_ROOT, 'icon', folder],
    retry: false,
    staleTime: 300000
  })

  return query.data && query.data.ok ? query.data : null
}

// Layered: the project's own image, then the glyph for what the project is, then
// the initial block. A loading icon keeps the initial so nothing flashes empty.
function Avatar({ project, size = 24 }) {
  const color = validColor(project.color)
  const initial = (String(project.label || '?').trim()[0] || '?').toUpperCase()
  const icon = useProjectIcon(primaryPath(project))
  const box = {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    flex: 'none',
    width: size,
    height: size,
    borderRadius: 7,
    overflow: 'hidden'
  }

  if (icon && icon.kind === 'image' && icon.data_url) {
    return jsx('span', {
      'aria-hidden': 'true',
      style: { ...box, background: 'var(--ui-bg-quaternary)', border: '1px solid var(--ui-stroke-secondary)' },
      children: jsx('img', {
        alt: '',
        decoding: 'async',
        src: icon.data_url,
        style: { height: '100%', width: '100%', objectFit: 'contain' }
      })
    })
  }

  const glyph = icon && icon.kind === 'glyph' ? STACK_GLYPH[String(icon.stack || '')] : ''

  if (glyph) {
    return jsx('span', {
      'aria-hidden': 'true',
      style: { ...box, color: color || 'var(--ui-text-secondary)' },
      children: jsx(Codicon, { name: glyph, size: `${Math.round(size * 0.64)}px` })
    })
  }

  return jsx('span', {
    'aria-hidden': 'true',
    style: {
      ...box,
      fontSize: size * 0.46,
      fontWeight: 600,
      color: 'var(--ui-text-primary)',
      background: color ? `color-mix(in srgb, ${color} 26%, transparent)` : 'var(--ui-bg-quaternary)',
      border: `1px solid ${color ? `color-mix(in srgb, ${color} 60%, transparent)` : 'var(--ui-stroke-secondary)'}`
    },
    children: initial
  })
}

function BranchChip({ branch }) {
  return jsxs('span', {
    style: S.chip,
    children: [
      jsx(Codicon, { name: 'git-branch', size: '0.75rem', style: { flex: 'none' } }),
      jsx('span', { style: S.ellipsis, children: branch })
    ]
  })
}

function PathLine({ path, inside }) {
  if (!path) return null
  // A vault subfolder resolves to a bigger work tree. Naming that root next to
  // the path stops the page from looking like it read the wrong repository.
  const rootName = inside ? String(inside).split('/').filter(Boolean).pop() : ''

  return jsxs('span', {
    style: { display: 'inline-flex', alignItems: 'center', gap: 6, minWidth: 0 },
    children: [
      jsx(Tip, { label: path, children: jsx('span', { style: S.path, children: middleTruncate(path) }) }),
      rootName
        ? jsx(Tip, {
            label: `This folder is inside the work tree at ${inside}`,
            children: jsx('span', { style: S.ghChipTag, children: `inside ${rootName}` })
          })
        : null
    ]
  })
}

// The app's own running-session ring (apps/desktop/src/app/chat/sidebar/
// session-row.tsx renders the same span). `.arc-border` carries the travelling
// gradient, the theme vars and the reduced-motion pause in the renderer's global
// stylesheet, so nothing is redeclared here; the host only has to be
// `position: relative` and own a radius.
function ArcRing({ radius, zIndex = 0 }) {
  return jsx('span', {
    'aria-hidden': 'true',
    className: 'arc-border arc-row',
    style: { '--arc-radius': radius, zIndex }
  })
}

// Arc and dot never disagree: both read the same resolved state.
function LiveDot({ state }) {
  if (!state || state === 'idle') return null

  return jsx(Tip, {
    label: LIVE_LABELS[state] || 'Live',
    children: jsx(StatusDot, { tone: liveTone(state) })
  })
}

function StatusPicker({ projectId, status }) {
  const current = STATUS_BY_ID[status] || STATUS_BY_ID[DEFAULT_STATUS]

  return jsxs(DropdownMenu, {
    children: [
      jsx(DropdownMenuTrigger, {
        asChild: true,
        children: jsx('button', {
          'aria-label': `Project status: ${current.label}. Change`,
          className: 'pcc-status-btn',
          style: { border: 0, padding: 0, background: 'transparent', cursor: 'pointer', borderRadius: 3, display: 'inline-flex' },
          type: 'button',
          children: jsxs(Badge, {
            variant: current.variant,
            children: [current.label, jsx(Codicon, { name: 'chevron-down', size: '0.625rem' })]
          })
        })
      }),
      jsx(DropdownMenuContent, {
        align: 'start',
        children: STATUSES.map(s =>
          jsxs(
            DropdownMenuItem,
            {
              onSelect: () => writeStatus(projectId, s.id),
              children: [
                jsx(Codicon, { name: 'check', size: '0.75rem', style: { visibility: s.id === current.id ? 'visible' : 'hidden' } }),
                s.label
              ]
            },
            s.id
          )
        )
      })
    ]
  })
}

function SortPicker({ sort }) {
  const current = SORTS.find(s => s.id === sort) || SORTS[0]

  return jsxs(DropdownMenu, {
    children: [
      jsx(DropdownMenuTrigger, {
        asChild: true,
        children: jsxs(Button, {
          'aria-label': `Sort projects by ${current.label}. Change`,
          size: 'sm',
          variant: 'ghost',
          children: [
            jsx(Codicon, { name: 'list-ordered', size: '0.875rem' }),
            jsx('span', { children: current.label }),
            jsx(Codicon, { name: 'chevron-down', size: '0.625rem' })
          ]
        })
      }),
      jsx(DropdownMenuContent, {
        align: 'end',
        children: SORTS.map(s =>
          jsxs(
            DropdownMenuItem,
            {
              onSelect: () => writeSort(s.id),
              children: [
                jsx(Codicon, { name: 'check', size: '0.75rem', style: { visibility: s.id === current.id ? 'visible' : 'hidden' } }),
                jsx(Codicon, { name: s.icon, size: '0.75rem' }),
                s.label
              ]
            },
            s.id
          )
        )
      })
    ]
  })
}

function SessionCard({ session, state, showProject, onOpen }) {
  const ms = sessionStamp(session)
  const model = shortModel(session.model)
  const running = RUNNING.has(state)

  return jsxs(RowButton, {
    className: 'pcc-card',
    onClick: () => onOpen(session.id),
    style: S.card,
    title: sessionTitle(session),
    children: [
      running ? jsx(ArcRing, { radius: '8px' }) : null,
      jsxs('span', {
        style: { display: 'flex', alignItems: 'flex-start', gap: 7, minWidth: 0 },
        children: [
          state && state !== 'idle' ? jsx('span', { style: { display: 'inline-flex', paddingTop: 5, flex: 'none' }, children: jsx(LiveDot, { state }) }) : null,
          jsx('span', { style: S.cardTitle, children: sessionTitle(session) })
        ]
      }),
      jsxs('span', {
        style: S.cardMeta,
        children: [
          session.git_branch ? jsx(BranchChip, { branch: session.git_branch }) : null,
          showProject ? jsx('span', { style: S.ellipsis, children: showProject }) : null,
          ms ? jsx(Tip, { label: stampTip(ms), children: jsx('span', { children: ago(ms) }) }) : null,
          model ? jsx('span', { style: { ...S.ellipsis, maxWidth: 120 }, children: model }) : null
        ]
      })
    ]
  })
}

function recentSessions(project) {
  const rows = [...(project.previewSessions || [])]

  rows.sort((a, b) => sessionStamp(b) - sessionStamp(a))

  return rows
}

// ── column ───────────────────────────────────────────────────────────────────

// What the board knows about a project beyond its sessions: the issues and pull
// requests a session was started from or linked to. Read-only tags — the detail
// page is where the rows themselves open.
function BoardLinkChips({ links }) {
  if (!links || (links.issues.length === 0 && links.prs.length === 0)) return null

  const rows = [
    ...links.prs.map(number => ({ key: `pr-${number}`, kind: 'pr', number })),
    ...links.issues.map(number => ({ key: `issue-${number}`, kind: 'issue', number }))
  ]
  const shown = rows.slice(0, 2)
  const rest = rows.length - shown.length

  return jsxs('span', {
    style: S.boardChips,
    children: [
      ...shown.map(row =>
        jsx(
          Tip,
          {
            label: `${row.kind === 'pr' ? 'Pull request' : 'Issue'} #${row.number}`,
            children: jsxs('span', {
              style: S.ghChipTag,
              children: [
                jsx(Codicon, { name: row.kind === 'pr' ? 'git-pull-request' : 'issues', size: '0.625rem' }),
                `#${row.number}`
              ]
            })
          },
          row.key
        )
      ),
      rest > 0 ? jsx('span', { style: { color: 'var(--ui-text-quaternary)' }, children: `+${rest}` }) : null
    ]
  })
}

const NO_COUNTS = {}
const COUNTS_PATH_LIMIT = 50

// ONE batched read for the whole board: every project's folder, a single
// /github/counts call. A missing route, a failure or ok:false all collapse to an
// empty map, so a column simply has no identifier row. The backend caches too.
function useRepoCounts(projects) {
  const key = useMemo(() => {
    const seen = new Set()

    for (const p of projects || []) {
      const folder = primaryPath(p)

      if (folder) seen.add(folder)
      if (seen.size >= COUNTS_PATH_LIMIT) break
    }

    return Array.from(seen).join('\n')
  }, [projects])

  const query = useQuery({
    enabled: key !== '',
    queryFn: () =>
      ghRest(`/github/counts?${key.split('\n').map(p => `path=${encodeURIComponent(p)}`).join('&')}`).catch(() => null),
    queryKey: [QUERY_ROOT, 'gh-counts', key],
    refetchInterval: 60000,
    retry: false,
    staleTime: 60000
  })
  const data = query.data

  return data && data.ok && data.counts && typeof data.counts === 'object' ? data.counts : NO_COUNTS
}

function countOf(value) {
  const n = Number(value)

  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
}

function idCount(n) {
  return n > 999 ? '999+' : String(n)
}

// Icon + number pairs: open issues, open PRs and, when there is room, the live
// sessions. A pair with 0 or an unknown count is left out; with nothing to show
// the whole row is null (live alone never makes a row, it only joins one).
function ProjectIdRow({ counts, liveCount }) {
  if (!counts) return null
  const pairs = [
    { icon: 'issues', label: 'open', noun: 'issue', value: countOf(counts.issues_open) },
    { icon: 'git-pull-request', label: 'open', noun: 'pull request', value: countOf(counts.prs_open) }
  ].filter(pair => pair.value > 0)

  if (pairs.length === 0) return null

  return jsxs('span', {
    style: S.idRow,
    children: [
      ...pairs.map(pair =>
        jsx(
          Tip,
          {
            label: `${pair.value} ${pair.label} ${pair.noun}${pair.value === 1 ? '' : 's'}`,
            children: jsxs('span', {
              style: S.idPair,
              children: [jsx(Codicon, { name: pair.icon, size: '0.75rem', style: { flex: 'none' } }), idCount(pair.value)]
            })
          },
          pair.icon
        )
      ),
      liveCount > 0
        ? jsx(Tip, {
            label: `${liveCount} live ${liveCount === 1 ? 'session' : 'sessions'}`,
            children: jsxs('span', { style: { ...S.idPair, color: 'var(--ui-accent)' }, children: [jsx(StatusDot, { tone: 'good' }), idCount(liveCount)] })
          })
        : null
    ]
  })
}

function ProjectColumn({ project, index, status, liveByStored, onOpenProject, onOpenSession, links, counts }) {
  const recent = recentSessions(project)
  const shown = recent.slice(0, BOARD_CARD_LIMIT)
  const hidden = Math.max(0, (project.sessionCount || 0) - shown.length)
  const cost = formatCost(project.totalCostUsd)
  const liveCount = useMemo(() => {
    let n = 0

    for (const id of project.sessionIds || []) if (RUNNING.has(liveByStored.get(id))) n += 1

    return n
  }, [project.sessionIds, liveByStored])

  return jsxs('section', {
    'aria-label': project.label,
    className: 'pcc-col',
    style: { ...S.column, '--pcc-i': index },
    children: [
      liveCount > 0 ? jsx(ArcRing, { radius: '12px', zIndex: 2 }) : null,
      jsxs('header', {
        style: S.colHeader,
        children: [
          jsxs('div', {
            style: S.row,
            children: [
              jsxs('button', {
                className: 'pcc-link',
                onClick: () => onOpenProject(project.id),
                style: S.nameBtn,
                title: `Open ${project.label}`,
                type: 'button',
                children: [
                  jsx(Avatar, { project, size: 32 }),
                  jsxs('span', {
                    style: { display: 'flex', flexDirection: 'column', minWidth: 0, gap: 1 },
                    children: [
                      jsx('span', { style: S.name, children: project.label }),
                      jsx(PathLine, { path: primaryPath(project) }),
                      jsx(ProjectIdRow, { counts, liveCount })
                    ]
                  })
                ]
              }),
              jsx(Tip, {
                label: 'Open project',
                children: jsx(Button, {
                  'aria-label': `Open ${project.label}`,
                  onClick: () => onOpenProject(project.id),
                  size: 'icon-xs',
                  variant: 'ghost',
                  children: jsx(Codicon, { name: 'chevron-right', size: '0.875rem' })
                })
              })
            ]
          }),
          jsxs('div', {
            style: S.metrics,
            children: [
              jsx(StatusPicker, { projectId: project.id, status }),
              jsx(BoardLinkChips, { links }),
              jsx('span', { style: S.metricMono, children: `${project.sessionCount || 0} ${project.sessionCount === 1 ? 'session' : 'sessions'}` }),
              liveCount > 0
                ? jsxs('span', {
                    style: { display: 'inline-flex', alignItems: 'center', gap: 4, color: 'var(--ui-accent)' },
                    children: [jsx(StatusDot, { tone: 'good' }), `${liveCount} live`]
                  })
                : null,
              jsx('span', { style: S.grow }),
              project.totalTokens > 0
                ? jsx(Tip, {
                    label: `${(project.totalTokens || 0).toLocaleString()} tokens`,
                    children: jsx('span', { style: S.metricMono, children: `${formatTokens(project.totalTokens)} tok` })
                  })
                : null,
              cost ? jsx('span', { style: S.metricMono, children: cost }) : null
            ]
          })
        ]
      }),
      jsx(ColumnBody, { project, shown, hidden, liveByStored, onOpenProject, onOpenSession })
    ]
  })
}

function ColumnBody({ project, shown, hidden, liveByStored, onOpenProject, onOpenSession }) {
  return jsx('div', {
    style: { flex: '1 1 0', minHeight: 0, overflowY: 'auto', overflowX: 'hidden', scrollbarWidth: 'thin' },
    children: jsxs('div', {
      style: S.body,
      children: [
        shown.length === 0
          ? jsx('div', { style: S.quiet, children: 'No sessions yet' })
          : shown.map(s => jsx(SessionCard, { onOpen: onOpenSession, session: s, state: liveByStored.get(s.id) }, s.id)),
        hidden > 0
          ? jsxs('button', {
              className: 'pcc-more',
              onClick: () => onOpenProject(project.id),
              style: S.more,
              type: 'button',
              children: [`+${hidden} more`, jsx(Codicon, { name: 'arrow-right', size: '0.75rem' })]
            })
          : null
      ]
    })
  })
}

// ── board with edge affordances ──────────────────────────────────────────────

function Board({ count, children }) {
  const ref = useRef(null)
  const [edge, setEdge] = useState({ left: false, right: false })

  const update = useCallback(() => {
    const el = ref.current

    if (!el) return
    const left = el.scrollLeft > 2
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 2

    setEdge(prev => (prev.left === left && prev.right === right ? prev : { left, right }))
  }, [])

  useEffect(() => {
    update()
    const el = ref.current

    if (!el || typeof ResizeObserver === 'undefined') return undefined
    const ro = new ResizeObserver(update)

    ro.observe(el)
    if (el.firstElementChild) ro.observe(el.firstElementChild)

    return () => ro.disconnect()
  }, [update, count])

  const scrollBy = dir => {
    const el = ref.current

    if (!el) return
    el.scrollBy({ left: dir * (COLUMN_WIDTH + COLUMN_GAP) * 1.5, behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
  }

  const fade = side => ({
    ...S.edge,
    [side]: 0,
    background: `linear-gradient(to ${side === 'left' ? 'right' : 'left'}, color-mix(in srgb, var(--ui-stroke-secondary) 60%, transparent), transparent)`
  })

  return jsxs('div', {
    style: S.boardWrap,
    children: [
      jsx('div', { className: 'pcc-scroller', onScroll: update, ref, style: S.scroller, children }),
      edge.left ? jsx('div', { 'aria-hidden': 'true', style: fade('left') }) : null,
      edge.right ? jsx('div', { 'aria-hidden': 'true', style: fade('right') }) : null,
      edge.left
        ? jsx(Button, {
            'aria-label': 'Scroll projects left',
            onClick: () => scrollBy(-1),
            size: 'icon-sm',
            style: { ...S.edgeBtn, left: 8 },
            variant: 'floating',
            children: jsx(Codicon, { name: 'chevron-left', size: '1rem' })
          })
        : null,
      edge.right
        ? jsx(Button, {
            'aria-label': 'Scroll projects right',
            onClick: () => scrollBy(1),
            size: 'icon-sm',
            style: { ...S.edgeBtn, right: 8 },
            variant: 'floating',
            children: jsx(Codicon, { name: 'chevron-right', size: '1rem' })
          })
        : null
    ]
  })
}

function BoardSkeleton() {
  return jsx('div', {
    style: S.boardWrap,
    children: jsx('div', {
      'aria-busy': 'true',
      style: S.scroller,
      children: [0, 1, 2].map(i =>
        jsxs(
          'div',
          {
            style: { ...S.column, padding: 14, gap: 10 },
            children: [
              jsx(Skeleton, { style: { height: 24, width: '60%' } }),
              jsx(Skeleton, { style: { height: 14, width: '80%' } }),
              jsx(Skeleton, { style: { height: 54 } }),
              jsx(Skeleton, { style: { height: 54 } }),
              jsx(Skeleton, { style: { height: 54 } })
            ]
          },
          i
        )
      )
    })
  })
}

// ── live strip ───────────────────────────────────────────────────────────────

function LiveStrip({ items, projectBySession, onOpenSession, loading }) {
  return jsxs('section', {
    'aria-label': 'Live sessions',
    style: S.strip,
    children: [
      jsx(Separator, {}),
      jsxs('div', {
        style: S.stripHead,
        children: [
          jsx('span', { style: S.stripTitle, children: 'Live sessions' }),
          items.length > 0 ? jsx('span', { style: S.count, children: items.length }) : null
        ]
      }),
      items.length === 0
        ? jsx('div', {
            style: { fontSize: '0.75rem', color: 'var(--ui-text-quaternary)', padding: '2px 0' },
            children: loading ? 'Checking for live sessions\u2026' : 'No live sessions right now.'
          })
        : jsx('div', {
            style: S.stripList,
            children: items.map(item => {
              const project = projectBySession.get(item.session_key)
              const model = shortModel(item.model)
              const ms = sessionStamp(item)
              const state = item.state || 'idle'

              return jsxs(
                RowButton,
                {
                  className: 'pcc-live-row',
                  onClick: () => onOpenSession(item.session_key),
                  style: S.liveRow,
                  children: [
                    jsx('span', { style: { display: 'inline-flex', flex: 'none' }, children: jsx(LiveDot, { state }) }),
                    jsx('span', { style: { ...S.ellipsis, flex: '1 1 0', fontWeight: 500 }, children: sessionTitle(item) }),
                    project ? jsx('span', { style: { ...S.ellipsis, flex: '0 1 140px', color: 'var(--ui-text-secondary)' }, children: project }) : null,
                    model ? jsx('span', { style: { ...S.ellipsis, flex: '0 1 120px', color: 'var(--ui-text-quaternary)' }, children: model }) : null,
                    jsx('span', { style: { flex: 'none', width: 64, color: 'var(--ui-text-secondary)' }, children: LIVE_LABELS[state] || state }),
                    jsx('span', {
                      style: { flex: 'none', width: 64, textAlign: 'right', color: 'var(--ui-text-quaternary)' },
                      children: ms ? jsx(Tip, { label: stampTip(ms), children: jsx('span', { children: ago(ms) }) }) : null
                    })
                  ]
                },
                item.session_key
              )
            })
          })
    ]
  })
}

// ── live state ───────────────────────────────────────────────────────────────

const $noBusy = atom({})

// One poll, one mapping. `busyBySession` is keyed by RUNTIME id and updates in
// real time, so it leads the 5s gateway poll; byStored is the stored-id view
// every card, row and column count paints from.
function useLiveSessions() {
  const query = useQuery({
    queryFn: () => host.request('session.active_list', profileParam()),
    queryKey: [QUERY_ROOT, 'live'],
    refetchInterval: LIVE_MS,
    staleTime: 2500
  })

  const busyByRuntime = useValue((host.state && host.state.busyBySession) || $noBusy)
  const activity = useValue($activity)
  const clock = useValue($activityClock)

  const items = useMemo(() => {
    const rows = ((query.data && query.data.sessions) || []).filter(s => s.session_key && s.id)

    return rows
      .map(s => {
        const at = activity[s.id] || 0
        // clock is 0 until the first tick, so fall back to the wall clock then.
        const recentlyActive = at > 0 && (clock || Date.now()) - at < ACTIVITY_TTL_MS

        return { ...s, state: liveState(s, Boolean(busyByRuntime && busyByRuntime[s.id]), recentlyActive) }
      })
      .sort((a, b) => sessionStamp(b) - sessionStamp(a))
  }, [query.data, busyByRuntime, activity, clock])

  const byStored = useMemo(() => new Map(items.map(s => [s.session_key, s.state])), [items])

  return { byStored, items, query }
}

// ── github section ───────────────────────────────────────────────────────────
// Two reads for one project folder: /github for the open board and /github with
// state=closed for the shipped list. Both are joined against the sessions the
// detail page already loaded (by git_branch) and against /repo's branch list.
//
// A payload can carry `error` (no remote, no auth, a rate limit): that renders as
// a quiet line. Only a THROWN request means the backend is not mounted yet, and
// that is the one case that gets the error row. Nothing here writes.

const GH_PRESET_KEY = 'githubPreset.v1'
const DEFAULT_GH_PRESET = 'flight'
const GH_PRESETS = [
  { id: 'flight', label: 'In flight' },
  { id: 'issue', label: 'By issue' },
  { id: 'branch', label: 'By branch' },
  { id: 'unlinked', label: 'Unlinked' },
  { id: 'shipped', label: 'Shipped' }
]
const GH_LIMIT = 50
const SHIPPED_CAP = 20
const BRANCH_CAP = 25
const PR_FILES_SHOWN = 8
// #123 references: a chip is only ever drawn for a number /github/refs resolved,
// so an unresolvable number stays the writer's plain text.
const GH_REF_RE = /(?<![\w/])#(\d{1,7})\b/g
const GH_REF_CHARS = 200
// The backend resolves at most this many numbers per /github/refs read.
const GH_REF_MAX = 50
const GH_LABEL_CHIPS = 5
const CHECKS_TONE = { success: 'good', failure: 'bad', pending: 'warn', none: 'muted' }
const CHECKS_TEXT = { success: 'all checks passing', failure: 'checks failing', pending: 'checks running', none: 'no checks reported' }
const REVIEW_BADGES = {
  APPROVED: { label: 'approved', title: 'Approved', variant: 'success' },
  CHANGES_REQUESTED: { label: 'changes', title: 'Changes requested', variant: 'destructive' },
  REVIEW_REQUIRED: { label: 'review', title: 'Review required', variant: 'muted' }
}

const $ghPreset = atom(DEFAULT_GH_PRESET)
// A board.changed event that names a pull request pokes the GitHub section:
// `{ project_id, pr, at }`. `at` makes two pokes for the same PR distinct.
const $ghPoke = atom(null)

function readGhPreset() {
  const raw = pluginCtx ? pluginCtx.storage.get(GH_PRESET_KEY, DEFAULT_GH_PRESET) : DEFAULT_GH_PRESET

  return GH_PRESETS.some(p => p.id === raw) ? raw : DEFAULT_GH_PRESET
}

function writeGhPreset(presetId) {
  if (!GH_PRESETS.some(p => p.id === presetId)) return
  $ghPreset.set(presetId)
  if (pluginCtx) pluginCtx.storage.set(GH_PRESET_KEY, presetId)
}

// ctx.rest rejects when the gateway has no router for this plugin, which is the
// "backend not enabled yet" signal the section renders. Guarding here keeps the
// failure a rejected promise instead of a render crash on an older host.
function ghRest(path, opts) {
  if (!pluginCtx || typeof pluginCtx.rest !== 'function') {
    return Promise.reject(new Error('This Desktop build has no plugin REST door.'))
  }

  return pluginCtx.rest(path, opts)
}

// A failure is a code, not a status line. Each normal condition gets one small
// muted sentence; the endpoint and status travel in a Tip so the line stays one
// line. Only a THROWN request is the red-ish ErrorState elsewhere.
function ghFailureText(data, folder) {
  const code = String((data && data.code) || '')
  const err = String((data && data.error) || '').trim()
  const detail = String((data && data.detail) || '').trim()
  const account = String((data && data.auth_account) || '').trim()

  if (code === 'folder_missing') return { text: `Folder not found: ${(data && data.path) || folder || ''}` }
  if (code === 'not_a_repo') return { text: 'Not a git repository' }
  if (code === 'no_remote') return { text: 'No GitHub remote' }
  if (code === 'repo_not_found') {
    const named = account && err && !err.includes(account) ? `${err} (as ${account})` : err
    return { text: named || `GitHub could not read this repo as ${account || 'the configured credential'}.`, detail }
  }
  if (code === 'no_auth') return { text: 'GitHub not authenticated. Set GITHUB_TOKEN or run gh auth login.', detail }
  if (code === 'rate_limited') return { text: 'GitHub rate limit reached, retrying later', detail }
  if (code === 'network' || code === 'upstream_error') return { text: detail || err || 'GitHub could not be reached.', detail }
  return { text: err || 'GitHub is not available for this folder right now.', detail }
}

function GhFailure({ data, folder }) {
  const { text, detail } = ghFailureText(data, folder)
  const line = jsx('span', {
    style: { ...S.ghQuiet, display: 'block' },
    children: text
  })

  return detail ? jsx(Tip, { label: detail, children: line }) : line
}

function ghNumber(n) {
  return `#${Number(n) || 0}`
}

// GitHub and `git log --iso-strict` hand back ISO 8601, while the gateway uses
// epoch seconds. `toMs` only knows the latter, so the GitHub side gets its own
// reader and a matching age formatter.
function ghMs(value) {
  if (value == null || value === '') return 0
  if (typeof value === 'number') return toMs(value)
  const str = String(value).trim()

  if (/^\d+(\.\d+)?$/.test(str)) return toMs(str)
  const parsed = Date.parse(str)

  return Number.isFinite(parsed) ? parsed : 0
}

function ghAgo(value) {
  const ms = ghMs(value)

  return ms ? relativeTime(ms) : ''
}

function ghInitials(login) {
  const parts = String(login || '').split(/[-_. ]+/).filter(Boolean)

  if (parts.length === 0) return '?'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()

  return `${parts[0][0]}${parts[1][0]}`.toUpperCase()
}

function ghChecksTip(checks) {
  const total = (checks && checks.total) || 0
  const failing = (checks && checks.failing) || 0

  if (!total) return CHECKS_TEXT.none

  return `${failing} failing of ${total} checks`
}

function ghPrLook(pr) {
  if (pr.merged) return { name: 'git-merge', color: 'var(--ui-accent)' }
  if (pr.state === 'closed') return { name: 'git-pull-request-closed', color: 'var(--ui-text-quaternary)' }
  if (pr.draft) return { name: 'git-pull-request-draft', color: 'var(--ui-text-tertiary)' }

  return { name: 'git-pull-request', color: 'var(--ui-text-secondary)' }
}

// Inverted prs[].linked_issues: one pass per board read, used by every preset.
function ghLinkedPrMap(prs) {
  const map = new Map()

  for (const pr of prs || []) {
    for (const n of pr.linked_issues || []) {
      const list = map.get(n) || []

      list.push(pr)
      map.set(n, list)
    }
  }

  return map
}

// The join the whole section rests on: branch name to how many of this project's
// sessions sit on it and how many of those are running right now.
function branchSessionMap(lanes, liveByStored) {
  const map = new Map()

  for (const lane of lanes || []) {
    for (const s of lane.sessions || []) {
      const name = String(s.git_branch || '').trim()

      if (!name) continue
      const row = map.get(name) || { count: 0, live: 0 }

      row.count += 1
      if (RUNNING.has(liveByStored.get(s.id))) row.live += 1
      map.set(name, row)
    }
  }

  return map
}

// Branch union: what /repo knows, what an open PR names, what a session sits on.
// A branch row can therefore be backed by any one of the three.
function ghBranchRows({ branches, prs, sessionsByBranch }) {
  const rows = new Map()
  const ensure = name => {
    const key = String(name || '').trim()

    if (!key) return null
    let row = rows.get(key)

    if (!row) {
      row = { name: key, local: null, pr: null, sessions: null, stamp: 0 }
      rows.set(key, row)
    }

    return row
  }

  for (const branch of branches || []) {
    const row = ensure(branch.name)

    if (!row) continue
    row.local = branch
    row.stamp = Math.max(row.stamp, ghMs(branch.last_commit_at))
  }

  for (const pr of prs || []) {
    const row = ensure(pr.head_ref)

    if (!row) continue
    if (!row.pr || ghMs(pr.updated_at) > ghMs(row.pr.updated_at)) row.pr = pr
    row.stamp = Math.max(row.stamp, ghMs(pr.updated_at))
  }

  for (const [name, sessions] of sessionsByBranch) {
    const row = ensure(name)

    if (row) row.sessions = sessions
  }

  const live = row => (row.sessions && row.sessions.live > 0 ? 1 : 0)
  const list = [...rows.values()]

  list.sort(
    (a, b) =>
      live(b) - live(a) ||
      (b.pr ? 1 : 0) - (a.pr ? 1 : 0) ||
      b.stamp - a.stamp ||
      a.name.localeCompare(b.name)
  )

  return list
}

// ── github pieces ────────────────────────────────────────────────────────────

function GhChecks({ checks }) {
  if (!checks) return null

  return jsx(Tip, {
    label: ghChecksTip(checks),
    children: jsx(StatusDot, { tone: CHECKS_TONE[checks.state] || 'muted' })
  })
}

function GhReview({ decision }) {
  const view = REVIEW_BADGES[decision]

  if (!view) return null

  return jsx(Badge, { size: 'xs', title: view.title, variant: view.variant, children: view.label })
}

function GhSessions({ row }) {
  if (!row || !row.count) return null

  return jsxs('span', {
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 4,
      flex: 'none',
      color: row.live > 0 ? 'var(--ui-accent)' : 'var(--ui-text-quaternary)'
    },
    children: [
      row.live > 0 ? jsx(StatusDot, { tone: 'good' }) : null,
      jsx('span', { children: `${row.count} ${row.count === 1 ? 'session' : 'sessions'}` })
    ]
  })
}

// Interactive when the caller can navigate (In flight), a plain tag when the
// target may not be in the open list (Shipped). No dead buttons either way.
function GhIssueChips({ numbers, onIssue, limit = 3 }) {
  const list = numbers || []

  if (list.length === 0) return null
  const shown = list.slice(0, limit)
  const rest = list.length - shown.length

  return jsxs('span', {
    style: { display: 'inline-flex', alignItems: 'center', gap: 4, flex: 'none' },
    children: [
      ...shown.map(n =>
        onIssue
          ? jsx(
              'button',
              {
                className: 'pcc-gh-chip',
                onClick: event => {
                  event.stopPropagation()
                  onIssue(n)
                },
                style: S.ghChipBtn,
                title: `Go to issue ${ghNumber(n)}`,
                type: 'button',
                children: ghNumber(n)
              },
              n
            )
          : jsx('span', { style: S.ghChipTag, title: `Issue ${ghNumber(n)}`, children: ghNumber(n) }, n)
      ),
      rest > 0 ? jsx('span', { style: { color: 'var(--ui-text-quaternary)' }, children: `+${rest}` }) : null
    ]
  })
}

function GhTotals({ counts }) {
  const rows = [
    ['open PRs', counts.prs_open],
    ['open issues', counts.issues_open],
    ['review ready', counts.review_ready],
    ['checks failing', counts.checks_failing],
    ['drafts', counts.drafts]
  ]

  return jsx('div', {
    style: S.ghTotals,
    children: rows.map(([label, value]) =>
      jsxs(
        'span',
        {
          style: S.ghTotal,
          children: [
            jsx('span', {
              style:
                label === 'checks failing' && value > 0
                  ? { ...S.ghTotalValue, color: 'var(--dt-destructive)' }
                  : S.ghTotalValue,
              children: value || 0
            }),
            label
          ]
        },
        label
      )
    )
  })
}

function GhSkeleton() {
  return jsx('div', {
    'aria-busy': 'true',
    style: { display: 'flex', flexDirection: 'column', paddingTop: 2 },
    children: [0, 1, 2].map(i =>
      jsxs(
        'div',
        {
          style: { display: 'flex', flexDirection: 'column', gap: 6, padding: '9px 0' },
          children: [
            jsx(Skeleton, { style: { height: 12, width: `${74 - i * 14}%` } }),
            jsx(Skeleton, { style: { height: 10, width: `${42 - i * 7}%` } })
          ]
        },
        i
      )
    )
  })
}

function GhFileChips({ files }) {
  if (!files || files.length === 0) return null
  const shown = files.slice(0, PR_FILES_SHOWN)

  return jsxs('span', {
    style: S.ghFiles,
    children: [
      ...shown.map(name =>
        jsx('span', { style: S.ghFile, title: name, children: String(name).split('/').pop() }, name)
      ),
      files.length > shown.length
        ? jsx('span', { style: { color: 'var(--ui-text-quaternary)' }, children: `+${files.length - shown.length} more` })
        : null
    ]
  })
}

// ── the issue / PR detail modal ─────────────────────────────────────────────
// A row is the summary; the modal is the conversation. Body, comments and review
// bodies all go through MessageTextContent, so fences, tables and images render
// with the app's own pipeline instead of a text approximation. #123 references
// are resolved in ONE batched /github/refs read per view and decorated into
// chips that push the next item onto the same stack, so Back is a pop and never
// a browser back.
//
// Every route here can 404 (the Python half may not be running yet): a missing
// refs route means plain text, a missing write route means the sentence it
// returned sits above the composer, and the thread still renders from the
// detail route alone.

const REVIEW_STATES = {
  APPROVED: { label: 'approved', variant: 'success' },
  CHANGES_REQUESTED: { label: 'changes requested', variant: 'destructive' },
  COMMENTED: { label: 'commented', variant: 'muted' },
  DISMISSED: { label: 'dismissed', variant: 'muted' }
}

function ghKindWord(kind) {
  return kind === 'pr' ? 'pull request' : 'issue'
}

function ghKindGlyph(kind) {
  return kind === 'pr' ? 'git-pull-request' : 'issues'
}

function ghStateTone(state) {
  return String(state || '').toLowerCase() === 'open' ? 'good' : 'muted'
}

function ghWhen(value) {
  const ms = ghMs(value)

  return ms ? fmtDateTime.format(new Date(ms)) : ''
}

function ghReviewView(state) {
  const key = String(state || '').toUpperCase()

  return REVIEW_STATES[key] || { label: key ? key.toLowerCase() : 'reviewed', variant: 'muted' }
}

// One scan of a body: a fresh regex per call, because a shared /g regex carries
// `lastIndex` as state and a view scans several bodies inside one render.
function ghRefScan(text) {
  const source = String(text || '')
  const re = new RegExp(GH_REF_RE.source, 'g')
  const out = []
  let hit = re.exec(source)

  while (hit) {
    out.push({ end: re.lastIndex, index: hit.index, number: Number(hit[1]) })
    hit = re.exec(source)
  }

  return out
}

// Every #number in a body, oldest sort first, de-duplicated: one `numbers=` for
// the whole view, so the modal makes one refs request and not one per body.
// Capped at the backend's own refs limit so the query string stays bounded.
function ghRefNumbers(texts) {
  const seen = new Set()

  for (const text of texts || []) {
    for (const hit of ghRefScan(text)) {
      if (hit.number > 0) seen.add(hit.number)
    }
  }

  return [...seen].sort((a, b) => a - b).slice(0, GH_REF_MAX)
}

function ghFlat(text, limit = GH_REF_CHARS) {
  const flat = String(text || '').replace(/\s+/g, ' ').trim()

  return flat.length > limit ? `${flat.slice(0, limit).trimEnd()}…` : flat
}

// decorateText hands back the rendered inline children of p/li/td, so a code
// span or a fenced block arrives already built and is passed through untouched.
// Only a string child is split, and only a number refs resolved becomes a chip.
function ghDecorateRefs(children, refs, onOpen, folder) {
  const split = text => {
    const out = []
    let last = 0

    for (const hit of ghRefScan(text)) {
      const entry = refs ? refs[String(hit.number)] : null

      if (!entry) continue
      if (hit.index > last) out.push(text.slice(last, hit.index))
      out.push(jsx(GhRefChip, { entry, folder, onOpen }, `ref-${hit.number}-${hit.index}`))
      last = hit.end
    }

    if (last === 0) return text
    if (last < text.length) out.push(text.slice(last))

    return out
  }

  const walk = node => {
    if (typeof node === 'string') return split(node)
    if (Array.isArray(node)) return node.map(child => walk(child))

    return node
  }

  return walk(children)
}

// A write route answers {ok:false, code, error}; a rejected request is folded
// into the same shape so the composer has exactly one sentence to show.
function ghWriteFailure(res, fallback) {
  const sentence = String((res && (res.error || res.detail)) || '').trim()

  return sentence || fallback
}

function ghReason(res) {
  const err = res && res.error

  return err && err.message ? err.message : String(err || 'the request failed')
}

// One chip: '#123 · Title', a state dot and an issue/PR glyph. Hover opens a
// popover (the pointer needs a grace period to cross the gap to it), and a click
// pushes that item onto the modal's stack.
function GhRefChip({ entry, folder, onOpen }) {
  const [open, setOpen] = useState(false)
  const grace = useRef(0)
  const kind = entry.kind === 'pr' ? 'pr' : 'issue'
  const number = Number(entry.number) || 0
  const title = String(entry.title || '')
  // The refs payload carries no body, so the hover card reads the same detail
  // endpoint the modal does — only while it is open, and cached with it.
  const query = useQuery({
    enabled: open && Boolean(folder) && number > 0,
    queryKey: [QUERY_ROOT, 'github', kind, folder, number],
    queryFn: () => ghRest(`/github/${kind}/${number}?path=${encodeURIComponent(folder)}`),
    retry: false,
    staleTime: 60000
  })
  const body = query.data && !query.data.error ? ghFlat(query.data.body) : ''
  const enter = () => {
    grace.current += 1
    setOpen(true)
  }
  const leave = () => {
    const at = grace.current + 1

    grace.current = at
    schedule(() => {
      if (grace.current === at) setOpen(false)
    }, 160)
  }

  return jsxs(Popover, {
    onOpenChange: next => (next ? enter() : setOpen(false)),
    open,
    children: [
      jsx(PopoverTrigger, {
        asChild: true,
        children: jsxs('button', {
          className: 'pcc-gh-ref',
          onClick: event => {
            event.preventDefault()
            event.stopPropagation()
            setOpen(false)
            onOpen(kind, number)
          },
          onMouseEnter: enter,
          onMouseLeave: leave,
          style: S.ghRefChip,
          title: `${ghNumber(number)} ${title}`,
          type: 'button',
          children: [
            jsx(StatusDot, { tone: ghStateTone(entry.state) }),
            jsx(Codicon, { name: ghKindGlyph(kind), size: '0.625rem', style: { flex: 'none' } }),
            jsx('span', { style: S.ghRefText, children: `${ghNumber(number)} · ${title || ghKindWord(kind)}` })
          ]
        })
      }),
      jsx(PopoverContent, {
        align: 'start',
        side: 'top',
        children: jsxs('div', {
          onMouseEnter: enter,
          onMouseLeave: leave,
          style: S.ghPop,
          children: [
            jsxs('div', {
              style: S.ghPopHead,
              children: [
                jsx(Codicon, { name: ghKindGlyph(kind), size: '0.6875rem', style: { flex: 'none' } }),
                jsx('span', { style: S.ghPopKind, children: ghKindWord(kind) }),
                jsx('span', { style: S.ghPopNum, children: ghNumber(number) }),
                jsx(Badge, {
                  children: String(entry.state || '').toLowerCase() === 'closed' ? 'closed' : 'open',
                  size: 'xs',
                  variant: ghStateTone(entry.state) === 'good' ? 'success' : 'muted'
                })
              ]
            }),
            jsx('div', { style: S.ghPopTitle, children: title || 'Untitled' }),
            jsxs('div', {
              style: S.ghPopMeta,
              children: [
                entry.author ? jsx('span', { children: entry.author }) : null,
                entry.updated_at ? jsx('span', { children: `updated ${ghAgo(entry.updated_at)}` }) : null
              ]
            }),
            body ? jsx('div', { style: S.ghPopBody, children: body }) : null,
            jsx(Button, {
              onClick: () => {
                setOpen(false)
                onOpen(kind, number)
              },
              size: 'xs',
              variant: 'outline',
              children: 'Open'
            })
          ]
        })
      })
    ]
  })
}

function GhMiniAvatar({ login }) {
  const name = String(login || 'ghost')

  return jsx(Blobatar, { name, size: 16, title: name })
}

function GhComment({ comment, decorate }) {
  const at = ghAgo(comment.created_at)

  return jsxs('div', {
    style: S.ghComment,
    children: [
      jsxs('div', {
        style: S.ghCommentHead,
        children: [
          jsx(GhMiniAvatar, { login: comment.author }),
          jsx('span', { style: S.ghActor, children: comment.author || 'ghost' }),
          at ? jsx('span', { style: S.ghTiny, title: ghWhen(comment.created_at), children: at }) : null
        ]
      }),
      comment.body
        ? jsx('div', { style: S.ghProse, children: jsx(MessageTextContent, { decorateText: decorate, media: true, text: String(comment.body) }) })
        : jsx('div', { style: S.ghQuiet, children: 'No comment text.' })
    ]
  })
}

function GhReviewEntry({ review, decorate }) {
  const view = ghReviewView(review.state)
  const at = ghAgo(review.submitted_at)

  return jsxs('div', {
    style: S.ghComment,
    children: [
      jsxs('div', {
        style: S.ghCommentHead,
        children: [
          jsx(GhMiniAvatar, { login: review.author }),
          jsx('span', { style: S.ghActor, children: review.author || 'ghost' }),
          jsx(Badge, { size: 'xs', variant: view.variant, children: view.label }),
          at ? jsx('span', { style: S.ghTiny, title: ghWhen(review.submitted_at), children: at }) : null
        ]
      }),
      review.body
        ? jsx('div', { style: S.ghProse, children: jsx(MessageTextContent, { decorateText: decorate, media: true, text: String(review.body) }) })
        : null
    ]
  })
}

function GhDiffStat({ detail }) {
  const parts = []
  const files = Number(detail.changed_files) || 0

  if (files) parts.push(`${files} ${files === 1 ? 'file' : 'files'}`)

  return jsxs('span', {
    style: S.ghDiff,
    children: [
      jsx('span', { style: { color: 'var(--ui-accent)' }, children: `+${Number(detail.additions) || 0}` }),
      jsx('span', { style: { color: 'var(--dt-destructive)' }, children: `-${Number(detail.deletions) || 0}` }),
      parts.length ? jsx('span', { children: parts.join(' · ') }) : null
    ]
  })
}

// The modal's meta line: who opened it, when it moved, labels, assignees, and —
// for a PR — refs, the diff size, checks and the review decision.
function GhModalMeta({ detail, kind, onOpen }) {
  const labels = Array.isArray(detail.labels) ? detail.labels : []
  const shown = labels.slice(0, GH_LABEL_CHIPS)
  const assignees = Array.isArray(detail.assignees) ? detail.assignees : []
  const created = ghWhen(detail.created_at)
  const updated = ghWhen(detail.updated_at)
  const checks = detail.checks
  const linked = kind === 'pr' ? detail.linked_issues || [] : detail.linked_prs || []
  const other = kind === 'pr' ? 'issue' : 'pr'

  return jsxs('div', {
    style: S.ghModalMeta,
    children: [
      jsx('span', { style: S.ghTiny, children: ghKindWord(kind) }),
      detail.author
        ? jsxs('span', {
            style: S.ghActorRow,
            children: [jsx(GhMiniAvatar, { login: detail.author }), jsx('span', { style: S.ghActor, children: detail.author })]
          })
        : null,
      created ? jsx('span', { style: S.ghTiny, title: ghAgo(detail.created_at), children: `opened ${created}` }) : null,
      updated ? jsx('span', { style: S.ghTiny, title: ghAgo(detail.updated_at), children: `updated ${updated}` }) : null,
      ...shown.map((label, i) => jsx('span', { style: S.ghLabel, children: String(label) }, `${label}-${i}`)),
      labels.length > shown.length ? jsx('span', { style: S.ghTiny, children: `+${labels.length - shown.length}` }) : null,
      assignees.length > 0
        ? jsxs('span', {
            style: S.ghActorRow,
            title: assignees.join(', '),
            children: [
              jsx(Codicon, { name: 'person', size: '0.6875rem', style: { flex: 'none' } }),
              jsx('span', { style: S.ghActor, children: assignees.join(', ') })
            ]
          })
        : null,
      kind === 'pr'
        ? jsxs('span', {
            style: S.ghActorRow,
            children: [
              jsx(BranchChip, { branch: detail.head_ref || 'head' }),
              jsx('span', { style: S.ghTiny, children: '→' }),
              jsx('span', { style: S.ghTiny, children: detail.base_ref || 'base' }),
              jsx(GhDiffStat, { detail })
            ]
          })
        : null,
      kind === 'pr' && (detail.files_changed || []).length > 0 ? jsx(GhFileChips, { files: detail.files_changed }) : null,
      kind === 'pr' && checks ? jsx('span', { style: S.ghTiny, children: CHECKS_TEXT[checks.state] || 'checks reported' }) : null,
      kind === 'pr' ? jsx(GhChecks, { checks }) : null,
      kind === 'pr' ? jsx(GhReview, { decision: detail.review_decision }) : null,
      linked.length > 0
        ? jsx(GhIssueChips, { numbers: linked, onIssue: number => (onOpen ? onOpen(other, number) : null) })
        : null
    ]
  })
}

// The loaded thread. Its own component so the refs query, the composer state and
// the mutations only exist once there is a detail to talk about — no hook is
// ever called conditionally.
function GhDetailThread({ detail, folder, kind, number, onOpen, owner, repo }) {
  const comments = Array.isArray(detail.comments) ? detail.comments : []
  const reviews = kind === 'pr' && Array.isArray(detail.reviews) ? detail.reviews : []
  const numbers = useMemo(
    () => ghRefNumbers([detail.body, ...comments.map(c => c.body), ...reviews.map(r => r.body)]),
    [detail]
  )
  const refQuery = useQuery({
    enabled: Boolean(folder) && numbers.length > 0,
    queryKey: [QUERY_ROOT, 'github', 'refs', folder, numbers.join(',')],
    // A missing route, a 404 or a refusal all mean the same thing here: no
    // chips, and the number stays the plain text the writer typed.
    queryFn: () =>
      ghRest(`/github/refs?path=${encodeURIComponent(folder)}&numbers=${numbers.join(',')}`).catch(() => null),
    retry: false,
    staleTime: 60000
  })
  const refsData = refQuery.data
  const refs = useMemo(() => (refsData && refsData.ok && refsData.refs ? refsData.refs : {}), [refsData])
  const decorate = useCallback(children => ghDecorateRefs(children, refs, onOpen, folder), [refs, onOpen, folder])

  const [draft, setDraft] = useState('')
  const [note, setNote] = useState('')
  const [confirming, setConfirming] = useState(false)
  const closed = String(detail.state || '').toLowerCase() !== 'open'
  const detailKey = [QUERY_ROOT, 'github', kind, folder, number]
  const path = `path=${encodeURIComponent(folder)}`

  const post = useMutation({
    mutationFn: text =>
      ghRest(`/github/${kind}/${number}/comment?${path}`, { body: { body: text }, method: 'POST' }).catch(error => ({
        error: ghReason(error),
        ok: false
      })),
    onSuccess: res => {
      if (res && res.ok) {
        setDraft('')
        setNote('')
        queryClient.invalidateQueries({ queryKey: detailKey })
        // The new comment may reference a number this view already asked about,
        // and a refs payload cached before the write would keep that chip missing
        // for its whole staleTime. Drop the refs reads with the thread.
        queryClient.invalidateQueries({ queryKey: [QUERY_ROOT, 'github', 'refs'] })

        return
      }
      setNote(ghWriteFailure(res, 'The comment did not post.'))
    }
  })

  const flip = useMutation({
    mutationFn: state =>
      ghRest(`/github/${kind}/${number}/state?${path}`, { body: { state }, method: 'POST' }).catch(error => ({
        error: ghReason(error),
        ok: false
      })),
    onSuccess: res => {
      setConfirming(false)
      if (res && res.ok) {
        setNote('')
        queryClient.invalidateQueries({ queryKey: detailKey })
        queryClient.invalidateQueries({ queryKey: [QUERY_ROOT, 'github', 'open', folder] })

        return
      }
      setNote(ghWriteFailure(res, 'The state did not change.'))
    }
  })

  const head = (icon, text, count) =>
    jsxs('div', {
      style: S.ghSectionHead,
      children: [
        jsx(Codicon, { name: icon, size: '0.75rem', style: { flex: 'none' } }),
        jsx('span', { children: text }),
        count != null ? jsx('span', { style: S.count, children: count }) : null
      ]
    })

  return jsxs('div', {
    style: S.ghThread,
    children: [
      jsxs('div', {
        style: S.ghSection,
        children: [
          head('book', 'Description'),
          detail.body
            ? jsx('div', {
                style: S.ghProse,
                children: jsx(MessageTextContent, { decorateText: decorate, media: true, text: String(detail.body) })
              })
            : jsx('div', { style: S.ghQuiet, children: 'No description.' })
        ]
      }),
      reviews.length > 0
        ? jsxs('div', {
            style: S.ghSection,
            children: [
              head('comment-discussion', 'Reviews', reviews.length),
              ...reviews.map((review, i) =>
                jsx(GhReviewEntry, { decorate, review }, `${review.author || 'x'}-${review.submitted_at || i}`)
              )
            ]
          })
        : null,
      jsxs('div', {
        style: S.ghSection,
        children: [
          head('comment', 'Comments', comments.length),
          comments.length > 0
            ? comments.map((comment, i) =>
                jsx(GhComment, { comment, decorate }, `${comment.id || comment.author || 'c'}-${comment.created_at || i}`)
              )
            : jsx('div', { style: S.ghQuiet, children: 'No comments yet.' })
        ]
      }),
      jsxs('div', {
        style: S.ghComposer,
        children: [
          note ? jsx('div', { role: 'alert', style: S.ghComposerError, children: note }) : null,
          jsx(Textarea, {
            'aria-label': `Comment on ${ghNumber(number)}`,
            disabled: post.isPending,
            onChange: event => setDraft(event.target.value),
            placeholder: `Leave a comment on ${ghNumber(number)}…`,
            rows: 3,
            value: draft
          }),
          jsxs('div', {
            style: S.ghComposerRow,
            children: [
              jsx(Button, {
                disabled: !draft.trim() || post.isPending,
                onClick: () => {
                  if (draft.trim()) post.mutate(draft)
                },
                size: 'xs',
                children: post.isPending ? 'Posting…' : 'Comment'
              }),
              confirming
                ? jsxs('span', {
                    style: S.ghComposerRow,
                    children: [
                      jsx(Button, {
                        disabled: flip.isPending,
                        onClick: () => flip.mutate('closed'),
                        size: 'xs',
                        variant: 'destructive',
                        children: flip.isPending ? 'Closing…' : 'Confirm close'
                      }),
                      jsx(Button, {
                        onClick: () => setConfirming(false),
                        size: 'xs',
                        variant: 'ghost',
                        children: 'Cancel'
                      })
                    ]
                  })
                : jsx(Button, {
                    disabled: flip.isPending || post.isPending,
                    onClick: () => {
                      if (closed) flip.mutate('open')
                      else setConfirming(true)
                    },
                    size: 'xs',
                    variant: 'outline',
                    children: flip.isPending ? 'Working…' : closed ? 'Reopen' : 'Close'
                  }),
              jsx('span', { style: S.grow }),
              jsx(StartSessionButton, {
                folder,
                issue: { ...detail, html_url: detail.url },
                owner,
                repo
              })
            ]
          })
        ]
      })
    ]
  })
}

function GhDetailModal({ depth, entry, folder, onBack, onClose, onOpen, owner, repo }) {
  const kind = entry.kind === 'pr' ? 'pr' : 'issue'
  const number = Number(entry.number) || 0
  const query = useQuery({
    enabled: Boolean(folder) && number > 0,
    queryKey: [QUERY_ROOT, 'github', kind, folder, number],
    queryFn: () => ghRest(`/github/${kind}/${number}?path=${encodeURIComponent(folder)}`),
    retry: false,
    staleTime: 60000
  })
  const payload = query.data || null
  const failed = payload && ((payload.code && payload.code !== 'ok') || payload.error) ? payload : null
  const detail = payload && !failed ? payload : null
  const title = (detail && detail.title) || `${ghKindWord(kind)} ${ghNumber(number)}`
  const state = detail ? String(detail.state || 'open').toLowerCase() : ''
  const url = detail && detail.url ? String(detail.url) : ''
  const canOpenExternal = Boolean(pluginCtx && pluginCtx.os && typeof pluginCtx.os.openExternal === 'function')
  const openExternal = () => {
    if (!url || !canOpenExternal) return
    Promise.resolve(pluginCtx.os.openExternal(url)).then(ok => {
      if (ok === false) host.notify({ kind: 'warning', message: 'Could not open the browser.' })
    })
  }

  return jsxs(Dialog, {
    onOpenChange: next => {
      if (!next) onClose()
    },
    open: true,
    children: [
      jsxs(DialogContent, {
        className: 'max-w-3xl',
        children: [
          jsxs(DialogHeader, {
            style: S.ghModalHead,
            children: [
              jsxs('div', {
                style: S.ghModalTitleRow,
                children: [
                  depth > 1 && onBack
                    ? jsx('button', {
                        'aria-label': 'Back',
                        className: 'pcc-gh-back',
                        onClick: onBack,
                        style: S.ghBackBtn,
                        title: 'Back',
                        type: 'button',
                        children: jsx(Codicon, { name: 'chevron-left', size: '0.875rem' })
                      })
                    : jsx(Codicon, {
                        name: ghKindGlyph(kind),
                        size: '0.875rem',
                        style: { flex: 'none', color: 'var(--ui-text-secondary)', paddingTop: 2 }
                      }),
                  jsx('span', { style: S.ghModalNum, children: ghNumber(number) }),
                  jsx(DialogTitle, { style: S.ghModalTitle, children: title }),
                  detail ? jsx(Badge, { size: 'xs', variant: state === 'open' ? 'success' : 'muted', children: state || 'unknown' }) : null,
                  url && canOpenExternal
                    ? jsxs('button', {
                        className: 'pcc-gh-chip',
                        onClick: openExternal,
                        style: S.ghChipBtn,
                        title: 'View on GitHub',
                        type: 'button',
                        children: ['View on GitHub', jsx(Codicon, { name: 'link-external', size: '0.6875rem' })]
                      })
                    : null
                ]
              }),
              detail ? jsx(GhModalMeta, { detail, kind, onOpen }) : null
            ]
          }),
          query.isLoading && !detail
            ? jsxs('div', {
                style: S.ghSection,
                children: [
                  jsx(Skeleton, { style: { height: 12, width: '82%' } }),
                  jsx(Skeleton, { style: { height: 12, width: '64%' } }),
                  jsx(Skeleton, { style: { height: 12, width: '48%' } })
                ]
              })
            : null,
          query.isError && !detail ? jsx('div', { style: S.ghQuiet, children: `Could not load this ${ghKindWord(kind)}.` }) : null,
          failed ? jsx(GhFailure, { data: failed, folder }) : null,
          detail ? jsx(GhDetailThread, { detail, folder, kind, number, onOpen, owner, repo }) : null
        ]
      })
    ]
  })
}

function GhPrRow({ pr, sessions, onIssue, onOpen, flash }) {
  const linked = pr.linked_issues || []
  const look = ghPrLook(pr)

  return jsxs('div', {
    'data-pr': pr.number,
    className: `pcc-gh-row${flash ? ' pcc-gh-focus' : ''}`,
    style: S.ghRow,
    children: [
      jsxs('div', {
        style: S.ghLine,
        children: [
          jsxs('button', {
            className: 'pcc-gh-title',
            onClick: onOpen,
            style: S.ghTitleBtn,
            title: 'Open the pull request thread',
            type: 'button',
            children: [
              jsx(Codicon, {
                name: look.name,
                size: '0.875rem',
                style: { color: look.color, flex: 'none', paddingTop: 2 }
              }),
              jsx('span', { style: S.ghNum, children: ghNumber(pr.number) }),
              jsx('span', { style: S.ghTitle, children: pr.title || 'Untitled pull request' }),
              jsx(Codicon, {
                name: 'chevron-right',
                size: '0.75rem',
                style: { flex: 'none', color: 'var(--ui-text-quaternary)', paddingTop: 2 }
              })
            ]
          }),
          jsx('span', { style: S.ghAge, children: ghAgo(pr.updated_at) })
        ]
      }),
      jsxs('div', {
        style: S.ghMeta,
        children: [
          pr.head_ref ? jsx(BranchChip, { branch: pr.head_ref }) : null,
          pr.draft ? jsx(Badge, { size: 'xs', variant: 'muted', children: 'draft' }) : null,
          jsx(GhChecks, { checks: pr.checks }),
          jsx(GhReview, { decision: pr.review_decision }),
          linked.length > 0 ? jsx(GhIssueChips, { numbers: linked, onIssue }) : null,
          jsx(GhSessions, { row: sessions }),
          jsx('span', { style: S.grow }),
          pr.additions || pr.deletions
            ? jsx('span', {
                style: { fontVariantNumeric: 'tabular-nums' },
                children: `+${pr.additions || 0} -${pr.deletions || 0}`
              })
            : null
        ]
      })
    ]
  })
}

function GhPrSubRow({ pr, sessions, onOpen }) {
  const look = ghPrLook(pr)

  return jsxs('button', {
    className: 'pcc-gh-sub-btn',
    onClick: onOpen,
    style: S.ghSubBtn,
    title: pr.title,
    type: 'button',
    children: [
      jsx(Codicon, { name: look.name, size: '0.75rem', style: { color: look.color, flex: 'none' } }),
      jsx('span', { style: S.ghNum, children: ghNumber(pr.number) }),
      jsx('span', { style: S.ghSubTitle, children: pr.title || 'Untitled pull request' }),
      jsx(GhSessions, { row: sessions }),
      jsx(GhChecks, { checks: pr.checks }),
      jsx(GhReview, { decision: pr.review_decision })
    ]
  })
}

// ── a chat about an issue ────────────────────────────────────────────────────
// The GitHub read is the only surface that knows an issue's repo and number, so
// a click here is the shortest path from "issue" to "a chat about it": the app
// opens a fresh chat DRAFT, the issue lands in that composer as ordinary,
// editable text, and the caret sits on the blank line underneath it. This path
// creates no session, sends no turn and writes no board link — there is no
// stored id at click time, so the link row belongs to the hooks on the first
// turn. Nothing is submitted: the user writes the question and presses Enter.

function issueUrl(owner, repo, number) {
  if (!owner || !repo) return ''

  return `https://github.com/${owner}/${repo}/issues/${number}`
}

// Entry points only. The agent reads the issue and its pull requests with its own
// tools, so a truncated body here would only invite it to answer from the
// truncation. Header, one context line, one instruction, then two newlines so
// the caret lands on an empty last line waiting for the user's question.
function issueDraftText({ labels, number, owner, repo, state, title, url }) {
  const link = url || issueUrl(owner, repo, number)
  const name = String(title || '').trim().slice(0, 120)
  const tags = (Array.isArray(labels) ? labels : [])
    .map(label => (typeof label === 'string' ? label : (label && label.name) || ''))
    .filter(Boolean)
    .slice(0, 3)
  const meta = [state ? `state: ${state}` : '', tags.length ? `labels: ${tags.join(', ')}` : '']
    .filter(Boolean)
    .join(' · ')
  const lines = [`#${number} ${name}`.trim(), link, owner && repo ? `repo: ${owner}/${repo}` : '', meta].filter(Boolean)

  return `${lines.join('\n')}\n\nRead this issue and its linked pull requests with gh before you start. Move this card with pcc_set_status.\n\n`
}

// The app's own "new chat" door: a fresh draft in this profile with no session
// id yet. `host.newChat` is the documented verb; the `session.new` app action is
// the fallback for a build that predates it.
function openNewChat() {
  const profile = profileParam().profile || null

  if (host && typeof host.newChat === 'function') {
    host.newChat(profile)

    return
  }

  if (pluginCtx && typeof pluginCtx.runAction === 'function') pluginCtx.runAction('session.new')
}

// Write the issue into the fresh chat's composer. `'new'` is the draft that has
// no session id yet, answered only by the primary pane while it shows no
// session; `null` is the composer the user is typing in. Both fail closed until
// the new chat has painted, so the write runs on a bounded retry schedule and
// the first address that answers true wins. submit() is never called: the text
// is the user's to edit.
function seedDraftText(text) {
  if (!pluginCtx || !host.composer || typeof host.composer.setDraft !== 'function') return Promise.resolve(false)

  const write = () =>
    Promise.resolve(host.composer.setDraft('new', text))
      .then(ok => (ok ? true : host.composer.setDraft(null, text)))
      .catch(() => host.composer.setDraft(null, text))
      .catch(() => false)

  return new Promise(resolve => {
    let attempt = 0

    const run = () => {
      if (pluginDisposed) {
        resolve(false)

        return
      }

      write().then(ok => {
        if (ok) {
          // The caret belongs in the input, on the blank line waiting under the
          // issue: that is where the user's question goes.
          if (typeof host.composer.focus === 'function') host.composer.focus(null)
          resolve(true)

          return
        }

        if (attempt >= DRAFT_RETRY_MS.length) {
          resolve(false)

          return
        }

        // A disposed plugin drops the timer; the caller must still hear back.
        if (!schedule(run, DRAFT_RETRY_MS[attempt])) {
          resolve(false)

          return
        }

        attempt += 1
      })
    }

    run()
  })
}

// The anchored chat is already showing, so `'new'` (which only answers while the primary
// shows NO session) resolves false. Focus the live composer first, then write through the
// retrying helper, which tries `'new'` and falls back to `null` on false as well as throw.
function placeDraftInOpenChat(text) {
  try {
    if (host.composer && typeof host.composer.focus === 'function') host.composer.focus(null)
  } catch (err) {
    // Focus is cosmetic; the write below is what matters.
  }

  const read =
    host.composer && typeof host.composer.getDraft === 'function'
      ? Promise.resolve(host.composer.getDraft(null)).catch(() => null)
      : Promise.resolve(null)

  return read
    .then(current => (current != null && String(current).trim() ? true : seedDraftText(text)))
    .catch(() => seedDraftText(text))
}

function openIssueDraft({ folder, issue, owner, repo }) {
  const text = issueDraftText({
    labels: issue.labels,
    number: Number(issue.number) || 0,
    owner,
    repo,
    state: issue.state,
    title: issue.title,
    url: issue.html_url
  })

  // The anchored door: the same call the sidebar's project "+" makes, so the
  // new chat's cwd is this project's folder and the text is already in its
  // composer. Feature-detected, because it is a newer SDK method.
  if (folder && host && typeof host.newChatInWorkspace === 'function') {
    if (host.newChatInWorkspace(folder, text)) {
      schedule(() => {
        placeDraftInOpenChat(text).catch(() => false)
      }, 400)

      return Promise.resolve(true)
    }
  }

  // Older shell: a detached draft, still with the text, but the cwd is the
  // app's workspace rather than this project.
  openNewChat()

  return seedDraftText(text)
}

function StartSessionButton({ folder, issue, owner, repo }) {
  const [busy, setBusy] = useState(false)

  if (!folder || !issue || !Number(issue.number)) return null

  const start = event => {
    if (event && event.stopPropagation) event.stopPropagation()
    if (busy) return
    setBusy(true)
    openIssueDraft({ folder, issue, owner, repo })
      .then(ok => {
        // The chat is open either way; one quiet note only when the text never
        // reached the composer, so the click is never a silent no-op.
        if (!ok) host.notify({ kind: 'info', message: 'New chat opened, but the issue text did not reach the composer.' })
      })
      .catch(err =>
        host.notify({
          kind: 'error',
          message: `Could not open a chat: ${err && err.message ? err.message : err}`
        })
      )
      .then(() => setBusy(false))
  }

  return jsxs(Button, {
    'aria-label': `Start a new chat about issue ${issue.number}`,
    disabled: busy,
    onClick: start,
    size: 'xs',
    title: 'Open a new chat with this issue in the composer',
    variant: 'outline',
    children: [
      jsx(Codicon, { name: busy ? 'loading' : 'comment-discussion', size: '0.75rem', spinning: busy }),
      busy ? 'Opening' : 'New chat'
    ]
  })
}

function GhIssueRow({ folder, issue, prs, sessionsByBranch, onOpen, onOpenPr, flash, owner, repo }) {
  const labels = issue.labels || []
  const shownLabels = labels.slice(0, 3)
  const restLabels = labels.length - shownLabels.length
  const assignees = issue.assignees || []

  return jsxs('div', {
    'data-issue': issue.number,
    className: `pcc-gh-row${flash ? ' pcc-gh-focus' : ''}`,
    style: S.ghRow,
    children: [
      jsxs('div', {
        style: S.ghLine,
        children: [
          jsxs('button', {
            className: 'pcc-gh-title',
            onClick: onOpen,
            style: S.ghTitleBtn,
            title: 'Open the issue thread',
            type: 'button',
            children: [
              jsx(Codicon, {
                name: 'issues',
                size: '0.875rem',
                style: { color: 'var(--ui-text-secondary)', flex: 'none', paddingTop: 2 }
              }),
              jsx('span', { style: S.ghNum, children: ghNumber(issue.number) }),
              jsx('span', { style: S.ghTitle, children: issue.title || 'Untitled issue' }),
              jsx(Codicon, {
                name: 'chevron-right',
                size: '0.75rem',
                style: { flex: 'none', color: 'var(--ui-text-quaternary)', paddingTop: 2 }
              })
            ]
          }),
          jsx('span', { style: S.ghAge, children: ghAgo(issue.updated_at) })
        ]
      }),
      jsxs('div', {
        style: S.ghMeta,
        children: [
          shownLabels.map((label, i) => jsx('span', { style: S.ghLabel, children: label }, `${label}-${i}`)),
          restLabels > 0 ? jsx('span', { style: { color: 'var(--ui-text-quaternary)' }, children: `+${restLabels}` }) : null,
          assignees.length > 0
            ? jsx(Tip, {
                label: assignees.join(', '),
                children: jsx('span', { style: S.ghInitials, children: ghInitials(assignees[0]) })
              })
            : null,
          issue.comments
            ? jsxs('span', {
                style: { display: 'inline-flex', alignItems: 'center', gap: 3 },
                children: [jsx(Codicon, { name: 'comment', size: '0.6875rem' }), issue.comments]
              })
            : null,
          jsx('span', { style: S.grow }),
          // The same action sits in the modal footer, so it is always the same
          // one click from either place.
          jsx(StartSessionButton, { folder, issue, owner, repo }),
          prs.length > 0
            ? jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: `${prs.length} ${prs.length === 1 ? 'PR' : 'PRs'}` })
            : jsx('span', { children: 'no PR' })
        ]
      }),
      prs.length > 0
        ? jsx('div', {
            style: { display: 'flex', flexDirection: 'column' },
            children: prs
              .slice(0, 3)
              .map(pr =>
                jsx(
                  GhPrSubRow,
                  { onOpen: () => onOpenPr(pr.number), pr, sessions: sessionsByBranch.get(pr.head_ref) },
                  pr.number
                )
              )
          })
        : null
    ]
  })
}

function GhBranchRow({ row, onOpenPr, onIssue }) {
  const local = row.local
  const pr = row.pr
  const running = row.sessions && row.sessions.live > 0

  return jsxs('div', {
    className: 'pcc-gh-row',
    style: S.ghRow,
    children: [
      running ? jsx(ArcRing, { radius: '0' }) : null,
      jsxs('div', {
        style: S.ghLine,
        children: [
          jsxs('span', {
            style: { display: 'flex', alignItems: 'center', gap: 7, flex: '1 1 0', minWidth: 0, paddingTop: 1 },
            children: [
              jsx(Codicon, {
                name: 'git-branch',
                size: '0.875rem',
                style: { flex: 'none', color: running ? 'var(--ui-accent)' : 'var(--ui-text-secondary)' }
              }),
              jsx('span', { style: S.ghBranchName, title: row.name, children: row.name })
            ]
          }),
          jsx(GhSessions, { row: row.sessions })
        ]
      }),
      jsxs('div', {
        style: { ...S.ghMeta, paddingLeft: 21 },
        children: [
          local && local.upstream ? jsx('span', { style: S.ghTiny, children: local.upstream }) : null,
          local && local.ahead ? jsx('span', { style: S.ghTiny, title: `${local.ahead} commits ahead of upstream`, children: `ahead ${local.ahead}` }) : null,
          local && local.behind ? jsx('span', { style: S.ghTiny, title: `${local.behind} commits behind upstream`, children: `behind ${local.behind}` }) : null,
          pr
            ? jsx('button', {
                className: 'pcc-gh-chip',
                onClick: () => onOpenPr(pr.number),
                style: S.ghChipBtn,
                title: pr.title,
                type: 'button',
                children: `${ghNumber(pr.number)} PR`
              })
            : jsx('span', { style: S.ghTiny, children: 'no PR' }),
          pr && (pr.linked_issues || []).length > 0 ? jsx(GhIssueChips, { numbers: pr.linked_issues, onIssue }) : null,
          local && local.subject ? jsx('span', { style: S.ghSubject, title: local.subject, children: local.subject }) : null,
          local && local.last_commit_at ? jsx('span', { style: S.ghAge, children: ghAgo(local.last_commit_at) }) : null
        ]
      })
    ]
  })
}

function GhShippedRow({ pr, onOpen }) {
  const linked = pr.linked_issues || []

  return jsxs('div', {
    'data-pr': pr.number,
    className: 'pcc-gh-row',
    style: S.ghRow,
    children: [
      jsxs('div', {
        style: S.ghLine,
        children: [
          jsxs('button', {
            className: 'pcc-gh-title',
            onClick: onOpen,
            style: S.ghTitleBtn,
            title: 'Open the pull request thread',
            type: 'button',
            children: [
              jsx(Codicon, {
                name: 'git-merge',
                size: '0.875rem',
                style: { color: 'var(--ui-accent)', flex: 'none', paddingTop: 2 }
              }),
              jsx('span', { style: S.ghNum, children: ghNumber(pr.number) }),
              jsx('span', { style: { ...S.ghTitle, WebkitLineClamp: 1 }, children: pr.title || 'Untitled pull request' })
            ]
          }),
          linked.length > 0 ? jsx(GhIssueChips, { numbers: linked }) : null,
          jsx('span', { style: S.ghAge, children: ghAgo(pr.updated_at) })
        ]
      })
    ]
  })
}

function GithubSection({ folder, projectId, sessionsByBranch }) {
  const preset = useValue($ghPreset)
  // The detail modal's navigation stack: one {kind, number} per open thread, last
  // on screen. A #123 chip pushes, Back pops, closing empties it, and the whole
  // thing lives here so a row, a chip and a review all reach the same door.
  const [stack, setStack] = useState([])
  const [focus, setFocus] = useState(null)
  const [flash, setFlash] = useState(null)
  const [spin, setSpin] = useState(false)
  const [diagOpen, setDiagOpen] = useState(false)
  const forceRef = useRef(0)
  const rootRef = useRef(null)
  // A poke is an atom, not an event: a section that mounts after one (navigating
  // into the project page) must not replay that poke's forced read on mount.
  const mountedAt = useRef(Date.now())
  const path = `path=${encodeURIComponent(folder || '')}`
  const fresh = () => (forceRef.current ? '&refresh=1' : '')
  // TanStack's refetch() ignores `enabled`, so a closed "shipped" query would
  // still make a forced read if something called it: only refetch what the
  // preset is actually showing.
  const refetchShipped = () => {
    if (preset === 'shipped' && folder) shippedQuery.refetch()
  }

  const repoQuery = useQuery({
    enabled: Boolean(folder),
    queryKey: [QUERY_ROOT, 'repo', folder],
    queryFn: () => ghRest(`/repo?${path}`),
    retry: false,
    staleTime: 20000
  })

  const openQuery = useQuery({
    enabled: Boolean(folder),
    queryKey: [QUERY_ROOT, 'github', 'open', folder],
    queryFn: () => ghRest(`/github?${path}&state=open&limit=${GH_LIMIT}${fresh()}`),
    retry: false,
    staleTime: 30000
  })

  const shippedQuery = useQuery({
    enabled: Boolean(folder) && preset === 'shipped',
    queryKey: [QUERY_ROOT, 'github', 'closed', folder],
    queryFn: () => ghRest(`/github?${path}&state=closed&limit=${GH_LIMIT}${fresh()}`),
    retry: false,
    staleTime: 60000
  })

  // Only runs when the panel is open: /diagnose probes GitHub once per available
  // credential, so it is not something a passing board read should pay for.
  const diagQuery = useQuery({
    enabled: Boolean(folder) && diagOpen,
    queryKey: [QUERY_ROOT, 'diagnose', folder],
    queryFn: () => ghRest(`/diagnose?path=${encodeURIComponent(folder || '')}`),
    retry: false,
    staleTime: 5000
  })

  const data = openQuery.data || null
  const repo = repoQuery.data || null
  const issues = useMemo(() => (data && data.issues) || [], [data])
  const prs = useMemo(() => (data && data.prs) || [], [data])
  const linkedPrs = useMemo(() => ghLinkedPrMap(prs), [prs])
  const branchRows = useMemo(
    () => ghBranchRows({ branches: (repo && repo.branches) || [], prs, sessionsByBranch }),
    [repo, prs, sessionsByBranch]
  )

  // Scroll the requested issue or pull request into view once the preset that
  // renders it has painted, then flash it so the jump is legible.
  useEffect(() => {
    if (!focus) return undefined
    const root = rootRef.current
    const selector =
      focus.kind === 'issue' ? `[data-issue="${Number(focus.number) || 0}"]` : `[data-pr="${Number(focus.number) || 0}"]`
    const el = root && root.querySelector(selector)

    if (!el) return undefined
    if (el.scrollIntoView) {
      el.scrollIntoView({ block: 'center', behavior: prefersReducedMotion() ? 'auto' : 'smooth' })
    }

    setFlash({ kind: focus.kind, number: focus.number })
    schedule(() => setFlash(null), 1500)

    return undefined
  }, [focus, preset])

  const refresh = () => {
    forceRef.current = 1
    setSpin(true)
    openQuery.refetch()
    refetchShipped()
    schedule(() => {
      forceRef.current = 0
      setSpin(false)
    }, 1200)
  }

  // A board.changed event naming a pull request for THIS project: the same
  // forced read the refresh button does, plus the flash the issue/PR jumps
  // already use. `poke.at` makes two pokes for the same row distinct, and a poke
  // older than this mount is history — the atom outlives the section.
  const poke = useValue($ghPoke)
  const pokeAt = poke && poke.at

  useEffect(() => {
    if (!pokeAt || !poke || !projectId || poke.project_id !== projectId) return undefined
    if (pokeAt <= mountedAt.current) return undefined

    forceRef.current = 1
    setSpin(true)
    openQuery.refetch()
    refetchShipped()
    setFlash({ kind: 'pr', number: poke.pr })

    schedule(() => {
      forceRef.current = 0
      setSpin(false)
    }, 1200)
    schedule(() => setFlash(null), 2000)

    return undefined
  }, [pokeAt])

  const openEntry = useCallback((kind, number) => {
    const entry = { kind: kind === 'pr' ? 'pr' : 'issue', number: Number(number) || 0 }

    if (!entry.number) return
    setStack(prev => {
      const last = prev[prev.length - 1]

      return last && last.kind === entry.kind && last.number === entry.number ? prev : [...prev, entry]
    })
  }, [])
  const backEntry = useCallback(() => setStack(prev => prev.slice(0, -1)), [])
  const closeEntry = useCallback(() => setStack([]), [])
  const currentEntry = stack.length > 0 ? stack[stack.length - 1] : null

  // Jumps from a row's linked chips stay list jumps (they also retarget the
  // preset); only the modal's own chips push onto its stack.
  const goIssue = number => {
    writeGhPreset('issue')
    setFocus({ kind: 'issue', number, at: Date.now() })
  }

  const goPr = number => {
    writeGhPreset('flight')
    setFocus({ kind: 'pr', number, at: Date.now() })
  }

  const flashFor = (kind, number) => Boolean(flash && flash.kind === kind && flash.number === number)

  const ownerRepo = data && data.owner && data.repo ? `${data.owner}/${data.repo}` : repo && repo.owner && repo.repo ? `${repo.owner}/${repo.repo}` : ''
  // The issue rows need the two halves separately to build a URL and a link row.
  const ownerName = (data && data.owner) || (repo && repo.owner) || ''
  const repoName = (data && data.repo) || (repo && repo.repo) || ''
  const repoUrl = (data && data.url) || (ownerRepo ? `https://github.com/${ownerRepo}` : '')
  const canOpenExternal = Boolean(pluginCtx && pluginCtx.os && typeof pluginCtx.os.openExternal === 'function')
  const openRepo = () => {
    if (!repoUrl) return
    Promise.resolve(pluginCtx.os.openExternal(repoUrl)).then(ok => {
      if (ok === false) host.notify({ kind: 'warning', message: 'Could not open the browser.' })
    })
  }

  const boardFailed = Boolean(openQuery.error) && !data
  const loading = openQuery.isLoading && !data
  // An older backend reports only `error`; a current one adds a code. Either way
  // a normal condition renders one quiet line, never the error box.
  const boardFailure = data && ((data.code && data.code !== 'ok') || data.error) ? data : null
  const withoutPr = issues.length - issues.filter(i => linkedPrs.has(i.number)).length

  const groupLabel = (text, count) =>
    jsxs('div', {
      style: S.ghGroup,
      children: [jsx('span', { children: text }), count != null ? jsx('span', { style: S.count, children: count }) : null]
    })

  const flightView = () => {
    const rows = [...prs].sort((a, b) => ghMs(b.updated_at) - ghMs(a.updated_at))

    return jsxs('div', {
      style: S.ghList,
      children: [
        rows.length === 0
          ? jsx('div', { style: S.ghQuiet, children: 'No open pull requests' })
          : rows.map(pr =>
              jsx(
                GhPrRow,
                {
                  flash: flashFor('pr', pr.number),
                  onIssue: goIssue,
                  onOpen: () => openEntry('pr', pr.number),
                  pr,
                  sessions: sessionsByBranch.get(pr.head_ref)
                },
                pr.number
              )
            ),
        withoutPr > 0
          ? jsx('div', {
              style: { paddingTop: 6 },
              children: jsxs('button', {
                className: 'pcc-gh-linkbtn',
                onClick: () => writeGhPreset('issue'),
                style: S.ghLinkBtn,
                type: 'button',
                children: [
                  `${withoutPr} open ${withoutPr === 1 ? 'issue' : 'issues'} with no pull request`,
                  jsx(Codicon, { name: 'arrow-right', size: '0.75rem' })
                ]
              })
            })
          : null
      ]
    })
  }

  const issueView = () => {
    const rows = [...issues].sort(
      (a, b) =>
        (linkedPrs.has(b.number) ? 1 : 0) - (linkedPrs.has(a.number) ? 1 : 0) || ghMs(b.updated_at) - ghMs(a.updated_at)
    )

    return jsxs('div', {
      style: S.ghList,
      children: [
        rows.length === 0
          ? jsx('div', { style: S.ghQuiet, children: 'No open issues' })
          : rows.map(issue =>
              jsx(
                GhIssueRow,
                {
                  flash: flashFor('issue', issue.number),
                  folder,
                  issue,
                  onOpen: () => openEntry('issue', issue.number),
                  onOpenPr: goPr,
                  owner: ownerName,
                  prs: linkedPrs.get(issue.number) || [],
                  repo: repoName,
                  sessionsByBranch
                },
                issue.number
              )
            )
      ]
    })
  }

  const branchView = () => {
    const rows = branchRows.slice(0, BRANCH_CAP)
    const hidden = branchRows.length - rows.length

    return jsxs('div', {
      style: S.ghList,
      children: [
        rows.length === 0
          ? jsx('div', { style: S.ghQuiet, children: 'No branches to join against yet' })
          : rows.map(row => jsx(GhBranchRow, { onIssue: goIssue, onOpenPr: goPr, row }, row.name)),
        hidden > 0 ? jsx('div', { style: S.ghQuiet, children: `${hidden} more branches` }) : null
      ]
    })
  }

  const unlinkedView = () => {
    const branchEntries = [...sessionsByBranch.entries()]
      .filter(([name]) => !prs.some(pr => pr.head_ref === name))
      .sort(
        (a, b) =>
          (b[1].live > 0 ? 1 : 0) - (a[1].live > 0 ? 1 : 0) || b[1].count - a[1].count || a[0].localeCompare(b[0])
      )
    const prsNoIssue = prs.filter(pr => (pr.linked_issues || []).length === 0)

    return jsxs('div', {
      style: S.ghList,
      children: [
        groupLabel('Sessions with no pull request', branchEntries.length),
        branchEntries.length === 0
          ? jsx('div', { style: S.ghQuiet, children: 'Every branch with sessions has a pull request' })
          : branchEntries.map(([name, row]) =>
              jsxs(
                'div',
                {
                  style: S.ghRow,
                  children: [
                    jsxs('div', {
                      style: { ...S.ghLine, padding: '2px 0' },
                      children: [
                        jsx(Codicon, {
                          name: 'git-branch',
                          size: '0.8125rem',
                          style: { flex: 'none', color: 'var(--ui-text-secondary)' }
                        }),
                        jsx('span', { style: S.ghBranchName, title: name, children: name }),
                        jsx(GhSessions, { row }),
                        jsx('span', { style: S.grow }),
                        jsx('span', { style: S.ghTiny, children: 'no PR' })
                      ]
                    })
                  ]
                },
                name
              )
            ),
        groupLabel('Pull requests with no issue', prsNoIssue.length),
        prsNoIssue.length === 0
          ? jsx('div', { style: S.ghQuiet, children: 'Every open pull request names an issue' })
          : prsNoIssue.map(pr =>
              jsx(
                GhPrRow,
                {
                  flash: flashFor('pr', pr.number),
                  onIssue: goIssue,
                  onOpen: () => openEntry('pr', pr.number),
                  pr,
                  sessions: sessionsByBranch.get(pr.head_ref)
                },
                pr.number
              )
            )
      ]
    })
  }

  const shippedView = () => {
    if (shippedQuery.isLoading && !shippedQuery.data) return jsx(GhSkeleton, {})
    if (shippedQuery.error && !shippedQuery.data) {
      return jsx('div', { style: S.ghQuiet, children: 'Could not load merged pull requests.' })
    }

    const shipped = shippedQuery.data || null

    if (shipped && ((shipped.code && shipped.code !== 'ok') || shipped.error)) {
      return jsx(GhFailure, { data: shipped, folder })
    }
    const merged = ((shipped && shipped.prs) || [])
      .filter(pr => pr.merged)
      .sort((a, b) => ghMs(b.updated_at) - ghMs(a.updated_at))
    const rows = merged.slice(0, SHIPPED_CAP)
    const hidden = merged.length - rows.length

    return jsxs('div', {
      style: S.ghList,
      children: [
        rows.length === 0
          ? jsx('div', { style: S.ghQuiet, children: 'No merged pull requests in the recent window' })
          : rows.map(pr =>
              jsx(
                GhShippedRow,
                {
                  onOpen: () => openEntry('pr', pr.number),
                  pr
                },
                pr.number
              )
            ),
        hidden > 0 ? jsx('div', { style: S.ghQuiet, children: `showing ${rows.length} of ${merged.length}` }) : null
      ]
    })
  }

  const diagnoseView = () => {
    if (!diagOpen) return null
    const head = text =>
      jsxs('div', {
        style: S.ghDiagHead,
        children: [jsx(Codicon, { name: 'pulse', size: '0.75rem' }), text]
      })

    if (diagQuery.isLoading && !diagQuery.data) {
      return jsxs('div', {
        style: S.ghDiag,
        children: [head('Diagnose'), jsx('span', { style: S.ghDiagNote, children: 'Reading the folder, git and both credentials...' })]
      })
    }
    if (diagQuery.error && !diagQuery.data) {
      const message = diagQuery.error.message || String(diagQuery.error)

      return jsxs('div', {
        style: S.ghDiag,
        children: [head('Diagnose'), jsx('span', { style: S.ghDiagNote, children: `Could not diagnose this folder: ${message}` })]
      })
    }

    const info = diagQuery.data || {}
    const folderInfo = info.folder || {}
    const git = info.git || {}
    const token = info.token || {}
    const ghInfo = info.gh || {}
    const probe = info.probe || {}
    const rows = []
    const add = (key, value) => {
      if (value != null && value !== '') rows.push([key, String(value)])
    }

    add('Folder', folderInfo.exists ? (folderInfo.is_dir ? 'exists' : 'not a directory') : 'missing')
    add('Git root', git.root || 'none')
    add('Root matches path', git.matches ? 'yes' : 'no')
    add('Branch', git.branch || '-')
    add('Remote', git.remote || 'none')
    add('Token', token.present ? `present, length ${token.length_bucket}` : 'absent')
    add('gh user', ghInfo.authenticated ? ghInfo.user : ghInfo.available ? 'installed, not signed in' : 'not installed')
    if (git.error) add('Git error', git.error)
    if (ghInfo.error) add('gh error', ghInfo.error)
    add(
      'Probe',
      probe.code === 'ok'
        ? `HTTP ${probe.status} via ${probe.via}`
        : `${probe.code || 'no probe'}${probe.status ? `, HTTP ${probe.status}` : ''}`
    )
    for (const attempt of probe.attempts || []) {
      add(
        `Probe via ${attempt.via}`,
        attempt.code === 'ok'
          ? `HTTP ${attempt.status}, reachable`
          : `${attempt.message}${attempt.status ? ` (HTTP ${attempt.status})` : ''}`
      )
    }

    return jsxs('div', {
      style: S.ghDiag,
      children: [
        head('Diagnose'),
        rows.map(([key, value]) =>
          jsxs(
            'div',
            {
              style: S.ghDiagRow,
              children: [
                jsx('span', { style: S.ghDiagKey, children: key }),
                jsx('span', { style: S.ghDiagValue, children: value })
              ]
            },
            key
          )
        ),
        probe.message ? jsx('span', { style: S.ghDiagNote, children: probe.message }) : null
      ]
    })
  }

  let body

  if (boardFailed) {
    body = jsx('div', {
      style: S.ghErrorWrap,
      children: jsx(ErrorState, {
        className: 'pcc-gh-error',
        description: jsx('span', {
          style: { fontSize: '0.75rem', color: 'var(--ui-text-quaternary)', maxWidth: 520 },
          children:
            'This section reads /github and /repo under /api/plugins/project-command-center. The rest of the page keeps working.'
        }),
        icon: jsx(Codicon, { name: 'error', size: '1rem', style: { color: 'var(--ui-text-quaternary)' } }),
        title: jsx('span', {
          style: { fontSize: '0.8125rem', fontWeight: 600, color: 'var(--ui-text-secondary)' },
          children: 'GitHub backend not available yet'
        })
      })
    })
  } else if (loading) {
    body = jsx(GhSkeleton, {})
  } else if (boardFailure) {
    body = jsx(GhFailure, { data: boardFailure, folder })
  } else if (preset === 'issue') {
    body = issueView()
  } else if (preset === 'branch') {
    body = branchView()
  } else if (preset === 'unlinked') {
    body = unlinkedView()
  } else if (preset === 'shipped') {
    body = shippedView()
  } else {
    body = flightView()
  }

  return jsxs('section', {
    'aria-label': 'On GitHub',
    ref: rootRef,
    style: S.ghSection,
    children: [
      jsxs('div', {
        style: S.ghHead,
        children: [
          jsx('span', { style: S.stripTitle, children: 'On GitHub' }),
          ownerRepo
            ? canOpenExternal
              ? jsxs('button', {
                  className: 'pcc-gh-chip',
                  onClick: openRepo,
                  style: S.ghRepoBtn,
                  title: `Open ${ownerRepo} on GitHub`,
                  type: 'button',
                  children: [ownerRepo, jsx(Codicon, { name: 'link-external', size: '0.6875rem' })]
                })
              : jsx('span', { style: S.ghRepoText, children: ownerRepo })
            : null,
          data && data.auth === 'none'
            ? jsx(Tip, {
                label: 'No token and no signed in gh CLI. Only public reads work.',
                children: jsx(Badge, { size: 'xs', variant: 'warn', children: 'unauthenticated' })
              })
            : null,
          jsx('span', { style: S.grow }),
          jsx(Tip, {
            label: 'Refresh GitHub',
            children: jsx(Button, {
              'aria-label': 'Refresh GitHub',
              disabled: spin,
              onClick: refresh,
              size: 'icon-xs',
              variant: 'ghost',
              children: jsx(Codicon, { name: 'refresh', size: '0.8125rem', spinning: spin })
            })
          }),
          jsxs(DropdownMenu, {
            children: [
              jsx(DropdownMenuTrigger, {
                asChild: true,
                children: jsx(Button, {
                  'aria-label': 'GitHub section actions',
                  size: 'icon-xs',
                  variant: 'ghost',
                  children: jsx(Codicon, { name: 'ellipsis', size: '0.875rem' })
                })
              }),
              jsx(DropdownMenuContent, {
                align: 'end',
                children: jsx(DropdownMenuItem, {
                  onSelect: () => setDiagOpen(open => !open),
                  children: [
                    jsx(Codicon, { name: 'pulse', size: '0.75rem' }),
                    diagOpen ? 'Hide diagnose' : 'Diagnose'
                  ]
                })
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        style: S.ghToolbar,
        children: [
          jsx(SegmentedControl, {
            onChange: writeGhPreset,
            options: GH_PRESETS,
            sizing: 'content',
            value: preset
          }),
          jsx('span', { style: S.grow }),
          data && data.counts ? jsx(GhTotals, { counts: data.counts }) : null
        ]
      }),
      diagnoseView(),
      body,
      currentEntry
        ? jsx(
            GhDetailModal,
            {
              depth: stack.length,
              entry: currentEntry,
              folder,
              onBack: backEntry,
              onClose: closeEntry,
              onOpen: openEntry,
              owner: ownerName,
              repo: repoName
            },
            `${currentEntry.kind}-${currentEntry.number}`
          )
        : null
    ]
  })
}

// ── project detail page ──────────────────────────────────────────────────────

function Stat({ label, value, tip }) {
  const body = jsx('span', {
    style: {
      fontSize: '0.8125rem',
      fontWeight: 600,
      fontVariantNumeric: 'tabular-nums',
      color: 'var(--ui-text-primary)',
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap'
    },
    children: value
  })

  return jsxs('div', {
    style: S.statCell,
    children: [
      jsx('span', { style: { fontSize: '0.6875rem', color: 'var(--ui-text-quaternary)' }, children: label }),
      tip ? jsx(Tip, { label: tip, children: body }) : body
    ]
  })
}

function folderList(project) {
  const seen = new Set()
  const out = []
  const add = p => {
    if (p && !seen.has(p)) {
      seen.add(p)
      out.push(p)
    }
  }

  add(project.path)
  for (const repo of project.repos || []) add(repo.path)

  return out
}

// Lanes arrive fully hydrated from projects.project_sessions, in the same
// grouping the sidebar tree uses. A lane with no sessions is kept: it is
// information, not absence.
function laneGroups(node) {
  const multiRepo = (node.repos || []).length > 1
  const out = []

  for (const repo of node.repos || []) {
    for (const lane of repo.groups || []) {
      const sessions = [...(lane.sessions || [])].sort((a, b) => sessionStamp(b) - sessionStamp(a))

      out.push({ key: `${repo.id}:${lane.id}`, label: multiRepo ? repo.label : '', lane, sessions })
    }
  }

  return out
}

function DetailSessionRow({ session, state, onOpen }) {
  const ms = sessionStamp(session)
  const model = shortModel(session.model)
  const running = RUNNING.has(state)
  const tokens = (session.input_tokens || 0) + (session.output_tokens || 0)
  const messages = session.message_count || 0

  return jsxs(RowButton, {
    className: 'pcc-detail-row',
    onClick: () => onOpen(session.id),
    style: S.laneRow,
    title: sessionTitle(session),
    children: [
      running ? jsx(ArcRing, { radius: '0' }) : null,
      jsxs('span', {
        style: { display: 'flex', alignItems: 'center', gap: 7, minWidth: 0 },
        children: [
          state && state !== 'idle' ? jsx('span', { style: { display: 'inline-flex', flex: 'none' }, children: jsx(LiveDot, { state }) }) : null,
          jsx('span', { style: S.laneRowTitle, children: sessionTitle(session) })
        ]
      }),
      jsxs('span', {
        style: S.laneRowMeta,
        children: [
          ms ? jsx(Tip, { label: stampTip(ms), children: jsx('span', { children: ago(ms) }) }) : null,
          model ? jsx('span', { children: model }) : null,
          messages ? jsx('span', { children: `${messages} ${messages === 1 ? 'message' : 'messages'}` }) : null,
          tokens
            ? jsx(Tip, {
                label: `${(session.input_tokens || 0).toLocaleString()} in / ${(session.output_tokens || 0).toLocaleString()} out`,
                children: jsx('span', { children: `${formatTokens(tokens)} tok` })
              })
            : null
        ]
      })
    ]
  })
}

function DetailEmpty({ description, title }) {
  return jsxs('div', {
    style: S.page,
    children: [
      jsx('style', { children: CSS_TEXT }),
      jsxs('div', {
        style: { flex: '1 1 0', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 14, padding: 20 },
        children: [
          jsx(EmptyState, { description, title }),
          jsx(Button, { onClick: () => host.navigate(ROUTE), size: 'sm', variant: 'outline', children: 'Back to projects' })
        ]
      })
    ]
  })
}

function ProjectDetailPage() {
  const projectId = useValue($openProject)
  const statuses = useValue($statuses)
  const [spin, setSpin] = useState(false)

  const tree = useQuery({
    queryFn: () => host.request('projects.tree', { preview_limit: 5, ...profileParam() }),
    queryKey: [QUERY_ROOT, 'tree'],
    refetchInterval: TREE_MS,
    staleTime: 4000
  })

  const node = useMemo(() => {
    const list = (tree.data && tree.data.projects) || []

    return list.find(p => p.id === projectId) || null
  }, [tree.data, projectId])

  const detail = useQuery({
    enabled: Boolean(projectId && node),
    queryFn: () => host.request('projects.project_sessions', { project_id: projectId, ...profileParam() }),
    queryKey: [QUERY_ROOT, 'project', projectId],
    refetchInterval: projectId && node ? TREE_MS : false,
    staleTime: 4000
  })

  const live = useLiveSessions()
  const project = (detail.data && detail.data.project) || node
  // Mounting the same /board read here keeps the header's status picker on the
  // server copy even when the detail page is opened first (a tab or deep link),
  // and the project sync keeps the backend's cwd index fresh either way.
  useBoard()
  const treeProjects = useMemo(() => (tree.data && tree.data.projects) || [], [tree.data])

  useProjectSync(treeProjects)
  const lanes = useMemo(() => (project ? laneGroups(project) : []), [project])
  const folderPath = useMemo(() => (project ? primaryPath(project) : ''), [project])

  // The header names the work tree a vault subfolder really belongs to. Same
  // queryKey as the GitHub section's read, so react-query answers both with one
  // request instead of two.
  const repoInfo = useQuery({
    enabled: Boolean(folderPath),
    queryKey: [QUERY_ROOT, 'repo', folderPath],
    queryFn: () => ghRest(`/repo?path=${encodeURIComponent(folderPath)}`),
    retry: false,
    staleTime: 20000
  })

  const loadedCount = useMemo(() => lanes.reduce((n, l) => n + l.sessions.length, 0), [lanes])
  const liveCount = useMemo(() => {
    let n = 0

    for (const lane of lanes) for (const s of lane.sessions) if (RUNNING.has(live.byStored.get(s.id))) n += 1

    return n
  }, [lanes, live.byStored])

  const sessionsByBranch = useMemo(() => branchSessionMap(lanes, live.byStored), [lanes, live.byStored])

  const refresh = () => {
    setSpin(true)
    schedule(() => setSpin(false), 700)
    queryClient.invalidateQueries({ queryKey: [QUERY_ROOT] })
  }

  const copyPath = target => {
    Promise.resolve(writeClipboardText(target)).then(ok => {
      host.notify(ok === false ? { kind: 'warning', message: 'Could not copy the path.' } : { kind: 'success', message: 'Path copied' })
    })
  }

  if (!projectId) {
    return jsx(DetailEmpty, {
      description: 'Open a project from the board to see its lanes, sessions and totals.',
      title: 'No project selected'
    })
  }

  // The stored id can outlive the project (renamed, moved, another profile), so
  // a settled tree with no match is a real answer, not a loading state.
  if (!project) {
    if (tree.isLoading) {
      return jsxs('div', {
        style: S.page,
        children: [
          jsx('style', { children: CSS_TEXT }),
          jsx('div', {
            style: { padding: 20, display: 'flex', flexDirection: 'column', gap: 10 },
            children: [jsx(Skeleton, { style: { height: 28, width: '40%' } }), jsx(Skeleton, { style: { height: 64 } }), jsx(Skeleton, { style: { height: 120 } })]
          })
        ]
      })
    }

    return jsx(DetailEmpty, {
      description: 'It is not in the project tree for this profile any more.',
      title: 'That project is gone'
    })
  }

  const folderPaths = folderList(project)
  const path = primaryPath(project)
  const updatedMs = projectRecency(project)

  return jsxs('div', {
    style: S.page,
    children: [
      jsx('style', { children: CSS_TEXT }),
      jsxs('nav', {
        'aria-label': 'Breadcrumb',
        style: S.crumbs,
        children: [
          jsx('button', { className: 'pcc-crumb', onClick: () => host.navigate(ROUTE), style: S.crumbBtn, type: 'button', children: 'Projects' }),
          jsx(Codicon, { name: 'chevron-right', size: '0.75rem' }),
          jsx('span', { style: { ...S.ellipsis, color: 'var(--ui-text-secondary)' }, children: project.label })
        ]
      }),
      jsxs('header', {
        style: S.detailHead,
        children: [
          jsx(Avatar, { project, size: 30 }),
          jsxs('div', {
            style: { display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: '1 1 200px' },
            children: [
              jsx('h1', { style: S.detailTitle, children: project.label }),
              jsx(PathLine, {
                path,
                inside: (repoInfo.data && repoInfo.data.inside_root) || null
              })
            ]
          }),
          jsx(StatusPicker, { projectId: project.id, status: statuses[project.id] || DEFAULT_STATUS }),
          jsxs(DropdownMenu, {
            children: [
              jsx(DropdownMenuTrigger, {
                asChild: true,
                children: jsx(Button, {
                  'aria-label': 'Project actions',
                  size: 'icon-xs',
                  variant: 'ghost',
                  children: jsx(Codicon, { name: 'ellipsis', size: '0.875rem' })
                })
              }),
              jsx(DropdownMenuContent, {
                align: 'end',
                children: [
                  jsx(DropdownMenuItem, {
                    onSelect: refresh,
                    children: [jsx(Codicon, { name: 'refresh', size: '0.75rem', spinning: spin }), 'Refresh']
                  }),
                  path
                    ? jsx(DropdownMenuItem, {
                        onSelect: () => copyPath(path),
                        children: [jsx(Codicon, { name: 'copy', size: '0.75rem' }), 'Copy path']
                      })
                    : null
                ]
              })
            ]
          })
        ]
      }),
      jsxs('div', {
        style: S.detailBody,
        children: [
          jsxs('div', {
            style: S.stats,
            children: [
              jsx(Stat, { label: 'Sessions', value: project.sessionCount || loadedCount }),
              jsx(Stat, { label: 'Live now', value: liveCount }),
              jsx(Stat, { label: 'Tokens', tip: `${(project.totalTokens || 0).toLocaleString()} tokens`, value: formatTokens(project.totalTokens) }),
              jsx(Stat, { label: 'Cost', value: formatCost(project.totalCostUsd) || '-' }),
              jsx(Stat, { label: 'Folders', tip: folderPaths.join('\n') || undefined, value: folderPaths.length }),
              jsx(Stat, { label: 'Last updated', tip: stampTip(updatedMs), value: updatedMs ? ago(updatedMs) : 'No activity' })
            ]
          }),
          path ? jsx(GithubSection, { folder: path, projectId: project.id, sessionsByBranch }) : null,
          jsxs('div', {
            style: { display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 },
            children: [
              jsxs('div', {
                style: { ...S.stripHead, paddingTop: 0 },
                children: [
                  jsx('span', { style: S.stripTitle, children: 'Sessions by lane' }),
                  detail.isFetching ? jsx(Codicon, { name: 'loading', size: '0.75rem', spinning: true }) : null,
                  jsx('span', { style: S.grow }),
                  jsx('span', { style: S.count, children: loadedCount })
                ]
              }),
              lanes.length === 0
                ? jsx('div', { style: S.quiet, children: detail.isLoading ? 'Loading sessions...' : 'No sessions in this project yet' })
                : lanes.map(group => {
                    const branch =
                      group.lane.isGit === false
                        ? ''
                        : ((group.sessions.find(s => s.git_branch && s.git_branch !== group.lane.label) || {}).git_branch || '')

                    return jsxs(
                      'section',
                      {
                        'aria-label': group.lane.label,
                        style: S.lane,
                        children: [
                          jsxs('div', {
                            style: S.laneHead,
                            children: [
                              jsx(Codicon, { name: group.lane.isKanban ? 'project' : 'git-branch', size: '0.8125rem', style: { flex: 'none' } }),
                              jsx('span', { style: { ...S.ellipsis, fontWeight: 600 }, children: group.lane.label }),
                              group.lane.isMain ? jsx(Badge, { variant: 'muted', children: 'main lane' }) : null,
                              group.lane.isKanban ? jsx(Badge, { variant: 'outline', children: 'kanban' }) : null,
                              branch ? jsx(BranchChip, { branch }) : null,
                              group.label ? jsx('span', { style: { ...S.ellipsis, color: 'var(--ui-text-quaternary)' }, children: group.label }) : null,
                              jsx('span', { style: S.grow }),
                              jsx('span', { style: S.count, children: group.sessions.length })
                            ]
                          }),
                          group.sessions.length === 0
                            ? jsx('div', { style: S.laneQuiet, children: 'No sessions on this lane' })
                            : group.sessions.map(s => jsx(DetailSessionRow, { onOpen: openSession, session: s, state: live.byStored.get(s.id) }, s.id))
                        ]
                      },
                      group.key
                    )
                  })
            ]
          })
        ]
      })
    ]
  })
}

// ── page ─────────────────────────────────────────────────────────────────────

function matchesQuery(project, q) {
  const hay = [project.label, project.path]

  for (const repo of project.repos || []) {
    hay.push(repo.label, repo.path)
    for (const lane of repo.groups || []) {
      hay.push(lane.label, lane.path)
      for (const s of lane.sessions || []) hay.push(s.git_branch)
    }
  }

  for (const s of project.previewSessions || []) hay.push(s.git_branch)

  return hay.some(v => v && String(v).toLowerCase().includes(q))
}

function ProjectsPage() {
  const [search, setSearch] = useState('')
  const statuses = useValue($statuses)

  const tree = useQuery({
    queryFn: () =>
      host.request('projects.tree', { preview_limit: 5, ...profileParam() }),
    queryKey: [QUERY_ROOT, 'tree'],
    refetchInterval: TREE_MS,
    staleTime: 4000
  })

  // session.active_list.id is the runtime id; session_key is the stored id used
  // everywhere else. useLiveSessions maps one to the other and resolves state.
  const live = useLiveSessions()
  const projects = useMemo(() => (tree.data && tree.data.projects) || [], [tree.data])
  // /board: the server copy of the status map plus the session→issue/PR links the
  // column headers chip. Both degrade to the old storage-only behaviour when the
  // route is not mounted yet. Project sync rides the same read.
  const boardState = useBoard()
  const linksForProject = useMemo(() => linksByProject(boardState.links), [boardState.links])
  // One /github/counts read for every column; a column takes its own folder's entry.
  const repoCounts = useRepoCounts(projects)

  useProjectSync(projects)
  const projectBySession = useMemo(() => {
    const map = new Map()

    for (const p of projects) {
      for (const id of p.sessionIds || []) if (!map.has(id)) map.set(id, p.label)
    }

    return map
  }, [projects])

  const q = search.trim().toLowerCase()
  const sortId = useValue($sort)
  const filtered = useMemo(() => (q ? projects.filter(p => matchesQuery(p, q)) : projects), [projects, q])
  const visible = useMemo(() => sortProjects(filtered, sortId, statuses), [filtered, sortId, statuses])

  // The 15s background poll also sets isFetching, so the glyph would spin on its
  // own. Spin only for a click.
  const [spin, setSpin] = useState(false)

  const refresh = () => {
    setSpin(true)
    schedule(() => setSpin(false), 700)
    queryClient.invalidateQueries({ queryKey: [QUERY_ROOT] })
  }

  const fetching = spin
  const countLabel = q ? `${visible.length} of ${projects.length}` : String(projects.length)

  let board

  if (tree.isLoading) {
    board = jsx(BoardSkeleton, {})
  } else if (tree.isError && !tree.data) {
    board = jsx('div', {
      style: { flex: '1 1 0', display: 'grid', placeItems: 'center', padding: 20 },
      children: jsx(ErrorState, {
        description: String((tree.error && tree.error.message) || 'The gateway did not return the project list.'),
        title: 'Could not load projects',
        children: jsx(Button, { onClick: () => tree.refetch(), size: 'sm', variant: 'outline', children: 'Try again' })
      })
    })
  } else if (visible.length === 0) {
    board = jsx('div', {
      style: { flex: '1 1 0', display: 'grid', placeItems: 'center' },
      children: jsx(EmptyState, {
        description: q ? 'Try a different name, folder or branch.' : 'Projects appear here once a session runs inside a folder.',
        title: q ? `No projects match \u201c${search.trim()}\u201d` : 'No projects yet'
      })
    })
  } else {
    board = jsx(Board, {
      count: visible.length,
      children: visible.map((p, i) =>
        jsx(
          ProjectColumn,
          {
            index: i,
            counts: Object.prototype.hasOwnProperty.call(repoCounts, primaryPath(p)) ? repoCounts[primaryPath(p)] : null,
            links: linksForProject.get(p.id),
            liveByStored: live.byStored,
            onOpenProject: openProject,
            onOpenSession: openSession,
            project: p,
            status: statuses[p.id] || DEFAULT_STATUS
          },
          p.id
        )
      )
    })
  }

  return jsxs('div', {
    style: S.page,
    children: [
      jsx('style', { children: CSS_TEXT }),
      jsxs('header', {
        style: S.header,
        children: [
          jsx('h1', { style: S.title, children: 'Projects' }),
          jsx('span', { style: S.count, children: tree.data ? `${countLabel} ${projects.length === 1 && !q ? 'project' : 'projects'}` : '' }),
          jsx('span', { style: S.grow }),
          jsx(SortPicker, { sort: sortId }),
          jsx('div', {
            style: S.searchWrap,
            children: jsx(SearchField, {
              'aria-label': 'Search projects',
              onChange: setSearch,
              placeholder: 'Search name or folder',
              value: search
            })
          }),
          jsx(Tip, {
            label: 'Refresh',
            children: jsx(Button, {
              'aria-label': 'Refresh projects',
              disabled: fetching,
              onClick: refresh,
              size: 'icon-xs',
              variant: 'ghost',
              children: jsx(Codicon, { name: 'refresh', size: '0.875rem', spinning: fetching })
            })
          })
        ]
      }),
      board,
      jsx(LiveStrip, { items: live.items, loading: live.query.isLoading, onOpenSession: openSession, projectBySession })
    ]
  })
}

// ── titlebar ─────────────────────────────────────────────────────────────────

// The `titleBar.left/center/right` areas are render SLOTS for page-owned chrome:
// anything registered there makes the app treat the route as chrome-owning and
// drop its own fixed right-hand tools. A persistent toolbar button belongs in
// the tool registry instead, which is where core pages put their own tools
// (app/contrib/panes.tsx `setTitlebarToolGroup`). There is no SDK constant for
// it yet, so the area name is written out.
const TITLEBAR_TOOL_AREA = 'titleBar.tools.right'

function titlebarTool() {
  return {
    id: 'pcc-open',
    label: 'Projects',
    title: 'Projects',
    icon: jsx(Codicon, { name: 'project', size: '13.9px' }),
    to: ROUTE
  }
}

// ── registration ─────────────────────────────────────────────────────────────

export default {
  id: PLUGIN_ID,
  name: 'Project Command Center',
  register(ctx) {
    pluginCtx = ctx
    // A reload re-registers this module: scheduling is armed again here, and a
    // promise that settles after teardown (a composer verb, a REST read) drops
    // its timer instead of writing into a retired lifetime set.
    pluginDisposed = false
    if (typeof ctx.onDispose === 'function') {
      ctx.onDispose(() => {
        pluginDisposed = true
      })
    }
    $statuses.set(readStatuses())
    $sort.set(readSort())
    $ghPreset.set(readGhPreset())
    $openProject.set(readOpenProject())

    ctx.registerMany([
      { id: 'titlebar', area: TITLEBAR_TOOL_AREA, order: 40, data: titlebarTool() },
      { id: 'page', area: ROUTES_AREA, title: 'Projects', data: { path: ROUTE }, render: () => jsx(ProjectsPage, {}) },
      // A second page, not a drawer: the board stays the board, and a project
      // gets a page of its own that a tab can hold.
      { id: 'project-page', area: ROUTES_AREA, title: 'Project', data: { path: PROJECT_ROUTE }, render: () => jsx(ProjectDetailPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: ROUTE, label: 'Projects', codicon: 'project' } },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: `${PLUGIN_ID}.open`,
          label: 'Open Projects',
          keywords: ['projects', 'project', 'board', 'kanban', 'sessions', 'command center'],
          run: () => host.navigate(ROUTE)
        }
      }
    ])

    // The poll tells us which sessions exist and what the gateway thinks they are
    // doing; the event stream tells us which ones are actually producing right
    // now. `event.session_id` is the runtime id, which is the key we keep.
    ctx.onEvent('*', event => {
      const id = event && event.session_id

      if (!id) return

      if (event.type === 'message.complete') {
        clearActivity(id)

        return
      }

      if (ACTIVITY_TYPES.has(event.type)) noteActivity(id, Date.now())
    })
    ctx.setInterval(() => $activityClock.set(Date.now()), 5000)

    // Gateway events can arrive in bursts; coalesce them into one refetch.
    let pending = false
    const invalidate = () => {
      if (pending) return
      pending = true
      ctx.setTimeout(() => {
        pending = false
        queryClient.invalidateQueries({ queryKey: [QUERY_ROOT] })
      }, 400)
    }

    ctx.onEvent('projects.changed', invalidate)
    ctx.onEvent('sessions.changed', invalidate)

    // The backend tells us when a status or a session→issue/PR link moved (a
    // click in another window, or its own cwd matcher). The board poll is always
    // stale after that; a PR number is also a cue for the GitHub section to take
    // a forced read and flash the row it names.
    ctx.onEvent('plugin.project-command-center.board.changed', event => {
      const payload = (event && event.payload) || {}

      queryClient.invalidateQueries({ queryKey: [QUERY_ROOT, 'board'] })

      const pr = Number(payload.pr)

      if (payload.project_id && Number.isFinite(pr) && pr > 0) {
        $ghPoke.set({ at: Date.now(), pr, project_id: payload.project_id })
      }
    })
  }
}
