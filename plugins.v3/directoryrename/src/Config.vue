<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import {
  directoryChoices, makeRule, readRules, serializeConfig,
  storageChoices, unwrap, validateRules,
} from './form.js'

const props = defineProps({
  initialConfig: { type: Object, default: () => ({}) },
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'DirectoryRename' },
})
const emit = defineEmits(['save', 'layout', 'close'])
const enabled = ref(Boolean(props.initialConfig.enabled))
const rules = ref([])
const expanded = ref([])
const storages = ref([])
const directories = ref([])
const busy = ref(false)
const loadingOptions = ref(false)
const error = ref('')
const loadError = ref('')
const optionWarning = ref('')
const pendingDelete = ref(null)
const choices = computed(() => storageChoices(storages.value, rules.value))
let originalConfig = { ...props.initialConfig }

function load(config) {
  try {
    const parsed = readRules(config)
    originalConfig = { ...config }
    rules.value = parsed
    enabled.value = Boolean(config.enabled)
    expanded.value = parsed.length ? [parsed[0]._key] : []
    loadError.value = ''
  } catch (failure) {
    loadError.value = `读取已保存规则失败：${failure.message}`
  }
}

function addRule(source) {
  const rule = source
    ? makeRule({ ...source, name: source.name ? `${source.name}（副本）` : '' })
    : makeRule({ storage: rules.value.at(-1)?.storage || choices.value[0]?.value || '' })
  rules.value.push(rule)
  expanded.value = [rule._key]
  error.value = ''
  nextTick(() => {
    const element = document.getElementById(`directory-rule-${rule._key}`)
    element?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  })
}

function deleteRule() {
  const key = pendingDelete.value?._key
  rules.value = rules.value.filter(rule => rule._key !== key)
  expanded.value = expanded.value.filter(value => value !== key)
  pendingDelete.value = null
  error.value = ''
}

async function refreshOptions() {
  loadingOptions.value = true
  optionWarning.value = ''
  const results = await Promise.allSettled([
    props.api.get('storage/options'),
    props.api.get('storage/directories', { params: { directory_type: 'library' } }),
  ])
  try {
    if (results[0].status === 'fulfilled') {
      const value = unwrap(results[0].value)
      storages.value = Array.isArray(value) ? value : []
    }
    if (results[1].status === 'fulfilled') {
      const value = unwrap(results[1].value)
      directories.value = Array.isArray(value) ? value : []
    }
    if (results.some(result => result.status === 'rejected')) {
      optionWarning.value = '未能读取全部存储或目录选项，仍可手动填写。'
    }
  } catch (failure) {
    optionWarning.value = failure.message || '读取选项失败，仍可手动填写。'
  } finally {
    loadingOptions.value = false
  }
}

async function retryLoad() {
  busy.value = true
  try {
    load(unwrap(await props.api.get(`plugin/${props.pluginId}`)))
  } catch (failure) {
    loadError.value = failure.message || '重新读取配置失败'
  } finally {
    busy.value = false
  }
}

