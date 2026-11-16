<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { formatBytes, unwrapResponse } from '../utils'

const props = defineProps({
  api: { type: Object, required: true },
  pluginBase: { type: String, required: true },
  mode: { type: String, default: 'records' },
  recordTab: { type: String, default: 'requests' },
})
const emit = defineEmits(['host-change', 'policy-synced'])
const state = ref({ requests: [], records: [] })
const draft = ref({
  enabled: false, host_coordinator: false, bot_username: '', group: 'resources',
  instance_label: '我的资源刷流', bot_token: '', allowed_users: '',
})
const busy = ref(false)
const ready = ref(false)
const canTest = computed(() => Boolean(state.value.bot_username))
const error = ref('')
const recordRows = ref({ items: [], total: 0 })
const recordPage = ref(1)
const recordsLoading = ref(false)
const recordFilters = ref({
  requests: { query: '', status: '' }, records: { query: '', status: '' },
})
const activeFilter = computed(() => recordFilters.value[props.recordTab])
let searchTimer
let recordsGeneration = 0
const phaseNames = {
  requesting: '等待审批', reserved: '已获准', adding: '暂停添加中', awaiting_start: '等待入队确认',
  timeout_cancel_pending: '超时取消中',
  queued: '已入队', completed: '已完成', released: '已释放', rejected: '已排除', uncertain: '待核验',
}
const phaseColors = {
  requesting: 'info', reserved: 'success', adding: 'info', awaiting_start: 'warning',
  queued: 'info', timeout_cancel_pending: 'warning', completed: 'success', released: 'secondary', rejected: 'error', uncertain: 'warning',
}
const specLabels = { resolution: '分辨率', effects: '特效', codec: '编码', source: '来源', fps: '帧率', size: '大小' }
const phaseIcons = {
  requesting: 'mdi-clock-outline', reserved: 'mdi-check-circle-outline', adding: 'mdi-clock-outline',
  awaiting_start: 'mdi-clock-outline', timeout_cancel_pending: 'mdi-clock-alert-outline',
  queued: 'mdi-check-circle-outline', completed: 'mdi-check-circle-outline',
  released: 'mdi-minus-circle-outline', rejected: 'mdi-close-circle-outline', uncertain: 'mdi-help-circle-outline',
}
const statusOptions = computed(() => [
  { value: '', title: '全部状态' },
  ...Object.entries(phaseNames).filter(([key]) =>
    props.recordTab === 'requests' || ['reserved', 'queued', 'completed', 'released'].includes(key),
  ).map(([value, title]) => ({ value, title })),
])

function rejectionReason(item) {
  const reason = item.reason === '同规格已占位或已完成' ? '资源已完成/已下载' : item.reason
  return `${reason || ''}${item.owner ? ` · 下载方：${item.owner}` : ''}`
}

async function loadRecords() {
  if (props.mode !== 'records' || !activeFilter.value) return
  clearTimeout(searchTimer)
  const generation = ++recordsGeneration
  recordsLoading.value = true
  error.value = ''
  const params = new URLSearchParams({
    kind: props.recordTab, query: activeFilter.value.query || '', status: activeFilter.value.status || '',
    page: String(recordPage.value), page_size: '20',
  })
  try {
    const data = unwrapResponse(await props.api.get(`${props.pluginBase}/shared/records?${params}`))
    if (generation !== recordsGeneration) return
    recordRows.value = data || { items: [], total: 0 }
    recordPage.value = data?.page || 1
  } catch (failure) {
    if (generation === recordsGeneration) error.value = failure?.message || '读取共享记录失败'
  } finally {
    if (generation === recordsGeneration) recordsLoading.value = false
  }
}

function timestamp(value) {
  if (!value) return '-'
  const date = new Date(Number(value) * 1000)
  if (Number.isNaN(date.getTime())) return '-'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    hourCycle: 'h23',
  }).format(date)
}

