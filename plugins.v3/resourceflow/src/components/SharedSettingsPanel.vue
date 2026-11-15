<script setup>
import { onMounted, ref, watch } from 'vue'
import { formatBytes, unwrapResponse } from '../utils'

const props = defineProps({ api: { type: Object, required: true }, pluginBase: { type: String, required: true } })
const emit = defineEmits(['host-change', 'policy-synced'])
const state = ref({ requests: [], records: [] })
const draft = ref({
  enabled: false, host_coordinator: false, bot_username: '', group: 'resources',
  instance_label: '我的资源刷流', bot_token: '', allowed_users: '',
})
const busy = ref(false)
const error = ref('')
const tab = ref('requests')
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
const decisionNames = { approved: '已批准', rejected: '已排除', not_approved: '未获批准', pending: '待审批' }
const decisionColors = { approved: 'success', rejected: 'error', not_approved: 'warning', pending: 'info' }

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

async function load(reset = false) {
  const data = unwrapResponse(await props.api.get(`${props.pluginBase}/shared/status`))
  state.value = data
  if (reset) draft.value = {
    enabled: Boolean(data.enabled), host_coordinator: Boolean(data.host_coordinator),
    bot_username: data.bot_username || '', group: data.group || 'resources',
    instance_label: data.instance_label || '我的资源刷流', bot_token: '',
    allowed_users: (data.allowed_users || []).join('\n'),
  }
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
    const users = draft.value.allowed_users.split(/[\s,，]+/).filter(Boolean)
    if (users.some(value => !/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) <= 0)) {
      throw new Error('白名单须为个人账号数字 ID')
    }
    const payload = { ...draft.value, allowed_users: users.map(Number) }
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
watch(() => draft.value.host_coordinator, value => emit('host-change', value), { immediate: true })
defineExpose({ save })
</script>

