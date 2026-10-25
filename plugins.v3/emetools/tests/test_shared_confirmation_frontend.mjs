import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')
const pendingCode = source.slice(source.indexOf('async function getPending('), source.indexOf('async function cancelConfirmation('))

function fixture(confirmations, initial = {}) {
  const context = vm.createContext({
    disposed: false, execute: async () => ({ pending: [], confirmations }),
    pendingJobs: { value: [] }, trash: { value: null }, moveInfo: { value: null },
    pendingDeleteToken: { value: '' }, dataConfirmationState: { value: '' },
    scan: { value: null }, selected: { value: [] }, notice: { value: '' }, ...initial,
  })
  vm.runInContext(pendingCode, context)
  return context
}

test('TG completion and cancellation remove the web confirmation buttons', async () => {
  const context = fixture([
    { token: 'trash', kind: 'trash', state: 'completed' },
    { token: 'move', kind: 'move', state: 'cancelled' },
    { token: 'data', kind: 'manual_tools', state: 'completed' },
  ], {
    trash: { value: { token: 'trash', count: 2 } }, moveInfo: { value: { token: 'move' } },
    pendingDeleteToken: { value: 'data' }, scan: { value: { count: 1 } }, selected: { value: ['path'] },
  })
  await context.getPending()
  assert.equal(context.trash.value, null)
  assert.equal(context.moveInfo.value, null)
  assert.equal(context.pendingDeleteToken.value, '')
  assert.equal(context.scan.value, null)
  assert.equal(context.selected.value.length, 0)
})

test('TG-originated previews appear on the web and processing disables their confirmation', async () => {
  const context = fixture([
    { token: 'trash', kind: 'trash', state: 'processing', count: 5 },
    { token: 'move', kind: 'move', state: 'pending', rules: [], pending: {} },
    { token: 'data', kind: 'manual_tools', state: 'pending', count: 4 },
  ])
  await context.getPending()
  assert.equal(context.trash.value.token, 'trash')
  assert.equal(context.trash.value.state, 'processing')
  assert.equal(context.moveInfo.value.token, 'move')
  assert.equal(context.pendingDeleteToken.value, 'data')
  assert.equal(context.dataConfirmationState.value, 'pending')
  assert.match(source, /dataConfirmationState !== 'pending'/)
  assert.match(source, /if \(\['processing', 'queued'\]\.includes\(moveInfo\.value\?\.state\)\) return/)
})

test('expired confirmations disappear without resubmitting any operation', async () => {
  const context = fixture([{ token: 'old', kind: 'trash', state: 'expired' }], {
    trash: { value: { token: 'old' } },
  })
  await context.getPending()
  assert.equal(context.trash.value, null)
})

test('manual destructive and transfer actions share explicit preview and confirm APIs', () => {
  for (const operation of ['trash_request', 'trash_clear', 'move_request', 'move_confirm', 'request_delete', 'confirmation_cancel']) {
    assert.ok(source.includes(`execute('${operation}'`), operation)
  }
  assert.doesNotMatch(source, /execute\('move_run'\)/)
  const deletion = source.slice(source.indexOf('async function deleteSelected('), source.indexOf('async function confirmDelete('))
  assert.doesNotMatch(deletion, /execute\('delete'/)
  for (const entry of ['Page.vue', 'AppPage.vue']) {
    assert.match(fs.readFileSync(new URL(`../src/${entry}`, import.meta.url), 'utf8'), /<Workbench/)
  }
})
