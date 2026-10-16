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
  assert.equal(disabled({ ...episode, status: 'missing', has_thumb: false }, false), false)
  assert.equal(disabled({ ...episode, dovi: false }, true), true)
})

test('preview UI does not equate a negative heuristic result with guaranteed normal color', () => {
  assert.ok(source.includes("keep: '未检出明显偏色'"))
  assert.ok(source.includes('偏色检测为启发式判断，可能漏检'))
})

test('wrong-sized previews can be selected and optimized without force', () => {
  assert.equal(disabled({ status: 'size', dovi: false, has_thumb: true }, false), false)
  assert.ok(source.includes("enrichmentAction('preview_size'"))
  assert.ok(source.includes('优化图片尺寸'))
  assert.ok(source.includes('尺寸比例异常，可优化'))
  assert.ok(source.includes('episode.width'))
})

test('force preference saves remotely and rolls back checkbox on failure', async () => {
  const body = source.match(/async function savePreviewForce\(event\) \{([\s\S]*?)\n\}/)[1]
  const context = {
    enrich: { force: false, forceSaving: false }, enrichSettings: { draft: {} },
    notice: { value: '' }, error: { value: '' },
    post: async (path, payload) => {
      assert.equal(path, 'enrichment/action')
      assert.equal(payload.operation, 'preview_preferences')
      return { preview_force: payload.force }
    },
  }
  const save = vm.runInNewContext(`(async function(event) {${body}})`, context)
  await save({ target: { checked: true } })
  assert.equal(context.enrich.force, true)
  context.post = async () => { throw new Error('save failed') }
  const event = { target: { checked: false } }
  await save(event)
  assert.equal(event.target.checked, true)
  assert.equal(context.enrich.forceSaving, false)
  assert.equal(context.error.value, 'save failed')
})
