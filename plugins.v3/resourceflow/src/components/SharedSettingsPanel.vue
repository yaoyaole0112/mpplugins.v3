<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import SharedRecordPagination from './SharedRecordPagination.vue'
import { unwrapResponse } from '../utils'
import { claimLinkSummary, releaseDetails, requestMediaSummaries, rejectionSummaries, uniqueRequestRows } from '../sharedRecordDisplay'

const props = defineProps({
  api: { type: Object, required: true },
  pluginBase: { type: String, required: true },
  mode: { type: String, default: 'records' },
  recordTab: { type: String, default: 'requests' },
})
const emit = defineEmits(['host-change', 'policy-synced', 'loading-change'])
const state = ref({ requests: [], records: [] })
const draft = ref({
  enabled: false, host_coordinator: false, bot_username: '', group: 'resources',
  instance_label: '我的资源刷流', bot_token: '', allowed_users: '',
  transport: 'telegram', telegram_listener: true, https_listener: false,
  https_host: '', https_authority_id: '', https_token: '', https_peers: '',
})
const busy = ref(false)
const ready = ref(false)
const canTest = computed(() => Boolean(state.value.bot_username))
const transportOptions = [
  { value: 'telegram', title: 'Telegram Bot' },
  { value: 'https', title: 'Tailscale 私网 HTTPS' },
]
const error = ref('')
const recordRows = ref({ items: [], total: 0 })
const requestRows = computed(() => uniqueRequestRows(recordRows.value.items).map(row => ({
  ...row, mediaSummaries: requestMediaSummaries(row), rejectionSummaries: rejectionSummaries(row),
  claimSummary: claimLinkSummary(row),
})))
const recordPage = ref(1)
const recordsLoading = ref(false)
const recordFilters = ref({
  requests: { query: '', owner: '', media_type: '', status: '' },
  records: { query: '', owner: '', media_type: '', status: '' },
})
const activeFilter = computed(() => recordFilters.value[props.recordTab])
const ownerOptions = computed(() => {
  const owners = new Set(recordRows.value.owners || [])
  if (activeFilter.value?.owner) owners.add(activeFilter.value.owner)
  return [
    { value: '', title: '全部下载方' },
    ...[...owners].filter(Boolean).sort((a, b) => a.localeCompare(b, 'zh-CN'))
      .map(value => ({ value, title: value })),
  ]
})
const mediaTypeOptions = [
  { value: '', title: '全部类型' },
  { value: 'tv', title: '剧集' },
  { value: 'movie', title: '电影' },
]
let searchTimer
let recordsGeneration = 0
const phaseNames = {
  requesting: '等待审批', reserved: '已获准', adding: '暂停添加中', awaiting_start: '等待入队确认',
  awaiting_qb_files: '等待 QB 文件清单', timeout_cancel_pending: '超时取消中',
  queued: '已入队', completed: '已完成', released: '已释放', rejected: '已过滤', uncertain: '待核验',
}
const phaseColors = {
  requesting: 'info', reserved: 'success', adding: 'info', awaiting_start: 'warning', awaiting_qb_files: 'warning',
  queued: 'info', timeout_cancel_pending: 'warning', completed: 'success', released: 'secondary', rejected: 'error', uncertain: 'warning',
}
const phaseIcons = {
  requesting: 'mdi-clock-outline', reserved: 'mdi-check-circle-outline', adding: 'mdi-clock-outline',
  awaiting_start: 'mdi-clock-outline', awaiting_qb_files: 'mdi-clock-outline', timeout_cancel_pending: 'mdi-clock-alert-outline',
  queued: 'mdi-check-circle-outline', completed: 'mdi-check-circle-outline',
  released: 'mdi-minus-circle-outline', rejected: 'mdi-close-circle-outline', uncertain: 'mdi-help-circle-outline',
}
const statusOptions = computed(() => [
  { value: '', title: '全部状态' },
  ...(props.recordTab === 'requests'
    ? ['completed', 'queued', 'awaiting_qb_files', 'released', 'rejected']
    : ['reserved', 'queued', 'completed', 'released'])
    .map(value => ({ value, title: phaseNames[value] })),
])