function identity(row) {
  const episodes = (row.episodes || []).map(value => `E${String(value).padStart(2, '0')}`).join(', ')
  const type = row.media_type === 'movie' ? '电影' : `S${String(row.season ?? 0).padStart(2, '0')}${episodes}`
  const title = row.media_title ? `${row.media_title} · ` : ''
  return `${title}${row.tmdb_id ? `TMDB ${row.tmdb_id}` : '身份待确认'} · ${type}`
}

function specifications(specs = {}) {
  return Object.entries(specLabels).flatMap(([key, label]) => {
    const value = specs?.[key]
    if (value === undefined || value === null || value === '') return []
    return [`${label} ${key === 'size' ? formatBytes(value) : key === 'fps' ? `${value} FPS` : value}`]
  }).join(' · ') || '暂无规格'
}

function releaseDetails(row) {
  return `${identity(row)} · ${specifications(row.specs)}`
}

function requestSummary(row) {
  const approved = Number(row.approved || 0)
  const rejected = row.rejected?.length || 0
  const completed = Number(row.completed || 0)
  if (row.phase === 'rejected') return `已排除（${rejected || row.file_count || 0}）`
  if (row.phase === 'queued' || row.phase === 'awaiting_start') return `已入队（${approved}）`
  if (row.phase === 'completed') return `已完成（${completed || approved}）`
  if (['reserved', 'adding'].includes(row.phase)) return `已批准（${approved}）`
  return phaseNames[row.phase] || row.phase
}

async function load(reset = false) {
  const data = unwrapResponse(await props.api.get(`${props.pluginBase}/shared/status`))
  state.value = data
  ready.value = true
  if (props.mode === 'records') emit('host-change', Boolean(data.host_coordinator))
  if (reset) draft.value = {
    enabled: Boolean(data.enabled), host_coordinator: Boolean(data.host_coordinator),
    bot_username: data.bot_username || '', group: data.group || 'resources',
    instance_label: data.instance_label || '我的资源刷流', bot_token: '',
    allowed_users: (data.allowed_users || []).join(', '),
  }
  await loadRecords()
}

async function perform(action) {
  busy.value = true
  error.value = ''
  try { await action() } catch (failure) { error.value = failure?.message || '共享审批操作失败' }
  finally { busy.value = false }
}

async function save() {
  busy.value = true
  error.value = ''
  try {
    if (!ready.value) throw new Error('共享连接尚未加载，请关闭后重新打开全局设置')
    const users = draft.value.allowed_users.split(/[\s,，;；、]+/).filter(Boolean)
    if (users.some(value => !/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) <= 0)) {
      throw new Error('白名单须为个人账号数字 ID')
    }
    const payload = { ...draft.value, allowed_users: [...new Set(users.map(Number))] }
    if (!payload.bot_token) delete payload.bot_token
    unwrapResponse(await props.api.post(`${props.pluginBase}/shared/settings`, payload))
    await load(true)
    return state.value
  } catch (failure) {
    error.value = failure?.message || '共享连接保存失败'
    throw failure
  } finally { busy.value = false }
}

function test() {
  return perform(async () => {
    state.value = unwrapResponse(await props.api.post(`${props.pluginBase}/shared/test`, {}))
    emit('policy-synced')
  })
}

function reconcile() {
  return perform(async () => {
    unwrapResponse(await props.api.post(`${props.pluginBase}/shared/reconcile`, {}))
    await load()
  })
}

