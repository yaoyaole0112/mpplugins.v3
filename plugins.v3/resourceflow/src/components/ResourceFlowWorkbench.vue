<script setup>
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue'
import TaskEditorDialog from './TaskEditorDialog.vue'
import SharedRecordPagination from './SharedRecordPagination.vue'
import SharedSettingsPanel from './SharedSettingsPanel.vue'
import {
  cloneTask,
  formatBytes,
  formatDateTime,
  formatDuration,
  normalizeSettings,
  taskStateMeta,
  torrentProgress,
  unwrapResponse,
} from '../utils'

const props = defineProps({
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'ResourceFlow' },
  initialTab: { type: String, default: 'overview' },
  showClose: { type: Boolean, default: false },
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['close', 'action'])
const loading = ref(true)
const taskLoading = ref(false)
const saving = ref(false)
const error = ref('')
// 首次状态请求完成前，不把默认的停用状态展示给用户。
const statusLoaded = ref(false)
const status = ref({
  enabled: false,
  show_sidebar_nav: true,
  summary: {},
  tasks: [],
  options: { sites: [], downloaders: [] },
})
const taskDetail = ref(null)
const selectedTaskId = ref('')
const activeTab = ref(props.initialTab)
const torrentState = ref('active')
const torrentPage = ref(1)
const editorOpen = ref(false)
const editorTask = ref(cloneTask())
const deleteDialog = ref(false)
const clearDialog = ref(false)
const settingsDialog = ref(false)
const settingsError = ref('')
const settingsTestFeedback = ref(null)
const settingsReady = ref(false)
const channelDedupDialog = ref(false)
const channelDedupLoading = ref(false)
const channelDedupError = ref('')
const sharedSettingsPanel = ref(null)
const sharedRecordsPanel = ref(null)
const sharedHost = ref(false)
const sharedRole = ref({ enabled: false, host_coordinator: false })
const sharedRecordsHost = ref(false)
const sharedRecordsLoading = ref(false)
const approvalTab = ref('requests')
const resourceRows = ref({ total: 0, items: [] })
const resourcePage = ref(1)
const activeResourceFilter = ref({ query: '', media_type: '' })
const pendingApprovalDialog = ref(false)
const pendingApprovalSaving = ref(false)
const pendingApprovalRow = ref(null)
const pendingApprovalError = ref('')
const pendingRejectingKey = ref('')
const pendingSearchOpen = ref(false)
const pendingSearchQuery = ref('')
const pendingSearchResults = ref([])
const pendingSearchLoading = ref(false)
const pendingSearchMessage = ref('')
const pendingSearchSource = ref('themoviedb')
let pendingSearchVersion = 0
const pendingApprovalForm = ref({
  media_source: 'themoviedb', media_id: '', tmdb_id: null, media_title: '', media_type: 'tv',
  season: 1, episodes: '', specs: {}, confirmed: false,
})
const resourceTypes = [
  { value: '', title: '全部类型' }, { value: 'tv', title: '剧集' }, { value: 'movie', title: '电影' },
]
let resourceSearchTimer
let resourceGeneration = 0
const ledgerLoading = ref(false)
const approvalLoading = computed(() => (
  channelDedupLoading.value || sharedRecordsLoading.value || ledgerLoading.value
))
const channelDedup = ref({ enabled: false, channels: [], attributes: [], records: [] })
const channelDedupDraft = ref({
  enabled: false, channels: '', attributes: [], size_tolerance_pct: 5,
  unknown_policy: 'exclude', identity_aliases: '', cancel_downloading: false,
  movie_identity_aliases: '',
})
const channelUnknownPolicies = [
  { value: 'exclude', title: '排除不下载' },
  { value: 'allow', title: '按未命中放行（兼容旧版）' },
]
const channelDedupAttributes = [
  { value: 'resolution', title: '分辨率' },
  { value: 'effects', title: '特效' },
  { value: 'codec', title: '编码' },
  { value: 'source', title: '来源' },
  { value: 'fps', title: '帧率' },
  { value: 'size', title: '大小' },
]
const settingsDraft = ref({
  enabled: false,
  show_sidebar_nav: true,
  global_disksize: null,
  global_maxdlcount: null,
  global_maxupspeed: null,
  global_maxdlspeed: null,
  global_proxy_delete: false,
  global_torrent_blacklist_enabled: false,
  global_delete_size_range: null,
  shared_download_timeout_minutes: 30,
  recent_missing_date_tolerance_days: 7,
  recent_missing_date_ai_enabled: false,
})
const hostToast = inject('moviepilot:toast', null)
let refreshTimer
let channelRefreshTimer

const pluginBase = computed(() => `plugin/${props.pluginId || 'ResourceFlow'}`)
const tasks = computed(() => status.value.tasks || [])
const selectedTask = computed(() => tasks.value.find(item => item.id === selectedTaskId.value) || null)
const selectedState = computed(() => taskStateMeta(selectedTask.value?.state))
const taskConfig = computed(() => taskDetail.value?.task || {})
const taskRuns = computed(() => taskDetail.value?.runs || [])
const latestBrushRun = computed(() => taskRuns.value.find(item => item.kind === 'brush') || null)
const torrentData = computed(() => taskDetail.value?.torrents || { items: [], total: 0, page: 1, page_size: 50 })
const totalTorrentPages = computed(() => Math.max(Math.ceil(torrentData.value.total / torrentData.value.page_size), 1))
const seedingPercent = computed(() => {
  const limit = Number(taskConfig.value.disksize || 0) * 1024 ** 3
  if (!limit) return 0
  return Math.min(Math.round((Number(selectedTask.value?.seeding_size || 0) * 100) / limit), 100)
})
const reasonEntries = computed(() => {
  const reasons = latestBrushRun.value?.reason_counts || {}
  return Object.entries(reasons)
    .map(([label, count]) => ({ label, count: Number(count || 0) }))
    .sort((left, right) => right.count - left.count)
})
const maxReasonCount = computed(() => Math.max(1, ...reasonEntries.value.map(item => item.count)))
const pipelineStages = computed(() => {
  const run = latestBrushRun.value || {}
  const sourceCount = Number(run.source_count || 0)
  const candidateCount = Number(run.candidate_count || 0)
  const addedCount = Number(run.added_count || 0)
  return [
    { title: '获取站点种子', detail: taskConfig.value.rss_support ? 'RSS' : '站点列表页', count: sourceCount },
    { title: '排除订阅内容', detail: `排除 ${Number(run.subscription_excluded || 0)} 个`, count: candidateCount },
    { title: '选种条件过滤', detail: `过滤 ${Number(run.filtered_count || 0)} 个`, count: addedCount },
    { title: '添加下载任务', detail: run.success === false ? '执行失败' : '下载器已确认', count: addedCount },
  ]
})

const torrentHeaders = [
  { title: '种子', key: 'title', sortable: false },
  { title: '状态', key: 'status', sortable: false },
  { title: '大小', key: 'size', align: 'end', sortable: false },
  { title: '上传', key: 'uploaded', align: 'end', sortable: false },
  { title: '分享率', key: 'ratio', align: 'end', sortable: false },
  { title: '处理条件', key: 'policy', sortable: false },
]

// 通过宿主注入的 Toast 显示操作结果，不在插件内创建第二套通知层。
function notify(text, color = 'success') {
  const method = ['error', 'info', 'warning', 'success'].includes(color) ? color : 'success'
  if (typeof hostToast?.[method] === 'function') {
    hostToast[method](text)
  } else if (method === 'error') {
    error.value = text
  }
}

// 从插件状态接口加载全局设置和任务摘要。
async function loadStatus({ preserveSelection = true, loadDetail = true, resetDraft = false } = {}) {
  loading.value = true
  error.value = ''
  try {
    const data = unwrapResponse(await props.api.get(`${pluginBase.value}/status`))
    status.value = data || status.value
    statusLoaded.value = true
    if (!settingsDialog.value || resetDraft) settingsDraft.value = {
      enabled: Boolean(status.value.enabled),
      show_sidebar_nav: status.value.show_sidebar_nav !== false,
      global_disksize: status.value.global_disksize ?? null,
      global_maxdlcount: status.value.global_maxdlcount ?? null,
      global_maxupspeed: status.value.global_maxupspeed ?? null,
      global_maxdlspeed: status.value.global_maxdlspeed ?? null,
      global_proxy_delete: Boolean(status.value.global_proxy_delete),
      global_torrent_blacklist_enabled: Boolean(status.value.global_torrent_blacklist_enabled),
      global_delete_size_range: status.value.global_delete_size_range ?? null,
      shared_download_timeout_minutes: Number(status.value.shared_download_timeout_minutes || 30),
      recent_missing_date_tolerance_days: Number(status.value.recent_missing_date_tolerance_days || 7),
      recent_missing_date_ai_enabled: Boolean(status.value.recent_missing_date_ai_enabled),
    }
    const selectedStillExists = tasks.value.some(item => item.id === selectedTaskId.value)
    if (!preserveSelection || !selectedStillExists) selectedTaskId.value = tasks.value[0]?.id || ''
    if (loadDetail && selectedTaskId.value) await loadTaskDetail()
  } catch (err) {
    error.value = err?.message || '加载刷流任务失败'
  } finally {
    loading.value = false
  }
}

// 加载选中任务的配置、诊断记录和当前分页种子。
async function loadTaskDetail() {
  if (!selectedTaskId.value) {
    taskDetail.value = null
    return
  }
  taskLoading.value = true
  try {
    const query = `state=${torrentState.value}&page=${torrentPage.value}&page_size=50`
    taskDetail.value = unwrapResponse(
      await props.api.get(`${pluginBase.value}/tasks/${selectedTaskId.value}?${query}`),
    )
  } catch (err) {
    error.value = err?.message || '加载任务详情失败'
  } finally {
    taskLoading.value = false
  }
}

// 切换左侧任务并从第一页加载对应明细。
async function selectTask(taskId) {
  if (!taskId || taskId === selectedTaskId.value) return
  selectedTaskId.value = taskId
  torrentState.value = 'active'
  torrentPage.value = 1
  await loadTaskDetail()
}

// 同时刷新任务摘要和当前任务详情。
async function refreshAll(showMessage = false) {
  await loadStatus()
  if (showMessage) notify('刷流任务数据已刷新')
  emit('action')
}

// 打开一个空白任务草稿。
function openCreateTask() {
  editorTask.value = cloneTask()
  editorOpen.value = true
}

// 打开当前任务的服务端配置快照。
function openEditTask() {
  if (!taskConfig.value.id) return
  editorTask.value = cloneTask(taskConfig.value)
  editorOpen.value = true
}

// 通过任务 API 创建或更新任务并刷新调度状态。
async function saveTask(task) {
  saving.value = true
  try {
    const response = task.id
      ? await props.api.put(`${pluginBase.value}/tasks/${task.id}`, task)
      : await props.api.post(`${pluginBase.value}/tasks`, task)
    const detail = unwrapResponse(response)
    editorOpen.value = false
    selectedTaskId.value = detail?.task?.id || task.id || selectedTaskId.value
    await loadStatus()
    notify(task.id ? '刷流任务已更新' : '刷流任务已创建')
  } catch (err) {
    notify(err?.message || '保存刷流任务失败', 'error')
  } finally {
    saving.value = false
  }
}

// 切换当前任务启停状态并立即重建宿主调度。
async function toggleSelectedTask() {
  if (!selectedTask.value) return
  saving.value = true
  try {
    unwrapResponse(
      await props.api.post(`${pluginBase.value}/tasks/${selectedTaskId.value}/state`, {
        enabled: !selectedTask.value.enabled,
      }),
    )
    await loadStatus()
    notify(selectedTask.value?.enabled ? '刷流任务已启用' : '刷流任务已暂停')
  } catch (err) {
    notify(err?.message || '更新任务状态失败', 'error')
  } finally {
    saving.value = false
  }
}

