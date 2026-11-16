// Proof for the board's GitHub-only filter embedded in desktop/plugin.js.
// The block is sliced out of the live file, so a change that is not self
// contained, imports something, or renders fails here instead of in the app.
//
// Run with:
//   node --test tests/github_filter.test.mjs

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN = join(resolve(HERE, '..'), 'desktop', 'plugin.js')
const BEGIN = '// github-filter:begin'
const END = '// github-filter:end'

const source = readFileSync(PLUGIN, 'utf8')
const beginAt = source.indexOf(BEGIN)
const endAt = source.indexOf(END)

assert.ok(beginAt >= 0, `${BEGIN} missing from desktop/plugin.js`)
assert.ok(endAt > beginAt, `${END} missing or before the begin marker`)
assert.equal(source.indexOf(BEGIN, beginAt + 1), -1, `${BEGIN} appears more than once`)

const block = source.slice(beginAt, endAt + END.length)
assert.doesNotMatch(block, /^\s*import\s/m, 'filter block must not import anything')
assert.doesNotMatch(block, /^\s*export\s/m, 'filter block must not export anything')
assert.doesNotMatch(block, /(?<![\w$])jsx\(/, 'filter block must not render')

vm.runInThisContext(
  `${block}\n;globalThis.__PCC_GH_FILTER__ = { GITHUB_PROBE_LIMIT, projectFolderPaths, githubProbePaths, isGithubRemote, githubProjectIds, filterProjectsByGithub }`,
  { filename: PLUGIN }
)
const F = globalThis.__PCC_GH_FILTER__

const ping = {
  id: 'ping',
  path: '/home/jawad/Ping',
  repos: [{ path: '/home/jawad/Ping' }, { path: '/home/jawad/Ping_Device' }]
}
const carRental = { id: 'car', path: '/home/jawad/car-rental-saas', repos: [] }
const jobHunt = {
  id: 'job',
  path: '/home/jawad/JobHunt',
  repos: [{ path: '/home/jawad/hiring-mail' }, { path: '/home/jawad/jobs_collector' }]
}
const studio = { id: 'studio', path: '/home/jawad/Storage/RedEast_Studio' }
const projects = [ping, carRental, jobHunt, studio]

test('every folder a project owns is probed once, primary first', () => {
  assert.deepEqual(F.projectFolderPaths(ping), ['/home/jawad/Ping', '/home/jawad/Ping_Device'])
  assert.deepEqual(F.projectFolderPaths(jobHunt), [
    '/home/jawad/JobHunt',
    '/home/jawad/hiring-mail',
    '/home/jawad/jobs_collector'
  ])
  assert.deepEqual(F.projectFolderPaths(studio), ['/home/jawad/Storage/RedEast_Studio'])
  assert.deepEqual(F.projectFolderPaths(null), [])
})

test('the probe list is unique and capped', () => {
  const paths = F.githubProbePaths(projects)

  assert.equal(new Set(paths).size, paths.length, 'no path is asked twice')
  assert.equal(paths[0], '/home/jawad/Ping')
  assert.equal(F.githubProbePaths(projects, 2).length, 2, 'the cap is honoured')
  assert.equal(F.githubProbePaths(null).length, 0)
  assert.ok(F.githubProbePaths(projects).length <= F.GITHUB_PROBE_LIMIT)
})

test('only a real github.com remote counts', () => {
  assert.equal(F.isGithubRemote('git@github.com:Jawad-02/RedEast-Ping.git'), true)
  assert.equal(F.isGithubRemote('https://github.com/Jawad-02/RedEast-Ping.git'), true)
  assert.equal(F.isGithubRemote('ssh://git@github.com/0xgani/car-rental-saas.git'), true)
  assert.equal(F.isGithubRemote('https://gitlab.com/a/b.git'), false)
  assert.equal(F.isGithubRemote('git@github.com.evil.test:a/b.git'), false)
  assert.equal(F.isGithubRemote('git@notgithub.com:a/b.git'), false)
  assert.equal(F.isGithubRemote(''), false)
  assert.equal(F.isGithubRemote(undefined), false)
  assert.equal(F.isGithubRemote(null), false)
})

test('a project counts when ANY of its folders is on GitHub', () => {
  const ids = F.githubProjectIds(projects, {
    '/home/jawad/Ping': 'git@github.com:Jawad-02/RedEast-Ping.git',
    '/home/jawad/Ping_Device': '',
    '/home/jawad/car-rental-saas': 'git@github.com:0xgani/car-rental-saas.git',
    '/home/jawad/JobHunt': '',
    '/home/jawad/hiring-mail': '',
    '/home/jawad/jobs_collector': '',
    '/home/jawad/Storage/RedEast_Studio': ''
  })

  assert.deepEqual([...ids].sort(), ['car', 'ping'])
  assert.equal(F.githubProjectIds(projects, { '/home/jawad/Ping_Device': 'git@github.com:a/b.git' }).has('ping'), true)
  assert.equal(F.githubProjectIds(projects, null).size, 0)
})

test('unchecked shows everything and an unanswered probe never narrows', () => {
  const ids = F.githubProjectIds(projects, { '/home/jawad/Ping': 'git@github.com:a/b.git' })

  assert.equal(F.filterProjectsByGithub(projects, ids, false).length, 4, 'off means every project')
  assert.equal(F.filterProjectsByGithub(projects, null, true).length, 4, 'unknown means every project')
  assert.deepEqual(F.filterProjectsByGithub(projects, ids, true).map(p => p.id), ['ping'])
  assert.deepEqual(F.filterProjectsByGithub(projects, undefined, true).map(p => p.id), [
    'ping',
    'car',
    'job',
    'studio'
  ])
  assert.deepEqual(F.filterProjectsByGithub(null, ids, true), [])
})