onMounted(() => perform(() => load(true)))
watch(() => draft.value.host_coordinator, value => {
  if (props.mode === 'settings') emit('host-change', value)
}, { immediate: true })
watch(() => [props.recordTab, activeFilter.value?.query, activeFilter.value?.status], (values, previous) => {
  if (props.mode !== 'records') return
  clearTimeout(searchTimer)
  recordsGeneration += 1
  recordsLoading.value = false
  recordRows.value = { items: [], total: 0 }
  recordPage.value = 1
  if (!activeFilter.value) return
  const delay = values[0] === previous[0] && values[2] === previous[2] ? 300 : 0
  recordsLoading.value = true
  searchTimer = setTimeout(loadRecords, delay)
})
onUnmounted(() => { clearTimeout(searchTimer); recordsGeneration += 1 })
defineExpose({ save, test, ready, busy, canTest, refresh: () => perform(() => load()) })
</script>

<template>
  <section class="shared-settings" :class="{ 'shared-settings--compact': mode === 'settings' }">
    <VAlert v-if="error" type="error" variant="tonal" density="compact">{{ error }}</VAlert>
    <VAlert v-if="state.last_error" type="warning" variant="tonal" density="compact">{{ state.last_error }}</VAlert>
    <template v-if="mode === 'settings'">
    <div class="text-subtitle-2">Telegram 共享审批</div>
    <div class="shared-settings__grid">
      <VSwitch v-model="draft.enabled" label="启用共享下载审批" color="primary" density="compact" hide-details inset />
      <VSwitch v-model="draft.host_coordinator" label="在本机运行协调端" color="primary" density="compact" hide-details inset />
      <VTextField v-model="draft.bot_username" label="协调 Bot 用户名" placeholder="@resource_bot" density="comfortable" hide-details />
      <VTextField v-model="draft.group" label="共享组" density="comfortable" hide-details />
      <VTextField v-model="draft.instance_label" label="本机名称" maxlength="80" density="comfortable" hide-details />
      <VTextField :model-value="state.instance_id || '-'" label="本机实例 ID"
        class="shared-settings__instance" readonly density="comfortable" hide-details />
    </div>
    <div v-if="draft.host_coordinator" class="shared-settings__grid shared-settings__credentials">
      <VTextField v-model="draft.bot_token" type="password" autocomplete="new-password"
        :label="state.token_configured ? '协调 Bot Token（已保存）' : '协调 Bot Token'" density="comfortable" hide-details />
      <VTextarea v-model="draft.allowed_users" label="允许的个人账号 ID" rows="1" density="comfortable"
        placeholder="123456789, 987654321; 112233445" auto-grow hide-details />
    </div>
    <dl class="shared-settings__status">
      <div><dt>账号来源</dt><dd>自动生成ED2K链接 · 个人账号</dd></div>
      <div><dt>协调端</dt><dd>{{ state.host_coordinator ? state.coordinator_running ? '本机运行中' : '本机未运行' : '远端' }}</dd></div>
      <div><dt>最近审批确认</dt><dd>{{ timestamp(state.last_ok) }}</dd></div>
      <div v-if="state.probe?.user_id"><dt>本机 Telegram ID</dt><dd>{{ state.probe.user_id }}</dd></div>
      <div v-if="state.probe?.policy"><dt>协调端规则版本</dt><dd>{{ state.probe.policy.revision.slice(0, 12) }}</dd></div>
    </dl>
    </template>
    <template v-else>
    <div v-if="activeFilter" class="shared-settings__search">
      <VTextField v-model="activeFilter.query" label="搜索记录" prepend-inner-icon="mdi-magnify"
        clearable maxlength="200" density="comfortable" hide-details />
      <VSelect v-model="activeFilter.status" label="状态筛选" :items="statusOptions"
        density="comfortable" hide-details />
    </div>
    <VProgressLinear v-if="recordsLoading" indeterminate />
    <div v-else-if="activeFilter" class="text-caption text-medium-emphasis">共 {{ recordRows.total }} 条</div>
    <div v-if="recordTab === 'requests'" class="shared-settings__records">
      <div v-if="!recordsLoading && !recordRows.items.length" class="text-body-2 text-medium-emphasis">
        {{ activeFilter.query || activeFilter.status ? '无匹配记录' : '暂无共享申请' }}
      </div>
      <article v-for="row in recordRows.items" :key="row.request_id" class="shared-settings__record">
        <div class="shared-settings__record-head">
          <strong>{{ row.title }}</strong>
          <span class="shared-settings__badge" :data-tone="phaseColors[row.phase] || 'secondary'">
            <VIcon :icon="phaseIcons[row.phase] || 'mdi-clock-outline'" size="16" />
            {{ requestSummary(row) }}
          </span>
        </div>
        <div class="shared-settings__meta text-caption">
          <span>申请时间：{{ timestamp(row.created) }}</span><span>最近更新：{{ timestamp(row.updated) }}</span>
          <span v-if="row.download_started_at">下载开始：{{ timestamp(row.download_started_at) }}</span>
          <span v-if="row.timeout_at">超时处理：{{ timestamp(row.timeout_at) }}</span>
          <span>任务：{{ row.task_name || row.task_id || '-' }}</span><span>下载器：{{ row.downloader || '-' }}</span>
        </div>
        <div v-if="row.files?.length" class="shared-settings__preview shared-settings__info text-body-2">
          <span>
            <template v-if="row.files.length > 1">首文件 · </template>{{ releaseDetails(row.files[0]) }}
          </span>
        </div>
        <p v-if="row.error" class="shared-settings__error text-body-2">{{ row.error }}</p>
        <p v-for="(item, index) in row.rejected || []" :key="index" class="shared-settings__reason text-body-2">
          {{ rejectionReason(item) }}
        </p>
        <span class="shared-settings__id text-caption">申请编号：{{ row.request_id }}</span>
      </article>
      <VBtn prepend-icon="mdi-refresh" variant="text" :disabled="busy" @click="reconcile">核验未完成申请</VBtn>
    </div>
    <div v-else-if="recordTab === 'records' && state.host_coordinator" class="shared-settings__records">
      <div v-if="!recordsLoading && !recordRows.items.length" class="text-body-2 text-medium-emphasis">
        {{ activeFilter.query || activeFilter.status ? '无匹配记录' : '暂无共享占位' }}
      </div>
      <article v-for="row in recordRows.items" :key="row.id" class="shared-settings__record">
        <div class="shared-settings__record-head">
          <strong>{{ row.title }}</strong>
          <span class="shared-settings__badge" :data-tone="phaseColors[row.state] || 'secondary'">
            <VIcon :icon="phaseIcons[row.state] || 'mdi-clock-outline'" size="16" />
            {{ phaseNames[row.state] || row.state }}
          </span>
        </div>
        <div class="shared-settings__claim-info text-body-2">
          <span class="shared-settings__info">下载方：{{ row.label || '-' }}</span>
          <span class="shared-settings__info">{{ releaseDetails(row) }}</span>
        </div>
        <div class="shared-settings__meta shared-settings__claim-meta text-caption">
          <span>占位时间：{{ timestamp(row.created) }}</span><span>最近更新：{{ timestamp(row.updated) }}</span>
          <span class="shared-settings__id">占位编号：{{ row.id }}</span>
        </div>
      </article>
    </div>
    <VPagination v-if="activeFilter && recordRows.total > 20" v-model="recordPage"
      :length="Math.ceil(recordRows.total / 20)" :total-visible="3" @update:model-value="loadRecords" />
    </template>
  </section>
