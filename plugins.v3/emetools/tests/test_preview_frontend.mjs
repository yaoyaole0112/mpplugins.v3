import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')
const disabledExpression = source.match(/class="eme-enrich-episode"><input type="checkbox" :disabled="([^"]+)"/)[1]
const disabled = (episode, force) => vm.runInNewContext(disabledExpression, { episode, enrich: { force } })

test('manual force can select an existing DoVi thumbnail missed by automatic detection', () => {
  const episode = { status: 'keep', dovi: true, has_thumb: true }
  assert.equal(disabled(episode, false), true)
  assert.equal(disabled(episode, true), false)
  assert.equal(disabled({ ...episode, status: 'candidate' }, false), false)
  assert.equal(disabled({ ...episode, status: 'missing', has_thumb: false }, true), true)
  assert.equal(disabled({ ...episode, dovi: false }, true), true)
})

test('preview UI does not equate a negative heuristic result with guaranteed normal color', () => {
  assert.ok(source.includes("keep: '未检出明显偏色'"))
  assert.ok(source.includes('偏色检测为启发式判断，可能漏检'))
})
