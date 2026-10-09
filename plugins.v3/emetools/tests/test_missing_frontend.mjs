import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')
const loading = source.slice(source.indexOf('async function loadMissing()'), source.indexOf('function scheduleMissingPoll()'))
const command = source.slice(source.indexOf('async function missingCommand('), source.indexOf('function downloadMissingCsv()'))
const config = () => ({ server_names: ['Q4'], library_names: ['国产剧'], skip_series_ids: [], episode_overrides: [], auto_cancel_enabled: false, auto_cancel_mode: 'ended_or_aired' })

test('template exposes one cancellation switch and a mode picker', () => {
  assert.equal((source.match(/v-model="missing.config.auto_cancel_enabled"/g) || []).length, 1)
  assert.equal(source.includes('v-model="missing.config.auto_cancel_completed"'), false)
  assert.equal(source.includes('v-model="missing.config.auto_cancel_aired_season"'), false)
  assert.ok(source.includes('aria-label="自动取消判定方式"'))
})

test('template exposes episode correction controls', () => {
  assert.ok(source.includes('集数修正'))
  assert.ok(source.includes('episode_overrides'))
  assert.ok(source.includes('添加修正'))
  assert.ok(source.includes('auto_episode_correction'))
  assert.ok(source.includes('自动修正集数'))
  assert.ok(source.includes('<th>总集数</th>'))
  assert.equal(source.includes('总集数判定'), false)
  assert.ok(source.includes('{{ item.TotalEpisodes }} 集'))
  assert.equal(source.includes('TotalEpisodesSource'), false)
  assert.ok(source.includes('text-align:center;vertical-align:middle'))
  assert.ok(source.includes('<th>剧集名称</th><th>在播状态</th><th>缺失季度</th>'))
  assert.ok(source.includes('item.AiringStatus'))
  assert.equal(source.includes('导出 CSV'), false)
  assert.equal(source.includes('downloadMissingCsv'), true)
})

test('AppPage stays full-page while dialog layout is isolated', () => {
  assert.ok(source.includes('.eme-shell--app{box-sizing:border-box;gap:22px;padding:18px 22px 22px;min-height:0;width:100%;max-width:100%;overflow:hidden}'))
  assert.ok(source.includes('.eme-shell--dialog{box-sizing:border-box;gap:22px;padding:22px 28px 26px;min-height:100%;width:100%;height:100%;overflow:hidden;background:rgb(var(--v-theme-background))}'))
  assert.ok(source.includes('.eme-shell--app .eme-main{padding:44px 0 20px}'))
  assert.ok(source.includes('.eme-shell.eme-shell--app{height:calc(100dvh - 88px);min-height:0'))
  assert.ok(source.includes('.eme-shell--app .eme-main{min-height:0;overflow-y:auto'))
  assert.ok(source.includes('html:has(.eme-shell--app)),:global(body:has(.eme-shell--app)),:global(.v-application:has(.eme-shell--app)),:global(.v-layout:has(.eme-shell--app)),:global(.v-main:has(.eme-shell--app)){overflow:hidden'))
})

test('AppPage adjusts the host dialog instead of relying only on scoped CSS', () => {
  const appPage = readFileSync(new URL('../src/AppPage.vue', import.meta.url), 'utf8')
  const page = readFileSync(new URL('../src/Page.vue', import.meta.url), 'utf8')
  assert.ok(appPage.includes('onMounted'))
  assert.ok(page.includes('v-overlay__content'))
  assert.ok(page.includes('[role="dialog"]'))
  assert.ok(page.includes("node.style.setProperty('width', '94vw'"))
  assert.ok(page.includes('dialog-page'))
})