async function loadRecords() {
  if (props.mode !== 'records' || !activeFilter.value) return
  clearTimeout(searchTimer)
  const generation = ++recordsGeneration
  recordsLoading.value = true
  error.value = ''
  const params = new URLSearchParams({
    kind: props.recordTab, query: activeFilter.value.query || '', owner: activeFilter.value.owner || '',
    media_type: activeFilter.value.media_type || '', status: activeFilter.value.status || '',
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

function requestSummary(row) {
  const approved = Number(row.approved || 0)
  const rejected = row.rejected?.length || 0
  const completed = Number(row.completed || 0)
  if (row.phase === 'rejected') return `${phaseNames.rejected}（${rejected || row.file_count || 0}）`
  if (row.phase === 'queued' || row.phase === 'awaiting_start') return `已入队（${approved}）`
  if (row.phase === 'completed') return `已完成（${completed || approved}）`
  if (['reserved', 'adding'].includes(row.phase)) return `已批准（${approved}）`
  return phaseNames[row.phase] || row.phase
}

async function load(reset = false) {
  // Status and records are independent endpoints; start them together so the
  // first view does not wait for the status request before showing records.
  const recordsPromise = props.mode === 'records' ? loadRecords() : Promise.resolve()
  const data = unwrapResponse(await props.api.get(`${props.pluginBase}/shared/status`))
  state.value = data
  ready.value = true
  if (props.mode === 'records') emit('host-change', Boolean(data.host_coordinator || data.enabled))
  if (reset) draft.value = {
    enabled: Boolean(data.enabled), host_coordinator: Boolean(data.host_coordinator),
    bot_username: data.bot_username || '', group: data.group || 'resources',
    instance_label: data.instance_label || '我的资源刷流', bot_token: '',
    allowed_users: (data.allowed_users || []).join(', '),
    transport: data.transport || 'telegram', telegram_listener: data.telegram_listener !== false,
    https_listener: Boolean(data.https_listener), https_host: data.https_host || '',
    https_authority_id: data.https_authority_id || '', https_token: '',
    https_peers: (data.https_peers || []).map(peer => `${peer.instance_id} ${peer.sender} ${peer.token_hash}`).join('\n'),
  }
  await recordsPromise
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
    if (!payload.https_token) delete payload.https_token
    payload.https_peers = draft.value.https_peers.split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(line => {
      const [instance_id, rawSender, token_hash, extra] = line.split(/\s+/)
      const sender = Number(rawSender)
      if (extra || !/^[a-f0-9]{32}$/.test(instance_id) || !/^\d+$/.test(rawSender)
        || !Number.isSafeInteger(sender) || sender <= 0 || !/^[a-f0-9]{64}$/.test(token_hash)) {
        throw new Error('私网设备格式无效：每行填写实例 ID、个人账号数字 ID、密钥 SHA-256 指纹')
      }
      return { instance_id, sender, token_hash }
    })
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

onMounted(() => perform(() => load(true)))
watch(() => draft.value.host_coordinator, value => {
  if (props.mode === 'settings') emit('host-change', value)
}, { immediate: true })
watch(recordsLoading, value => emit('loading-change', value), { immediate: true })
watch(() => [props.recordTab, activeFilter.value?.query, activeFilter.value?.owner,
  activeFilter.value?.media_type, activeFilter.value?.status], (values, previous) => {
  if (props.mode !== 'records') return
  clearTimeout(searchTimer)
  recordsGeneration += 1
  // Keep the previous page visible while the next response is in flight.
  // Clearing it here caused the dialog to collapse and visibly jump on every
  // search, tab switch, and periodic refresh.
  recordsLoading.value = true
  recordPage.value = 1
  if (!activeFilter.value) return
  const delay = values[0] === previous[0] && values[2] === previous[2]
    && values[3] === previous[3] && values[4] === previous[4] ? 300 : 0
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
    <VAlert v-if="state.integrity_error" type="error" variant="tonal" density="compact">{{ state.integrity_error }}</VAlert>
    <VAlert v-if="state.history_sync?.pending" type="warning" variant="tonal" density="compact">
      历史完成证据待同步：{{ state.history_sync.pending }} 条；同步确认前暂缓新增下载。
    </VAlert>
    <template v-if="mode === 'settings'">
    <VAlert v-if="state.https_listener_error" type="warning" variant="tonal" density="compact">
      {{ state.https_listener_error }}
    </VAlert>
    <div class="text-subtitle-2">共享下载审批</div>
    <div class="shared-settings__grid">
      <VSwitch v-model="draft.enabled" label="启用共享下载审批" color="primary" density="compact" hide-details inset />
      <VSwitch v-model="draft.host_coordinator" label="在本机运行协调端" color="primary" density="compact" hide-details inset />
      <VSelect v-model="draft.transport" label="审批传输" :items="transportOptions" density="comfortable" hide-details />
      <VTextField v-model="draft.group" label="共享组" density="comfortable" hide-details />
      <VTextField v-model="draft.bot_username" label="协调 Bot 用户名（现有账本保留原值）"
        placeholder="@resource_bot" density="comfortable" hide-details />
      <VTextField v-model="draft.instance_label" label="本机名称" maxlength="80" density="comfortable" hide-details />
      <VTextField :model-value="state.instance_id || '-'" label="本机实例 ID"
        class="shared-settings__instance" readonly density="comfortable" hide-details />
    </div>
    <div v-if="draft.host_coordinator" class="shared-settings__grid">
      <VSwitch v-model="draft.telegram_listener" label="提供 Telegram 审批入口" color="primary" density="compact" hide-details inset />
      <VSwitch v-model="draft.https_listener" label="提供私网 HTTPS 审批入口" color="primary" density="compact" hide-details inset />
    </div>
    <div v-if="draft.host_coordinator && draft.telegram_listener" class="shared-settings__grid shared-settings__credentials">
      <VTextField v-model="draft.bot_token" type="password" autocomplete="new-password"
        :label="state.token_configured ? '协调 Bot Token（已保存）' : '协调 Bot Token'" density="comfortable" hide-details />
      <VTextarea v-model="draft.allowed_users" label="允许的个人账号 ID" rows="1" density="comfortable"
        placeholder="123456789, 987654321; 112233445" auto-grow hide-details />
    </div>
    <div v-if="draft.host_coordinator && draft.https_listener" class="shared-settings__grid">
      <VTextField :model-value="state.coordinator_authority_id || ''" label="共享账本 ID"
        class="shared-settings__instance" readonly density="comfortable" hide-details />
      <VTextarea v-model="draft.https_peers" label="允许的私网设备（实例 ID / 个人账号 ID / 密钥指纹）"
        rows="2" max-rows="6" density="comfortable" auto-grow hide-details />
    </div>
    <div v-if="draft.transport === 'https'" class="shared-settings__grid">
      <VTextField v-if="!draft.host_coordinator" v-model="draft.https_host" label="协调端 Tailscale HTTPS 域名"
        placeholder="approval.example.ts.net" density="comfortable" hide-details />
      <VTextField v-if="!draft.host_coordinator" v-model="draft.https_authority_id" label="协调端共享账本 ID"
        class="shared-settings__instance" density="comfortable" hide-details />
      <VTextField v-model="draft.https_token" type="password" autocomplete="new-password"
        :label="state.https_token_configured ? '本机私网审批密钥（已保存）' : '本机私网审批密钥'"
        density="comfortable" hide-details />
    </div>
    <dl class="shared-settings__status">
      <div><dt>审批传输：</dt><dd>{{ state.transport === 'https' ? 'Tailscale HTTPS' : 'Telegram（自动生成ED2K链接）' }}</dd></div>
      <div><dt>协调端：</dt><dd>{{ state.host_coordinator ? state.coordinator_running ? '本机运行中' : '本机未运行' : '远端' }}</dd></div>
      <div><dt>最近审批确认：</dt><dd>{{ timestamp(state.last_ok || state.coordinator_last_ok) }}</dd></div>
      <div><dt>协调端规则版本：</dt><dd>{{ state.probe?.policy?.revision?.slice(0, 12) || '-' }}</dd></div>
    </dl>
    </template>
    <template v-else>
    <div v-if="activeFilter" class="shared-settings__search">
      <VTextField v-model="activeFilter.query" label="搜索记录" prepend-inner-icon="mdi-magnify"
        clearable maxlength="200" density="comfortable" hide-details />
      <VSelect v-model="activeFilter.owner" label="下载方筛选" :items="ownerOptions"
        item-title="title" item-value="value" clearable density="comfortable" hide-details />
      <VSelect v-model="activeFilter.media_type" label="类型筛选" :items="mediaTypeOptions"
        item-title="title" item-value="value" density="comfortable" hide-details />
      <VSelect v-model="activeFilter.status" label="状态筛选" :items="statusOptions"
        density="comfortable" hide-details />
    </div>
    <div v-if="recordTab === 'requests'" class="shared-settings__records">
      <div v-if="!recordsLoading && !recordRows.items.length" class="text-body-2 text-medium-emphasis">
        {{ activeFilter.query || activeFilter.owner || activeFilter.media_type || activeFilter.status ? '无匹配记录' : '暂无共享申请' }}
      </div>
      <article v-for="row in requestRows" :key="row.request_id" class="shared-settings__record">
        <div class="shared-settings__record-head">
          <div v-if="row.mediaSummaries.length" class="shared-settings__heading">
            <div v-for="(summary, index) in row.mediaSummaries" :key="index"
              class="shared-settings__preview shared-settings__media-summary shared-settings__info text-body-2">
              <span>{{ summary }}</span>
            </div>
          </div>
          <strong v-else>{{ row.title }}</strong>
          <span class="shared-settings__badge" :data-tone="phaseColors[row.phase] || 'secondary'">
            <VIcon :icon="phaseIcons[row.phase] || 'mdi-clock-outline'" size="16" />
            {{ requestSummary(row) }}
          </span>
        </div>
        <strong v-if="row.mediaSummaries.length" class="shared-settings__torrent-title">{{ row.title }}</strong>
        <div class="shared-settings__meta text-caption">
          <span>申请时间：{{ timestamp(row.created) }}</span><span>最近更新：{{ timestamp(row.updated) }}</span>
          <span v-if="row.download_started_at">下载开始：{{ timestamp(row.download_started_at) }}</span>
          <span v-if="row.timeout_at">超时处理：{{ timestamp(row.timeout_at) }}</span>
          <span>任务：{{ row.task_name || row.task_id || '-' }}</span><span>下载器：{{ row.downloader || '-' }}</span>
        </div>
        <p v-if="row.error && row.phase !== 'rejected'" class="shared-settings__error text-body-2">{{ row.error }}</p>
        <p v-if="row.integrity_error" class="shared-settings__error text-body-2">{{ row.integrity_error }}</p>
        <p v-for="(reason, index) in row.rejectionSummaries" :key="index" class="shared-settings__reason text-body-2">
          {{ reason }}
        </p>
        <div class="shared-settings__ids">
          <span class="shared-settings__id text-caption">申请编号：{{ row.request_id }}</span>
          <span v-if="row.claimSummary" class="shared-settings__id text-caption">
            关联占位：{{ row.claimSummary }}
          </span>
        </div>
      </article>
    </div>
    <div v-else-if="recordTab === 'records' && (state.host_coordinator || state.enabled)" class="shared-settings__records">
      <div v-if="!recordsLoading && !recordRows.items.length" class="text-body-2 text-medium-emphasis">
        {{ activeFilter.query || activeFilter.owner || activeFilter.media_type || activeFilter.status ? '无匹配记录' : '暂无共享占位' }}
      </div>
      <article v-for="row in recordRows.items" :key="row.id" class="shared-settings__record">
        <div class="shared-settings__record-head">
          <div class="shared-settings__heading">
            <div class="shared-settings__preview shared-settings__media-summary shared-settings__info text-body-2">
              <span>{{ releaseDetails(row) }}</span>
            </div>
          </div>
          <span class="shared-settings__badge" :data-tone="phaseColors[row.state] || 'secondary'">
            <VIcon :icon="phaseIcons[row.state] || 'mdi-clock-outline'" size="16" />
            {{ phaseNames[row.state] || row.state }}
          </span>
        </div>
        <strong class="shared-settings__torrent-title">{{ row.title }}</strong>
        <div class="shared-settings__meta shared-settings__claim-meta text-caption">
          <span>下载时间：{{ timestamp(row.created) }}</span><span>最近更新：{{ timestamp(row.updated) }}</span>
          <span class="shared-settings__claim-owner">下载方：{{ row.label || '-' }}</span>
        </div>
        <div class="shared-settings__ids">
          <span class="shared-settings__id text-caption">占位编号：{{ row.id }}</span>
          <span v-if="row.request_id" class="shared-settings__id text-caption">关联申请：{{ row.request_id }}</span>
        </div>
      </article>
    </div>
    <SharedRecordPagination v-if="activeFilter" v-model="recordPage" :total="recordRows.total"
      @update:model-value="loadRecords" />
  </template>
  </section>
</template>

<style scoped>
.shared-settings { display: flex; flex-direction: column; gap: 20px; min-inline-size: 0; }
.shared-settings--compact { gap: 18px; }
.shared-settings__search { display: grid; grid-template-columns: minmax(0, 1fr) minmax(150px, 210px) minmax(130px, 160px) minmax(150px, 170px); gap: 12px; }
.shared-settings__search > * { min-inline-size: 0; }
.shared-settings__grid { display: grid; grid-template-columns: minmax(0, 1fr); align-items: start; gap: 16px 24px; min-inline-size: 0; }
.shared-settings__grid > * { min-inline-size: 0; }
.shared-settings__instance :deep(input) { font-family: monospace; font-size: 11px; }
.shared-settings__status { margin: 0; display: grid; gap: 10px; }
.shared-settings__status > div { display: flex; justify-content: space-between; gap: 12px; }
.shared-settings__status dd { margin: 0; overflow-wrap: anywhere; text-align: end; }
.shared-settings--compact .shared-settings__status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 0;
  padding: 12px 14px;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 6px;
  background: rgba(var(--v-theme-surface-variant), 0.14);
  font-size: 13px;
}
.shared-settings--compact .shared-settings__status > div {
  min-inline-size: 0;
  justify-content: flex-start;
  gap: 4px;
  flex-wrap: nowrap;
  white-space: nowrap;
}
.shared-settings--compact .shared-settings__status > div:not(:last-child)::after {
  content: "·";
  margin-inline: 12px;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
}
.shared-settings--compact .shared-settings__status dt { flex-shrink: 0; }
.shared-settings--compact .shared-settings__status dd { text-align: start; }
@media (min-width: 600px) {
  .shared-settings__grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .shared-settings--compact .shared-settings__status { grid-template-columns: none; }
}
.shared-settings__records { min-inline-size: 0; }
.shared-settings__record > * { margin-inline-start: 0; }
.shared-settings__record { display: flex; flex-direction: column; gap: 10px; padding-block: 20px; border-block-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); overflow-wrap: anywhere; min-inline-size: 0; }
.shared-settings__record-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; min-inline-size: 0; }
.shared-settings__record-head strong { flex: 1; min-inline-size: 0; font-size: 14px; line-height: 1.6; }
.shared-settings__heading { display: flex; flex: 1; flex-direction: column; gap: 8px; min-inline-size: 0; }
.shared-settings__torrent-title { font-size: 14px; line-height: 1.6; }
.shared-settings__media-summary { font-size: 16px; font-weight: 600; line-height: 1.55; }
.shared-settings__meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; font-size: 14px !important; line-height: 1.55; }
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
.shared-settings__claim-owner { color: rgba(var(--v-theme-on-surface), 0.78); }
.shared-settings__meta, .shared-settings__id { color: rgba(var(--v-theme-on-surface), 0.72); }
.shared-settings__error { margin: 0; color: rgb(var(--v-theme-error)); }
.shared-settings__reason { margin: 0; }
.shared-settings__id { font-family: monospace; }
.shared-settings__ids { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 18px; min-inline-size: 0; }
.shared-settings__claim-meta { display: flex; flex-wrap: wrap; }
@media (min-width: 600px) and (max-width: 800px) {
  .shared-settings__search { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 599px) {
  .shared-settings__search { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .shared-settings__search > :nth-child(-n+2) { grid-column: 1 / -1; }
  .shared-settings__record-head { gap: 8px; }
  .shared-settings__record-head .shared-settings__badge { max-inline-size: 56%; white-space: normal; }
  .shared-settings__meta { flex-direction: column; align-items: flex-start; gap: 4px; }
  .shared-settings__media-summary { font-size: 15px; }
  .shared-settings--compact .shared-settings__status > div { flex-wrap: wrap; white-space: normal; }
  .shared-settings__status dd { margin-inline-start: auto; }
}
</style>
