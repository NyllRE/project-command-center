// Proof for the lane engine embedded in desktop/plugin.js. The engine is not
// copied here: the marker block is sliced out of the live file, so a change in
// the plugin that is not compilable or not self contained fails this test.
//
// Run with:
//   node --test tests/graph_layout.test.mjs
//
// SVGs are written to ~/.hermes/cache/scratch/pcc-graph by default. Override
// the directory with PCC_GRAPH_SVG_DIR.

import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..')
const PLUGIN = join(REPO, 'desktop', 'plugin.js')
const FIXTURES = join(HERE, 'fixtures', 'graph')
const OUT = process.env.PCC_GRAPH_SVG_DIR || join(homedir(), '.hermes', 'cache', 'scratch', 'pcc-graph')
const HERMES = join(homedir(), '.hermes', 'hermes-agent')
const BEGIN = '// graph-layout:begin'
const END = '// graph-layout:end'
const PALETTE = ['#4c8dff', '#e8912d', '#a879ff', '#2ec9d8', '#3fbf7f', '#d8b53a', '#e5686d', '#5aa9e6']

// ── load the engine from the live plugin ────────────────────────────────────

const pluginSource = readFileSync(PLUGIN, 'utf8')
const beginAt = pluginSource.indexOf(BEGIN)
const endAt = pluginSource.indexOf(END)
const pluginHash = createHash('sha256').update(pluginSource).digest('hex')

assert.ok(beginAt >= 0, `${BEGIN} missing from desktop/plugin.js`)
assert.ok(endAt > beginAt, `${END} missing or before the begin marker`)
assert.equal(pluginSource.indexOf(BEGIN, beginAt + 1), -1, `${BEGIN} appears more than once`)

const block = pluginSource.slice(beginAt, endAt + END.length)
assert.ok(block.length > 4000, `graph block looks truncated (${block.length} bytes)`)
assert.doesNotMatch(block, /^\s*import\s/m, 'graph block must not import anything')
assert.doesNotMatch(block, /^\s*export\s/m, 'graph block must not export anything')

vm.runInThisContext(
  `${block}\n;globalThis.__PCC_GRAPH__ = { pccGraphLayout, pccGraphPaths, GRAPH_ROW_H, GRAPH_LANE_W, GRAPH_DOT_R, GRAPH_STROKE, GRAPH_MAX_LANES, GRAPH_LANE_COLORS }`,
  { filename: PLUGIN }
)
const G = globalThis.__PCC_GRAPH__

console.log(`plugin.js sha256 ${pluginHash}`)
console.log(`graph block ${Buffer.byteLength(block)} bytes, ${block.split('\n').length} lines`)

// ── helpers ─────────────────────────────────────────────────────────────────

function commitIndex(input) {
  return new Map(input.commits.map((commit, i) => [commit.sha, i]))
}

function tile(text, width) {
  const value = String(text == null ? '' : text)
  return value.length > width ? `${value.slice(0, width - 1)}…` : value
}

function xml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function normalize(layout, input) {
  return JSON.stringify({
    anomalies: layout.anomalies,
    bands: layout.bands,
    maxLanes: layout.maxLanes,
    rows: layout.rows.map(row => ({
      after: row.after.map(slot => (slot ? slot.sha : null)),
      born: [...row.born].sort((a, b) => a - b),
      color: row.color,
      converging: row.converging,
      freed: [...row.freed].sort((a, b) => a - b),
      isHead: row.isHead,
      joins: row.joins,
      key: row.key,
      lane: row.lane,
      refs: row.refs,
      sha: row.sha
    })),
    trunkSize: layout.trunkSize
  })
}

function circleCount(d) {
  return (String(d).match(/ A /g) || []).length / 2
}

function pathText(paths) {
  return [
    paths.headRing,
    paths.leaders,
    paths.clippedDots,
    ...Object.values(paths.byColor),
    ...Object.values(paths.dotsByColor)
  ].join(' ')
}

function assertFinitePaths(label, paths) {
  const text = pathText(paths)
  assert.doesNotMatch(text, /NaN|Infinity|undefined|null/, `${label}: non finite geometry token`)
  assert.doesNotMatch(paths.headRing, /NaN|Infinity/, `${label}: head ring is not finite`)
  assert.ok(Number.isFinite(paths.width) && paths.width > 0, `${label}: width is ${paths.width}`)
  assert.ok(Number.isFinite(paths.height) && paths.height >= 0, `${label}: height is ${paths.height}`)
  for (const [color, d] of Object.entries(paths.byColor)) {
    assert.match(d, /^M /, `${label}: color ${color} path does not start with M`)
  }
  for (const [color, d] of Object.entries(paths.dotsByColor)) {
    assert.match(d, /^M /, `${label}: dot color ${color} path does not start with M`)
  }
}

