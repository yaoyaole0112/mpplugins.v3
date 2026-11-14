<script setup>
import { onMounted, ref } from 'vue'
import { formatDateTime, unwrapResponse } from '../utils'

const props = defineProps({ api: { type: Object, required: true }, pluginBase: { type: String, required: true } })
const emit = defineEmits(['policy'])
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
  queued: '已入队', completed: '已完成', released: '已释放', rejected: '已排除', uncertain: '待核验',
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

function save() {
  return perform(async () => {
    const users = draft.value.allowed_users.split(/[\s,，]+/).filter(Boolean)
    if (users.some(value => !/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) <= 0)) {
      throw new Error('白名单须为个人账号数字 ID')
    }
    const payload = { ...draft.value, allowed_users: users.map(Number) }
    if (!payload.bot_token) delete payload.bot_token
    unwrapResponse(await props.api.post(`${props.pluginBase}/shared/settings`, payload))
    await load(true)
  })
}

function test() {
  return perform(async () => {
    state.value = unwrapResponse(await props.api.post(`${props.pluginBase}/shared/test`, {}))
  })
}

function reconcile() {
  return perform(async () => {
    unwrapResponse(await props.api.post(`${props.pluginBase}/shared/reconcile`, {}))
    await load()
  })
}

onMounted(() => perform(() => load(true)))
</script>

<template>
  <section class="shared-settings">
    <div class="text-subtitle-2">Telegram 共享审批</div>
    <VAlert v-if="error" type="error" variant="tonal" density="compact">{{ error }}</VAlert>
    <VAlert v-if="state.last_error" type="warning" variant="tonal" density="compact">{{ state.last_error }}</VAlert>
    <VSwitch v-model="draft.enabled" label="参与跨实例共享审批" color="primary" hide-details inset />
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
      <VBtn prepend-icon="mdi-content-save-outline" :loading="busy" variant="tonal" @click="save">保存共享连接</VBtn>
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
      <div><dt>最近审批确认</dt><dd>{{ state.last_ok ? formatDateTime(new Date(state.last_ok * 1000).toISOString()) : '-' }}</dd></div>
      <div v-if="state.probe?.user_id"><dt>本机 Telegram ID</dt><dd>{{ state.probe.user_id }}</dd></div>
      <div v-if="state.probe?.policy"><dt>协调端规则版本</dt><dd>{{ state.probe.policy.revision.slice(0, 12) }}</dd></div>
    </dl>
    <VBtn v-if="state.probe?.policy" prepend-icon="mdi-sync" variant="text"
      @click="emit('policy', state.probe.policy)">采用协调端匹配字段</VBtn>
    <VTabs v-model="tab" density="compact">
      <VTab value="requests">本机共享申请</VTab>
      <VTab v-if="state.host_coordinator" value="records">共享占位记录</VTab>
    </VTabs>
    <div v-if="tab === 'requests'" class="shared-settings__records">
      <div v-if="!state.requests?.length" class="text-body-2 text-medium-emphasis">暂无共享申请</div>
      <article v-for="row in state.requests || []" :key="row.request_id" class="shared-settings__record">
        <strong>{{ row.title }}</strong>
        <span>{{ phaseNames[row.phase] || row.phase }} · 批准 {{ row.approved }} · 排除 {{ row.rejected?.length || 0 }}</span>
        <small v-if="row.error">{{ row.error }}</small>
        <small v-for="(item, index) in row.rejected || []" :key="index">{{ item.reason }} · {{ item.owner }}</small>
      </article>
      <VBtn prepend-icon="mdi-refresh" variant="text" :disabled="busy" @click="reconcile">核验未完成申请</VBtn>
    </div>
    <div v-else class="shared-settings__records">
      <div v-if="!state.records?.length" class="text-body-2 text-medium-emphasis">暂无共享占位</div>
      <article v-for="row in state.records || []" :key="row.id" class="shared-settings__record">
        <strong>{{ row.title }}</strong>
        <span>TMDB {{ row.tmdb_id }} · {{ row.media_type === 'movie' ? '电影' : `S${row.season} E${row.episodes.join(',')}` }}</span>
        <small>{{ row.label }} · {{ phaseNames[row.state] || row.state }} · {{ Object.values(row.specs).join(' · ') }}</small>
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
.shared-settings__records { max-block-size: 24rem; overflow-y: auto; min-inline-size: 0; }
.shared-settings__record { display: flex; flex-direction: column; gap: 4px; padding-block: 12px; border-block-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); overflow-wrap: anywhere; }
</style>
