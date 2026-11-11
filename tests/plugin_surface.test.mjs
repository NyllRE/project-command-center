// Source-level proof that the Desktop half stays on the plugin SDK surface
// (catalog rule 8). The renderer may not reach the Desktop preload bridge:
// worktree list and add are this plugin's own dashboard routes, called through
// ctx.rest the same way /branch/switch is.
//
// The functions are sliced out of the live file rather than imported, so this
// fails if a future edit puts a bridge call back next to them.
//
// Run with:
//   node --test tests/plugin_surface.test.mjs

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(resolve(HERE, '..'), 'desktop', 'plugin.js')
const source = readFileSync(PLUGIN, 'utf8')

function body(name) {
  const start = source.indexOf(`function ${name}(`)

  assert.ok(start >= 0, `${name} missing from desktop/plugin.js`)

  const end = source.indexOf('\n}\n', start)

  assert.ok(end > start, `${name} has no closing brace`)

  return source.slice(start, end + 3)
}

test('the renderer never touches the Desktop preload bridge', () => {
  assert.equal((source.match(/hermesDesktop/g) || []).length, 0)
  assert.doesNotMatch(source, /window\.hermesDesktop/)
  assert.doesNotMatch(source, /desktop\.git\.worktree(Add|List)/)
  assert.doesNotMatch(source, /\/api\/git\/worktrees?/)
})

test('listWorktrees reads the plugin route through ctx.rest', () => {
  const block = body('listWorktrees')

  assert.match(block, /pluginCtx\.rest/, 'must check the ctx.rest door')
  assert.match(block, /boardRest\(/, 'must call the existing ctx.rest wrapper')
  assert.match(block, /\/worktrees\?path=/, 'must call the plugin /worktrees route')
  assert.match(block, /Promise\.resolve\(null\)/, 'null is the no-router answer')
})

test('createWorktree posts to the plugin route through ctx.rest', () => {
  const block = body('createWorktree')

  assert.match(block, /boardRest\('\/worktree'/, 'must call the plugin /worktree route')
  assert.match(block, /method: 'POST'/)
  assert.match(block, /res\.ok !== true/, 'a refusal must reject')
  assert.match(block, /res\.error/, 'the backend sentence is the rejection message')
})

test('the worktree helpers are the only door to those routes', () => {
  const uses = (source.match(/boardRest\(\s*[`']\/worktrees?/g) || []).length

  assert.equal(uses, 2, `expected two worktree calls, found ${uses}`)
  assert.equal((source.match(/\blistWorktrees\(/g) || []).length, 3,
    'definition plus the two menu call sites')
  assert.equal((source.match(/\bcreateWorktree\(/g) || []).length, 2,
    'definition plus the dialog submit')
})
