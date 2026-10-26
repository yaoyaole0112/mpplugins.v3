import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')

test('subscription warnings are scoped to subscription monitoring in both entries', () => {
  assert.match(source, /v-if="scope === 'sub' && monitor\.subscription_warning" class="eme-message eme-warning"/)
  assert.match(source, /v-if="monitor\.last_error" class="eme-message eme-error"/)
  for (const entry of ['Page.vue', 'AppPage.vue']) {
    assert.match(fs.readFileSync(new URL(`../src/${entry}`, import.meta.url), 'utf8'), /<Workbench/)
  }
})
