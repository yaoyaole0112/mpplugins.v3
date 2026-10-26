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
  assert.equal(/(?:width|height|overflow)\s*:/.test(css), false)
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

test('open picker cards rise above sibling glass stacking contexts only while a menu exists', () => {
  const css = read('glass.css')
  const rule = css.match(/\.eme-card:has\(\.eme-picker-menu\)\s*\{([^}]+)\}/)?.[1]
  assert.ok(rule?.includes('position: relative'))
  assert.ok(rule?.includes('z-index: 40'))
  assert.ok(css.includes('.eme-card:has(.eme-picker-menu):focus-within'))
  assert.ok(css.includes('.eme-picker:has(> .eme-picker-menu)'))
  assert.equal(/\.eme-card\s*\{[^}]*z-index:/.test(css), false)
})

test('dropdown menus blend host glass with a readable translucent surface and stronger frost', () => {
  const rule = read('glass.css').match(/\.eme-shell \.eme-picker-menu\s*\{([^}]+)\}/)?.[1]
  assert.ok(rule?.includes('--eme-picker-glass: color-mix(in srgb, var(--glass-surface-raised'))
  assert.ok(rule?.includes('rgba(var(--v-theme-surface), .8)'))
  assert.ok(rule?.includes('background: linear-gradient('))
  assert.ok(rule?.includes('color: rgb(var(--v-theme-on-surface))'))
  assert.ok(rule?.includes('blur(max(28px,'))
  assert.ok(rule?.includes('saturate(1.12)'))
  assert.equal(rule?.includes('.96'), false)
})

test('open cards retain their frost on a noninteractive backdrop so nested menus can blur sibling content', () => {
  const css = read('glass.css')
  const card = css.match(/\.eme-card:has\(\.eme-picker-menu\)\s*\{([^}]+)\}/)?.[1]
  const backdrop = css.match(/\.eme-card:has\(\.eme-picker-menu\)::before\s*\{([^}]+)\}/)?.[1]
  assert.ok(card?.includes('backdrop-filter: none'))
  for (const declaration of ['pointer-events: none', 'z-index: -1', 'background: var(--glass-surface)', 'backdrop-filter: blur(']) {
    assert.ok(backdrop?.includes(declaration))
  }
})

test('search and selected rows stay legible without reverting to solid colored blocks', () => {
  const css = read('glass.css')
  const selected = css.match(/\.eme-picker-menu \.eme-picker-option\.selected\s*\{([^}]+)\}/)?.[1]
  assert.ok(selected?.includes('background: rgba(var(--v-theme-primary), .2)'))
  assert.ok(selected?.includes('color: rgb(var(--v-theme-on-surface))'))
  assert.ok(css.includes('.eme-picker-option:focus-visible'))
  assert.ok(css.includes('.eme-picker-option.selected i'))
  assert.ok(css.includes('background: rgba(var(--v-theme-on-surface), .055) !important'))
})
