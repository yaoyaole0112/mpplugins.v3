import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')
const functions = source.slice(source.indexOf('function fillKey('), source.indexOf('async function monitorCommand('))
const record = { ServerName: 'Emby', LibraryName: '剧集', TmdbId: '123', SeasonNum: 5, SeriesName: '半熟恋人', Year: '2021' }
function fixture(accepted = true) {
  const calls = []
  const prompts = []
  const context = vm.createContext({
    fill: { tasks: [], max_points: 4 }, fillLoaded: { value: true }, fillMaxPoints: { value: 4 },
    active: { value: 'subscription' }, error: { value: '' }, notice: { value: '' },
    fillPollTimer: null, setTimeout() {}, clearTimeout() {}, loadMissing: async () => {},
    get: async () => ({ tasks: [], busy: false, max_points: 4 }), work: async callback => callback(),
    window: { confirm: text => { prompts.push(text); return accepted } },
    post: async (path, payload) => { calls.push(payload); return { tasks: [], max_points: 4 } },
  })
  vm.runInContext(functions, context)
  return { context, calls, prompts }
}

test('resource confirmation explains cost and season-pack risk before sending', async () => {
  const { context, calls, prompts } = fixture()
  context.confirmFill({ id: 'task' }, { id: 'option', label: 'S05 [4积分]', ambiguous: true, points: 4, covered: [] })
  await new Promise(resolve => setImmediate(resolve))
  assert.match(prompts[0], /仅可能覆盖/)
  assert.match(prompts[0], /扣除 4 积分/)
  assert.match(prompts[0], /整包/)
  assert.equal(calls[0].operation, 'confirm')
  assert.equal(calls[0].confirmed, true)
})

test('dismissed confirmation never submits', () => {
  const { context, calls } = fixture(false)
  context.confirmFill({ id: 'task' }, { id: 'option', label: '资源', points: 4, covered: [12] })
  assert.equal(calls.length, 0)
})

test('free resource confirmation keeps zero cost and explicit consent', async () => {
  const { context, calls, prompts } = fixture()
  context.confirmFill({ id: 'task' }, { id: 'free', label: 'S05 [免费]', points: 0, covered: [12] })
  await new Promise(resolve => setImmediate(resolve))
  assert.match(prompts[0], /扣除 0 积分/)
  assert.equal(calls[0].option_id, 'free')
  assert.equal(calls[0].confirmed, true)
})

test('limit label sits outside the centered controls row', () => {
  assert.ok(source.includes('<label for="eme-fill-max-points">单次积分上限</label>'))
  assert.ok(source.includes('<div class="eme-inline eme-fill-limit-row"><input id="eme-fill-max-points"'))
  assert.ok(source.includes('.eme-fill-limit-row{align-items:center;margin-top:7px}'))
  assert.ok(source.includes('.eme-card .eme-fill-limit-row input{flex:0 1 140px;width:140px;height:40px;margin-top:0}'))
  assert.ok(source.includes('.eme-fill-limit-row .eme-button{height:40px;display:inline-flex;align-items:center;justify-content:center}'))
})

test('resource cards show file size and revised bot guidance', () => {
  assert.ok(source.includes('Bot 交互串行执行，期间频道转发暂缓；请勿同时手动操作此 Bot。'))
  assert.ok(!source.includes('第一版仅手动发起，不自动扣积分。'))
  assert.ok(source.includes("option.size ? ' · ' + option.size : ''"))
  assert.ok(source.includes('eme-fill-limit-hint{margin-top:16px}'))
})

test('pending same-season tasks prevent duplicate start despite changed episodes', () => {
  const { context } = fixture()
  context.fill.tasks = [{ record, state: 'uncertain' }]
  assert.equal(context.fillBlocked({ ...record, MissingEpisodes: '13' }), true)
  assert.equal(context.fillBlocked({ ...record, SeasonNum: 4 }), false)
  context.fill.tasks[0].state = 'complete'
  assert.equal(context.fillBlocked(record), false)
})

test('template blocks unknown cost, excess cost, wrong coverage and repeated resources', () => {
  assert.ok(source.includes('option.points === null || option.points > fill.max_points || option.previously_submitted'))
  assert.ok(source.includes('!option.eligible'))
  assert.ok(source.includes('搜索补全'))
  assert.equal(source.includes('TG 搜索补全'), false)
  assert.ok(source.includes('已执行搜索'))
  assert.equal(source.includes('已有待核实任务'), false)
  assert.ok(source.includes('<th>资源补全</th>'))
  assert.ok(source.includes('清除历史任务'))
  assert.ok(source.includes('clear_history'))
  assert.ok(source.includes('eme-missing-target'))
  assert.ok(source.includes('目标缺集'))
  assert.ok(source.includes('复查入库'))
  assert.ok(source.includes('clearTimeout(fillPollTimer)'))
})

test('missing results and fill task history use horizontal pagination', () => {
  assert.ok(source.includes('const missingResultsPageSize = 6'))
  assert.ok(source.includes('missing.results.slice('))
  assert.ok(source.includes('v-for="item in missingResultsPageItems"'))
  assert.ok(source.includes('aria-label="检测结果分页"'))
  assert.ok(source.includes('const fillTasksPageSize = 2'))
  assert.ok(source.includes('v-for="task in fillTasksPageItems"'))
  assert.ok(source.includes('aria-label="缺集补全任务分页"'))
  assert.ok(source.includes('function changeMissingResultsPage(delta)'))
  assert.ok(source.includes('function changeFillTasksPage(delta)'))
  assert.ok(source.includes('.eme-missing-results{overflow:visible;max-height:none;margin-top:16px}'))
})