function fixture() {
  const calls = []
  const saved = config()
  const context = vm.createContext({
    missing: { config: config() }, active: { value: 'subscription' },
    notice: { value: '' }, missingConfigInitialized: false, missingSavedConfig: '',
    missingScanPendingId: 0, missingResultsPage: { value: 1 }, scheduleMissingPoll() {},
    get: async () => ({ config: structuredClone(saved), scanning: false, results: [] }),
    post: async (path, payload) => {
      calls.push(structuredClone(payload))
      if (payload.config) Object.assign(saved, structuredClone(payload.config))
      return { message: 'ok', scan_id: 1 }
    },
    work: async callback => callback(),
  })
  vm.runInContext(`${loading}\n${command}`, context)
  return { context, calls, saved }
}

test('switching pages and refreshing preserve library selections and toggle', async () => {
  const { context } = fixture()
  await context.loadMissing()
  context.missing.config.library_names.push('日韩剧')
  context.missing.config.auto_cancel_enabled = true
  context.missing.config.auto_cancel_mode = 'ended'
  await context.loadMissing()
  await context.loadMissing()
  assert.deepEqual(Array.from(context.missing.config.library_names), ['国产剧', '日韩剧'])
  assert.equal(context.missing.config.auto_cancel_enabled, true)
  assert.equal(context.missing.config.auto_cancel_mode, 'ended')
})

test('late initial response cannot overwrite a newer selection', async () => {
  const { context, saved } = fixture()
  let resolveStatus
  context.get = () => new Promise(resolve => { resolveStatus = resolve })
  const pending = context.loadMissing()
  context.missing.config.library_names.push('日韩剧')
  resolveStatus({ config: saved, scanning: false })
  await pending
  assert.deepEqual(Array.from(context.missing.config.library_names), ['国产剧', '日韩剧'])
})

test('manual scan saves current scope and cancellation setting first', async () => {
  const { context, calls } = fixture()
  await context.loadMissing()
  context.missing.config.library_names.push('日韩剧')
  context.missing.config.auto_cancel_enabled = true
  await context.missingCommand('scan')
  assert.deepEqual(calls.map(item => item.operation), ['save', 'scan'])
  assert.deepEqual(Array.from(calls[0].config.library_names), ['国产剧', '日韩剧'])
  assert.equal(calls[0].config.auto_cancel_enabled, true)
  assert.equal(calls[0].config.auto_cancel_mode, 'ended_or_aired')
  await context.missingCommand('scan')
  assert.deepEqual(calls.map(item => item.operation), ['save', 'scan', 'scan'])
})

test('save failure prevents scanning with stale configuration', async () => {
  const { context } = fixture()
  await context.loadMissing()
  context.missing.config.library_names.push('日韩剧')
  const operations = []
  context.post = async (path, payload) => {
    operations.push(payload.operation)
    throw new Error('保存失败')
  }
  await assert.rejects(context.missingCommand('scan'), /保存失败/)
  assert.deepEqual(operations, ['save'])
})

test('switch off is saved without losing the selected rule', async () => {
  const { context, calls } = fixture()
  await context.loadMissing()
  context.missing.config.auto_cancel_enabled = true
  context.missing.config.auto_cancel_mode = 'ended'
  await context.missingCommand('save')
  context.missing.config.auto_cancel_enabled = false
  await context.missingCommand('save')
  await context.loadMissing()
  assert.equal(calls[1].config.auto_cancel_enabled, false)
  assert.equal(context.missing.config.auto_cancel_enabled, false)
  assert.equal(context.missing.config.auto_cancel_mode, 'ended')
})

test('concurrent initial responses do not reset initialized config', async () => {
  const { context, saved } = fixture()
  const resolvers = []
  context.get = () => new Promise(resolve => resolvers.push(resolve))
  const first = context.loadMissing()
  const second = context.loadMissing()
  resolvers[1]({ config: structuredClone(saved), scanning: false })
  await second
  context.missing.config.library_names.push('日韩剧')
  resolvers[0]({ config: structuredClone(saved), scanning: false })
  await first
  assert.deepEqual(Array.from(context.missing.config.library_names), ['国产剧', '日韩剧'])
})