async function save() {
  if (busy.value || loadError.value) return
  error.value = validateRules(rules.value, enabled.value)
  if (error.value) return
  busy.value = true
  const config = serializeConfig(originalConfig, enabled.value, rules.value)
  try {
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/validate`, config))
    if (result?.valid !== true) throw new Error('规则校验未完成，请稍后重试')
    emit('save', config)
  } catch (failure) {
    error.value = failure?.response?.data?.message || failure.message || '校验失败，尚未保存'
  } finally {
    busy.value = false
  }
}

load(props.initialConfig)
onMounted(() => {
  emit('layout', { maxWidth: '68rem' })
  refreshOptions()
})
</script>

<template>
  <div class="directory-rename-config">
    <div class="rules-toolbar">
      <v-switch v-model="enabled" label="启用目标目录重命名" color="primary" hide-details
        :disabled="busy || Boolean(loadError)" />
      <span class="text-body-2 text-medium-emphasis rule-count">{{ rules.length }} 条规则</span>
      <v-tooltip text="刷新存储和目录选项" location="bottom">
        <template #activator="{ props: activator }">
          <v-btn v-bind="activator" icon="mdi-refresh" variant="text" size="small"
            :loading="loadingOptions" aria-label="刷新存储和目录选项" @click="refreshOptions" />
        </template>
      </v-tooltip>
    </div>

    <v-alert v-if="loadError" type="error" variant="tonal" class="mb-4" role="alert">
      {{ loadError }}
      <template #append>
        <v-btn variant="text" prepend-icon="mdi-refresh" :loading="busy" @click="retryLoad">重新读取</v-btn>
      </template>
    </v-alert>
    <v-alert v-if="optionWarning" type="warning" variant="tonal" density="compact"
      class="mb-4">{{ optionWarning }}</v-alert>

    <v-expansion-panels v-model="expanded" multiple variant="accordion" class="rule-panels">
      <v-expansion-panel v-for="(rule, index) in rules" :key="rule._key" :value="rule._key"
        :id="`directory-rule-${rule._key}`" :disabled="busy" elevation="0">
        <v-expansion-panel-title class="rule-title" :aria-label="`展开规则：${rule.name || index + 1}`">
          <v-icon icon="mdi-folder-outline" size="21" class="mr-3 flex-shrink-0 text-medium-emphasis" />
          <div class="rule-heading">
            <div class="rule-heading-top">
              <span class="font-weight-medium">{{ rule.name || `规则 ${index + 1}` }}</span>
              <span v-if="rule.storage" class="text-caption text-medium-emphasis">{{ rule.storage }}</span>
            </div>
            <div class="rule-path text-body-2 text-medium-emphasis">{{ rule.path || '未填写目标目录' }}</div>
          </div>
        </v-expansion-panel-title>
        <v-expansion-panel-text>
          <div class="rule-actions">
            <v-tooltip text="复制规则" location="bottom">
              <template #activator="{ props: activator }">
                <v-btn v-bind="activator" icon="mdi-content-copy" variant="text" size="small"
                  aria-label="复制规则" :disabled="busy" @click="addRule(rule)" />
              </template>
            </v-tooltip>
            <v-tooltip text="删除规则" location="bottom">
              <template #activator="{ props: activator }">
                <v-btn v-bind="activator" icon="mdi-delete-outline" variant="text" size="small"
                  color="error" aria-label="删除规则" :disabled="busy" @click="pendingDelete = rule" />
              </template>
            </v-tooltip>
          </div>
          <div class="rule-fields">
            <v-text-field v-model="rule.name" label="规则名称" placeholder="例如：分享池"
              variant="outlined" density="comfortable" hide-details class="name-field" :disabled="busy" />
            <v-combobox v-model="rule.storage" :items="choices" item-title="title" item-value="value"
              :return-object="false" label="目标存储" variant="outlined" density="comfortable"
              hide-details class="storage-field" :loading="loadingOptions" :disabled="busy" />
            <v-combobox v-model="rule.path" :items="directoryChoices(directories, rule.storage)"
              item-title="title" item-value="value" :return-object="false" label="目标目录"
              placeholder="/影视资源/已整理/电视剧/" variant="outlined" density="comfortable"
              hide-details class="path-field" :loading="loadingOptions" :disabled="busy" />
          </div>
          <v-tabs v-model="rule._tab" color="primary" density="comfortable" class="template-tabs"
            :aria-label="`${rule.name || '规则'}的媒体类型`">
            <v-tab value="tv" prepend-icon="mdi-television" :disabled="busy">电视剧模板</v-tab>
            <v-tab value="movie" prepend-icon="mdi-movie-open-outline" :disabled="busy">电影模板</v-tab>
          </v-tabs>
          <v-textarea v-model="rule[rule._tab]" :key="rule._tab"
            :label="rule._tab === 'tv' ? '电视剧重命名模板' : '电影重命名模板'"
            :aria-label="rule._tab === 'tv' ? '电视剧重命名模板' : '电影重命名模板'"
            variant="outlined" rows="6" auto-grow hide-details spellcheck="false"
            class="template-input" :disabled="busy" />
        </v-expansion-panel-text>
      </v-expansion-panel>
    </v-expansion-panels>

    <div v-if="!rules.length && !loadError" class="empty-rules text-medium-emphasis">
      <v-icon icon="mdi-folder-plus-outline" size="30" class="mb-2" />
      <div>暂无目标目录规则</div>
    </div>
    <v-alert v-if="error" type="error" variant="tonal" class="mt-4" role="alert">{{ error }}</v-alert>
    <div class="rule-footer">
      <v-btn prepend-icon="mdi-plus" variant="tonal" color="primary"
        :disabled="busy || Boolean(loadError)" @click="addRule()">添加规则</v-btn>
      <div class="save-actions">
        <v-btn variant="text" :disabled="busy" @click="emit('close')">取消</v-btn>
        <v-btn prepend-icon="mdi-content-save-outline" color="primary" variant="flat"
          :loading="busy" :disabled="Boolean(loadError)" @click="save">保存</v-btn>
      </div>
    </div>

    <v-dialog :model-value="Boolean(pendingDelete)" max-width="420"
      @update:model-value="value => { if (!value) pendingDelete = null }">
      <v-card>
        <v-card-title class="text-subtitle-1">删除规则</v-card-title>
        <v-card-text class="delete-message">删除“{{ pendingDelete?.name || '未命名规则' }}”？</v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="pendingDelete = null">取消</v-btn>
          <v-btn color="error" variant="tonal" prepend-icon="mdi-delete-outline" @click="deleteRule">删除</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<style scoped>
.directory-rename-config { padding: 20px 24px 0; min-width: 0; letter-spacing: 0; }
.rules-toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
.rules-toolbar :deep(.v-switch) { flex: 1 1 auto; min-width: 0; }
.rule-count { flex-shrink: 0; white-space: nowrap; }
.rule-panels { border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 6px; overflow: hidden; }
.rule-panels :deep(.v-expansion-panel) { border-radius: 0 !important; }
.rule-title { padding: 18px 20px; }
.rule-heading { flex: 1 1 auto; min-width: 0; padding-right: 12px; }
.rule-heading-top { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 14px; overflow-wrap: anywhere; }
.rule-path { margin-top: 4px; overflow-wrap: anywhere; }
.rule-actions { display: flex; justify-content: flex-end; gap: 4px; margin-bottom: 8px; }
.rule-fields { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 18px 16px; }
.rule-fields > * { min-width: 0; }
.path-field { grid-column: 1 / -1; }
.template-tabs { margin-top: 20px; margin-bottom: 18px; }
.template-input :deep(textarea) { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 13px; line-height: 1.7; overflow-wrap: anywhere; letter-spacing: 0; }
.path-field :deep(input) { min-width: 0; }
.empty-rules { text-align: center; padding: 32px 16px; }
.rule-footer { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 18px 0 20px; background: transparent; }
.save-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.delete-message { overflow-wrap: anywhere; }
@media (max-width: 600px) {
  .directory-rename-config { padding: 16px 12px 0; }
  .rules-toolbar { gap: 4px; }
  .rule-count { font-size: 12px !important; }
  .rule-title { padding: 16px 12px; }
  .rule-fields { grid-template-columns: minmax(0, 1fr); gap: 16px; }
  .rule-panels :deep(.v-expansion-panel-text__wrapper) { padding: 4px 12px 16px; }
  .template-tabs :deep(.v-tab) { min-width: 0; padding-inline: 10px; }
  .rule-footer { padding-block: 16px; }
  .rule-footer :deep(.v-btn) { padding-inline: 12px; }
  .save-actions { gap: 4px; }
}
</style>
