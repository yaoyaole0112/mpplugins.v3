import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import { createServer } from 'vite'

const root = fileURLToPath(new URL('../../', import.meta.url))
const profile = await mkdtemp(path.join(os.tmpdir(), 'eme-layout-browser-'))
const server = await createServer({ root, configFile: false, plugins: [(await import('@vitejs/plugin-vue')).default()], server: { host: '127.0.0.1', port: 0 } })
await server.listen()
const origin = `http://127.0.0.1:${server.httpServer.address().port}`
const browser = spawn(process.env.CHROME_BIN || '/opt/google/chrome/chrome', ['--headless=new', '--no-sandbox', '--disable-gpu', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] })
let socket
const pending = new Map()
const browserErrors = []
let sequence = 0
let sessionId
try {
  const endpoint = await new Promise((resolve, reject) => {
    let output = ''
    browser.stderr.on('data', chunk => {
      output += chunk
      const match = output.match(/DevTools listening on (ws:\/\/\S+)/)
      if (match) resolve(match[1])
    })
    browser.on('error', reject)
    browser.on('exit', code => reject(new Error(`Chrome exited ${code}`)))
  })
  socket = new WebSocket(endpoint)
  await new Promise(resolve => socket.addEventListener('open', resolve, { once: true }))
  socket.addEventListener('message', ({ data }) => {
    const message = JSON.parse(data)
    if (message.method === 'Runtime.exceptionThrown') browserErrors.push(message.params.exceptionDetails)
    if (!message.id) return
    const request = pending.get(message.id)
    pending.delete(message.id)
    if (message.error) request.reject(message.error)
    else request.resolve(message.result)
  })
  function command(method, params = {}, targetSession = sessionId) {
    return new Promise((resolve, reject) => {
      const id = ++sequence
      pending.set(id, { resolve, reject })
      socket.send(JSON.stringify({ id, method, params, ...(targetSession ? { sessionId: targetSession } : {}) }))
    })
  }
  const target = await command('Target.createTarget', { url: 'about:blank' })
  sessionId = (await command('Target.attachToTarget', { targetId: target.targetId, flatten: true })).sessionId
  await command('Runtime.enable')
  await command('Page.enable')
  async function evaluate(expression) {
    const result = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails))
    return result.result.value
  }
  async function waitFor(expression) {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (await evaluate(expression)) return
      await delay(100)
    }
    throw new Error(`Browser wait expired: ${expression}`)
  }
  for (const width of [320, 390, 430, 760, 1280]) {
    await command('Emulation.setDeviceMetricsOverride', { width, height: 844, deviceScaleFactor: 1, mobile: width <= 760 })
    await command('Page.navigate', { url: `${origin}/tests/browser/mobile_dialog.html` })
    await waitFor('window.fixtureReady && document.querySelector(".eme-hit")')
    if (width > 760) {
      assert.equal(await evaluate('document.querySelector(".eme-hit").computedStyleMap().get("display").value'), 'flex')
      assert.equal(await evaluate('document.querySelector(".v-overlay__content").style.width'), '94vw')
      assert.equal(await evaluate('getComputedStyle(document.querySelector(".eme-sidebar-card")).display === "none"'), false)
    } else {
      assert.ok(await evaluate('Math.abs(document.querySelector(".v-overlay__content").getBoundingClientRect().bottom - (visualViewport.height - 8)) < 2'))
      assert.equal(await evaluate('getComputedStyle(document.querySelector(".eme-hit")).display'), 'block')
      for (const section of ['subscription', 'missing', 'media', 'enrichment', 'invalid', 'cleanup', 'move', 'trash', 'settings']) {
        await evaluate(`(() => { const select = document.querySelector('.eme-mobile-navigation select'); select.value = '${section}'; select.dispatchEvent(new Event('change')); })()`)
        await delay(100)
        const overflow = await evaluate(`(() => {
          const main = document.querySelector('.eme-main')
          const cards = [...document.querySelectorAll('.eme-card')]
          return cards.filter(card => card.scrollWidth > card.clientWidth + 2).map(card => ({ title: card.querySelector('h3')?.textContent, width: card.clientWidth, content: card.scrollWidth }))
        })()`)
        assert.deepEqual(overflow, [], `${width}px ${section}: card overflow`)
        assert.ok(await evaluate('document.querySelector(".eme-main").clientHeight > 200'), `${width}px ${section}: usable content height`)
        assert.ok(await evaluate(`(() => { const main = document.querySelector('.eme-main'); main.scrollTop = main.scrollHeight; const last = main.querySelector('.eme-card:last-child'); return last.getBoundingClientRect().bottom <= main.getBoundingClientRect().bottom + 1 })()`), `${width}px ${section}: bottom reachable`)
        if (['media', 'enrichment', 'move'].includes(section)) {
          const opener = section === 'media' ? '清理规则' : section === 'enrichment' ? '补全设置' : null
          await evaluate(opener
            ? `Array.from(document.querySelectorAll('.eme-main button')).find(button => button.textContent.trim() === '${opener}').click()`
            : `document.querySelector('.eme-move-row>.eme-folder-choice').click()`)
          await waitFor('document.querySelector(".eme-overlay .eme-dialog")')
          assert.ok(await evaluate(`(() => { const dialog = document.querySelector('.eme-overlay .eme-dialog'); const rect = dialog.getBoundingClientRect(); return rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= visualViewport.height && dialog.scrollWidth <= dialog.clientWidth + 2 })()`), `${width}px ${section}: child dialog fits`)
          await evaluate(`Array.from(document.querySelectorAll('.eme-overlay button')).find(button => button.textContent.trim() === '关闭').click()`)
          await waitFor('!document.querySelector(".eme-overlay")')
        }
        if (section === 'missing') {
          assert.ok(await evaluate(`(() => { const table = document.querySelector('.eme-missing-table-scroll'); table.scrollLeft = table.scrollWidth; return table.scrollLeft > 0 && document.querySelector('.eme-pagination').getBoundingClientRect().width <= table.clientWidth + 1 })()`), 'results and pagination accessible')
          await evaluate(`document.querySelector('.eme-missing-results').closest('.eme-card').scrollIntoView({block:'start'})`)
          const screenshot = await command('Page.captureScreenshot', { format: 'png' })
          await writeFile(path.join(profile, `missing-${width}.png`), Buffer.from(screenshot.data, 'base64'))
        }
        if (section === 'move') {
          assert.equal(await evaluate('getComputedStyle(document.querySelector(".eme-move-row")).display'), 'grid')
          assert.ok(await evaluate(`(() => { const buttons = document.querySelectorAll('.eme-move-row>.eme-folder-choice'); return buttons[1].getBoundingClientRect().top > buttons[0].getBoundingClientRect().bottom })()`), 'source and target use separate rows')
          const screenshot = await command('Page.captureScreenshot', { format: 'png' })
          await writeFile(path.join(profile, `move-${width}.png`), Buffer.from(screenshot.data, 'base64'))
        }
      }
      const screenshot = await command('Page.captureScreenshot', { format: 'png' })
      await writeFile(path.join(profile, `mobile-${width}.png`), Buffer.from(screenshot.data, 'base64'))
      await command('Emulation.setDeviceMetricsOverride', { width, height: 520, deviceScaleFactor: 1, mobile: true })
      await delay(100)
      assert.ok(await evaluate('Math.abs(document.querySelector(".v-overlay__content").getBoundingClientRect().bottom - (visualViewport.height - 8)) < 2'), 'dialog follows viewport resize')
    }
    await evaluate('window.fixtureApp.unmount()')
    const restored = await evaluate('({ host: document.querySelector(".v-overlay__content").getAttribute("style"), original: window.originalHostStyle, hostClass: document.querySelector(".v-overlay__content").className, wrappers: document.querySelectorAll(".eme-mobile-dialog-wrapper").length })')
    assert.deepEqual({ ...restored, host: restored.host || null }, { host: null, original: null, hostClass: 'v-overlay__content', wrappers: 0 }, `host restored on close: ${JSON.stringify(restored)}`)
    console.log(`PASS: ${width}px dialog layout`)
  }
  assert.deepEqual(browserErrors, [], 'no uncaught browser errors')
} finally {
  socket?.close()
  browser.kill()
  await server.close()
  if (!process.env.KEEP_BROWSER_ARTIFACTS) await rm(profile, { recursive: true, force: true })
  else console.log(`Browser artifacts: ${profile}`)
}