// 提交一次刷流刷新或种子检查操作。
async function runOperation(operation) {
  if (!selectedTaskId.value) return
  saving.value = true
  try {
    const path = operation === 'brush' ? 'run' : 'check'
    unwrapResponse(await props.api.post(`${pluginBase.value}/tasks/${selectedTaskId.value}/${path}`, {}))
    await loadStatus()
    notify(operation === 'brush' ? '刷流刷新已提交' : '种子检查已提交')
  } catch (err) {
    notify(err?.message || '提交任务失败', 'error')
  } finally {
    saving.value = false
  }
}

async function openSettings() {
  settingsError.value = ''
  settingsTestFeedback.value = null
  settingsReady.value = false
  settingsDialog.value = true
  await loadStatus({ loadDetail: false, resetDraft: true })
  const loaded = await loadChannelDedup()
  settingsReady.value = loaded && !error.value
}

// 依次保存共享连接、协调端规则及原全局设置；失败时保留弹窗。
async function saveSettings() {
  saving.value = true
  settingsError.value = ''
  try {
    if (!settingsReady.value || !sharedSettingsPanel.value) throw new Error('全局设置尚未加载，请关闭后重试')
    const payload = normalizeSettings(settingsDraft.value)
    const shared = await sharedSettingsPanel.value.save()
    await saveChannelDedup(shared)
    status.value = unwrapResponse(await props.api.post(`${pluginBase.value}/settings`, payload))
    await loadStatus({ resetDraft: true })
    notify('全局设置已保存')
  } catch (err) {
    settingsError.value = `全局设置未全部保存：${err?.message || '保存失败'}`
  } finally {
    saving.value = false
  }
}

async function loadChannelDedup({ resetDraft = true } = {}) {
  channelDedupLoading.value = true
  channelDedupError.value = ''
  try {
    const data = unwrapResponse(await props.api.get(`${pluginBase.value}/channel-dedup`))
    channelDedup.value = data || channelDedup.value
    if (resetDraft) {
      channelDedupDraft.value = {
        enabled: Boolean(data?.enabled),
        cancel_downloading: Boolean(data?.cancel_downloading),
        channels: (data?.channels || []).join('\n'),
        attributes: [...(data?.attributes || [])],
        size_tolerance_pct: Number(data?.size_tolerance_pct ?? 5),
        unknown_policy: data?.unknown_policy || 'exclude',
        identity_aliases: Object.entries(data?.identity_aliases || {}).map(([name, id]) => `${name} = ${id}`).join('\n'),
        movie_identity_aliases: Object.entries(data?.movie_identity_aliases || {}).map(([name, id]) => `${name} = ${id}`).join('\n'),
      }
    }
    return true
  } catch (err) {
    channelDedupError.value = err?.message || '读取资源去重设置失败'
    return false
  } finally {
    channelDedupLoading.value = false
  }
}

async function openChannelDedup() {
  approvalTab.value = 'requests'
  sharedRecordsHost.value = false
  channelDedupDialog.value = true
  await Promise.all([
    loadChannelDedup(),
    approvalTab.value === 'pending' ? loadResourceRows() : Promise.resolve(),
  ])
}

async function loadResourceRows() {
  clearTimeout(resourceSearchTimer)
  const generation = ++resourceGeneration
  ledgerLoading.value = true
  const params = new URLSearchParams({
    state: 'pending', page: String(resourcePage.value), page_size: '20',
    query: activeResourceFilter.value.query || '', media_type: activeResourceFilter.value.media_type || '',
  })
  try {
    const data = unwrapResponse(await props.api.get(`${pluginBase.value}/resource-ledger?${params}`))
    if (generation === resourceGeneration) {
      resourceRows.value = data || { total: 0, items: [] }
      resourcePage.value = data?.page || 1
    }
  } catch (err) {
    if (generation === resourceGeneration) channelDedupError.value = err?.message || '读取下载待核验记录失败'
  } finally {
    if (generation === resourceGeneration) ledgerLoading.value = false
  }
}

watch(() => [activeResourceFilter.value.query, activeResourceFilter.value.media_type],
  (values, previous) => {
    clearTimeout(resourceSearchTimer)
    resourceGeneration += 1
    resourcePage.value = 1
    // Keep the previous records visible while the filtered result is loading.
    ledgerLoading.value = true
    const delay = values[1] === previous[1] ? 300 : 0
    resourceSearchTimer = setTimeout(loadResourceRows, delay)
  })
watch(approvalTab, value => {
  if (value === 'pending') loadResourceRows()
})

async function refreshApprovalRecords() {
  await Promise.all([
    sharedRecordsPanel.value?.refresh(),
    loadChannelDedup({ resetDraft: false }),
    approvalTab.value === 'pending' ? loadResourceRows() : Promise.resolve(),
  ])
}

async function saveChannelDedup(shared) {
  const data = unwrapResponse(await props.api.post(`${pluginBase.value}/channel-dedup/settings`, {
    enabled: false,
    cancel_downloading: false,
    ...(shared.host_coordinator ? {
      attributes: channelDedupDraft.value.attributes,
      size_tolerance_pct: channelDedupDraft.value.size_tolerance_pct,
      unknown_policy: channelDedupDraft.value.unknown_policy,
    } : {}),
    channels: '',
    ...((shared.host_coordinator || !shared.enabled) ? {
      identity_aliases: channelDedupDraft.value.identity_aliases,
      movie_identity_aliases: channelDedupDraft.value.movie_identity_aliases,
    } : {}),
  }))
  channelDedup.value = data || channelDedup.value
}

function formatChannelTimestamp(value) {
  if (!value) return '尚未同步'
  return new Date(Number(value) * 1000).toLocaleString()
}

function openPendingApproval(row) {
  pendingApprovalRow.value = row
  pendingApprovalError.value = ''
  pendingSearchSource.value = 'themoviedb'
  pendingApprovalForm.value = {
    media_source: row.media_source || 'themoviedb',
    media_id: row.media_id || row.tmdb_id || '',
    tmdb_id: row.tmdb_id || null,
    media_title: row.media_title || '',
    media_type: row.media_type === 'movie' ? 'movie' : 'tv',
    season: row.source_season && row.source_season === row.season ? '' : (row.season ?? 1),
    episodes: (row.episodes || []).join(','),
    specs: Object.fromEntries(channelDedup.value.attributes.map(key => [key, row.specs?.[key] ?? ''])),
    confirmed: false,
  }
  pendingApprovalDialog.value = true
}

watch(pendingApprovalDialog, (open) => {
  if (!open) {
    pendingSearchVersion += 1
    pendingSearchOpen.value = false
    pendingSearchLoading.value = false
    pendingSearchResults.value = []
    pendingSearchMessage.value = ''
    pendingSearchSource.value = 'themoviedb'
  }
})

function togglePendingSearch() {
  pendingSearchOpen.value = !pendingSearchOpen.value
  if (!pendingSearchOpen.value) {
    pendingSearchVersion += 1
    pendingSearchLoading.value = false
    return
  }
  pendingSearchQuery.value = pendingApprovalForm.value.media_title
  searchPendingMedia()
}

function setPendingSearchSource(source) {
  if (pendingSearchSource.value === source) return
  pendingSearchSource.value = source
  searchPendingMedia()
}

async function searchPendingMedia() {
  const version = ++pendingSearchVersion
  const title = (pendingSearchQuery.value || '').trim()
  pendingSearchResults.value = []
  pendingSearchMessage.value = ''
  pendingSearchLoading.value = Boolean(title)
  if (!title) return
  try {
    const params = new URLSearchParams({
      title, type: 'media', media_source: pendingSearchSource.value, count: '20',
    })
    const rows = unwrapResponse(await props.api.get(`media/search?${params}`))
    if (version !== pendingSearchVersion) return
    const unique = new Map()
    for (const row of Array.isArray(rows) ? rows : []) {
      const type = { 电视剧: 'tv', 剧集: 'tv', tv: 'tv', 电影: 'movie', movie: 'movie' }[row?.type]
      const sourceId = pendingSearchSource.value === 'douban'
        ? row?.douban_id ?? (String(row?.media_source || '').toLowerCase() === 'douban' ? row?.media_id : '')
        : row?.tmdb_id ?? (String(row?.media_source || '').toLowerCase() === 'themoviedb' ? row?.media_id : '')
      const id = String(sourceId ?? '').trim()
      if (!type || !/^\d+$/.test(id) || id === '0' || !row.title) continue
      unique.set(`${type}:${id}`, { id, type, title: row.title, year: row.year })
    }
    pendingSearchResults.value = [...unique.values()]
    if (!unique.size) pendingSearchMessage.value = `未找到匹配的${pendingSearchSource.value === 'douban' ? '豆瓣' : 'TMDB'}媒体`
  } catch (err) {
    if (version === pendingSearchVersion) pendingSearchMessage.value = err?.message || '媒体搜索失败'
  } finally {
    if (version === pendingSearchVersion) pendingSearchLoading.value = false
  }
}

async function selectPendingMedia(row) {
  if (pendingSearchSource.value === 'douban') {
    const version = ++pendingSearchVersion
    pendingSearchLoading.value = true
    pendingSearchMessage.value = ''
    try {
      const result = unwrapResponse(await props.api.post(
        `${pluginBase.value}/resource-ledger/pending/resolve-douban`,
        { douban_id: row.id, media_type: row.type, media_title: row.title },
      ))
      if (version !== pendingSearchVersion) return
      await applyPendingMedia({
        ...row,
        id: result?.media_id || row.id,
        media_source: result?.media_source || 'douban',
        tmdb_id: result?.tmdb_id ?? null,
        title: row.title || result?.media_title,
      })
    } catch (err) {
      if (version === pendingSearchVersion) pendingSearchMessage.value = err?.message || '豆瓣身份映射失败'
    } finally {
      if (version === pendingSearchVersion) pendingSearchLoading.value = false
    }
    return
  }
  await applyPendingMedia({
    ...row, media_source: 'themoviedb', tmdb_id: Number(row.id),
  })
}

async function applyPendingMedia(row) {
  const form = pendingApprovalForm.value
  Object.assign(form, {
    media_source: row.media_source,
    media_id: String(row.id),
    tmdb_id: row.media_source === 'themoviedb' ? Number(row.id) : null,
    media_title: row.title,
    media_type: row.type,
    confirmed: false,
  })
  pendingSearchOpen.value = false
  const version = ++pendingSearchVersion
  pendingSearchLoading.value = false
  const pending = pendingApprovalRow.value
  const year = pending?.source_season
  if (row.type !== 'tv' || !year || row.media_source !== 'themoviedb') return
  if (String(pending.media_id || pending.tmdb_id || '') === String(row.id)
      && Number.isInteger(pending.season) && pending.season !== year) {
    form.season = pending.season
    return
  }
  form.season = ''
  pendingSearchLoading.value = true
  try {
    const params = new URLSearchParams({ media_source: 'themoviedb', media_id: String(row.id) })
    const seasons = unwrapResponse(await props.api.get(`media/seasons?${params}`))
    if (version !== pendingSearchVersion || form.season !== '' || String(form.media_id) !== String(row.id)
        || form.media_type !== 'tv') return
    const candidates = new Set((Array.isArray(seasons) ? seasons : [])
      .filter(item => [`${year}`, `${year}年`].includes(String(item?.name || '').trim())
        && Number.isInteger(item.season_number) && item.season_number > 0 && item.season_number <= 999)
      .map(item => item.season_number))
    if (candidates.size === 1) form.season = [...candidates][0]
  } catch {
    // Keep the standard season editable if the lookup is unavailable.
  } finally {
    if (version === pendingSearchVersion) pendingSearchLoading.value = false
  }
}

