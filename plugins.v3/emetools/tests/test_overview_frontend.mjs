import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { computed, reactive, ref, watch, nextTick } from 'vue'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')
const backend = fs.readFileSync(new URL('../__init__.py', import.meta.url), 'utf8')

test('read-only overview prechecks cover selected media and selected libraries in shared workbench', () => {
  assert.ok(source.includes("enrichmentAction('overview_preview', { series_id: enrich.selected.id })"))
  assert.ok(source.includes("enrichmentAction('overview_preview', { library_ids: [...enrich.libraryIds] })"))
  assert.ok(source.includes('只读 Emby，不写库、不调用 AI'))
  assert.ok(source.includes('来源是否可用需实际补全时核实'))
  assert.ok(backend.includes('self._enrichment.start_overview_preview(action.series_id, action.library_ids)'))
})

test('overview settings use safe defaults and describe real-source-only enrichment', () => {
  assert.ok(source.includes('overview_movie_min: 120, overview_episode_min: 40'))
  assert.ok(source.includes('overview_english_fallback: true, overview_lock: false'))
  for (const key of ['overview_lock', 'overview_english_fallback', 'overview_movie_min', 'overview_episode_min']) {
    assert.ok(source.includes(`v-model${key.endsWith('_min') ? '.number' : ''}="enrichSettings.draft.${key}"`))
  }
  assert.ok(source.includes('关闭不会解除已有字段锁'))
  assert.ok(source.includes('不为凑字数生成剧情'))
})

test('preview and persisted per-item records paginate instead of adding nested vertical scrollbars', async () => {
  const enrich = reactive({ status: { overview_preview: { items: Array.from({ length: 14 }, (_, index) => ({ id: index })) },
    records: Array.from({ length: 20 }, (_, index) => ({ id: index })) } })
  const context = vm.createContext({ ref, computed, watch, enrich })
  const script = source.slice(source.indexOf('const overviewPreviewPage ='), source.indexOf('const enrichLibraryMatches ='))
  vm.runInContext(script + '\nglobalThis.state = { overviewPreviewPage, overviewPreviewPageCount, overviewPreviewItems, enrichmentRecordsPage, enrichmentRecordsPageCount, enrichmentRecordItems }', context)
  const state = context.state
  assert.equal(state.overviewPreviewPageCount.value, 3)
  assert.equal(state.enrichmentRecordsPageCount.value, 4)
  state.overviewPreviewPage.value = 3
  state.enrichmentRecordsPage.value = 2
  assert.deepEqual(Array.from(state.overviewPreviewItems.value, item => item.id), [12, 13])
  assert.deepEqual(Array.from(state.enrichmentRecordItems.value, item => item.id), [6, 7, 8, 9, 10, 11])
  enrich.status.overview_preview.items = []
  await nextTick()
  assert.equal(state.overviewPreviewPage.value, 1)
  assert.ok(source.includes('aria-label="简介预检分页"'))
  assert.ok(source.includes('aria-label="补全记录分页"'))
})

test('settings save validates overview thresholds and persists them with other shared options', async () => {
  const draft = { max_actors: 30, cast_lock_min: 10, overview_movie_min: '120',
    overview_episode_min: '40', overview_lock: true, overview_english_fallback: true }
  const calls = []
  const context = vm.createContext({
    enrichSettings: { draft, open: true }, notice: { value: '' },
    post: async (path, values) => { calls.push({ path, values }); return { config: values } },
    work: callback => callback(),
  })
  vm.runInContext(source.slice(source.indexOf('async function saveEnrichSettings()'), source.indexOf('async function saveMetadataSource(')), context)
  await context.saveEnrichSettings()
  assert.equal(calls[0].values.overview_movie_min, 120)
  assert.equal(calls[0].values.overview_episode_min, 40)
  assert.equal(calls[0].values.overview_lock, true)
  draft.overview_movie_min = -1
  await assert.rejects(context.saveEnrichSettings(), /简介最低字数/)
  assert.equal(calls.length, 1)
})
