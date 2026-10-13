import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')

test('incomplete scans show failures and retained result count', () => {
  assert.ok(source.includes('missing.scan_status.complete === false'))
  assert.ok(source.includes('missing.scan_status.retained_count'))
  assert.ok(source.includes('missing.scan_status.issues'))
  assert.ok(source.includes('旧记录不代表本次确认缺集'))
})

test('pending review rows are labelled and blocked from search', () => {
  assert.ok(source.includes('v-if="item.PendingReview"'))
  assert.ok(source.includes('missing.scanning || item.PendingReview || fillBlocked(item)'))
  assert.ok(source.includes("item.PendingReview ? '待复查'"))
})