function parseManualEpisodes(value) {
  const episodes = new Set()
  for (const part of String(value || '').split(/[，,\s]+/).filter(Boolean)) {
    const range = part.match(/^(\d+)\s*[-~至]\s*(\d+)$/)
    if (range) {
      const first = Number(range[1])
      const last = Number(range[2])
      if (last < first || last - first > 500) return null
      for (let number = first; number <= last; number += 1) episodes.add(number)
      continue
    }
    if (!/^\d+$/.test(part)) return null
    episodes.add(Number(part))
  }
  return [...episodes].sort((a, b) => a - b)
}

async function submitPendingApproval() {
  const row = pendingApprovalRow.value
  if (!row || pendingApprovalSaving.value) return
  const form = pendingApprovalForm.value
  const episodes = parseManualEpisodes(form.episodes)
  const id = String(form.media_id ?? '').trim()
  const tmdbId = form.media_source === 'themoviedb' ? Number(id) : null
  const season = Number(form.season)
  if (!/^\d{1,32}$/.test(id) || id === '0'
    || form.media_source === 'themoviedb' && (!Number.isSafeInteger(tmdbId) || tmdbId <= 0)
    || !form.media_title.trim() || !form.confirmed
    || (form.media_type === 'tv' && (String(form.season ?? '').trim() === ''
      || !Number.isInteger(season) || season < 0 || season > 999 || !episodes?.length))) {
    pendingApprovalError.value = '请填写有效的媒体 ID、媒体名称及季集号。'
    return
  }
  pendingApprovalSaving.value = true
  pendingApprovalError.value = ''
  try {
    unwrapResponse(await props.api.post(`${pluginBase.value}/resource-ledger/pending/approve`, {
      key: row.key,
      tmdb_id: tmdbId,
      media_source: form.media_source,
      media_id: id,
      media_title: form.media_title.trim(),
      media_type: form.media_type,
      season: form.media_type === 'tv' ? season : null,
      episodes: form.media_type === 'tv' ? episodes : [],
      specs: form.specs,
      confirmed: form.confirmed,
    }))
    pendingApprovalDialog.value = false
    await loadResourceRows()
    const poll = async remaining => {
      if (remaining <= 0 || approvalTab.value !== 'pending') return
      await new Promise(resolve => setTimeout(resolve, 2000))
      await loadResourceRows()
      const current = resourceRows.value.items.find(item => item.key === row.key)
      if (current?.manual_state === 'queued' || current?.manual_state === 'processing') {
        poll(remaining - 1)
      }
    }
    poll(30)
  } catch (err) {
    pendingApprovalError.value = err?.message || '提交人工核验失败'
  } finally {
    pendingApprovalSaving.value = false
  }
}

async function rejectPendingResource(row) {
  if (!row?.key || pendingRejectingKey.value) return
  pendingRejectingKey.value = row.key
  channelDedupError.value = ''
  try {
    unwrapResponse(await props.api.post(`${pluginBase.value}/resource-ledger/pending/reject`, {
      key: row.key,
    }))
    if (resourceRows.value.items.some(item => item.key === row.key)) {
      resourceRows.value = {
        ...resourceRows.value,
        items: resourceRows.value.items.filter(item => item.key !== row.key),
        total: Math.max(0, resourceRows.value.total - 1),
      }
    }
    await loadResourceRows()
  } catch (err) {
    channelDedupError.value = err?.message || '驳回待核验申请失败'
  } finally {
    pendingRejectingKey.value = ''
  }
}

function channelRecordEpisodes(record) {
  const season = String(record.season || 0).padStart(2, '0')
  const episodes = (record.episodes || []).map(number => `E${String(number).padStart(2, '0')}`).join(', ')
  return `S${season} ${episodes}`
}

function resourceRecordIdentity(record) {
  const name = record.media_title || '中文名待识别'
  const episode = record.media_type === 'movie' ? '电影' : channelRecordEpisodes(record)
  const source = record.media_source === 'douban' ? '豆瓣' : 'TMDB'
  const id = record.media_id || record.tmdb_id
  return [name, episode, id ? `${source} ${id}` : '身份待确认'].join(' · ')
}

function channelRecordSpecs(record) {
  const specs = record.specs || {}
  return channelDedupAttributes
    .map(({ value, title }) => {
      const item = specs[value]
      if (item === null || item === undefined || item === '') return ''
      if (value === 'size') return `${title} ${formatBytes(item)}`
      if (value === 'fps') return `${title} ${item} FPS`
      return `${title} ${item}`
    })
    .filter(Boolean)
    .join(' · ')
}

// 删除当前没有活跃种子的任务。
async function confirmDeleteTask() {
  saving.value = true
  try {
    unwrapResponse(await props.api.delete(`${pluginBase.value}/tasks/${selectedTaskId.value}`))
    deleteDialog.value = false
    selectedTaskId.value = ''
    await loadStatus({ preserveSelection: false })
    notify('刷流任务已删除')
  } catch (err) {
    notify(err?.message || '删除刷流任务失败', 'error')
  } finally {
    saving.value = false
  }
}

// 清空当前任务的统计、诊断与种子记录。
async function confirmClearTask() {
  saving.value = true
  try {
    taskDetail.value = unwrapResponse(
      await props.api.post(`${pluginBase.value}/tasks/${selectedTaskId.value}/clear`, {}),
    )
    clearDialog.value = false
    await loadStatus()
    notify('任务数据已清除')
  } catch (err) {
    notify(err?.message || '清除任务数据失败', 'error')
  } finally {
    saving.value = false
  }
}

// 切换活跃或已删除种子分页视图。
async function changeTorrentState(value) {
  torrentState.value = value
  torrentPage.value = 1
  await loadTaskDetail()
}

// 加载指定页的种子记录。
async function changeTorrentPage(value) {
  torrentPage.value = value
  await loadTaskDetail()
}

// 根据配置生成当前种子的下一项处理条件摘要。
function torrentPolicy(item) {
  if (item.deleted) return '已删除'
  if (item.hit_and_run && taskConfig.value.hr_seed_time) return `H&R ${taskConfig.value.hr_seed_time} 小时`
  if (taskConfig.value.seed_time) return `${taskConfig.value.seed_time} 小时后检查`
  if (taskConfig.value.seed_ratio) return `分享率 ${taskConfig.value.seed_ratio}`
  return taskConfig.value.proxy_delete ? '动态删种托管' : '等待删除条件'
}

// 返回种子当前下载或做种状态文本。
function torrentStateText(item) {
  const progress = torrentProgress(item)
  if (item.deleted) return '已删除'
  return progress >= 100 ? '做种' : `下载 ${progress}%`
}

// 将站点分享率状态格式化为当前值、无限或暂无数据。
function formatSiteRatio(siteRatio) {
  if (!siteRatio?.available) return '-'
  if (siteRatio.unlimited) return '∞'
  return Number(siteRatio.current || 0).toFixed(2)
}

watch(
  () => props.initialTab,
  value => {
    if (value) activeTab.value = value
  },
)

onMounted(async () => {
  await loadStatus({ preserveSelection: false })
  refreshTimer = window.setInterval(() => {
    if (!editorOpen.value && !settingsDialog.value && document.visibilityState === 'visible') loadStatus()
    if (channelDedupDialog.value && document.visibilityState === 'visible') refreshApprovalRecords()
  }, 30000)
})

onUnmounted(() => {
  clearTimeout(resourceSearchTimer)
  resourceGeneration += 1
  if (refreshTimer) window.clearInterval(refreshTimer)
  if (channelRefreshTimer) window.clearTimeout(channelRefreshTimer)
})

defineExpose({ loadStatus, refreshAll, loading, saving })
</script>