</template>

<style scoped>
.shared-settings { display: flex; flex-direction: column; gap: 16px; min-inline-size: 0; }
.shared-settings--compact { gap: 12px; }
.shared-settings__search { display: grid; grid-template-columns: minmax(0, 1fr) 160px; gap: 12px; }
.shared-settings__search > * { min-inline-size: 0; }
.shared-settings__grid { display: grid; grid-template-columns: minmax(0, 1fr); align-items: start; gap: 12px 20px; min-inline-size: 0; }
.shared-settings__grid > * { min-inline-size: 0; }
.shared-settings__instance :deep(input) { font-family: monospace; font-size: 11px; }
.shared-settings__status { margin: 0; display: grid; gap: 10px; }
.shared-settings__status > div { display: flex; justify-content: space-between; gap: 12px; }
.shared-settings__status dd { margin: 0; overflow-wrap: anywhere; text-align: end; }
.shared-settings--compact .shared-settings__status { gap: 8px 20px; font-size: 13px; }
.shared-settings--compact .shared-settings__status > div { min-inline-size: 0; justify-content: flex-start; gap: 8px; flex-wrap: wrap; }
.shared-settings--compact .shared-settings__status dt { flex-shrink: 0; }
@media (min-width: 600px) {
  .shared-settings__grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .shared-settings--compact .shared-settings__status { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
.shared-settings__records { min-inline-size: 0; }
.shared-settings__record { display: flex; flex-direction: column; gap: 10px; padding-block: 20px; border-block-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); overflow-wrap: anywhere; min-inline-size: 0; }
.shared-settings__record-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; min-inline-size: 0; }
.shared-settings__record-head strong { flex: 1; min-inline-size: 0; font-size: 14px; line-height: 1.6; }
.shared-settings__meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; }
/* Native badges keep approval colors independent of host VChip/glass theme overrides. */
.shared-settings__badge { display: inline-flex; align-items: center; justify-content: center; gap: 4px; flex: 0 0 auto; max-inline-size: 52%; min-block-size: 32px; padding: 6px 10px; border: 0; border-radius: 7px; font-size: 13px; font-weight: 400; line-height: 20px; color: rgb(var(--shared-badge-ink)); background-color: rgba(var(--shared-badge-fill), 0.13); --shared-badge-ink: 139, 145, 155; --shared-badge-fill: 139, 145, 155; }
.shared-settings__badge :deep(.v-icon) { flex: 0 0 auto; }
.shared-settings__badge[data-tone="success"] { --shared-badge-ink: 87, 185, 133; --shared-badge-fill: 60, 180, 110; }
.shared-settings__badge[data-tone="error"] { --shared-badge-ink: 223, 123, 123; --shared-badge-fill: 220, 60, 60; }
.shared-settings__badge[data-tone="info"] { --shared-badge-ink: 86, 160, 220; --shared-badge-fill: 60, 140, 220; }
.shared-settings__badge[data-tone="warning"] { --shared-badge-ink: 219, 163, 75; --shared-badge-fill: 220, 150, 60; }
.shared-settings__info { align-self: flex-start; inline-size: fit-content; max-inline-size: 100%; min-inline-size: 0; padding: 7px 10px; border-radius: 7px; background-color: rgba(var(--v-theme-on-surface), 0.06); overflow-wrap: anywhere; }
.shared-settings__preview { display: flex; flex-wrap: wrap; gap: 4px; }
.shared-settings__preview > span { min-inline-size: 0; }
.shared-settings__claim-info { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 8px; }
.shared-settings__meta, .shared-settings__id { color: rgba(var(--v-theme-on-surface), 0.72); }
.shared-settings__error { margin: 0; color: rgb(var(--v-theme-error)); }
.shared-settings__reason { margin: 0; }
.shared-settings__id { font-family: monospace; }
.shared-settings__claim-meta { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.5fr); }
.shared-settings__claim-meta .shared-settings__id { overflow-wrap: anywhere; }
@media (max-width: 599px) {
  .shared-settings__search { grid-template-columns: minmax(0, 1fr); }
  .shared-settings__record-head { gap: 8px; }
  .shared-settings__record-head .shared-settings__badge { max-inline-size: 56%; white-space: normal; }
  .shared-settings__meta { flex-direction: column; align-items: flex-start; gap: 4px; }
  .shared-settings__status > div { flex-wrap: wrap; }
  .shared-settings__status dd { margin-inline-start: auto; }
}
</style>
