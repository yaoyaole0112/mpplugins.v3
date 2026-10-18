import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')

test('MoviePilot sidebar masks the actual Vuetify icon with the brand outline', () => {
  assert.ok(source.includes(':global(a[href*="EmeTools" i] :is(.nav-item-icon,.v-icon))'))
  assert.ok(source.includes("mask:url('./brand-stars.svg') center/contain no-repeat"))
  assert.ok(source.includes('::before){content:none!important}'))
  assert.ok(!source.includes('href*="/plugin-app/EmeTools/"'))
})

test('shared workbench leaves space between controls and block descriptions', () => {
  assert.ok(source.includes('.eme-card p.eme-hint{margin:14px 0 12px;line-height:1.65}'))
  assert.ok(source.includes('.eme-fill-limit-row .eme-hint{margin:0;line-height:1.5}'))
  for (const entry of ['Page.vue', 'AppPage.vue']) {
    assert.ok(fs.readFileSync(new URL(`../src/${entry}`, import.meta.url), 'utf8').includes('Workbench'))
  }
})
