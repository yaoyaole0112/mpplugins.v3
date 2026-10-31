import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')

test('queued actions poll status instead of replaying the original submission', () => {
  const execute = source.slice(source.indexOf('async function execute('), source.indexOf('async function work('))
  assert.ok(execute.includes("operation: 'p115_task_status', token: result.job_id"))
  assert.ok(execute.includes('result = task.result'))
  assert.ok(execute.includes("['queued', 'running'].includes(task.state)"))
  assert.equal(execute.match(/post\('action', \{ operation, \.\.\.body \}\)/g)?.length, 1)
})

test('both entrypoints share queue visibility and stop polling on unmount', () => {
  assert.ok(source.includes('手动任务优先于等待中的定时任务'))
  assert.ok(source.includes('disposed = true; clearTimeout(p115PollTimer)'))
  for (const entry of ['Page.vue', 'AppPage.vue']) {
    assert.ok(fs.readFileSync(new URL(`../src/${entry}`, import.meta.url), 'utf8').includes('<Workbench'))
  }
})

test('queue notice is lifted to the title row without taking desktop card space', () => {
  const notice = source.indexOf('class="eme-message eme-task-status"')
  const main = source.indexOf('<main class="eme-main">')
  assert.ok(notice >= 0 && notice < main)
  assert.match(source, /\.eme-task-status\{position:absolute;[^}]*top:26px;left:calc\(266px \+ clamp\(18px,4%,50px\)\);right:clamp\(18px,4%,50px\)/)
  assert.match(source, /\.eme-shell--app \.eme-task-status\{right:28px\}/)
  assert.match(source, /\.eme-shell--dialog \.eme-task-status\{left:266px;right:28px\}/)
  assert.match(source, /\.eme-main\{flex:1;min-width:0;overflow:auto;padding:76px/)
  assert.match(source, /@media\(max-width:760px\)\{\s*\.eme-task-status\{position:static/)
})

test('queued request returns the original confirmation result without resubmission', async () => {
  const calls = []
  let polls = 0
  const context = vm.createContext({
    disposed: false, notice: { value: '' },
    setTimeout: callback => { callback(); return 1 },
    post: async (path, body) => {
      calls.push(body.operation)
      if (body.operation === 'cleanup_request') return { queued: true, job_id: 'test-id' }
      assert.equal(body.token, 'test-id')
      polls++
      return polls === 1 ? { state: 'running' } : { state: 'completed', result: { ok: true, token: 'confirm-id', file_count: 3 } }
    },
  })
  vm.runInContext(source.slice(source.indexOf('async function execute('), source.indexOf('async function pollP115Tasks(')), context)
  const result = await context.execute('cleanup_request')
  assert.equal(result.token, 'confirm-id')
  assert.deepEqual(calls, ['cleanup_request', 'p115_task_status', 'p115_task_status'])
})

test('file cleanup preview creates one shared confirmation and removes the extra request step', () => {
  assert.match(source, /preview\.value = await execute\('cleanup_request'\)/)
  assert.doesNotMatch(source, /生成清理确认/)
  assert.match(source, /operation: 'cleanup_status', token/)
  assert.match(source, /status\.state === 'completed'.*preview\.value = null/s)
})
