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
  assert.ok(source.includes('TG 搜索补全'))
  assert.ok(source.includes('复查入库'))
  assert.ok(source.includes('clearTimeout(fillPollTimer)'))
})
