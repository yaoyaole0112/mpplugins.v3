import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const read = name => fs.readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('both entrypoints use shared theme-aware workbench styles', () => {
  for (const entry of ['AppPage.vue', 'Page.vue']) {
    assert.ok(read(entry).includes("import Workbench from './Workbench.vue'"))
  }
  assert.ok(read('Workbench.vue').includes("import './glass.css'"))
})

test('glass overrides are limited to the official glass theme and plugin elements', () => {
  const css = read('glass.css')
  for (const rule of css.split('}').filter(value => value.trim())) {
    const selectors = rule.split('{')[0].split(',')
    for (const selector of selectors) {
      assert.ok(selector.trim().startsWith('html[data-theme="glass"] .eme-'))
    }
  }
  assert.ok(css.includes('.eme-app-page-host'))
  assert.ok(css.includes('.eme-dialog-page-root'))
  assert.ok(css.includes('.eme-sidebar-card'))
  assert.ok(css.includes('.eme-card'))
  assert.ok(css.includes('background: transparent !important'))
  assert.ok(css.includes('background: var(--glass-surface) !important'))
  assert.ok(css.includes('blur(var(--glass-blur-surface, var(--glass-blur)))'))
  assert.equal(/(?:width|height|position|overflow)\s*:/.test(css), false)
})

test('dialog host follows live theme variables rather than a captured theme value', () => {
  const page = read('Page.vue')
  assert.ok(page.includes("'var(--eme-dialog-background, rgb(var(--v-theme-background)))'"))
  assert.ok(read('glass.css').includes('--eme-dialog-background: var(--glass-surface-raised'))
  assert.ok(page.includes("dialogHost.setAttribute('style', originalHostStyle)"))
})

test('custom pickers and native controls share lighter glass without flattening primary buttons', () => {
  const css = read('glass.css')
  for (const selector of ['input:not([type="checkbox"])', 'select', 'textarea', '.eme-picker-trigger', '.eme-folder-choice']) {
    assert.ok(css.includes(selector))
  }
  assert.ok(css.includes('color-mix(in srgb, var(--glass-control, var(--glass-surface-soft)) 55%, transparent)'))
  assert.ok(css.includes('.eme-shell .eme-picker-menu'))
  assert.ok(css.includes('.eme-picker-trigger:focus-visible'))
  assert.equal(css.includes('.eme-shell .eme-button {'), false)
})

test('portrait fallback is a persisted shared enrichment setting with honest capability text', () => {
  const source = read('Workbench.vue')
  assert.ok(source.includes('ai_portraits: true'))
  assert.ok(source.includes('v-model="enrichSettings.draft.ai_portraits"'))
  assert.ok(source.includes('宿主模型支持原生联网搜索'))
  assert.ok(source.includes('不生成头像'))
})
