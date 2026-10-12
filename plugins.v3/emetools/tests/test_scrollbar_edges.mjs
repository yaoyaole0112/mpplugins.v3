import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')
const desktop = source.slice(source.indexOf('@media(min-width:761px){'), source.indexOf('@media(max-width:760px){', source.indexOf('@media(min-width:761px){')))

test('app scrollbar reaches host edge while cards retain internal right spacing', () => {
  assert.ok(desktop.includes(':global(.eme-app-host-lock){padding-right:0!important}'))
  assert.ok(desktop.includes('.eme-shell.eme-shell--app{padding-right:0}'))
  assert.ok(desktop.includes('.eme-shell--app .eme-main{padding-right:28px}'))
})

test('dialog scrollbar reaches its edge without removing card spacing', () => {
  assert.ok(desktop.includes('.eme-shell.eme-shell--dialog{padding-right:0}'))
  assert.ok(desktop.includes('.eme-shell--dialog .eme-main{padding-right:28px}'))
  assert.equal(desktop.includes('padding-left'), false)
})
