import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')
const functions = source.slice(source.indexOf('function episodeOverrideTitle('), source.indexOf('function selectMissingAction('))

function fixture() {
  const stored = { cron: '35 3 * * *', episode_overrides: [], server_names: ['Q4'] }
  const calls = []
  const missing = { config: { ...structuredClone(stored), cron: '0 4 * * *' }, scanning: false }
  const draft = { tmdb_id: '12345', season: 3, total_episodes: 41 }
  let failSave = false
  const context = vm.createContext({
    missing, episodeOverrideDraft: draft, missingOptions: { series: [] },
    busy: { value: false }, error: { value: '' }, notice: { value: '' },
    post: async (path, body) => {
      calls.push({ path, body: structuredClone(body) })
      if (failSave) throw new Error('保存失败')
      stored.episode_overrides = structuredClone(body.config.episode_overrides)
      return { config: structuredClone(stored) }
    },
    work: async callback => {
      context.error.value = ''
      try { await callback() } catch (error) { context.error.value = error.message }
    },
  })
  vm.runInContext(`let missingConfigInitialized = true; let missingSavedConfig = ${JSON.stringify(JSON.stringify(stored))}; ${functions}`, context)
  return { context, missing, draft, stored, calls, fail: () => { failSave = true } }
}

test('adding and updating an override saves immediately without saving other draft settings', async () => {
  const { context, missing, draft, stored, calls } = fixture()
  await context.addEpisodeOverride()
  assert.deepEqual(stored.episode_overrides, [{ tmdb_id: '12345', season: 3, total_episodes: 41 }])
  assert.equal(stored.cron, '35 3 * * *')
  assert.equal(missing.config.cron, '0 4 * * *')
  assert.equal(calls[0].path, 'missing/action')
  assert.deepEqual(Object.keys(calls[0].body.config), ['episode_overrides'])
  assert.equal(JSON.parse(vm.runInContext('missingSavedConfig', context)).cron, '35 3 * * *')
  assert.deepEqual(structuredClone(missing.config.episode_overrides), stored.episode_overrides)
  draft.tmdb_id = '12345'
  draft.season = 3
  draft.total_episodes = 42
  await context.addEpisodeOverride()
  assert.deepEqual(stored.episode_overrides, [{ tmdb_id: '12345', season: 3, total_episodes: 42 }])
})

test('removing an override saves the deletion immediately', async () => {
  const { context, missing, stored, calls } = fixture()
  await context.addEpisodeOverride()
  await context.removeEpisodeOverride(0)
  assert.deepEqual(stored.episode_overrides, [])
  assert.deepEqual(structuredClone(missing.config.episode_overrides), [])
  assert.equal(calls.length, 2)
})

test('failed save keeps the input and the previous saved override visible', async () => {
  const { context, missing, draft, stored, fail } = fixture()
  fail()
  await context.addEpisodeOverride()
  assert.equal(context.error.value, '保存失败')
  assert.equal(draft.tmdb_id, '12345')
  assert.deepEqual(stored.episode_overrides, [])
  assert.deepEqual(missing.config.episode_overrides, [])
})

test('scanning prevents edits until the scan completes', async () => {
  const { context, missing, calls } = fixture()
  missing.scanning = true
  await context.addEpisodeOverride()
  assert.equal(calls.length, 0)
})