// Every in-window parent must be reachable from the child dot through band
// edges that carry the parent sha. A join into an already waiting lane is a
// valid intermediate step, so this checks connectivity rather than dot degree.
function assertParentReachability(label, input, layout) {
  const index = commitIndex(input)
  const rowBySha = new Map(layout.rows.map(row => [row.sha, row]))

  input.commits.forEach((commit, childRow) => {
    const seen = new Set()
    for (const parent of new Set(commit.parents || [])) {
      const parentRow = index.get(parent)
      if (parentRow == null || parentRow <= childRow) continue
      let lane = layout.rows[childRow].lane
      let row = childRow
      let reached = false
      while (row < parentRow) {
        const edge = (layout.bands[row] || []).find(item => item.from === lane && item.sha === parent)
        assert.ok(edge, `${label}: row ${row} lane ${lane} has no edge carrying ${parent.slice(0, 8)}`)
        const next = layout.rows[row + 1]
        if (edge.to === next.lane && next.sha === parent) {
          reached = true
          break
        }
        lane = edge.to
        row += 1
        const guard = `${childRow}:${parent}:${row}:${lane}`
        assert.ok(!seen.has(guard), `${label}: parent walk looped at ${guard}`)
        seen.add(guard)
      }
      assert.ok(reached, `${label}: ${commit.sha.slice(0, 8)} cannot reach parent ${parent.slice(0, 8)}`)
      assert.ok(rowBySha.has(parent), `${label}: parent row missing`)
    }
  })
}

function checkLayout(label, input, layout, geometry) {
  assert.equal(layout.rows.length, input.commits.length, `${label}: row count`)
  assert.equal(layout.anomalies, 0, `${label}: anomalies ${JSON.stringify(layout.anomalyList)}`)
  assert.ok(Number.isInteger(layout.trunkSize) && layout.trunkSize >= 0, `${label}: trunkSize`)
  assert.ok(Number.isInteger(layout.maxLanes) && layout.maxLanes >= 0, `${label}: maxLanes`)
  assert.equal(layout.bands.length, layout.rows.length, `${label}: one band per row`)

  const index = commitIndex(input)
  const highestLane = Math.max(0, ...layout.rows.map(row => row.lane))

  layout.rows.forEach((row, i) => {
    assert.ok(Number.isInteger(row.lane) && row.lane >= 0, `${label}: row ${i} lane ${row.lane}`)
    assert.ok(row.lane < layout.maxLanes, `${label}: row ${i} lane exceeds maxLanes`)
    assert.ok(Number.isInteger(row.color) && row.color >= 0 && row.color <= 7, `${label}: row ${i} color`)
    assert.ok(Array.isArray(row.refs), `${label}: row ${i} refs`)
  })
  assert.ok(layout.maxLanes >= highestLane + 1, `${label}: maxLanes ${layout.maxLanes} below lane ${highestLane}`)

  // Lane 0 is a chain: a lane 0 row's first parent, when in window, is lane 0.
  layout.rows.forEach((row, i) => {
    const first = (input.commits[i].parents || [])[0]
    if (row.lane !== 0 || !first || !index.has(first)) return
    const parent = layout.rows[index.get(first)]
    assert.equal(parent.lane, 0, `${label}: lane 0 breaks at ${row.sha.slice(0, 8)} into lane ${parent.lane}`)
  })

  layout.rows.forEach((row, i) => {
    const born = new Set(row.born)
    const freed = new Set(row.freed)
    for (const lane of born) assert.ok(!freed.has(lane), `${label}: row ${i} lane ${lane} born and freed`)

    const band = layout.bands[i]
    assert.ok(Array.isArray(band), `${label}: band ${i}`)
    const next = layout.rows[i + 1]
    const targets = new Map()

    for (const edge of band) {
      assert.ok(Number.isInteger(edge.from) && edge.from >= 0, `${label}: band ${i} from`)
      assert.ok(Number.isInteger(edge.to) && edge.to >= 0, `${label}: band ${i} to`)
      assert.ok(Number.isInteger(edge.color) && edge.color >= 0 && edge.color <= 7, `${label}: band ${i} color`)
      assert.equal(typeof edge.sha, 'string', `${label}: band ${i} sha`)
      assert.ok(edge.sha.length > 0, `${label}: band ${i} empty sha`)
      assert.equal(Boolean(edge.stub), !next, `${label}: band ${i} stub flag`)

      if (next && edge.sha === next.sha) {
        assert.equal(edge.to, next.lane, `${label}: band ${i} edge for next dot lands on ${edge.to}, not ${next.lane}`)
      } else {
        assert.equal(edge.to, edge.slot, `${label}: band ${i} slot ${edge.slot} shifted to ${edge.to} without a dot`)
        if (next) {
          const slot = next.after[edge.to]
          assert.ok(slot, `${label}: band ${i} edge lands on empty slot ${edge.to}`)
          assert.equal(slot.sha, edge.sha, `${label}: band ${i} edge changes sha in slot ${edge.to}`)
        }
      }

      const list = targets.get(edge.to) || []
      list.push(edge)
      targets.set(edge.to, list)
    }

    for (const [to, list] of targets) {
      const dot = next && to === next.lane && list.every(edge => edge.sha === next.sha)
      if (dot) continue
      const normal = list.filter(edge => !edge.join)
      assert.ok(normal.length <= 1, `${label}: band ${i} slot ${to} has ${normal.length} non join edges`)
    }

    const parents = new Set((input.commits[i].parents || []).filter(parent => parent))
    const leaving = band.filter(edge => edge.from === row.lane).length
    assert.equal(leaving, parents.size, `${label}: row ${i} leaves ${leaving} edges for ${parents.size} parents`)
  })

  assertParentReachability(label, input, layout)

  if (geometry) {
    const paths = G.pccGraphPaths(layout, geometry)
    assertFinitePaths(label, paths)
    const unclipped = layout.rows.filter(row => row.lane < paths.maxLanes).length
    const drawn = Object.values(paths.dotsByColor).reduce((sum, d) => sum + circleCount(d), 0)
    assert.equal(drawn, unclipped, `${label}: drawn dots ${drawn}, expected ${unclipped}`)
    assert.equal(circleCount(paths.clippedDots), layout.rows.length - unclipped, `${label}: clipped dots`)
    const edgeCount = layout.bands.reduce((sum, band) => sum + band.length, 0)
    const segmentCount = Object.values(paths.byColor).reduce((sum, d) => sum + (d.match(/(?:^| )M /g) || []).length, 0)
    assert.equal(segmentCount, edgeCount, `${label}: ${segmentCount} drawn segments for ${edgeCount} edges`)

    const expectsCurve = layout.bands.some(band => band.some(edge => edge.from !== edge.to))
    const hasCurve = Object.values(paths.byColor).some(d => d.includes(' C '))
    assert.equal(hasCurve, expectsCurve, `${label}: curve geometry does not match the edge list`)

    if (layout.rows.some(row => row.isHead) && layout.rows.find(row => row.isHead).lane < paths.maxLanes) {
      assert.ok(paths.headRing, `${label}: HEAD row has no ring`)
    }
    if (geometry.labelGutter > 0 && layout.rows.some(row => row.refs.length)) {
      assert.ok(paths.leaders, `${label}: refs without leader lines`)
    }
    return paths
  }

  return null
}