<template>
  <div class="resourceflow-page" :class="{ 'resourceflow-page--compact': compact }">
    <header class="resourceflow-page__header">
      <div class="resourceflow-page__identity">
        <VIcon icon="mdi-sync" color="primary" size="28" />
        <div>
          <h1>资源刷流</h1>
          <p>多站点任务独立调度与托管</p>
        </div>
      </div>
      <div class="resourceflow-page__actions">
        <VChip v-if="status.summary.task_count" size="small" variant="tonal">
          {{ status.summary.running_count ?? status.summary.enabled_count ?? 0 }} / {{ status.summary.task_count }} 运行
        </VChip>
        <VTooltip text="共享下载审批">
          <template #activator="{ props: tooltipProps }">
            <VBtn
              v-bind="tooltipProps"
              icon="mdi-send"
              variant="text"
              aria-label="共享下载审批"
              @click="openChannelDedup"
            />
          </template>
        </VTooltip>
        <VTooltip text="全局设置">
          <template #activator="{ props: tooltipProps }">
            <VBtn v-bind="tooltipProps" icon="mdi-tune-variant" variant="text" aria-label="全局设置"
              @click="openSettings" />
          </template>
        </VTooltip>
        <VBtn v-if="showClose" icon="mdi-close" variant="text" aria-label="关闭" @click="emit('close')" />
      </div>
    </header>

    <VAlert v-if="error" type="error" variant="tonal" closable @click:close="error = ''">{{ error }}</VAlert>
    <VAlert v-if="statusLoaded && !status.enabled" type="warning" variant="tonal">
      插件当前未启用，任务配置与历史仍可查看，启用后才会注册刷新和检查服务。
    </VAlert>

    <div v-if="loading && !tasks.length" class="resourceflow-loading">
      <VSkeletonLoader type="list-item-three-line, list-item-three-line, article" />
    </div>

    <div v-else-if="!tasks.length" class="resourceflow-empty">
      <VIcon icon="mdi-sync-off" size="52" color="medium-emphasis" />
      <div class="text-h6">还没有刷流任务</div>
      <div class="text-body-2 text-medium-emphasis">创建任务后可为每个站点分别设置刷新、筛选和删种规则</div>
      <VBtn color="primary" variant="flat" prepend-icon="mdi-plus" @click="openCreateTask">创建第一个任务</VBtn>
    </div>

    <template v-else>
      <VSelect
        class="resourceflow-mobile-select"
        :model-value="selectedTaskId"
        :items="tasks"
        item-title="name"
        item-value="id"
        label="当前任务"
        hide-details
        @update:model-value="selectTask"
      >
        <template #item="{ props: itemProps, item }">
          <VListItem v-bind="itemProps" :subtitle="item.raw.site_name">
            <template #prepend>
              <VIcon :icon="taskStateMeta(item.raw.state).icon" :color="taskStateMeta(item.raw.state).color" />
            </template>
          </VListItem>
        </template>
      </VSelect>

      <div class="resourceflow-layout">
        <VSheet tag="aside" class="resourceflow-task-rail app-surface-static">
          <div class="resourceflow-task-rail__head">
            <span class="text-subtitle-2">刷流任务</span>
            <VChip size="x-small" variant="tonal">{{ tasks.length }}</VChip>
          </div>
          <div class="resourceflow-task-list">
            <button
              v-for="task in tasks"
              :key="task.id"
              type="button"
              class="resourceflow-task-item"
              :class="{ 'resourceflow-task-item--selected': task.id === selectedTaskId }"
              :aria-pressed="task.id === selectedTaskId"
              @click="selectTask(task.id)"
            >
              <span class="resourceflow-task-item__title">
                <strong>{{ task.name }}</strong>
                <span class="resourceflow-status-dot" :class="`resourceflow-status-dot--${taskStateMeta(task.state).color}`" />
              </span>
              <span>{{ task.spec_dedup_enabled ? '下载审批 · ' : '' }}{{ task.site_name }} · {{ task.downloader }}</span>
              <span class="resourceflow-task-item__meta">
                <span>{{ task.statistic.active || 0 }} 个种子</span>
                <span>{{ task.next_run_at ? formatDateTime(task.next_run_at) : '暂无计划' }}</span>
              </span>
            </button>
          </div>
          <VBtn class="resourceflow-create-task" block variant="tonal" prepend-icon="mdi-plus" @click="openCreateTask">
            新建任务
          </VBtn>
        </VSheet>

        <main v-if="selectedTask" class="resourceflow-workspace">
          <section class="resourceflow-task-head">
            <div class="resourceflow-task-head__identity">
              <VAvatar color="primary" variant="tonal" rounded size="42">
                <VIcon icon="mdi-web" />
              </VAvatar>
              <div>
                <div class="resourceflow-task-head__title">
                  <h2>{{ selectedTask.name }}</h2>
                  <VChip v-if="selectedTask.spec_dedup_enabled" size="small" variant="outlined">下载审批</VChip>
                  <VChip :color="selectedState.color" size="small" variant="tonal" :prepend-icon="selectedState.icon">
                    {{ selectedState.text }}
                  </VChip>
                </div>
                <p>
                  {{ selectedTask.site_name }} · 最近 {{ selectedTask.last_run ? formatDateTime(selectedTask.last_run.started_at) : '尚未运行' }}
                  · 下次 {{ selectedTask.next_run_at ? formatDateTime(selectedTask.next_run_at) : '暂无计划' }}
                </p>
              </div>
            </div>
            <div class="resourceflow-task-head__actions">
              <VTooltip text="立即刷新">
                <template #activator="{ props: tipProps }">
                  <VBtn v-bind="tipProps" icon="mdi-sync" variant="text" :loading="selectedTask.operation === 'brush'" @click="runOperation('brush')" />
                </template>
              </VTooltip>
              <VTooltip text="检查种子">
                <template #activator="{ props: tipProps }">
                  <VBtn v-bind="tipProps" icon="mdi-progress-check" variant="text" :loading="selectedTask.operation === 'check'" @click="runOperation('check')" />
                </template>
              </VTooltip>
              <VTooltip :text="selectedTask.enabled ? '暂停任务' : '启用任务'">
                <template #activator="{ props: tipProps }">
                  <VBtn
                    v-bind="tipProps"
                    :icon="selectedTask.enabled ? 'mdi-pause' : 'mdi-play'"
                    variant="text"
                    @click="toggleSelectedTask"
                  />
                </template>
              </VTooltip>
              <VTooltip text="编辑任务">
                <template #activator="{ props: tipProps }">
                  <VBtn v-bind="tipProps" icon="mdi-pencil-outline" variant="text" @click="openEditTask" />
                </template>
              </VTooltip>
            </div>
          </section>

          <VTabs v-model="activeTab" color="primary" class="resourceflow-tabs">
            <VTab value="overview">任务概览</VTab>
            <VTab value="diagnostics">运行诊断</VTab>
            <VTab value="config">任务配置</VTab>
          </VTabs>

          <VDivider />

          <VWindow v-model="activeTab" :touch="false" class="resourceflow-window">
            <VWindowItem value="overview">
              <div class="resourceflow-stat-grid">
                <VSheet class="resourceflow-stat app-surface-static">
                  <span>活跃种子</span>
                  <strong>{{ selectedTask.statistic.active || 0 }}</strong>
                  <small>{{ selectedTask.statistic.unarchived || 0 }} 个待归档</small>
                </VSheet>
                <VSheet class="resourceflow-stat app-surface-static">
                  <span>累计上传 / 下载</span>
                  <strong>{{ formatBytes(selectedTask.statistic.uploaded) }} / {{ formatBytes(selectedTask.statistic.downloaded) }}</strong>
                  <small>当前任务累计</small>
                </VSheet>
                <VSheet class="resourceflow-stat app-surface-static">
                  <span>当前做种</span>
                  <strong>{{ formatBytes(selectedTask.seeding_size) }}</strong>
                  <small>{{ taskConfig.disksize ? `上限 ${taskConfig.disksize} GB` : '未设置体积上限' }}</small>
                  <VProgressLinear v-if="taskConfig.disksize" :model-value="seedingPercent" height="4" color="primary" />
                </VSheet>
                <VSheet class="resourceflow-stat app-surface-static">
                  <span>{{ taskConfig.site_ratio_control ? '站点分享率' : '最近刷新' }}</span>
                  <strong>
                    {{ taskConfig.site_ratio_control ? formatSiteRatio(selectedTask.site_ratio) : latestBrushRun ? `${latestBrushRun.added_count || 0} / ${latestBrushRun.source_count || 0}` : '-' }}
                  </strong>
                  <small v-if="taskConfig.site_ratio_control">
                    {{ selectedTask.site_ratio?.available ? `目标 ${Number(taskConfig.site_ratio_target || 0).toFixed(2)}` : '暂无站点统计数据' }}
                  </small>
                  <small v-else>新增 / 候选</small>
                </VSheet>
              </div>

              <div class="resourceflow-overview-grid">
                <VSheet tag="section" class="resourceflow-panel app-surface-static">
                  <header class="resourceflow-panel__head">
                    <div>
                      <div class="text-subtitle-1 font-weight-medium">运行状态</div>
                      <div class="text-body-2 text-medium-emphasis">当前任务调度与核心策略</div>
                    </div>
                    <VChip :color="selectedState.color" size="small" variant="tonal">{{ selectedState.text }}</VChip>
                  </header>
                  <dl class="resourceflow-facts">
                    <div><dt>刷新周期</dt><dd>{{ taskConfig.cron || `每 ${selectedTask.brush_interval} 分钟` }}</dd></div>
                    <div><dt>检查周期</dt><dd>每 {{ selectedTask.check_interval }} 分钟</dd></div>
                    <div><dt>开启时段</dt><dd>{{ taskConfig.active_time_range || '全天' }}</dd></div>
                    <div v-if="taskConfig.site_ratio_control"><dt>站点分享率</dt><dd>{{ formatSiteRatio(selectedTask.site_ratio) }} / {{ Number(taskConfig.site_ratio_target || 0).toFixed(2) }}</dd></div>
                    <div><dt>选种来源</dt><dd>{{ taskConfig.rss_support ? 'RSS' : '站点列表页' }}</dd></div>
                    <div><dt>促销要求</dt><dd>{{ taskConfig.freeleech === '2xfree' ? '2X 免费' : taskConfig.freeleech === 'free' ? '免费' : '全部' }}</dd></div>
                    <div><dt>删种策略</dt><dd>{{ taskConfig.proxy_delete ? `动态 ${taskConfig.delete_size_range || '-' } GB` : '满足任一条件' }}</dd></div>
                  </dl>
                </VSheet>

                <VSheet tag="section" class="resourceflow-panel app-surface-static">
                  <header class="resourceflow-panel__head">
                    <div>
                      <div class="text-subtitle-1 font-weight-medium">最近一次刷新</div>
                      <div class="text-body-2 text-medium-emphasis">
                        {{ latestBrushRun ? `${formatDateTime(latestBrushRun.started_at)} · ${formatDuration(latestBrushRun.started_at, latestBrushRun.finished_at)}` : '暂无运行记录' }}
                      </div>
                    </div>
                    <VChip v-if="latestBrushRun" :color="latestBrushRun.success === false ? 'error' : 'success'" size="small" variant="tonal">
                      {{ latestBrushRun.success === false ? '失败' : '完成' }}
                    </VChip>
                  </header>
                  <div class="resourceflow-run-summary">
                    <div><span>站点候选</span><strong>{{ latestBrushRun?.source_count || 0 }}</strong></div>
                    <div><span>规则过滤</span><strong>{{ latestBrushRun?.filtered_count || 0 }}</strong></div>
                    <div><span>新增下载</span><strong>{{ latestBrushRun?.added_count || 0 }}</strong></div>
                  </div>
                  <VAlert v-if="latestBrushRun?.error" type="error" variant="tonal" density="compact">
                    {{ latestBrushRun.error }}
                  </VAlert>
                  <VBtn variant="text" color="primary" append-icon="mdi-arrow-right" @click="activeTab = 'diagnostics'">
                    查看运行诊断
                  </VBtn>
                </VSheet>
              </div>

              <VSheet tag="section" class="resourceflow-panel resourceflow-torrents app-surface-static">
                <header class="resourceflow-panel__head">
                  <div>
                    <div class="resourceflow-panel__title-row">
                      <span class="text-subtitle-1 font-weight-medium">托管种子</span>
                      <VChip size="x-small" variant="tonal">{{ torrentData.total }}</VChip>
                    </div>
                    <div class="text-body-2 text-medium-emphasis">当前任务独立记录</div>
                  </div>
                  <VBtnToggle :model-value="torrentState" mandatory color="primary" density="compact" @update:model-value="changeTorrentState">
                    <VBtn value="active">活跃</VBtn>
                    <VBtn value="deleted">已删除</VBtn>
                    <VBtn value="all">全部</VBtn>
                  </VBtnToggle>
                </header>

                <VDataTable
                  class="resourceflow-torrent-table"
                  :headers="torrentHeaders"
                  :items="torrentData.items"
                  :loading="taskLoading"
                  :items-per-page="-1"
                  hide-default-footer
                  density="comfortable"
                >
                  <template #item.title="{ item }">
                    <div class="torrent-title-cell">
                      <strong>{{ item.title || '未知种子' }}</strong>
                      <span>{{ item.site_name }} · {{ formatDateTime((item.time || 0) * 1000) }}</span>
                    </div>
                  </template>
                  <template #item.status="{ item }">
                    <VChip size="small" :color="item.deleted ? 'secondary' : torrentProgress(item) >= 100 ? 'success' : 'info'" variant="tonal">
                      {{ torrentStateText(item) }}
                    </VChip>
                  </template>
                  <template #item.size="{ item }">{{ formatBytes(item.size) }}</template>
                  <template #item.uploaded="{ item }">{{ formatBytes(item.uploaded) }}</template>
                  <template #item.ratio="{ item }">{{ Number(item.ratio || 0).toFixed(2) }}</template>
                  <template #item.policy="{ item }">{{ torrentPolicy(item) }}</template>
                  <template #no-data>
                    <div class="resourceflow-table-empty">当前筛选下没有种子记录</div>
                  </template>
                </VDataTable>

                <div class="resourceflow-mobile-torrents">
                  <article v-for="item in torrentData.items" :key="`${item.task_id}-${item.title}-${item.time}`" class="resourceflow-mobile-torrent">
                    <div class="resourceflow-mobile-torrent__head">
                      <strong>{{ item.title || '未知种子' }}</strong>
                      <VChip size="x-small" variant="tonal">{{ torrentStateText(item) }}</VChip>
                    </div>
                    <div class="resourceflow-mobile-torrent__meta">
                      <span>{{ formatBytes(item.size) }}</span><span>上传 {{ formatBytes(item.uploaded) }}</span><span>分享率 {{ Number(item.ratio || 0).toFixed(2) }}</span>
                    </div>
                    <div class="text-body-2 text-medium-emphasis">{{ torrentPolicy(item) }}</div>
                  </article>
                  <div v-if="!torrentData.items.length" class="resourceflow-table-empty">当前筛选下没有种子记录</div>
                </div>

                <VPagination
                  v-if="totalTorrentPages > 1"
                  :model-value="torrentPage"
                  :length="totalTorrentPages"
                  :total-visible="5"
                  density="comfortable"
                  @update:model-value="changeTorrentPage"
                />
              </VSheet>
            </VWindowItem>

            <VWindowItem value="diagnostics">
              <div class="resourceflow-diagnostic-head">
                <div>
                  <div class="text-subtitle-1 font-weight-medium">
                    {{ latestBrushRun ? `刷流刷新 #${latestBrushRun.id.slice(0, 8)}` : '暂无刷流刷新记录' }}
                  </div>
                  <div class="text-body-2 text-medium-emphasis">
                    {{ latestBrushRun ? `${formatDateTime(latestBrushRun.started_at)} · ${formatDuration(latestBrushRun.started_at, latestBrushRun.finished_at)}` : '执行一次任务后将显示筛选流水线和过滤原因' }}
                  </div>
                </div>
                <VChip v-if="latestBrushRun" :color="latestBrushRun.success === false ? 'error' : 'success'" variant="tonal">
                  {{ latestBrushRun.success === false ? '失败' : '完成' }}
                </VChip>
              </div>

              <div class="resourceflow-diagnostic-grid">
                <VSheet tag="section" class="resourceflow-panel app-surface-static">
                  <header class="resourceflow-panel__head">
                    <div>
                      <div class="text-subtitle-1 font-weight-medium">选种流水线</div>
                      <div class="text-body-2 text-medium-emphasis">本轮候选在各阶段的剩余数量</div>
                    </div>
                  </header>
                  <ol class="resourceflow-pipeline">
                    <li v-for="(stage, index) in pipelineStages" :key="stage.title">
                      <span class="resourceflow-pipeline__index">{{ index + 1 }}</span>
                      <div><strong>{{ stage.title }}</strong><span>{{ stage.detail }}</span></div>
                      <VChip size="small" variant="tonal">{{ stage.count }}</VChip>
                    </li>
                  </ol>
                </VSheet>

                <VSheet tag="section" class="resourceflow-panel app-surface-static">
                  <header class="resourceflow-panel__head">
                    <div>
                      <div class="text-subtitle-1 font-weight-medium">过滤原因</div>
                      <div class="text-body-2 text-medium-emphasis">本轮未进入下载器的候选分布</div>
                    </div>
                  </header>
                  <div v-if="reasonEntries.length" class="resourceflow-reasons">
                    <div v-for="item in reasonEntries" :key="item.label" class="resourceflow-reason">
                      <div><span>{{ item.label }}</span><strong>{{ item.count }}</strong></div>
                      <span class="resourceflow-reason__track"><i :style="{ width: `${(item.count / maxReasonCount) * 100}%` }" /></span>
                    </div>
                  </div>
                  <div v-else class="resourceflow-table-empty">本轮没有记录过滤原因</div>
                </VSheet>
              </div>

              <VSheet tag="section" class="resourceflow-panel app-surface-static">
                <header class="resourceflow-panel__head">
                  <div>
                    <div class="text-subtitle-1 font-weight-medium">最近事件</div>
                    <div class="text-body-2 text-medium-emphasis">刷流刷新与种子检查独立记录</div>
                  </div>
                </header>
                <div class="resourceflow-events">
                  <article v-for="run in taskRuns" :key="run.id">
                    <VIcon
                      :icon="run.success === false ? 'mdi-alert-circle-outline' : run.kind === 'brush' ? 'mdi-sync' : 'mdi-progress-check'"
                      :color="run.success === false ? 'error' : run.kind === 'brush' ? 'primary' : 'info'"
                    />
                    <div>
                      <strong>{{ run.kind === 'brush' ? '刷流刷新' : '种子检查' }}</strong>
                      <span>
                        {{ formatDateTime(run.started_at) }} · {{ formatDuration(run.started_at, run.finished_at) }} ·
                        {{ run.kind === 'brush' ? `新增 ${run.added_count || 0}，过滤 ${run.filtered_count || 0}` : `活跃 ${run.active_count || 0}，删除 ${run.deleted_count || 0}` }}
                      </span>
                      <span v-if="run.error" class="text-error">{{ run.error }}</span>
                    </div>
                  </article>
                  <div v-if="!taskRuns.length" class="resourceflow-table-empty">暂无运行事件</div>
                </div>
              </VSheet>
            </VWindowItem>

            <VWindowItem value="config">
              <div class="resourceflow-config-grid">
                <VSheet tag="section" class="resourceflow-panel app-surface-static">
                  <header class="resourceflow-panel__head">
                    <div>
                      <div class="text-subtitle-1 font-weight-medium">任务规则</div>
                      <div class="text-body-2 text-medium-emphasis">当前服务端生效配置</div>
                    </div>
                  </header>
                  <dl class="resourceflow-facts resourceflow-facts--two">
                    <div><dt>任务状态</dt><dd>{{ selectedTask.enabled ? '启用' : '暂停' }}</dd></div>
                    <div><dt>通知</dt><dd>{{ taskConfig.notify ? '发送' : '关闭' }}</dd></div>
                    <div><dt>站点</dt><dd>{{ selectedTask.site_name }}</dd></div>
                    <div><dt>下载器</dt><dd>{{ selectedTask.downloader }}</dd></div>
                    <div><dt>种子大小</dt><dd>{{ taskConfig.size || '不限' }}</dd></div>
                    <div><dt>做种人数</dt><dd>{{ taskConfig.seeder || '不限' }}</dd></div>
                    <div><dt>发布时间</dt><dd>{{ taskConfig.pubtime ? `${taskConfig.pubtime} 分钟` : '不限' }}</dd></div>
                    <div><dt>排除 H&R</dt><dd>{{ taskConfig.hr === 'yes' ? '是' : '否' }}</dd></div>
                    <div><dt>包含规则</dt><dd>{{ taskConfig.include || '无' }}</dd></div>
                    <div><dt>排除规则</dt><dd>{{ taskConfig.exclude || '无' }}</dd></div>
                    <div><dt>保种上限</dt><dd>{{ taskConfig.disksize ? `${taskConfig.disksize} GB` : '不限' }}</dd></div>
                    <div><dt>目标分享率</dt><dd>{{ taskConfig.site_ratio_control ? Number(taskConfig.site_ratio_target || 0).toFixed(2) : '关闭' }}</dd></div>
                    <div><dt>归档天数</dt><dd>{{ taskConfig.auto_archive_days || '不自动归档' }}</dd></div>
                  </dl>
                </VSheet>

                <VSheet tag="section" class="resourceflow-panel app-surface-static">
                  <header class="resourceflow-panel__head">
                    <div>
                      <div class="text-subtitle-1 font-weight-medium">任务数据</div>
                      <div class="text-body-2 text-medium-emphasis">以下操作只影响当前任务</div>
                    </div>
                  </header>
                  <div class="resourceflow-config-actions">
                    <div>
                      <strong>清除统计与记录</strong>
                      <span>下载器中的任务标签种子会在下次检查时重新纳入</span>
                      <VBtn color="warning" variant="tonal" prepend-icon="mdi-eraser" @click="clearDialog = true">清除数据</VBtn>
                    </div>
                    <VDivider />
                    <div>
                      <strong>删除任务</strong>
                      <span>存在活跃种子时后端会拒绝删除，避免留下失管任务</span>
                      <VBtn color="error" variant="tonal" prepend-icon="mdi-delete-outline" @click="deleteDialog = true">删除任务</VBtn>
                    </div>
                  </div>
                </VSheet>
              </div>
            </VWindowItem>
          </VWindow>
        </main>
      </div>
    </template>

    <TaskEditorDialog
      v-model="editorOpen"
      :task="editorTask"
      :sites="status.options.sites"
      :downloaders="status.options.downloaders"
      :download-categories="status.options.download_categories || []"
      :global-dynamic-delete="Boolean(status.global_proxy_delete)"
      :saving="saving"
      @save="saveTask"
      @dedup-settings="openSettings"
    />

    <VDialog v-model="settingsDialog" :width="$vuetify.display.xs ? '100vw' : '94vw'"
      :max-width="$vuetify.display.xs ? '100vw' : '80rem'"
      :height="$vuetify.display.xs ? '100dvh' : '92dvh'"
      :fullscreen="$vuetify.display.xs" :persistent="saving" scrollable>
      <VCard class="channel-dedup__card">
        <VCardTitle class="channel-dedup__title">
          <VIcon icon="mdi-tune-variant" color="primary" />
          <span>全局设置</span>
        </VCardTitle>
        <VCardText class="channel-dedup__body global-settings__body">
          <VAlert v-if="settingsError || channelDedupError" type="error" variant="tonal" density="compact">
            {{ settingsError || channelDedupError }}
          </VAlert>
          <div class="global-settings__progress">
            <VProgressLinear :class="{ 'global-settings__progress--idle': !loading && !channelDedupLoading }"
              indeterminate />
          </div>
          <section class="global-settings__section">
            <h3><VIcon icon="mdi-cog-outline" />基础</h3>
            <div class="global-settings__toggles">
              <VSwitch v-model="settingsDraft.enabled" label="启用插件"
                color="primary" density="compact" hide-details inset />
              <VSwitch v-model="settingsDraft.show_sidebar_nav" label="显示侧栏入口"
                color="primary" density="compact" hide-details inset />
            </div>
          </section>
          <section class="global-settings__section">
            <h3><VIcon icon="mdi-gauge" />运行限额与删种</h3>
            <div class="global-settings__grid global-settings__delete-policy">
              <VSwitch v-model="settingsDraft.global_proxy_delete" label="全局动态删种"
                color="primary" density="compact" hide-details inset />
              <VTextField v-if="settingsDraft.global_proxy_delete" v-model="settingsDraft.global_delete_size_range"
                label="全局动态删种阈值（GB）" placeholder="50-100" density="comfortable" clearable hide-details />
            </div>
            <div class="global-settings__grid global-settings__limits">
              <VTextField v-model.number="settingsDraft.global_disksize" type="number" min="0"
                label="全局保种体积（GB）" density="comfortable" clearable hide-details />
              <VTextField v-model.number="settingsDraft.global_maxdlcount" type="number" min="0"
                label="全局同时下载任务数" density="comfortable" clearable hide-details />
              <VTextField v-model.number="settingsDraft.global_maxupspeed" type="number" min="0"
                label="全局总上传带宽（KB/s）" density="comfortable" clearable hide-details />
              <VTextField v-model.number="settingsDraft.global_maxdlspeed" type="number" min="0"
                label="全局总下载带宽（KB/s）" density="comfortable" clearable hide-details />
              <VTextField v-model.number="settingsDraft.shared_download_timeout_minutes" type="number"
                min="1" max="1440" step="1" suffix="分钟" label="共享下载超时"
                hint="获批下载开始后仍有文件未完成时，停止并移除 QB 任务；已下载数据保留"
                persistent-hint density="comfortable" hide-details="auto" />
            </div>
          </section>
          <section class="global-settings__section">
            <h3><VIcon icon="mdi-calendar-clock-outline" />缺失播出日期兜底</h3>
            <div class="global-settings__grid global-settings__recent-date">
              <VTextField v-model.number="settingsDraft.recent_missing_date_tolerance_days"
                type="number" min="1" max="30" step="1"
                label="缺失日期容差天数" hint="仅在 TMDB、豆瓣都没有逐集日期时使用"
                persistent-hint density="comfortable" hide-details="auto" />
              <VSwitch v-model="settingsDraft.recent_missing_date_ai_enabled"
                label="启用 AI 官方排播核验" color="primary" density="compact" hide-details inset />
            </div>
          </section>
          <section class="global-settings__section">
            <h3><VIcon icon="mdi-filter-check-outline" />选种与去重</h3>
            <VSwitch v-model="settingsDraft.global_torrent_blacklist_enabled" label="种子黑名单"
              hint="开启共享下载审批的任务站点之间，按完整种子名称排重"
              persistent-hint color="primary" density="compact" inset />
            <template v-if="sharedHost">
              <h4 class="text-subtitle-2">共享规格去重字段</h4>
              <div class="global-settings__spec-row">
                <div class="global-settings__attributes">
                  <VCheckbox v-for="attribute in channelDedupAttributes" :key="attribute.value"
                    v-model="channelDedupDraft.attributes" :label="attribute.title" :value="attribute.value"
                    color="primary" density="compact" hide-details />
                </div>
                <VSelect v-model="channelDedupDraft.unknown_policy" :items="channelUnknownPolicies"
                  class="global-settings__unknown" label="候选缺少勾选规格时" density="comfortable"
                  :disabled="!channelDedupDraft.attributes.length" hide-details />
              </div>
              <div v-if="channelDedupDraft.attributes.includes('size')" class="global-settings__grid">
                <VTextField v-model.number="channelDedupDraft.size_tolerance_pct"
                  type="number" min="0" max="20" step="1" suffix="%" label="单集文件大小容差" density="comfortable"
                  hint="按种子内单个视频文件大小比较，不使用整包总大小" persistent-hint />
              </div>
            </template>
          </section>
          <section class="global-settings__section">
            <h3><VIcon icon="mdi-shield-link-variant-outline" />共享下载审批</h3>
            <SharedSettingsPanel v-if="settingsDialog" ref="sharedSettingsPanel" mode="settings"
              :api="api" :plugin-base="pluginBase"
              @host-change="sharedHost = $event" @policy-synced="loadChannelDedup()"
              @role-change="sharedRole = $event"
              @test-result="settingsTestFeedback = $event" />
          </section>
          <section class="global-settings__section">
            <h3><VIcon icon="mdi-tag-multiple-outline" />规范媒体身份绑定</h3>
            <div class="global-settings__grid global-settings__aliases">
              <VTextarea v-model="channelDedupDraft.identity_aliases" label="剧集身份绑定"
                placeholder="剧名或别名 = 规范 TMDB ID&#10;TMDB 336181 = 336292&#10;DOUBAN:12345 = 336292" rows="2"
                hint="可填写名称、TMDB 旧 ID 或 DOUBAN:豆瓣ID 映射；共享审批启用后由协调端统一维护并下发"
                persistent-hint :disabled="!settingsReady || (sharedRole.enabled && !sharedRole.host_coordinator)"
                density="comfortable" auto-grow hide-details="auto" />
              <VTextarea v-model="channelDedupDraft.movie_identity_aliases" label="电影身份绑定"
                placeholder="电影名或别名 = 规范 TMDB ID&#10;TMDB 123456 = 234567&#10;DOUBAN:12345 = 234567" rows="2"
                hint="名称、TMDB 旧 ID 和豆瓣 ID 映射共用此处配置"
                persistent-hint :disabled="!settingsReady || (sharedRole.enabled && !sharedRole.host_coordinator)"
                density="comfortable" auto-grow hide-details="auto" />
            </div>
          </section>
        </VCardText>
        <footer class="global-settings__footer">
          <div class="global-settings__footer-group">
            <div v-if="settingsTestFeedback" class="global-settings__test-feedback"
              :data-tone="settingsTestFeedback.type" role="status">
              <VIcon :icon="settingsTestFeedback.success ? 'mdi-check-circle-outline' : 'mdi-alert-circle-outline'"
                size="18" />
              <span>{{ settingsTestFeedback.message }}</span>
            </div>
            <VTooltip text="测试审批连接">
              <template #activator="{ props: tip }">
                <VBtn v-bind="tip" aria-label="测试审批连接" variant="text"
                  :icon="$vuetify.display.xs ? 'mdi-connection' : undefined"
                  :prepend-icon="$vuetify.display.xs ? undefined : 'mdi-connection'"
                  :disabled="saving || !sharedSettingsPanel?.ready || sharedSettingsPanel?.busy || !sharedSettingsPanel?.canTest"
                  :loading="sharedSettingsPanel?.busy"
                  @click="sharedSettingsPanel?.test()">
                  <VIcon v-if="$vuetify.display.xs" icon="mdi-connection" />
                  <template v-else>测试审批连接</template>
                </VBtn>
              </template>
            </VTooltip>
            <VTooltip text="刷新共享状态">
              <template #activator="{ props: tip }">
                <VBtn v-bind="tip" icon="mdi-refresh" aria-label="刷新共享状态" variant="text"
                  :disabled="saving || sharedSettingsPanel?.busy" @click="sharedSettingsPanel?.refresh()" />
              </template>
            </VTooltip>
          </div>
          <div class="global-settings__footer-group">
            <VBtn variant="text" :disabled="saving || sharedSettingsPanel?.busy" @click="settingsDialog = false">关闭</VBtn>
            <VBtn color="primary" variant="flat" :loading="saving"
              :disabled="!settingsReady || channelDedupLoading || sharedSettingsPanel?.busy" @click="saveSettings">保存设置</VBtn>
          </div>
        </footer>
      </VCard>
    </VDialog>

    <VDialog v-model="channelDedupDialog" :width="$vuetify.display.xs ? '100vw' : '94vw'"
      :max-width="$vuetify.display.xs ? '100vw' : '80rem'"
      :height="$vuetify.display.xs ? '100dvh' : '92dvh'"
      :fullscreen="$vuetify.display.xs" scrollable>
      <VCard class="channel-dedup__card">
        <VCardTitle class="channel-dedup__title">
          <VIcon icon="mdi-send" color="primary" />
          <span>共享下载审批</span>
          <VSpacer />
          <VTooltip v-if="approvalLoading" text="加载中">
            <template #activator="{ props: tip }">
              <VProgressCircular v-bind="tip" indeterminate color="primary" :size="20" :width="2"
                aria-label="加载中" class="channel-dedup__loading" />
            </template>
          </VTooltip>
          <VTooltip text="刷新资源记录">
            <template #activator="{ props: tip }">
              <VBtn v-bind="tip" icon="mdi-refresh" variant="text" aria-label="刷新资源记录"
                @click="refreshApprovalRecords" />
            </template>
          </VTooltip>
          <VTooltip text="关闭">
            <template #activator="{ props: tip }">
              <VBtn v-bind="tip" icon="mdi-close" variant="text" aria-label="关闭共享下载审批"
                @click="channelDedupDialog = false" />
            </template>
          </VTooltip>
        </VCardTitle>
        <VCardText class="channel-dedup__body">
          <VAlert v-if="channelDedupError" type="error" variant="tonal" density="compact">
            {{ channelDedupError }}
          </VAlert>
          <VAlert v-if="channelDedup.last_error" type="warning" variant="tonal" density="compact">
            {{ channelDedup.last_error }}
          </VAlert>
          <VTabs v-model="approvalTab" density="compact">
            <VTab value="requests">本机下载申请</VTab>
            <VTab v-if="sharedRecordsHost" value="records">共享下载记录</VTab>
            <VTab value="pending">下载待核验</VTab>
          </VTabs>
          <div class="channel-dedup__tab-content">
          <SharedSettingsPanel v-if="channelDedupDialog" v-show="approvalTab !== 'pending'"
            ref="sharedRecordsPanel" :api="api" :plugin-base="pluginBase" :record-tab="approvalTab"
            @host-change="sharedRecordsHost = $event"
            @loading-change="sharedRecordsLoading = $event" />
          <div v-if="approvalTab === 'pending'" class="channel-dedup__section">
            <div class="channel-dedup__search approval-search">
              <VTextField v-model="activeResourceFilter.query" label="搜索记录" prepend-inner-icon="mdi-magnify"
                clearable maxlength="200" density="comfortable" hide-details />
              <VSelect v-model="activeResourceFilter.media_type" label="类型筛选" :items="resourceTypes"
                density="comfortable" hide-details />
            </div>
            <div v-if="!ledgerLoading && !resourceRows.items?.length" class="text-body-2 text-medium-emphasis">
              {{ activeResourceFilter.query || activeResourceFilter.media_type ? '无匹配记录' : '暂无记录' }}
            </div>
            <div class="channel-dedup__records">
              <article v-for="row in resourceRows.items" :key="row.key" class="channel-dedup__record">
                <div class="channel-dedup__record-head">
                  <div class="channel-dedup__record-main">
                    <div class="channel-dedup__record-identity channel-dedup__record-info">
                      {{ resourceRecordIdentity(row) }}
                      <template v-if="channelRecordSpecs(row)"> · {{ channelRecordSpecs(row) }}</template>
                    </div>
                  </div>
                  <div class="channel-dedup__record-actions">
                    <VBtn size="small" density="compact" variant="tonal"
                      class="channel-dedup__pending-action"
                      color="error" prepend-icon="mdi-close-circle-outline"
                      :loading="pendingRejectingKey === row.key"
                      :disabled="Boolean(pendingRejectingKey) || row.manual_state === 'queued' || row.manual_state === 'processing'"
                      @click="rejectPendingResource(row)">
                      驳回申请
                    </VBtn>
                    <VBtn v-if="row.actionable" size="small" density="compact" variant="tonal"
                      class="channel-dedup__pending-action"
                      prepend-icon="mdi-check-circle-outline"
                      :disabled="pendingRejectingKey === row.key"
                      @click="openPendingApproval(row)">
                      核验下载
                    </VBtn>
                  </div>
                </div>
                <strong class="channel-dedup__record-title">
                  {{ row.title || row.path || row.file_key || row.title_key }}
                </strong>
                <div class="channel-dedup__record-meta">
                  <span>最近更新：{{ formatChannelTimestamp(row.last_seen) }}</span>
                  <span>站点：{{ row.task_name || '-' }}</span>
                  <span v-if="row.path">文件：{{ row.path }}</span>
                </div>
                <p class="channel-dedup__record-reason">{{ row.reason || '身份或规格待核验' }}</p>
                <p v-if="row.manual_state === 'queued' || row.manual_state === 'processing'"
                  class="channel-dedup__record-progress">
                  <VProgressCircular indeterminate size="14" width="2" class="mr-2" />
                  正在重新核验并执行下载审批
                </p>
                <p v-else-if="row.manual_error" class="channel-dedup__record-error">
                  上次处理未完成：{{ row.manual_error }}
                </p>
                <p v-if="row.source_consumed" class="channel-dedup__record-error">
                  同一种子中的其他待核验文件不可再次单独添加
                </p>
              </article>
            </div>
            <SharedRecordPagination v-model="resourcePage" :total="resourceRows.total"
              @update:model-value="loadResourceRows" />
          </div>
          </div>
        </VCardText>
      </VCard>
    </VDialog>

    <VDialog v-model="pendingApprovalDialog" max-width="48rem">
      <VCard title="人工核验并继续下载">
        <VCardText class="pending-approval__body">
          <VAlert type="warning" variant="tonal" density="compact">
            你确认的身份和规格将重新进入原选种规则与共享下载审批；重复命中、规则排除或协调端拒绝时不会下载。
          </VAlert>
          <div class="pending-approval__grid">
            <VSelect v-model="pendingApprovalForm.media_type" label="媒体类型"
              :items="[{ title: '剧集', value: 'tv' }, { title: '电影', value: 'movie' }]" hide-details />
            <VSelect v-model="pendingApprovalForm.media_source" label="身份来源"
              :items="[{ title: 'TMDB', value: 'themoviedb' }, { title: '豆瓣', value: 'douban' }]"
              hide-details />
            <VTextField v-model="pendingApprovalForm.media_id"
              :label="pendingApprovalForm.media_source === 'douban' ? '豆瓣 ID' : 'TMDB ID'"
              inputmode="numeric" hide-details>
              <template #append-inner>
                <VBtn icon="mdi-magnify" variant="text" size="small" type="button"
                  aria-label="按名称查询媒体编号" title="按名称查询媒体编号"
                  :aria-expanded="pendingSearchOpen" :disabled="pendingApprovalSaving" @click="togglePendingSearch" />
              </template>
            </VTextField>
            <div v-if="pendingSearchOpen" class="pending-approval__lookup">
              <VBtnToggle :model-value="pendingSearchSource" mandatory density="comfortable" color="primary"
                @update:model-value="setPendingSearchSource">
                <VBtn value="themoviedb" size="small">TMDB</VBtn>
                <VBtn value="douban" size="small">豆瓣</VBtn>
              </VBtnToggle>
              <p v-if="pendingSearchSource === 'douban'" class="text-caption text-medium-emphasis">
                有 TMDB 映射时优先采用 TMDB；豆瓣独有条目会保留豆瓣 ID 进入共享审批。
              </p>
              <form class="pending-approval__query" @submit.prevent="searchPendingMedia">
                <VTextField v-model="pendingSearchQuery" label="搜索媒体名称" density="comfortable" clearable hide-details />
                <VBtn type="submit" variant="tonal" color="primary" prepend-icon="mdi-magnify"
                  :loading="pendingSearchLoading" :disabled="!pendingSearchQuery?.trim()">搜索</VBtn>
              </form>
              <div v-if="pendingSearchMessage" role="status" class="text-body-2 text-medium-emphasis">
                {{ pendingSearchMessage }}
              </div>
              <VList v-if="pendingSearchResults.length" class="pending-approval__results" density="compact">
                <VListItem v-for="row in pendingSearchResults" :key="`${row.type}:${row.id}`"
                  :title="row.title" :subtitle="[row.type === 'tv' ? '剧集' : '电影', row.year,
                    `${pendingSearchSource === 'douban' ? '豆瓣' : 'TMDB'} ${row.id}`].filter(Boolean).join(' · ')"
                  @click="selectPendingMedia(row)" />
              </VList>
            </div>
            <VTextField v-model="pendingApprovalForm.media_title" label="中文媒体名" hide-details
              class="pending-approval__name" />
            <template v-if="pendingApprovalForm.media_type === 'tv'">
              <VTextField v-model="pendingApprovalForm.season" label="季号" type="number" min="0" max="999" hide-details
                :placeholder="pendingApprovalRow?.source_season ? '标准季号' : undefined"
                :loading="pendingSearchLoading && !pendingSearchOpen" />
              <VTextField v-model="pendingApprovalForm.episodes" label="集号" placeholder="1, 2-4" hide-details />
            </template>
            <template v-for="field in channelDedupAttributes.filter(item =>
              channelDedup.attributes.includes(item.value))" :key="field.value">
              <VTextField v-model="pendingApprovalForm.specs[field.value]"
                :label="field.value === 'size' ? '大小（字节）' : field.title"
                :type="['size', 'fps'].includes(field.value) ? 'number' : 'text'"
                :min="['size', 'fps'].includes(field.value) ? 0.001 : undefined" hide-details />
            </template>
          </div>
          <VCheckbox v-model="pendingApprovalForm.confirmed" density="compact" hide-details
            label="我已核对以上媒体身份、季集号及规格" />
          <VAlert v-if="pendingApprovalError" type="error" variant="tonal" density="compact">
            {{ pendingApprovalError }}
          </VAlert>
          <div v-if="pendingApprovalRow" class="pending-approval__filename text-caption text-medium-emphasis">
            文件：{{ pendingApprovalRow.path }}
          </div>
        </VCardText>
        <VCardActions>
          <VSpacer />
          <VBtn variant="text" :disabled="pendingApprovalSaving" @click="pendingApprovalDialog = false">取消</VBtn>
          <VBtn color="primary" variant="flat" :loading="pendingApprovalSaving"
            :disabled="!pendingApprovalForm.confirmed || (pendingSearchLoading && !pendingSearchOpen)" @click="submitPendingApproval">
            重新核验并提交
          </VBtn>
        </VCardActions>
      </VCard>
    </VDialog>

    <VDialog v-model="deleteDialog" max-width="28rem">
      <VCard title="删除刷流任务">
        <VCardText>确认删除“{{ selectedTask?.name }}”？存在活跃种子时不会执行删除。</VCardText>
        <VCardActions>
          <VSpacer />
          <VBtn variant="text" @click="deleteDialog = false">取消</VBtn>
          <VBtn color="error" variant="flat" :loading="saving" @click="confirmDeleteTask">删除</VBtn>
        </VCardActions>
      </VCard>
    </VDialog>

    <VDialog v-model="clearDialog" max-width="28rem">
      <VCard title="清除任务数据">
        <VCardText>将清除当前任务的统计、运行诊断、托管和归档记录，下载器内的种子与文件不会删除。</VCardText>
        <VCardActions>
          <VSpacer />
          <VBtn variant="text" @click="clearDialog = false">取消</VBtn>
          <VBtn color="warning" variant="flat" :loading="saving" @click="confirmClearTask">清除</VBtn>
        </VCardActions>
      </VCard>
    </VDialog>

  </div>
