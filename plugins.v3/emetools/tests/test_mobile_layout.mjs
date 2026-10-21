import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { parse, compileStyle } from '@vue/compiler-sfc'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')
const page = fs.readFileSync(new URL('../src/Page.vue', import.meta.url), 'utf8')
const mobile = source.split('@media(max-width:760px){').slice(1).join('@media(max-width:760px){')

test('mobile cards use equal internal gutters without nested page scrolling', () => {
  assert.ok(mobile.includes('padding:18px 16px calc(112px + env(safe-area-inset-bottom,0px))'))
  assert.ok(mobile.includes('flex:0 0 auto;width:100%'))
  assert.ok(mobile.includes('overflow:visible;scrollbar-gutter:auto;overscroll-behavior:auto'))
  assert.ok(mobile.includes('.eme-card{box-sizing:border-box;min-width:0;width:100%;margin-inline:auto}'))
})

test('only mobile app entry releases document and host scroll locks', () => {
  assert.ok(mobile.includes(':global(html:has(.eme-shell--app))'))
  assert.ok(mobile.includes('overflow-y:auto!important;overscroll-behavior:auto!important'))
  assert.ok(mobile.includes(':global(.eme-app-host-lock)'))
  assert.ok(mobile.includes('padding-inline:0!important;overflow:visible!important'))
  assert.ok(page.includes('.eme-dialog-page-root{display:block;min-height:0;height:100%;padding:0;overflow-x:hidden;overflow-y:auto'))
})

test('mobile plugin dialog uses the visual viewport and restores host styles', () => {
  assert.ok(page.includes('window.visualViewport'))
  assert.ok(page.includes('mobileDialogQuery.addEventListener'))
  assert.ok(page.includes("dialogHost.setAttribute('style', originalHostStyle)"))
  assert.ok(page.includes("node.style.setProperty('height', '92dvh'"))
  assert.ok(page.includes('.eme-mobile-dialog-wrapper{'))
})

test('mobile dialog keeps hit details readable and results accessible', () => {
  assert.ok(source.includes('class="eme-result eme-hit"'))
  assert.ok(source.includes('class="eme-hit-delivery"'))
  assert.ok(source.includes('class="eme-missing-table-scroll" tabindex="0"'))
  assert.ok(source.includes('.eme-shell--dialog .eme-missing-results table{width:900px;min-width:900px}'))
  assert.ok(source.includes('.eme-shell--dialog .eme-move-row{display:grid;grid-template-columns:28px minmax(0,1fr) 28px'))
})

test('mobile toast escapes clipping and clears bottom navigation with safe area', () => {
  assert.ok(source.includes('<Teleport to="body" :disabled="!mobileViewport">'))
  assert.ok(source.includes("window.matchMedia('(max-width: 760px)')"))
  assert.ok(source.includes("removeEventListener('change', syncMobileViewport)"))
  assert.ok(mobile.includes('bottom:calc(96px + env(safe-area-inset-bottom,0px))'))
  assert.ok(mobile.includes('left:max(16px,env(safe-area-inset-left,0px))'))
  const descriptor = parse(source).descriptor
  const compiled = compileStyle({ source: descriptor.styles[0].content, id: 'data-v-mobile-test', scoped: true })
  assert.deepEqual(compiled.errors, [])
  assert.ok(compiled.code.includes('.eme-toast[data-v-mobile-test]'))
})
