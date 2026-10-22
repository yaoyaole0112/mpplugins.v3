import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { parse, compileStyle } from '@vue/compiler-sfc'
import postcss from 'postcss'

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
  assert.ok(source.includes('.eme-shell .eme-missing-results table{width:900px;min-width:900px}'))
  assert.ok(source.includes('.eme-shell--dialog .eme-move-row{display:grid;grid-template-columns:28px minmax(0,1fr) 28px'))
})

test('both mobile entries share hit wrapping, table scrolling and pagination without changing desktop', () => {
  const descriptor = parse(source).descriptor
  const styles = postcss.parse(descriptor.styles[0].content)
  const expected = new Map([
    ['.eme-shell .eme-hit', ['display', 'block']],
    ['.eme-shell .eme-hit-delivery', ['display', 'block']],
    ['.eme-shell .eme-missing-results', ['min-width', '0']],
    ['.eme-shell .eme-missing-results::before', ['display', 'block']],
    ['.eme-shell .eme-missing-table-scroll', ['overflow-x', 'auto']],
    ['.eme-shell .eme-missing-table-scroll:focus-visible', ['outline-offset', '2px']],
    ['.eme-shell .eme-missing-results table', ['min-width', '900px']],
    ['.eme-shell .eme-pagination', ['flex-wrap', 'wrap']],
    ['.eme-shell .eme-pagination>span', ['flex', '1 1 100%']],
  ])
  for (const [selector, [property, value]] of expected) {
    const rules = []
    styles.walkRules(selector, rule => rules.push(rule))
    assert.equal(rules.length, 1, selector)
    assert.equal(rules[0].parent.name, 'media', selector)
    assert.equal(rules[0].parent.params, '(max-width:760px)', selector)
    assert.ok(rules[0].nodes.some(node => node.prop === property && node.value === value), selector)
    assert.equal(source.includes(selector.replace('.eme-shell ', '.eme-shell--dialog ') + '{'), false, selector)
  }
  assert.ok(source.includes('.eme-shell .eme-hit{display:block;min-width:0;line-height:1.55;overflow-wrap:anywhere}'))
  assert.ok(source.includes('.eme-shell .eme-hit-delivery{display:block;min-width:0;max-width:100%;margin-top:4px;'))
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