<template>
  <section class="shared-settings">
    <div class="text-subtitle-2">Telegram 共享审批</div>
    <VAlert v-if="error" type="error" variant="tonal" density="compact">{{ error }}</VAlert>
    <VAlert v-if="state.last_error" type="warning" variant="tonal" density="compact">{{ state.last_error }}</VAlert>
    <VSwitch v-model="draft.enabled" label="启用共享下载审批" color="primary" hide-details inset />
    <VRow>
      <VCol cols="12" sm="6">
        <VTextField v-model="draft.bot_username" label="协调 Bot 用户名" placeholder="@resource_bot" hide-details />
      </VCol>
      <VCol cols="12" sm="6">
        <VTextField v-model="draft.group" label="共享组" hide-details />
      </VCol>
      <VCol cols="12" sm="6">
        <VTextField v-model="draft.instance_label" label="本机名称" maxlength="80" hide-details />
      </VCol>
      <VCol cols="12" sm="6">
        <VTextField :model-value="state.instance_id || '-'" label="本机实例 ID"
          class="shared-settings__instance" readonly hide-details />
      </VCol>
    </VRow>
    <VSwitch v-model="draft.host_coordinator" label="在本机运行协调端" color="primary" hide-details inset />
    <template v-if="draft.host_coordinator">
      <VTextField v-model="draft.bot_token" type="password" autocomplete="new-password"
        :label="state.token_configured ? '协调 Bot Token（已保存）' : '协调 Bot Token'" hide-details />
      <VTextarea v-model="draft.allowed_users" label="允许的个人账号 ID" rows="2" auto-grow hide-details />
    </template>
    <div class="shared-settings__actions">
      <VBtn prepend-icon="mdi-connection" :disabled="busy || !state.bot_username" variant="text" @click="test">测试审批连接</VBtn>
      <VTooltip text="刷新共享状态">
        <template #activator="{ props: tip }">
          <VBtn v-bind="tip" icon="mdi-refresh" aria-label="刷新共享状态" variant="text"
            :disabled="busy" @click="perform(() => load())" />
        </template>
      </VTooltip>
    </div>
    <dl class="shared-settings__status">
      <div><dt>账号来源</dt><dd>自动生成ED2K链接 · 个人账号</dd></div>
      <div><dt>协调端</dt><dd>{{ state.host_coordinator ? state.coordinator_running ? '本机运行中' : '本机未运行' : '远端' }}</dd></div>
      <div><dt>最近审批确认</dt><dd>{{ timestamp(state.last_ok) }}</dd></div>
      <div v-if="state.probe?.user_id"><dt>本机 Telegram ID</dt><dd>{{ state.probe.user_id }}</dd></div>
      <div v-if="state.probe?.policy"><dt>协调端规则版本</dt><dd>{{ state.probe.policy.revision.slice(0, 12) }}</dd></div>
    </dl>
    <VTabs v-model="tab" density="compact">
      <VTab value="requests">本机共享申请</VTab>
      <VTab v-if="state.host_coordinator" value="records">共享占位记录</VTab>
    </VTabs>
    <div v-if="tab === 'requests'" class="shared-settings__records">
      <div v-if="!state.requests?.length" class="text-body-2 text-medium-emphasis">暂无共享申请</div>
      <article v-for="row in state.requests || []" :key="row.request_id" class="shared-settings__record">
        <strong>{{ row.title }}</strong>
        <div class="shared-settings__badges">
          <span class="shared-settings__badge" :data-tone="phaseColors[row.phase] || 'secondary'">{{ phaseNames[row.phase] || row.phase }}</span>
          <span class="shared-settings__badge" data-tone="success"><VIcon icon="mdi-check-circle-outline" size="16" />批准 {{ row.approved || 0 }}</span>
          <span class="shared-settings__badge" data-tone="error"><VIcon icon="mdi-close-circle-outline" size="16" />排除 {{ row.rejected?.length || 0 }}</span>
          <span class="text-caption">文件 {{ row.file_count ?? row.files?.length ?? 0 }} · 完成 {{ row.completed || 0 }}</span>
        </div>
        <div class="shared-settings__meta text-caption">
          <span>申请时间：{{ timestamp(row.created) }}</span><span>最近更新：{{ timestamp(row.updated) }}</span>
          <span v-if="row.download_started_at">下载开始：{{ timestamp(row.download_started_at) }}</span>
          <span v-if="row.timeout_at">超时处理：{{ timestamp(row.timeout_at) }}</span>
          <span>任务：{{ row.task_name || row.task_id || '-' }}</span><span>下载器：{{ row.downloader || '-' }}</span>
        </div>
        <div v-if="row.files?.length" class="shared-settings__preview text-body-2">
          <span><template v-if="row.files.length > 1">首文件 · </template>{{ identity(row.files[0]) }}</span>
          <span class="text-caption">{{ specifications(row.files[0].specs) }}</span>
        </div>
        <p v-if="row.error" class="shared-settings__error text-body-2">{{ row.error }}</p>
        <p v-for="(item, index) in row.rejected || []" :key="index" class="shared-settings__reason text-body-2">
          {{ item.reason }}<template v-if="item.owner"> · 占位方 {{ item.owner }}</template>
        </p>
        <details v-if="row.files?.length" class="shared-settings__details">
          <summary>文件审批明细（{{ row.files.length }}）</summary>
          <div v-for="file in row.files" :key="file.id" class="shared-settings__file">
            <div class="shared-settings__badges">
              <span class="shared-settings__badge" :data-tone="decisionColors[file.decision] || 'secondary'">{{ decisionNames[file.decision] || '待审批' }}</span>
              <span class="text-body-2">{{ identity(file) }}</span>
            </div>
            <span class="text-body-2">{{ file.path }}</span>
            <span class="text-caption">{{ specifications(file.specs) }}</span>
            <span v-if="file.reason" class="text-caption">{{ file.reason }}<template v-if="file.owner"> · 占位方 {{ file.owner }}</template></span>
          </div>
        </details>
        <span class="shared-settings__id text-caption">申请编号 {{ row.request_id }}</span>
      </article>
      <VBtn prepend-icon="mdi-refresh" variant="text" :disabled="busy" @click="reconcile">核验未完成申请</VBtn>
    </div>
    <div v-else class="shared-settings__records">
      <div v-if="!state.records?.length" class="text-body-2 text-medium-emphasis">暂无共享占位</div>
      <article v-for="row in state.records || []" :key="row.id" class="shared-settings__record">
        <strong>{{ row.title }}</strong>
        <div class="shared-settings__badges">
          <span class="shared-settings__badge" :data-tone="phaseColors[row.state] || 'secondary'">{{ phaseNames[row.state] || row.state }}</span>
          <span class="text-body-2">下载方：{{ row.label || '-' }}</span>
          <span class="text-body-2">{{ identity(row) }}</span>
        </div>
        <span class="text-body-2">{{ specifications(row.specs) }}</span>
        <div class="shared-settings__meta shared-settings__claim-meta text-caption">
          <span>占位时间：{{ timestamp(row.created) }}</span><span>最近更新：{{ timestamp(row.updated) }}</span>
          <span class="shared-settings__id">占位编号：{{ row.id }}</span>
        </div>
      </article>
    </div>
  </section>
