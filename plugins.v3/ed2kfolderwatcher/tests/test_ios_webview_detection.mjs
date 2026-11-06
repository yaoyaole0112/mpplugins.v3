import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = await readFile(new URL('../src/useMobilePage.js', import.meta.url), 'utf8')
const detection = source.match(/export function isIosAppWebView[\s\S]*?\n}/)?.[0]
assert.ok(detection, 'iOS app WebView detector exists')
const isIosAppWebView = new Function(`${detection.replace('export ', '')}; return isIosAppWebView`)()

test('detects iOS WKWebView user agent', () => {
  assert.equal(isIosAppWebView('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148'), true)
})

test('does not alter iPhone Safari or other platforms', () => {
  assert.equal(isIosAppWebView('Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'), false)
  assert.equal(isIosAppWebView('Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36'), false)
  assert.equal(isIosAppWebView('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15'), false)
})