</template>

<style scoped>
.resourceflow-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-inline-size: 0;
  padding: 16px;
  color: rgb(var(--v-theme-on-background));
}

.resourceflow-page--compact {
  padding: 20px;
}

.resourceflow-page__header,
.resourceflow-page__identity,
.resourceflow-page__actions,
.resourceflow-task-head,
.resourceflow-task-head__identity,
.resourceflow-task-head__title,
.resourceflow-task-head__actions,
.resourceflow-panel__head,
.resourceflow-panel__title-row,
.resourceflow-task-item__title,
.resourceflow-task-item__meta,
.resourceflow-mobile-torrent__head,
.resourceflow-mobile-torrent__meta,
.resourceflow-diagnostic-head {
  display: flex;
  align-items: center;
}

.resourceflow-page__header,
.resourceflow-task-head,
.resourceflow-panel__head,
.resourceflow-mobile-torrent__head,
.resourceflow-diagnostic-head {
  justify-content: space-between;
}

.resourceflow-page__header {
  min-block-size: 48px;
  gap: 16px;
}

.resourceflow-page__identity,
.resourceflow-task-head__identity {
  min-inline-size: 0;
  gap: 12px;
}

.resourceflow-page__identity h1,
.resourceflow-task-head h2 {
  margin: 0;
  font-size: 1.35rem;
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: 0;
}