</template>

<style scoped>
.shared-settings { display: flex; flex-direction: column; gap: 16px; min-inline-size: 0; }
.shared-settings__actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.shared-settings__instance :deep(input) { font-family: monospace; font-size: 11px; }
.shared-settings__status { margin: 0; display: grid; gap: 10px; }
.shared-settings__status > div { display: flex; justify-content: space-between; gap: 12px; }
.shared-settings__status dd { margin: 0; overflow-wrap: anywhere; text-align: end; }
.shared-settings__records { min-inline-size: 0; }
.shared-settings__record { display: flex; flex-direction: column; gap: 10px; padding-block: 20px; border-block-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); overflow-wrap: anywhere; min-inline-size: 0; }
.shared-settings__record strong { font-size: 14px; line-height: 1.6; }
.shared-settings__badges, .shared-settings__meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; }
/* Native badges keep approval colors independent of host VChip/glass theme overrides. */
.shared-settings__badge { display: inline-flex; align-items: center; gap: 4px; max-inline-size: 100%; padding: 3px 8px; border: 1px solid rgba(var(--shared-badge-ink), 0.24); border-radius: 4px; font-size: 12px; font-weight: 600; line-height: 20px; color: rgb(var(--shared-badge-ink)); background-color: rgb(var(--shared-badge-fill)); --shared-badge-ink: 75, 85, 99; --shared-badge-fill: 243, 244, 246; }
.shared-settings__badge[data-tone="success"] { --shared-badge-ink: 21, 103, 52; --shared-badge-fill: 232, 245, 233; }
.shared-settings__badge[data-tone="error"] { --shared-badge-ink: 176, 32, 42; --shared-badge-fill: 255, 235, 238; }
.shared-settings__badge[data-tone="info"] { --shared-badge-ink: 0, 88, 153; --shared-badge-fill: 227, 242, 253; }
.shared-settings__badge[data-tone="warning"] { --shared-badge-ink: 133, 77, 14; --shared-badge-fill: 255, 248, 225; }
.shared-settings__preview { display: flex; flex-direction: column; gap: 4px; }
.shared-settings__meta, .shared-settings__id { color: rgba(var(--v-theme-on-surface), 0.72); }
.shared-settings__error { margin: 0; color: rgb(var(--v-theme-error)); }
.shared-settings__reason { margin: 0; }
.shared-settings__details summary { cursor: pointer; font-size: 13px; padding-block: 4px; }
.shared-settings__details summary:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: 2px; }
.shared-settings__file { display: flex; flex-direction: column; gap: 6px; padding: 12px 0 12px 12px; margin-block-start: 8px; border-inline-start: 2px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.shared-settings__id { font-family: monospace; }
.shared-settings__claim-meta { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1.5fr); }
.shared-settings__claim-meta .shared-settings__id { overflow-wrap: anywhere; }
@media (max-width: 599px) {
  .shared-settings__meta { flex-direction: column; align-items: flex-start; gap: 4px; }
  .shared-settings__status > div { flex-wrap: wrap; }
  .shared-settings__status dd { margin-inline-start: auto; }
}
</style>
