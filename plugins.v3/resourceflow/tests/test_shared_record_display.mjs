import assert from 'node:assert/strict'
import test from 'node:test'
import { claimLinkSummary, episodeLabel, releaseDetails, requestMediaSummaries, rejectionSummaries, uniqueRequestRows } from '../src/sharedRecordDisplay.js'

function file(episode = 1, extra = {}) {
  return {
    id: `file-${episode}`, path: `Show.S01E${episode}.mkv`, media_title: '凤起微月',
    media_type: 'tv', tmdb_id: 342129, season: 1, episodes: [episode],
    specs: { resolution: '2160p', effects: 'HDR', codec: 'H.265', source: 'WEB-DL', fps: 60, size: 1024 ** 3 },
    ...extra,
  }
}

test('single release puts episode before TMDB without a first-file label', () => {
  assert.equal(releaseDetails(file()),
    '凤起微月 · S01E01 · TMDB 342129 · 分辨率 2160p · 特效 HDR · 编码 H.265 · 来源 WEB-DL · 帧率 60 FPS · 大小 1.0 GB')
})

test('six physical files show real range and total size', () => {
  const files = Array.from({ length: 6 }, (_, index) => file(index + 1))
  const rows = requestMediaSummaries({ files })
  assert.equal(rows.length, 1)
  assert.match(rows[0], /^凤起微月 · S01E01-06 · TMDB 342129/)
  assert.match(rows[0], /大小 6.0 GB$/)
  assert(!rows[0].includes('首文件'))
})

test('five-file request shows the range and total once even with repeated file rows', () => {
  const files = Array.from({ length: 5 }, (_, index) => file(36 + index, {
    media_title: '走马上任', tmdb_id: 275297,
    specs: { ...file().specs, fps: index % 2 ? '' : null, size: 3543348019 },
  }))
  assert.deepEqual(requestMediaSummaries({ files: [...files, ...files] }), [
    '走马上任 · S01E36-40 · TMDB 275297 · 分辨率 2160p · 特效 HDR · 编码 H.265 · 来源 WEB-DL · 大小 16.5 GB',
  ])
})

test('identical visible summaries do not repeat within a card', () => {
  const files = [file(1, { tmdb_id: null }), file(1, { tmdb_id: null, path: 'other.mkv' })]
  assert.equal(requestMediaSummaries({ files }).length, 1)
})

test('duplicate request IDs show the latest snapshot without hiding distinct requests', () => {
  const row = { request_id: 'same', title: '走马上任', updated: 1, phase: 'queued', task_id: 'a' }
  const latest = { ...row, updated: 2, phase: 'completed' }
  const other = { ...row, request_id: 'other', phase: 'rejected', task_id: 'b' }
  assert.deepEqual(uniqueRequestRows([row, other, latest, row]), [latest, other])
})

test('linked claims show first distinct ID and total only when multiple', () => {
  assert.equal(claimLinkSummary({ claim_links: ['first', 'b', 'c', 'd', 'e', 'b'] }), 'first 等5个')
  assert.equal(claimLinkSummary({ claim_links: ['first', 'first'] }), 'first')
  assert.equal(claimLinkSummary({}), '')
})

test('episode ranges are sorted, unique, and do not invent missing episodes', () => {
  assert.equal(episodeLabel(file(1, { episodes: [9, 3, 2, 1, 5, 9, 6] })), 'S01E01-03,E05-06,E09')
})

test('one multi-episode physical file counts its size once', () => {
  const files = [file(1, { episodes: [1, 2] }), file(3)]
  assert.match(requestMediaSummaries({ files })[0], /S01E01-03.*大小 2.0 GB$/)
})

test('repeated physical path cannot inflate the total', () => {
  const files = [file(1), file(2, { path: file(1).path })]
  assert.match(requestMediaSummaries({ files })[0], /S01E01-02.*大小 1.0 GB$/)
})

test('conflicting sizes for the same path are not presented as a verified total', () => {
  const files = [file(1), file(2, { path: file(1).path, specs: { ...file().specs, size: 2 * 1024 ** 3 } })]
  assert(!requestMediaSummaries({ files })[0].includes('大小'))
})