.resourceflow-page__identity p,
.resourceflow-task-head p {
  margin: 2px 0 0;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.875rem;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.resourceflow-page__actions,
.resourceflow-task-head__actions,
.resourceflow-task-head__title,
.resourceflow-panel__title-row,
.resourceflow-mobile-torrent__meta {
  flex-wrap: wrap;
  gap: 8px;
}

.channel-dedup__title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 1.125rem;
  white-space: normal;
  overflow-wrap: anywhere;
}

.channel-dedup__body {
  display: grid;
  align-content: start;
  gap: 16px;
  min-block-size: 0;
}

.channel-dedup__card {
  block-size: 92dvh;
  min-block-size: 92dvh;
  max-block-size: 92dvh;
  flex-shrink: 0;
}

.channel-dedup__tab-content { min-inline-size: 0; padding-block-start: 16px; }
.channel-dedup__tab-content :deep(.approval-search) { display: grid; gap: 12px; }
.channel-dedup__tab-content :deep(.approval-search > *) { min-inline-size: 0; }
.channel-dedup__search { grid-template-columns: minmax(0, 1fr) 160px; }

.global-settings__body {
  gap: 0;
}

.global-settings__section {
  display: grid;
  gap: 14px;
  min-inline-size: 0;
  padding-block: 16px;
}

