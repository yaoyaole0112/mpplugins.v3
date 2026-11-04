import assert from 'node:assert/strict'
import { readFile, access } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import * as vue from 'vue'

const entryUrl = new URL('../dist/assets/remoteEntry.js', import.meta.url)
const styles = []
globalThis.document = {
  createElement: () => ({}),
  head: { appendChild: element => styles.push(element.href) },
}

test('built federation entry resolves all public components and styles', async () => {
  const source = await readFile(entryUrl, 'utf8')
  assert.equal(source.includes('${__federation_expose_'), false)
  const remote = await import(entryUrl.href)
  remote.init({ vue: { [vue.version]: { get: () => () => vue, loaded: true } } })
  for (const name of ['./Page', './Config', './AppPage']) {
    const factory = await remote.get(name)
    const component = factory()
    assert.equal(typeof component.setup, 'function', name)
  }
  assert.ok(styles.length > 0)
  for (const style of styles) await access(fileURLToPath(style))
})

test('saving settings does not prompt, while destructive actions retain in-page confirmation', async () => {
  const source = await readFile(new URL('../src/Panel.vue', import.meta.url), 'utf8')
  assert.match(source, /role="alertdialog"/)
  assert.match(source, /function askConfirmation\(/)
  const saveSettings = source.match(/async function saveSettings\(\) \{([\s\S]*?)\n\}/)?.[1]
  assert.ok(saveSettings)
  assert.equal(saveSettings.includes('askConfirmation('), false)
  assert.equal(source.includes('window.confirm('), false)
})