test('a missing file size never passes off the first file size as the total', () => {
  for (const size of [null, undefined, 0, -1, NaN, Infinity, '123']) {
    const files = [file(1), file(2, { specs: { ...file().specs, size } })]
    assert(!requestMediaSummaries({ files })[0].includes('大小'))
  }
})

test('different quality files are shown separately', () => {
  for (const [key, value] of Object.entries({
    resolution: '1080p', effects: 'SDR', codec: 'H.264', source: 'BluRay', fps: 24,
  })) {
    const rows = requestMediaSummaries({ files: [file(1), file(2, { specs: { ...file().specs, [key]: value } })] })
    assert.equal(rows.length, 2, key)
    assert(rows.every(row => row.includes('大小 1.0 GB')))
  }
})

test('different seasons and media identities are never merged', () => {
  for (const extra of [{ season: 2 }, { tmdb_id: 999 }, { media_type: 'movie', episodes: [] }]) {
    assert.equal(requestMediaSummaries({ files: [file(1), file(2, extra)] }).length, 2)
  }
})

test('unidentified files are never combined into a guessed identity', () => {
  assert.equal(requestMediaSummaries({ files: [file(1, { tmdb_id: null }), file(2, { tmdb_id: null })] }).length, 2)
  assert.equal(episodeLabel({}), '季集待确认')
  assert.equal(episodeLabel({ media_type: 'movie' }), '电影')
})

test('missing Chinese title can use another file of the same verified media', () => {
  const rows = requestMediaSummaries({ files: [file(1, { media_title: '' }), file(2)] })
  assert.match(rows[0], /^凤起微月 · S01E01-02/)
})

test('aggregation does not mutate API records', () => {
  const row = { files: [file(1), file(2)] }
  const before = structuredClone(row)
  requestMediaSummaries(row)
  assert.deepEqual(row, before)
})

test('same owner and duplicate reason collapse into one line without claim IDs', () => {
  const rows = rejectionSummaries({ rejected: Array.from({ length: 6 }, (_, index) => ({
    reason: '同规格已占位或已完成', owner: '设备B', state: 'completed', claim: `claim-${index}`,
  })) })
  assert.deepEqual(rows, ['资源已下载 · 下载方：设备B'])
})

test('multiple owners are preserved in a single reason line', () => {
  assert.deepEqual(rejectionSummaries({ rejected: [
    { reason: '同规格已占位或已完成', owner: '设备B' },
    { reason: '同规格已占位或已完成', owner: '设备A' },
    { reason: '同规格已占位或已完成', owner: '设备B' },
  ] }), ['资源已下载 · 下载方：设备B、设备A'])
})

test('in-progress conflicts are distinguished from completed downloads', () => {
  assert.deepEqual(rejectionSummaries({ rejected: [
    { reason: '同规格已占位或已完成', owner: '设备A', state: 'queued' },
    { reason: '同规格已占位或已完成', owner: '设备B', state: 'reserved' },
    { reason: '同规格已占位或已完成', owner: '设备C', state: 'completed' },
  ] }), ['资源下载中 · 下载方：设备A、设备B', '资源已下载 · 下载方：设备C'])
})

test('distinct non-duplicate rejection reasons remain visible', () => {
  assert.deepEqual(rejectionSummaries({ rejected: [
    { reason: '缺少分辨率' }, { reason: '身份不明' }, { reason: '缺少分辨率' },
  ] }), ['缺少分辨率', '身份不明'])
})

test('legacy filtered requests use their recorded error only when no file reason exists', () => {
  assert.deepEqual(rejectionSummaries({ phase: 'rejected', error: '规格数值无效', rejected: [{}] }),
    ['规格数值无效'])
  assert.deepEqual(rejectionSummaries({ phase: 'rejected', error: '规格数值无效', rejected: [{ reason: '身份不明' }] }),
    ['身份不明'])
  assert.deepEqual(rejectionSummaries({ phase: 'uncertain', error: '暂不可用' }), [])
})

test('empty legacy records remain displayable', () => {
  assert.deepEqual(requestMediaSummaries({}), [])
  assert.deepEqual(rejectionSummaries({}), [])
  assert.deepEqual(rejectionSummaries({ rejected: [{}] }), [])
})