// ── fixture tests ───────────────────────────────────────────────────────────

const fixtureFiles = readdirSync(FIXTURES).filter(name => name.endsWith('.json')).sort()
assert.ok(fixtureFiles.length >= 13, `expected at least 13 fixtures, found ${fixtureFiles.length}`)

const fixtureCache = new Map()
for (const file of fixtureFiles) {
  fixtureCache.set(file.replace(/\.json$/, ''), JSON.parse(readFileSync(join(FIXTURES, file), 'utf8')))
}

const SVG_GEOM = { labelGutter: 148, laneW: 14, rowH: 26, dotR: 3.5, maxLanes: 16 }

for (const [name, fixture] of fixtureCache) {
  test(`fixture ${name}: ${fixture.description}`, () => {
    const input = fixture.input
    const layout = G.pccGraphLayout(input)
    checkLayout(name, input, layout, SVG_GEOM)

    const expect = fixture.expect || {}
    if (expect.anomalies != null) assert.equal(layout.anomalies, expect.anomalies)
    if (expect.trunkSize != null) assert.equal(layout.trunkSize, expect.trunkSize, 'trunkSize')
    if (expect.maxLanes != null) assert.equal(layout.maxLanes, expect.maxLanes, 'maxLanes')

    const rows = new Map(layout.rows.map(row => [row.sha, row]))
    if (expect.lanes) {
      for (const [sha, lane] of Object.entries(expect.lanes)) assert.equal(rows.get(sha).lane, lane, `lane ${sha}`)
    }
    if (expect.converging) {
      for (const [sha, lanes] of Object.entries(expect.converging)) {
        assert.deepEqual(rows.get(sha).converging, lanes, `converging ${sha}`)
      }
    }
    if (expect.joins) {
      for (const [sha, lanes] of Object.entries(expect.joins)) assert.deepEqual(rows.get(sha).joins, lanes, `joins ${sha}`)
    }
    if (expect.bandEdges) {
      assert.deepEqual(layout.bands.map(band => band.length), expect.bandEdges, 'band edge counts')
    }
    if (expect.roots) {
      for (const [sha, lane] of Object.entries(expect.roots)) {
        assert.deepEqual(input.commits.find(commit => commit.sha === sha).parents, [], `root ${sha}`)
        assert.equal(rows.get(sha).lane, lane, `root lane ${sha}`)
      }
    }
    if (expect.stubBands) {
      for (const i of expect.stubBands) {
        assert.ok(layout.bands[i].length > 0, `stub band ${i} is empty`)
        assert.ok(layout.bands[i].every(edge => edge.stub), `stub band ${i} has a non stub edge`)
      }
    }
    if (expect.outDegree) {
      for (const [sha, degree] of Object.entries(expect.outDegree)) {
        const i = layout.rows.findIndex(row => row.sha === sha)
        assert.equal(layout.bands[i].filter(edge => edge.from === layout.rows[i].lane).length, degree, `out degree ${sha}`)
      }
    }
    if (expect.fan) {
      for (const [sha, lanes] of Object.entries(expect.fan)) {
        const i = layout.rows.findIndex(row => row.sha === sha)
        const targets = layout.bands[i].filter(edge => edge.from === layout.rows[i].lane).map(edge => edge.to).sort((a, b) => a - b)
        assert.deepEqual(targets, lanes, `fan ${sha}`)
      }
    }
    if (expect.reuse) {
      const lanes = Object.values(expect.reuse)
      assert.equal(new Set(lanes).size, 1, 'reuse lanes differ')
    }
    if (expect.laneCount) {
      const lanes = [...new Set(layout.rows.map(row => row.lane))].sort((a, b) => a - b)
      assert.equal(lanes.length, expect.laneCount, 'distinct lane count')
      assert.equal(lanes[0], 0, 'lane numbering starts at 0')
      assert.equal(lanes[lanes.length - 1], expect.laneCount - 1, 'lanes are contiguous for this fixture')
    }
  })
}

