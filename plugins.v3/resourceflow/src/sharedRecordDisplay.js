import { formatBytes } from './utils.js'

const specLabels = { resolution: '分辨率', effects: '特效', codec: '编码', source: '来源', fps: '帧率', size: '大小' }
const qualityKeys = Object.keys(specLabels).filter(key => key !== 'size')

export function episodeLabel(row) {
  if (row.media_type === 'movie') return '电影'
  if (!Number.isInteger(row.season)) return '季集待确认'
  const episodes = [...new Set((row.episodes || []).filter(Number.isInteger))].sort((a, b) => a - b)
  const groups = []
  for (const episode of episodes) {
    const last = groups.at(-1)
    if (last && episode === last[1] + 1) last[1] = episode
    else groups.push([episode, episode])
  }
  const pad = value => String(value).padStart(2, '0')
  return `S${pad(row.season)}${groups.map(([first, last]) =>
    `E${pad(first)}${first === last ? '' : `-${pad(last)}`}`).join(',')}`
}

export function releaseDetails(row) {
  const identity = [row.media_title, episodeLabel(row), row.tmdb_id ? `TMDB ${row.tmdb_id}` : '身份待确认']
  const specs = Object.entries(specLabels).flatMap(([key, label]) => {
    const value = row.specs?.[key]
    if (value === undefined || value === null || value === '') return []
    return [`${label} ${key === 'size' ? formatBytes(value) : key === 'fps' ? `${value} FPS` : value}`]
  })
  return [...identity.filter(Boolean), ...specs].join(' · ')
}

export function requestMediaSummaries(row) {
  const groups = new Map()
  for (const [index, file] of (row.files || []).entries()) {
    const key = JSON.stringify([
      file.media_type, file.tmdb_id || `unknown-${index}`, file.season,
      ...qualityKeys.map(field => file.specs?.[field] ?? null),
    ])
    if (!groups.has(key)) groups.set(key, { file, episodes: new Set(), sizes: new Map() })
    const group = groups.get(key)
    for (const episode of file.episodes || []) group.episodes.add(episode)
    if (!group.file.media_title && file.media_title) group.file = file
    // A physical multi-episode file contributes its size once, not once per episode.
    const physicalFile = file.path || file.id || index
    const size = file.specs?.size
    const validSize = typeof size === 'number' && Number.isFinite(size) && size > 0
    const previous = group.sizes.get(physicalFile)
    if (!group.sizes.has(physicalFile)) group.sizes.set(physicalFile, validSize ? size : null)
    else if (previous !== size) group.sizes.set(physicalFile, null)
  }
  return [...groups.values()].map(group => {
    const sizes = [...group.sizes.values()]
    const total = sizes.every(value => value !== null) ? sizes.reduce((sum, value) => sum + value, 0) : null
    const size = Number.isFinite(total) && total > 0 ? total : null
    return releaseDetails({
      ...group.file, episodes: [...group.episodes], specs: { ...group.file.specs, size },
    })
  })
}

export function rejectionSummaries(row) {
  const reasons = new Map()
  for (const item of row.rejected || []) {
    const reason = item.reason === '同规格已占位或已完成'
      ? ['reserved', 'queued'].includes(item.state) ? '资源下载中' : '资源已下载'
      : item.reason || ''
    if (!reason && !item.owner) continue
    if (!reasons.has(reason)) reasons.set(reason, new Set())
    if (item.owner) reasons.get(reason).add(item.owner)
  }
  return [...reasons].map(([reason, owners]) =>
    [reason, owners.size ? `下载方：${[...owners].join('、')}` : ''].filter(Boolean).join(' · '))
}