.global-settings__section + .global-settings__section {
  border-block-start: thin solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.global-settings__section h3 {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  font-size: 1rem;
  font-weight: 600;
}

.global-settings__section h3 > .v-icon {
  color: rgb(var(--v-theme-primary));
}

.global-settings__section h3 > span:not(.v-icon) {
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.8125rem;
  font-weight: 400;
}

.global-settings__progress {
  block-size: 4px;
}

.global-settings__progress--idle {
  visibility: hidden;
}

.global-settings__grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  align-items: start;
  gap: 16px 24px;
  min-inline-size: 0;
}

.global-settings__delete-policy,
.global-settings__recent-date {
  align-items: center;
}

.global-settings__toggles {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 24px;
}

.global-settings__toggles > * {
  flex: 0 0 auto;
  min-inline-size: 0;
}

.global-settings__spec-row,
.global-settings__attributes {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 22px;
  min-inline-size: 0;
}

.global-settings__attributes > * {
  flex: 0 0 auto;
}

.global-settings__unknown {
  flex: 0 1 18rem;
  min-inline-size: 0;
  max-inline-size: 100%;
}

.global-settings__footer {
  display: flex;
  flex: 0 0 auto;
  justify-content: space-between;
  align-items: center;
  position: relative;
  gap: 8px;
  padding: 12px 20px;
}

.global-settings__footer-group {
  display: flex;
  align-items: center;
  gap: 4px;
  min-inline-size: 0;
}

.global-settings__test-feedback {
  display: inline-flex;
  align-items: center;
  flex: 1 1 18rem;
  min-inline-size: 0;
  max-inline-size: min(18rem, 100%);
  gap: 6px;
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 0.8125rem;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.global-settings__test-feedback[data-tone="success"] {
  color: rgb(var(--v-theme-success));
  background: rgba(var(--v-theme-success), 0.12);
}

.global-settings__test-feedback[data-tone="error"] {
  color: rgb(var(--v-theme-error));
  background: rgba(var(--v-theme-error), 0.12);
}

@media (max-width: 599px) {
  .global-settings__toggles { gap: 4px 14px; }
  .global-settings__footer { padding: 8px; gap: 4px; }
  .global-settings__footer-group { gap: 0; }
  .global-settings__test-feedback {
    position: absolute;
    inset-inline: 8px;
    inset-block-end: calc(100% + 4px);
    max-inline-size: none;
  }
}

@media (min-width: 600px) {
  .global-settings__grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (min-width: 960px) {
  .global-settings__limits { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}

@media (max-width: 599px) {
  .channel-dedup__card { block-size: 100dvh; min-block-size: 100dvh; max-block-size: 100dvh; }
  .channel-dedup__search { grid-template-columns: minmax(0, 1fr); }
}

.channel-dedup__body :deep(.v-input) {
  min-inline-size: 0;
}

.channel-dedup__body :deep(.v-selection-control .v-label),
.channel-dedup__body :deep(.v-select__selection-text) {
  white-space: normal;
  overflow-wrap: anywhere;
}

.channel-dedup__section {
  display: grid;
  gap: 12px;
  min-inline-size: 0;
}

.channel-dedup__attributes {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
  gap: 2px 12px;
}

.channel-dedup__record-meta {
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.8rem;
}

.channel-dedup__records {
  display: grid;
  align-content: start;
}

.channel-dedup__record {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-inline-size: 0;
  padding: 20px 0;
  border-block-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  overflow-wrap: anywhere;
}

.channel-dedup__record-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  min-inline-size: 0;
}

.channel-dedup__record-info {
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(var(--v-theme-on-surface), 0.055);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.6;
}

.channel-dedup__record-title {
  font-size: 14px;
  line-height: 1.55;
}

.channel-dedup__record-reason {
  margin: 0;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 13px;
  line-height: 1.5;
}

.channel-dedup__record-progress,
.channel-dedup__record-error {
  display: flex;
  align-items: center;
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
}

.channel-dedup__record-progress { color: rgb(var(--v-theme-primary)); }
.channel-dedup__record-error { color: rgb(var(--v-theme-error)); }

.pending-approval__body { display: grid; gap: 20px; }
.pending-approval__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 20px;
}
.pending-approval__grid > * { min-inline-size: 0; }
.pending-approval__name, .pending-approval__lookup { grid-column: 1 / -1; }
.pending-approval__lookup { display: grid; gap: 8px; }
.pending-approval__query { display: grid; align-items: center; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; }
.pending-approval__results { max-block-size: 12rem; overflow: auto; }
.pending-approval__results :deep(.v-list-item-title) { white-space: normal; overflow-wrap: anywhere; }
.pending-approval__filename { overflow-wrap: anywhere; }

.channel-dedup__badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  flex: 0 0 auto;
  max-inline-size: 52%;
  min-block-size: 32px;
  padding: 6px 10px;
  border-radius: 7px;
  color: rgb(219, 163, 75);
  background: rgba(220, 150, 60, 0.13);
  font-size: 13px;
  line-height: 20px;
}

.channel-dedup__record-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex: 0 0 auto;
  gap: 8px;
  min-inline-size: 0;
}

.channel-dedup__pending-action { min-block-size: 32px; }

.channel-dedup__record-main,
.channel-dedup__record-meta {
  display: grid;
  gap: 4px;
  min-inline-size: 0;
}