test('determinism: same input twice is byte identical after normalisation', () => {
  for (const [name, fixture] of fixtureCache) {
    const a = G.pccGraphLayout(fixture.input)
    const b = G.pccGraphLayout(JSON.parse(JSON.stringify(fixture.input)))
    assert.equal(normalize(a, fixture.input), normalize(b, fixture.input), `${name}: layouts differ`)
  }
})

test('color stability: one new commit on main recolors nothing below it', () => {
  const fixture = fixtureCache.get('trunk-pinning')
  const before = G.pccGraphLayout(fixture.input)
  const old = new Map(before.rows.map(row => [row.sha, row.color]))
  const next = {
    ...fixture.input,
    head: { sha: 'n', branch: 'main', detached: false },
    refs: fixture.input.refs.map(ref => (ref.name === 'refs/heads/main' || ref.name === 'refs/remotes/origin/main' ? { ...ref, sha: 'n' } : ref)),
    commits: [{ sha: 'n', parents: ['m2'], subject: 'new tip' }, ...fixture.input.commits]
  }
  const after = G.pccGraphLayout(next)
  const now = new Map(after.rows.map(row => [row.sha, row.color]))
  for (const [sha, color] of old) assert.equal(now.get(sha), color, `color changed for ${sha}`)
  assert.equal(now.get('n'), 0, 'new trunk tip is not on lane 0')
})

test('defensive: a parent before its child is dropped and counted as an anomaly', () => {
  const input = {
    commits: [
      { sha: 'a', parents: ['b'] },
      { sha: 'b', parents: ['a'] }
    ],
    default_branch: 'origin/main',
    head: { sha: 'a' },
    refs: [{ kind: 'local', name: 'refs/heads/main', sha: 'a', short: 'main' }]
  }
  const layout = G.pccGraphLayout(input)
  assert.equal(layout.anomalies, 1, 'backwards parent was not counted')
  assert.equal(layout.anomalyList[0].parent, 'a', 'anomaly names the wrong parent')
  assert.equal(layout.anomalyList[0].reason, 'parent_not_after_child')
  assert.deepEqual(layout.rows[1].after, [], 'dropped parent still opened a lane')
})

// ── geometry unit tests ─────────────────────────────────────────────────────

function bareRow(lane, extra = {}) {
  return { color: 1, isHead: false, lane, refs: [], sha: `s${lane}`, ...extra }
}

