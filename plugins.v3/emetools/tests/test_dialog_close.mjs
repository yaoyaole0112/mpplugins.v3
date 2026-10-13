import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const page = fs.readFileSync(new URL('../src/Page.vue', import.meta.url), 'utf8')
const appPage = fs.readFileSync(new URL('../src/AppPage.vue', import.meta.url), 'utf8')

test('plugin dialog close button emits the host close event', () => {
  assert.ok(page.includes("defineEmits(['close'])"))
  assert.match(page, /<button[^>]*type="button"[^>]*aria-label="关闭窗口"[^>]*@click="emit\('close'\)"/)
  assert.ok(page.includes('dialog-page @close="emit(\'close\')"'))
})

test('close button stays in dialog corner without changing sidebar app page', () => {
  const style = page.slice(page.indexOf('<style>'))
  assert.match(style, /\.eme-dialog-page-root\s*\{\s*position: relative/)
  assert.match(style, /\.eme-dialog-page-close\s*\{\s*position: absolute;\s*top: 12px;\s*right: 16px/)
  assert.ok(style.includes(':focus-visible'))
  assert.ok(!appPage.includes('eme-dialog-page-close'))
})