.channel-dedup__record-main strong,
.channel-dedup__record-main small,
.channel-dedup__record-meta span,
.channel-dedup__record-meta time,
.channel-dedup__record-title {
  overflow-wrap: anywhere;
}

.channel-dedup__record-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 14px;
  line-height: 1.55;
}

@media (max-width: 600px) {
  .pending-approval__grid { grid-template-columns: minmax(0, 1fr); }
  .channel-dedup__record-head {
    gap: 8px;
    flex-wrap: wrap;
  }
  .channel-dedup__record-main { flex: 1 0 100%; }
  .channel-dedup__record-actions { margin-inline-start: auto; }

  .channel-dedup__badge {
    max-inline-size: 56%;
    white-space: normal;
  }

  .channel-dedup__record-meta { flex-direction: column; align-items: flex-start; gap: 4px; }
}

.resourceflow-loading,
.resourceflow-empty {
  min-block-size: 20rem;
}

.resourceflow-empty {
  display: grid;
  place-items: center;
  align-content: center;
  gap: 12px;
  text-align: center;
}

.resourceflow-mobile-select {
  display: none;
}

.resourceflow-layout {
  display: grid;
  grid-template-columns: minmax(13.5rem, 0.3fr) minmax(0, 1.7fr);
  align-items: start;
  gap: 18px;
  min-inline-size: 0;
}

.resourceflow-task-rail {
  position: sticky;
  top: 76px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-block-size: calc(100dvh - 104px);
  padding: 12px;
  overflow-y: auto;
  border: var(--app-surface-border);
  border-radius: var(--app-surface-radius);
}

.resourceflow-task-rail__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-inline: 4px;
}

.resourceflow-task-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.resourceflow-create-task {
  flex: 0 0 auto;
}

.resourceflow-task-item {
  appearance: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
  inline-size: 100%;
  padding: 11px 12px;
  border: 1px solid transparent;
  border-radius: var(--app-control-radius);
  color: rgb(var(--v-theme-on-surface));
  background: transparent;
  font: inherit;
  text-align: start;
  cursor: pointer;
}

.resourceflow-task-item:hover {
  background: rgba(var(--v-theme-primary), 0.05);
}

.resourceflow-task-item:focus-visible {
  outline: 2px solid rgb(var(--v-theme-primary));
  outline-offset: 2px;
}

.resourceflow-task-item--selected {
  border-color: rgba(var(--v-theme-primary), 0.28);
  background: rgba(var(--v-theme-primary), 0.1);
}

.resourceflow-task-item__title,
.resourceflow-task-item__meta {
  justify-content: space-between;
  gap: 8px;
}

.resourceflow-task-item__title strong {
  min-inline-size: 0;
  overflow-wrap: anywhere;
}

.resourceflow-task-item > span:not(.resourceflow-task-item__title) {
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.78rem;
}

.resourceflow-status-dot {
  inline-size: 8px;
  block-size: 8px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: rgb(var(--v-theme-secondary));
}

.resourceflow-status-dot--success { background: rgb(var(--v-theme-success)); }
.resourceflow-status-dot--primary { background: rgb(var(--v-theme-primary)); }
.resourceflow-status-dot--info { background: rgb(var(--v-theme-info)); }
.resourceflow-status-dot--warning { background: rgb(var(--v-theme-warning)); }
.resourceflow-status-dot--error { background: rgb(var(--v-theme-error)); }

.resourceflow-workspace {
  min-inline-size: 0;
}

.resourceflow-task-head {
  min-block-size: 52px;
  gap: 12px;
  margin-block-end: 12px;
}

.resourceflow-tabs {
  max-inline-size: 100%;
}

.resourceflow-window {
  padding-block-start: 16px;
}

.resourceflow-stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.resourceflow-stat,
.resourceflow-panel {
  border: var(--app-surface-border);
  border-radius: var(--app-surface-radius);
}

.resourceflow-stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-block-size: 116px;
  padding: 16px;
}

.resourceflow-stat > span,
.resourceflow-stat > small {
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
}

.resourceflow-stat > strong {
  font-size: 1.25rem;
  font-weight: 600;
  line-height: 1.3;
  overflow-wrap: anywhere;
}

.resourceflow-stat :deep(.v-progress-linear) {
  margin-block-start: auto;
}

.resourceflow-overview-grid,
.resourceflow-diagnostic-grid,
.resourceflow-config-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-block-start: 12px;
}

.resourceflow-panel {
  min-inline-size: 0;
  padding: 16px;
}

.resourceflow-panel__head,
.resourceflow-diagnostic-head {
  align-items: flex-start;
  gap: 12px;
}

.resourceflow-facts {
  display: grid;
  gap: 11px;
  margin: 18px 0 0;
}

.resourceflow-facts--two {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.resourceflow-facts > div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  min-inline-size: 0;
}

.resourceflow-facts dt {
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
}

.resourceflow-facts dd {
  margin: 0;
  text-align: end;
  overflow-wrap: anywhere;
}

.resourceflow-run-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin-block: 20px;
}

.resourceflow-run-summary > div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.resourceflow-run-summary span {
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.8rem;
}

.resourceflow-run-summary strong {
  font-size: 1.2rem;
}

.resourceflow-torrents {
  margin-block-start: 12px;
}

.resourceflow-torrent-table {
  margin-block-start: 8px;
  background: transparent;
}

.torrent-title-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-inline-size: 30rem;
}

.torrent-title-cell strong,
.torrent-title-cell span {
  overflow-wrap: anywhere;
}

.torrent-title-cell span {
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.78rem;
}

.resourceflow-table-empty {
  padding: 28px 12px;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  text-align: center;
}

.resourceflow-mobile-torrents {
  display: none;
}

.resourceflow-diagnostic-head {
  margin-block-end: 12px;
}

.resourceflow-pipeline {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 16px 0 0;
  padding: 0;
  list-style: none;
}

.resourceflow-pipeline li {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  padding-block: 7px;
}

.resourceflow-pipeline li > div,
.resourceflow-events article > div,
.resourceflow-config-actions > div {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-inline-size: 0;
}

.resourceflow-pipeline li span,
.resourceflow-events article span,
.resourceflow-config-actions span {
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.82rem;
  overflow-wrap: anywhere;
}

.resourceflow-pipeline__index {
  display: grid;
  place-items: center;
  inline-size: 28px;
  block-size: 28px;
  border-radius: 50%;
  color: rgb(var(--v-theme-on-primary));
  background: rgb(var(--v-theme-primary));
  font-size: 0.78rem;
}

.resourceflow-reasons {
  display: flex;
  flex-direction: column;
  gap: 13px;
  max-block-size: min(30rem, 52dvh);
  margin-block-start: 18px;
  padding-inline-end: 4px;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}

.resourceflow-reason > div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  margin-block-end: 5px;
}

.resourceflow-reason__track {
  display: block;
  block-size: 6px;
  overflow: hidden;
  border-radius: 3px;
  background: rgba(var(--v-theme-on-surface), 0.08);
}

.resourceflow-reason__track i {
  display: block;
  block-size: 100%;
  border-radius: inherit;
  background: rgb(var(--v-theme-warning));
}

.resourceflow-events {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-block-size: min(30rem, 52dvh);
  margin-block-start: 16px;
  padding-inline-end: 4px;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}

.resourceflow-events article {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: start;
  gap: 10px;
}

.resourceflow-config-actions {
  display: flex;
  flex-direction: column;
  gap: 16px;
  margin-block-start: 16px;
}

.resourceflow-config-actions :deep(.v-btn) {
  align-self: flex-start;
  margin-block-start: 8px;
}

@media (min-width: 960px) {
  /* 详情弹窗由宿主提供固定高度，内部只让右侧工作区承担页面滚动。 */
  .resourceflow-page--compact {
    block-size: calc(100dvh - 48px);
    min-block-size: 0;
    overflow: hidden;
  }

  .resourceflow-page--compact .resourceflow-layout {
    flex: 1 1 auto;
    grid-template-rows: minmax(0, 1fr);
    align-items: stretch;
    min-block-size: 0;
    overflow: hidden;
  }

  .resourceflow-page--compact .resourceflow-task-rail {
    position: static;
    block-size: 100%;
    min-block-size: 0;
    max-block-size: none;
    overflow: hidden;
  }

  .resourceflow-page--compact .resourceflow-task-list {
    flex: 1 1 auto;
    min-block-size: 0;
    padding-inline-end: 2px;
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
  }

  .resourceflow-page--compact .resourceflow-workspace {
    block-size: 100%;
    min-block-size: 0;
    padding-inline-end: 4px;
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
  }
}

@media (max-width: 1199px) {
  .resourceflow-stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 959px) {
  .resourceflow-page {
    padding: 12px;
  }

  .resourceflow-page--compact {
    padding-block-start: 0;
  }

  .resourceflow-page--compact .resourceflow-page__header {
    position: sticky;
    top: 0;
    z-index: 4;
    margin-inline: -12px;
    padding: 12px;
    border-block-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
    backdrop-filter: blur(var(--transparent-blur, 0px));
    background-color: rgba(var(--v-theme-surface), var(--transparent-opacity-heavy, 1));
  }

  .resourceflow-page__header,
  .resourceflow-task-head {
    align-items: flex-start;
  }

  .resourceflow-task-rail {
    display: none;
  }

  .resourceflow-mobile-select {
    display: block;
  }

  .resourceflow-layout {
    grid-template-columns: 1fr;
  }

  .resourceflow-overview-grid,
  .resourceflow-diagnostic-grid,
  .resourceflow-config-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 699px) {
  .resourceflow-task-head {
    flex-direction: column;
  }

  .resourceflow-page__header {
    align-items: center;
    flex-direction: row;
  }

  .resourceflow-page__identity {
    flex: 1 1 auto;
    overflow: hidden;
  }

  .resourceflow-page__identity > div {
    min-inline-size: 0;
  }

  .resourceflow-page__identity h1,
  .resourceflow-page__identity p {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .resourceflow-page--compact .resourceflow-page__header {
    align-items: center;
    flex-direction: row;
  }

  .resourceflow-task-head__actions {
    inline-size: 100%;
  }

  .resourceflow-page__actions,
  .resourceflow-page--compact .resourceflow-page__actions {
    flex: 0 0 auto;
    flex-wrap: nowrap;
    inline-size: auto;
    margin-inline-start: auto;
  }

  .resourceflow-page--compact .resourceflow-page__actions > :deep(.v-chip),
  .resourceflow-page--compact .resourceflow-page__identity p {
    display: none;
  }

  .resourceflow-stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .resourceflow-stat {
    min-block-size: 104px;
    padding: 13px;
  }

  .resourceflow-stat > strong {
    font-size: 1.05rem;
  }

  .resourceflow-panel {
    padding: 14px;
  }

  .resourceflow-panel__head {
    flex-wrap: wrap;
  }

  .resourceflow-facts--two {
    grid-template-columns: 1fr;
  }

  .resourceflow-torrent-table {
    display: none;
  }

  .resourceflow-mobile-torrents {
    display: flex;
    flex-direction: column;
    gap: 0;
    margin-block-start: 12px;
  }

  .resourceflow-mobile-torrent {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-block: 13px;
    border-block-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  }

  .resourceflow-mobile-torrent__head {
    align-items: flex-start;
    gap: 8px;
  }

  .resourceflow-mobile-torrent__head strong {
    min-inline-size: 0;
    overflow-wrap: anywhere;
  }

  .resourceflow-run-summary {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}

@media (max-width: 419px) {
  .resourceflow-stat-grid {
    grid-template-columns: 1fr;
  }

  .resourceflow-page:not(.resourceflow-page--compact) .resourceflow-page__identity p {
    display: none;
  }

  .resourceflow-page:not(.resourceflow-page--compact) .resourceflow-page__header,
  .resourceflow-page:not(.resourceflow-page--compact) .resourceflow-page__identity {
    gap: 8px;
  }

  .resourceflow-task-head__title {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