test('geometry: lane x, dot y, vertical V and cubic C commands', () => {
  const layout = {
    maxLanes: 2,
    rows: [bareRow(0, { isHead: true }), bareRow(1), bareRow(0)],
    bands: [[{ color: 1, from: 0, sha: 's1', to: 1 }], [{ color: 1, from: 1, sha: 's0', to: 0 }], []]
  }
  const paths = G.pccGraphPaths(layout, { labelGutter: 0 })
  assert.equal(paths.width, 36, 'narrow width')
  assert.equal(paths.height, 78, 'row height')
  assert.equal(paths.byColor['1'], 'M 7 13 C 7 26, 21 26, 21 39 M 21 39 C 21 52, 7 52, 7 65', 'curve paths')
  assert.equal(paths.leaders, '', 'narrow layout has no leaders')
  assert.ok(paths.headRing.startsWith('M 2.5 13 A 4.5 4.5'), 'head ring radius is dot + 1')

  const vertical = G.pccGraphPaths(
    { maxLanes: 1, rows: [bareRow(0), bareRow(0)], bands: [[{ color: 0, from: 0, sha: 's0', to: 0 }], []] },
    { labelGutter: 0 }
  )
  assert.equal(vertical.byColor['0'], 'M 7 13 V 39', 'vertical path')

  const bottomJoin = G.pccGraphPaths(
    { maxLanes: 2, rows: [bareRow(0), bareRow(1)], bands: [[], [{ color: 1, from: 0, sha: 's1', to: 1 }]] },
    { labelGutter: 0 }
  )
  assert.equal(bottomJoin.byColor['1'], 'M 7 39 C 7 45.5, 21 45.5, 21 52', 'last band curve keeps vertical tangents')
})

test('geometry: wide gutter leaders and clamped overflow lanes', () => {
  const layout = {
    maxLanes: 1,
    rows: [bareRow(0, { refs: ['main'] }), bareRow(0)],
    bands: [[{ color: 0, from: 0, sha: 's0', to: 0 }], []]
  }
  const wide = G.pccGraphPaths(layout, { labelGutter: 148 })
  assert.equal(wide.width, 178, 'gutter width')
  assert.equal(wide.leaders, 'M 148 13 H 158.5', 'dashed leader spans gutter to dot')

  const crowded = {
    maxLanes: 20,
    rows: [bareRow(0), bareRow(19), bareRow(5)],
    bands: [[], [], []]
  }
  const clipped = G.pccGraphPaths(crowded, { labelGutter: 0, maxLanes: 16 })
  assert.equal(clipped.width, 232, 'width is capped at 16 lanes')
  assert.equal(circleCount(clipped.clippedDots), 1, 'lane 19 is pinned as a hollow dot')
  assert.equal(Object.values(clipped.dotsByColor).reduce((sum, d) => sum + circleCount(d), 0), 2, 'two dots fit')
})

test('geometry: empty and single row layouts stay finite', () => {
  const empty = G.pccGraphPaths({ maxLanes: 0, rows: [], bands: [] }, { labelGutter: 0 })
  assert.equal(empty.height, 0)
  assert.equal(empty.width, 22)
  assert.equal(empty.clippedDots, '')
  assert.equal(empty.headRing, '')
  assert.equal(circleCount(''), 0)

  const root = G.pccGraphPaths({ maxLanes: 1, rows: [bareRow(0)], bands: [[]] }, { labelGutter: 0 })
  assert.equal(root.height, 26)
  assert.equal(Object.keys(root.byColor).length, 0)
  assert.equal(Object.values(root.dotsByColor).reduce((sum, d) => sum + circleCount(d), 0), 1)
})

// ── real windows ────────────────────────────────────────────────────────────

const WINDOW_LIMIT = 200
const LOG_FORMAT = '%H%x1f%P%x1f%aN%x1f%aE%x1f%aI%x1f%cI%x1f%s'
const REF_FORMAT = '%(refname)%09%(objectname)%09%(*objectname)%09%(upstream:short)%09%(upstream:track,nobracket)%09%(HEAD)%09%(symref)'

function git(root, args) {
  return execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 })
}

function readCommits(root, anchor, limit = WINDOW_LIMIT) {
  const args = [
    '-c', 'log.showSignature=false',
    '-c', 'i18n.logOutputEncoding=UTF-8',
    'log', '--date-order', '--no-color', '--no-decorate', '-z',
    '-n', String(limit + 1), `--format=${LOG_FORMAT}`
  ]
  if (anchor) args.push(anchor, '--')
  else args.push('--branches', '--remotes', 'HEAD', '--')
  const raw = execFileSync('git', ['-C', root, ...args], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 })
  const records = raw.split('\0').filter(record => record.trim())
  return records.slice(0, limit).map(record => {
    const fields = record.replace(/^\n/, '').split('\x1f')
    return {
      sha: fields[0],
      parents: fields[1] ? fields[1].split(' ').filter(Boolean) : [],
      author: { name: fields[2], email: fields[3] },
      authored_at: fields[4],
      committed_at: fields[5],
      subject: fields[6] || ''
    }
  })
}

