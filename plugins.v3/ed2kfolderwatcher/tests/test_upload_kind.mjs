import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { uploadKindBadge } from '../src/uploadKind.js'

test('instant uploads show a badge without depending on forwarding being enabled', () => {
  const item = { upload_mode: 'mp', upload_kind: 'instant', ordinary_upload_telegram_enabled: false }
  const badge = uploadKindBadge(item)
  assert.equal(badge.kind, 'instant')
  assert.equal(badge.label, '秒传')
  assert.match(badge.title, /不触发FUCK孤本转发/)
  assert.equal(item.upload_kind, 'instant')
  assert.equal(Object.hasOwn(item, 'upload_badge'), false)
})

test('ordinary uploads are labelled separately from delivery success', () => {
  for (const sent of [false, true]) {
    const badge = uploadKindBadge({
      upload_mode: 'direct', upload_kind: 'ordinary', ordinary_upload_telegram_sent: sent,
    })
    assert.equal(badge.kind, 'ordinary')
    assert.equal(badge.label, '普通上传')
    assert.match(badge.title, /投递结果另行显示/)
  }
})

test('missing, unknown and unrecognized upload kinds never imply ordinary upload', () => {
  for (const mode of ['mp', 'direct', 'clouddrive']) {
    for (const kind of [undefined, null, '', 'unknown', 'toString', 'constructor', 'unexpected']) {
      const badge = uploadKindBadge({ upload_mode: mode, upload_kind: kind })
      assert.equal(badge.kind, 'unknown', `${mode}: ${kind}`)
      assert.equal(badge.label, '上传方式未知')
    }
  }
  assert.equal(uploadKindBadge({ upload_state: 'upload_queued' }).kind, 'unknown')
  assert.equal(uploadKindBadge({ upload_kind: 'instant' }).label, '秒传')
})

test('plain local and cloud monitoring records do not invent upload activity', () => {
  for (const source_type of ['local', 'cloud']) {
    assert.equal(uploadKindBadge({ source_type }), null)
    assert.equal(uploadKindBadge({
      source_type, upload_mode: '', upload_state: '', upload_kind: '',
    }), null)
  }
})

test('record badges stay inline with file size and retain separate delivery status', async () => {
  const page = await readFile(new URL('../src/Page.vue', import.meta.url), 'utf8')
  assert.match(page, /upload_badge: uploadKindBadge\(item\)/)
  assert.match(page, /文件大小：[^\n]*class="upload-kind-tag"[^\n]*生成时间：/)
  assert.match(page, /:aria-label="`上传方式：\$\{item\.upload_badge\.label\}`"/)
  assert.match(page, /item\.display_status\.steps/)
  assert.match(page, /step\.label \}\}：\{\{ step\.text/)
  assert.equal(page.includes("ordinary_upload_telegram_sent ? '已投递'"), false)
})
