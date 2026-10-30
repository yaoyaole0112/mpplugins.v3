import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')
const backend = readFileSync(new URL('../__init__.py', import.meta.url), 'utf8')

test('local rapid upload is available from the shared sidebar and page', () => {
  assert.match(source, /key: 'rapid', title: '本地秒传'/)
  assert.match(source, /active === 'rapid'/)
  assert.match(source, /SHA1/)
  assert.match(source, /rapid\/status/)
  assert.match(source, /rapid\/config/)
  assert.match(source, /rapid\/action/)
  assert.match(source, /browse\('rapid_source'/)
  assert.match(source, /browse\('rapid_target'/)
  assert.match(source, /启用实时监控/)
  assert.match(source, /rapidConfigDirtyFor\(config\)/)
  assert.match(source, /eme-rapid-switches/)
  assert.match(source, /rapid\.realtime_watched_configs/)
})

test('rapid upload uses the shared 115 API and task coordinator', () => {
  assert.match(backend, /from \.rapid_upload import RapidUpload/)
  assert.match(backend, /self\._p115_tasks\.submit\(/)
  assert.match(backend, /rapid\/status/)
  assert.match(backend, /rapid\/config/)
  assert.match(backend, /rapid\/action/)
  assert.match(backend, /_run_rapid_realtime/)
  assert.match(backend, /set_realtime_callback/)
})
