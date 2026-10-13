import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'

const source = fs.readFileSync(new URL('../src/Workbench.vue', import.meta.url), 'utf8')

test('automatic enrichment label and help cover movies and series', () => {
  assert.ok(source.includes('<span>入库自动补全</span>'))
  assert.ok(!source.includes('Emby 分集入库后自动补全整剧'))
  assert.ok(source.includes('电影补全本片，分集补全所属整剧'))
})

test('shared settings include movies and distinguish episode-only settings', () => {
  assert.ok(source.includes('过滤无头像演员（电影与剧集）'))
  assert.ok(source.includes('电影／剧集／分集简介汉化'))
  assert.ok(source.includes('分集写入演职员（仅剧集，电影直接写入本片）'))
})
