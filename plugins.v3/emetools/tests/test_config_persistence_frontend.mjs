import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')
const monitorFunctions = source.slice(source.indexOf('async function loadMonitor('), source.indexOf('async function loadMissing(')) +
  source.slice(source.indexOf('async function monitorCommand('), source.indexOf('function logoutTelegram('))
const scheduleFunctions = source.slice(source.indexOf('async function saveSchedule('), source.indexOf('async function startScan(')) +
  source.slice(source.indexOf('async function getPreview('), source.indexOf('async function requestCleanup('))
const mediaFunctions = source.slice(source.indexOf('function applyMediaStatus('), source.indexOf('function scheduleMediaPoll(')) +
  source.slice(source.indexOf('async function mediaCommand('), source.indexOf('async function deleteMedia('))

function monitorFixture() {
  const config = { sub: { enabled: false, channels: [], keywords: [], blacklist: [] },
    kw: { enabled: false, channels: [], keywords: [], blacklist: [] } }
  const monitor = structuredClone(config)
  const drafts = structuredClone(config)
  const entry = { sub: { channels: '' }, kw: { channels: '', keywords: '', blacklist: '' } }
  const calls = []
  let failSave = false
  const context = vm.createContext({
    monitor, drafts, entry, busy: { value: false }, loading: { value: false }, monitorLoaded: { value: true },
    error: { value: '' }, notice: { value: '' }, telegram: { password_required: false },
    post: async (path, body) => {
      calls.push({ path, body: structuredClone(body) })
      if (failSave) throw new Error('保存失败')
      if (body.operation !== 'save') return { message: '操作成功' }
      config[body.scope] = { ...config[body.scope], channels: body.channels.map(value => value.replace(/^@/, '')),
        keywords: [...body.keywords], blacklist: [...body.blacklist] }
      return { config: structuredClone(config[body.scope]) }
    },
    get: async () => structuredClone(config),
    work: async callback => {
      context.busy.value = true
      context.error.value = ''
      try { await callback() } catch (error) { context.error.value = error.message }
      finally { context.busy.value = false }
    },
  })
  vm.runInContext(monitorFunctions, context)
  return { context, config, monitor, drafts, entry, calls, fail: () => { failSave = true } }
}

test('monitor add and removal persist immediately, including normalized channel names', async () => {
  const { context, config, drafts, entry, calls } = monitorFixture()
  entry.sub.channels = '@example'
  await context.addEntry('sub', 'channels')
  assert.deepEqual(structuredClone(config.sub.channels), ['example'])
  assert.deepEqual(structuredClone(drafts.sub.channels), ['example'])
  assert.equal(entry.sub.channels, '')
  assert.equal(calls[0].path, 'monitor/action')
  assert.deepEqual(Object.keys(calls[0].body).sort(), ['blacklist', 'channels', 'keywords', 'operation', 'scope'])
  entry.kw.keywords = 'Movie.*2026'
  await context.addEntry('kw', 'keywords')
  assert.deepEqual(structuredClone(config.kw.keywords), ['Movie.*2026'])
  await context.removeEntry('sub', 'channels', 0)
  assert.deepEqual(structuredClone(config.sub.channels), [])
  await context.loadMonitor()
  assert.deepEqual(structuredClone(drafts.kw.keywords), ['Movie.*2026'])
})

test('failed monitor save preserves input and saved entries', async () => {
  const { context, config, drafts, entry, fail } = monitorFixture()
  entry.kw.blacklist = '[broken'
  fail()
  await context.addEntry('kw', 'blacklist')
  assert.equal(context.error.value, '保存失败')
  assert.equal(entry.kw.blacklist, '[broken')
  assert.deepEqual(config.kw.blacklist, [])
  assert.deepEqual(drafts.kw.blacklist, [])
})

test('refreshing one monitor does not discard edits in the other', async () => {
  const { context, drafts, config } = monitorFixture()
  drafts.kw.keywords.push('未保存的草稿')
  await context.monitorCommand('save', 'sub', { channels: [], keywords: [], blacklist: [] })
  assert.deepEqual(drafts.kw.keywords, ['未保存的草稿'])
  await context.monitorCommand('send_code', 'sub')
  assert.deepEqual(drafts.kw.keywords, ['未保存的草稿'])
  assert.deepEqual(config.kw.keywords, [])
})

test('monitor refuses incomplete status and unsaved input before starting or saving', async () => {
  const { context, entry, calls, drafts } = monitorFixture()
  entry.sub.channels = '@pending'
  await context.saveMonitor('sub')
  assert.match(context.error.value, /先点击“添加”/)
  await context.toggleMonitor('sub')
  assert.match(context.error.value, /再启动监控/)
  assert.equal(calls.length, 0)
  context.get = async () => ({ configured: true })
  await assert.rejects(context.loadMonitor(), { message: /读取未完成/ })
  assert.deepEqual(drafts.sub.channels, [])
})

function scheduleFixture() {
  const schedule = { tools: {}, p115_cleanup: { enabled: false, cron: '0 3 * * *', dir_ids: [], dir_names: [] },
    p115_move: { enabled: false, check_interval: 120, rules: [] } }
  const cleanupDirs = { value: [] }
  const savedCleanupDirs = { value: '[]' }
  const savedMoveConfig = { value: JSON.stringify(schedule.p115_move) }
  const calls = []
  const context = vm.createContext({
    schedule, cleanupDirs, savedCleanupDirs, savedMoveConfig,
    settingsLoaded: { value: true }, savedSchedule: { tools: '', p115_cleanup: '', p115_trash: '' },
    cleanupDirsDirty: { get value() { return JSON.stringify(cleanupDirs.value) !== savedCleanupDirs.value } },
    rules: { get value() { return schedule.p115_move.rules } }, savedStrmRoot: { value: '/strm' },
    error: { value: '' }, notice: { value: '' }, preview: { value: null }, cleanupToken: { value: '' }, moveInfo: { value: null },
    post: async (path, payload) => { calls.push({ path, payload: structuredClone(payload) }); return { message: '配置已保存' } },
    execute: async operation => { calls.push({ operation }); return { count: 0 } },
    work: async callback => { try { await callback() } catch (error) { context.error.value = error.message } },
  })
  vm.runInContext(scheduleFunctions, context)
  return { context, schedule, cleanupDirs, savedCleanupDirs, savedMoveConfig, calls }
}