function readRefs(root, index) {
  const raw = git(root, ['for-each-ref', `--format=${REF_FORMAT}`, 'refs/heads', 'refs/remotes', 'refs/tags'])
  const refs = []
  let defaultBranch = null
  for (const line of raw.split('\n')) {
    if (!line) continue
    const [name, objectname, peeled, upstream, track, marker, symref] = line.split('\t')
    if (symref) {
      if (name.endsWith('/HEAD') && !defaultBranch) defaultBranch = symref.replace(/^refs\/remotes\//, '')
      continue
    }
    const kind = name.startsWith('refs/heads/') ? 'local' : name.startsWith('refs/remotes/') ? 'remote' : 'tag'
    const sha = kind === 'tag' && peeled ? peeled : objectname
    if (!index.has(sha)) continue
    const short = name.replace(/^refs\/heads\//, '').replace(/^refs\/remotes\//, '').replace(/^refs\/tags\//, '')
    const gone = /^gone$/.test(track || '')
    const match = String(track || '').match(/ahead (\d+)/)
    const behind = String(track || '').match(/behind (\d+)/)
    refs.push({
      ahead: match ? Number(match[1]) : 0,
      behind: behind ? Number(behind[1]) : 0,
      gone,
      is_head: marker === '*',
      is_remote: kind === 'remote',
      kind,
      name,
      sha,
      short,
      upstream: upstream || null
    })
  }
  return { defaultBranch, refs }
}

function readWindow(root, anchor, limit = WINDOW_LIMIT) {
  const commits = readCommits(root, anchor, limit)
  const index = new Map(commits.map(commit => [commit.sha, commit]))
  const { defaultBranch, refs } = readRefs(root, index)
  let head = { sha: '', branch: null, detached: false }
  try {
    head.sha = git(root, ['rev-parse', '--verify', '--quiet', 'HEAD']).trim()
  } catch {
    head.sha = ''
  }
  try {
    head.branch = git(root, ['symbolic-ref', '--quiet', '--short', 'HEAD']).trim()
  } catch {
    head.branch = null
    head.detached = Boolean(head.sha)
  }
  return {
    anchor: anchor || null,
    input: { commits, default_branch: defaultBranch, head, refs },
    root
  }
}

let realCache = null
function realWindows() {
  if (realCache) return realCache
  assert.ok(existsSync(HERMES), `fixture repo missing: ${HERMES}`)
  const newest = readWindow(HERMES)
  const octopusSha = git(HERMES, ['rev-list', '--all', '--min-parents=3', '-n', '1']).trim()
  assert.ok(/^[0-9a-f]{40}$/.test(octopusSha), 'no octopus merge found in hermes-agent')
  const octopus = readWindow(HERMES, octopusSha)
  const octopusView = readWindow(HERMES, octopusSha, 60)
  const pcc = readWindow(REPO)
  realCache = { newest, octopus, octopusView, pcc }
  return realCache
}

function assertTrunkIsLaneZero(label, window, layout) {
  const { input } = window
  const { defaultBranch, refs } = input
  if (!defaultBranch) return
  const ref = refs.find(item => item.name === `refs/remotes/${defaultBranch}`)
  if (!ref) return
  const row = layout.rows.find(item => item.sha === ref.sha)
  if (!row) return
  assert.equal(row.lane, 0, `${label}: ${defaultBranch} is on lane ${row.lane}`)
  const index = commitIndex(input)
  let cursor = ref.sha
  const seen = new Set()
  while (cursor && index.has(cursor) && !seen.has(cursor)) {
    seen.add(cursor)
    const at = index.get(cursor)
    assert.equal(layout.rows[at].lane, 0, `${label}: first parent chain leaves lane 0 at ${cursor.slice(0, 8)}`)
    cursor = (input.commits[at].parents || [])[0]
  }
}

test('real window: project command center (linear, master trunk)', () => {
  const { pcc } = realWindows()
  const layout = G.pccGraphLayout(pcc.input)
  checkLayout('pcc', pcc.input, layout, SVG_GEOM)
  assert.equal(layout.rows.length, Math.min(WINDOW_LIMIT, pcc.input.commits.length))
  assert.ok(layout.trunkSize > 0, 'master trunk was not found')
  assertTrunkIsLaneZero('pcc', pcc, layout)
  writeSvg('real-pcc', pcc, layout)
})

test('real window: hermes-agent newest 200 (many refs)', () => {
  const { newest } = realWindows()
  const layout = G.pccGraphLayout(newest.input)
  checkLayout('hermes-newest', newest.input, layout, SVG_GEOM)
  assert.equal(layout.rows.length, WINDOW_LIMIT)
  assert.ok(layout.maxLanes > 1, 'expected branch lanes in the hermes window')
  assertTrunkIsLaneZero('hermes-newest', newest, layout)
  writeSvg('real-hermes-newest', newest, layout)
})

test('real window: hermes-agent octopus fan', () => {
  const { octopus } = realWindows()
  const layout = G.pccGraphLayout(octopus.input)
  checkLayout('hermes-octopus', octopus.input, layout, SVG_GEOM)
  const top = octopus.input.commits[0]
  assert.ok(top.parents.length >= 3, 'anchor is not an octopus merge')
  const fan = layout.bands[0].filter(edge => edge.from === layout.rows[0].lane).map(edge => edge.to).sort((a, b) => a - b)
  assert.deepEqual(fan, top.parents.map((_, i) => i), 'octopus fan is not left to right in parent order')
  assert.equal(new Set(fan).size, top.parents.length, 'octopus parents share a lane')
  writeSvg('real-hermes-octopus', octopus, layout)
})

test('real window: hermes-agent octopus readable crop', () => {
  const { octopusView } = realWindows()
  const layout = G.pccGraphLayout(octopusView.input)
  checkLayout('hermes-octopus-60', octopusView.input, layout, SVG_GEOM)
  assert.equal(layout.rows.length, 60)
  assert.ok(layout.maxLanes > 3, 'crop lost the parallel branches')
  writeSvg('real-hermes-octopus-60', octopusView, layout)
})

// ── timing ──────────────────────────────────────────────────────────────────

function median(values) {
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

function timeLayout(input, runs) {
  for (let i = 0; i < 5; i += 1) G.pccGraphLayout(input)
  const samples = []
  for (let i = 0; i < runs; i += 1) {
    const start = performance.now()
    G.pccGraphLayout(input)
    samples.push(performance.now() - start)
  }
  return { max: Math.max(...samples), median: median(samples), min: Math.min(...samples) }
}

function synthetic16() {
  const branches = 16
  const depth = 31
  const commits = []
  for (let depthIndex = 0; depthIndex < depth; depthIndex += 1) {
    for (let branch = 0; branch < branches; branch += 1) {
      const sha = `b${branch}c${depthIndex}`
      const parent = depthIndex + 1 < depth ? `b${branch}c${depthIndex + 1}` : 'base'
      commits.push({ sha, parents: [parent], subject: sha })
    }
  }
  commits.push({ sha: 'base', parents: [], subject: 'base' })
  return {
    commits,
    default_branch: 'origin/main',
    head: { branch: 'main', detached: false, sha: 'b0c0' },
    refs: [
      { kind: 'local', name: 'refs/heads/main', sha: 'b0c0', short: 'main' },
      { kind: 'remote', name: 'refs/remotes/origin/main', sha: 'b0c0', short: 'origin/main' }
    ]
  }
}

test('timing: 500 synthetic commits across 16 lanes under 5 ms', () => {
  const input = synthetic16()
  const layout = G.pccGraphLayout(input)
  checkLayout('synthetic-16', input, layout, null)
  assert.equal(layout.maxLanes, 16, 'synthetic fixture does not hold 16 lanes')
  const timing = timeLayout(input, 50)
  console.log(`timing synthetic 16 lanes / ${layout.rows.length} commits: median ${timing.median.toFixed(3)} ms, min ${timing.min.toFixed(3)} ms, max ${timing.max.toFixed(3)} ms`)
  assert.ok(timing.median < 5, `layout median ${timing.median.toFixed(3)} ms is not under 5 ms`)
})

test('timing: real 200 commit window', () => {
  const { newest } = realWindows()
  const timing = timeLayout(newest.input, 30)
  console.log(`timing hermes-agent newest ${newest.input.commits.length} commits / ${G.pccGraphLayout(newest.input).maxLanes} lanes: median ${timing.median.toFixed(3)} ms, min ${timing.min.toFixed(3)} ms, max ${timing.max.toFixed(3)} ms`)
  assert.ok(timing.median < 20, `200 row layout median ${timing.median.toFixed(3)} ms is above 20 ms`)
})

// ── SVG proof ───────────────────────────────────────────────────────────────

function writeSvg(name, window, layout) {
  mkdirSync(OUT, { recursive: true })
  const paths = G.pccGraphPaths(layout, SVG_GEOM)
  const graphWidth = paths.width
  const rowH = SVG_GEOM.rowH
  const top = 32
  const width = 980
  const height = top + layout.rows.length * rowH + 10
  const out = []

  out.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`)
  out.push('<rect width="100%" height="100%" fill="#11151c"/>')
  out.push(`<text x="12" y="20" fill="#c8cdd6" font-family="ui-monospace, monospace" font-size="11">${xml(name)}  ${layout.rows.length} commits  ${layout.maxLanes} lanes  trunk ${layout.trunkSize}  anomalies ${layout.anomalies}</text>`)
  out.push(`<g transform="translate(0 ${top})">`)

  for (const [color, d] of Object.entries(paths.byColor)) {
    out.push(`<path d="${d}" fill="none" stroke="${PALETTE[Number(color)] || '#8a93a3'}" stroke-width="1.75" stroke-linecap="round"/>`)
  }
  for (const [color, d] of Object.entries(paths.dotsByColor)) {
    out.push(`<path d="${d}" fill="${PALETTE[Number(color)] || '#8a93a3'}" stroke="#11151c" stroke-width="1.5"/>`)
  }
  if (paths.clippedDots) out.push(`<path d="${paths.clippedDots}" fill="none" stroke="#8a93a3" stroke-width="1.25"/>`)
  if (paths.headRing) out.push(`<path d="${paths.headRing}" fill="none" stroke="${PALETTE[0]}" stroke-width="1"/>`)
  if (paths.leaders) out.push(`<path d="${paths.leaders}" fill="none" stroke="#6b7482" stroke-width="1" stroke-dasharray="2 3"/>`)

  const textX = graphWidth + 14
  layout.rows.forEach((row, i) => {
    const baseline = i * rowH + 17
    const refs = row.refs.length ? `  ${row.refs.join(' ')}` : ''
    if (refs) {
      out.push(`<text x="${SVG_GEOM.labelGutter - 6}" y="${baseline}" text-anchor="end" fill="${PALETTE[row.color]}" font-family="ui-monospace, monospace" font-size="10">${xml(tile(refs.trim(), 18))}</text>`)
    }
    const commit = window.input.commits[i]
    const when = String(commit.authored_at || '').slice(0, 10)
    const author = (commit.author && commit.author.name) || ''
    out.push(`<text x="${textX}" y="${baseline}" fill="${row.isHead ? '#eef1f6' : '#c2c8d2'}" font-family="ui-monospace, monospace" font-size="11">${xml(tile(commit.subject || row.sha, 62))}</text>`)
    out.push(`<text x="724" y="${baseline}" fill="#7d8798" font-family="ui-monospace, monospace" font-size="10">${xml(tile(when, 10))}</text>`)
    out.push(`<text x="800" y="${baseline}" fill="#98a1b0" font-family="ui-monospace, monospace" font-size="10">${xml(tile(author, 18))}</text>`)
    out.push(`<text x="924" y="${baseline}" fill="#5f8fd8" font-family="ui-monospace, monospace" font-size="10">${xml(String(row.sha).slice(0, 7))}</text>`)
  })

  out.push('</g>')
  out.push('</svg>')
  const file = join(OUT, `${name}.svg`)
  writeFileSync(file, out.join('\n'))
  console.log(`svg ${file}`)
  return file
}

test('svg: every fixture plus both real windows is written and inspectable', () => {
  mkdirSync(OUT, { recursive: true })
  const written = []
  for (const [name, fixture] of fixtureCache) {
    const layout = G.pccGraphLayout(fixture.input)
    written.push(writeSvg(`fixture-${name}`, fixture, layout))
  }
  const roots = realWindows()
  written.push(writeSvg('real-pcc', roots.pcc, G.pccGraphLayout(roots.pcc.input)))
  written.push(writeSvg('real-hermes-newest', roots.newest, G.pccGraphLayout(roots.newest.input)))
  written.push(writeSvg('real-hermes-octopus', roots.octopus, G.pccGraphLayout(roots.octopus.input)))
  for (const file of written) {
    const body = readFileSync(file, 'utf8')
    assert.match(body, /^<svg /, `${file}: not an SVG`)
    assert.match(body, /<\/svg>$/, `${file}: not closed`)
    assert.doesNotMatch(body, /NaN|Infinity|undefined/, `${file}: bad geometry token`)
    assert.ok(body.length > 500, `${file}: implausibly small`)
  }
  console.log(`svg output directory ${OUT}`)
})
