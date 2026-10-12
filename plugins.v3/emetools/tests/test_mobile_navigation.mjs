import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')

test('mobile dropdown lists the same sections and uses the existing switching handler', () => {
  const markup = source.slice(source.indexOf('<div class="eme-mobile-navigation">'), source.indexOf('<nav class="eme-sidebar-card"'))
  assert.ok(markup.includes(':value="active"'))
  assert.ok(markup.includes('@change="chooseSection($event.target.value)"'))
  assert.ok(markup.includes('v-for="section in sections"'))
  assert.ok(markup.includes(':value="section.key"'))
  assert.ok(markup.includes('aria-label="选择媒体增强功能"'))
})

test('desktop keeps the original navigation and only mobile hides it', () => {
  const additions = source.slice(source.lastIndexOf('.eme-mobile-navigation{display:none}'))
  assert.ok(additions.startsWith('.eme-mobile-navigation{display:none}'))
  assert.ok(additions.includes('@media(max-width:760px){'))
  assert.ok(additions.includes('.eme-sidebar .eme-sidebar-card{display:none}'))
  assert.ok(additions.includes('.eme-mobile-navigation{display:block;'))
  assert.ok(source.includes('@click="chooseSection(section.key)"'))
})