test('incomplete cleanup directory cannot report a successful save or preview', async () => {
  const { context, cleanupDirs, savedCleanupDirs, calls } = scheduleFixture()
  cleanupDirs.value.push({ cid: '', name: '' })
  await context.saveSchedule('p115_cleanup')
  assert.match(context.error.value, /选择清理目录或移除空白项/)
  assert.equal(calls.length, 0)
  cleanupDirs.value[0] = { cid: '123', name: '整理' }
  context.error.value = ''
  await context.saveSchedule('p115_cleanup')
  assert.equal(calls[0].payload.settings.dir_ids[0], '123')
  assert.equal(savedCleanupDirs.value, JSON.stringify(cleanupDirs.value))
  cleanupDirs.value[0].name = '新名称'
  await context.getPreview()
  assert.match(context.error.value, /未保存/)
  assert.equal(calls.length, 1)
})

test('incomplete move rules are drafts until explicitly saved', async () => {
  const { context, schedule, savedMoveConfig, calls } = scheduleFixture()
  schedule.p115_move.rules.push({ src_id: '', src_name: '', dst_id: '', dst_name: '' })
  await context.saveSchedule('p115_move')
  assert.match(context.error.value, /请填写每条转存规则/)
  assert.equal(calls.length, 0)
  schedule.p115_move.rules[0].src_id = '123'
  schedule.p115_move.rules[0].dst_id = '456'
  await context.saveSchedule('p115_move')
  assert.equal(calls.length, 1)
  assert.equal(savedMoveConfig.value, JSON.stringify(schedule.p115_move))
})

test('editing move rules during a pending save does not mark the new draft as saved', async () => {
  const { context, schedule, savedMoveConfig, calls } = scheduleFixture()
  schedule.p115_move.rules.push({ src_id: '123', src_name: '源', dst_id: '456', dst_name: '目标' })
  let finishSave
  context.post = async (path, payload) => {
    calls.push({ path, payload: structuredClone(payload) })
    await new Promise(resolve => { finishSave = resolve })
    return { message: '配置已保存' }
  }
  const saving = context.saveSchedule('p115_move')
  schedule.p115_move.rules[0].dst_id = '789'
  finishSave()
  await saving
  assert.equal(calls[0].payload.settings.rules[0].dst_id, '456')
  assert.equal(JSON.parse(savedMoveConfig.value).rules[0].dst_id, '456')
  assert.notEqual(savedMoveConfig.value, JSON.stringify(schedule.p115_move))
})

test('visible warnings distinguish drafts from saved actions in every affected page', () => {
  assert.match(source, /v-if="missingConfigDirty"[^>]*>运行配置或检测范围有未保存的修改/)
  assert.match(source, /v-if="cleanupDirsDirty"[^>]*>清理目录有未保存的修改/)
  assert.match(source, /v-if="moveConfigDirty"[^>]*>转存规则或监控配置有未保存的修改/)
  assert.match(source, /v-if="mediaConfigDirty"[^>]*>媒体库、定时任务或清理规则有未保存的修改/)
  for (const name of ['basicSettingsDirty', 'telegramSettingsDirty', 'fillMaxPointsDirty']) {
    assert.ok(source.includes(`v-if="${name}"`))
  }
  for (const section of ['tools', 'p115_cleanup', 'p115_trash']) {
    assert.ok(source.includes(`v-if="scheduleDirty('${section}')"`))
  }
  assert.ok(source.includes(':disabled="busy || !settingsLoaded || cleanupDirsDirty" @click="getPreview"'))
  assert.ok(source.includes(':disabled="busy || !settingsLoaded || moveConfigDirty || !rules.length" @click="runMove"'))
})

test('saving only media cleanup rules leaves other unsaved settings visible as drafts', async () => {
  const media = { config: null, result: null, running: false, last_error: '' }
  const savedMediaConfig = { value: '' }
  const calls = []
  const context = vm.createContext({
    media, savedMediaConfig, mediaScanLibraryIds: { value: [] }, mediaScanPending: { value: false },
    active: { value: 'subscription' }, notice: { value: '' }, mediaSelection: { value: [] },
    post: async (path, body) => { calls.push({ path, body: structuredClone(body) }); return { message: '已保存' } },
    loadMedia: async () => {},
    work: async callback => callback(),
  })
  vm.runInContext(`let mediaLoaded = false; ${mediaFunctions}`, context)
  context.applyMediaStatus({ config: { enabled: false, cron: '0 3 * * *', library_ids: [],
    rules: [{ id: 'size', direction: 'desc' }] }, scan_library_ids: [], running: false, result: null })
  media.config.cron = '0 4 * * *'
  media.config.rules[0].direction = 'asc'
  await context.mediaCommand('save', { config: { rules: media.config.rules } })
  assert.equal(calls[0].path, 'media-cleanup/action')
  assert.equal(JSON.parse(savedMediaConfig.value).rules[0].direction, 'asc')
  assert.equal(JSON.parse(savedMediaConfig.value).cron, '0 3 * * *')
  assert.equal(media.config.cron, '0 4 * * *')
})
