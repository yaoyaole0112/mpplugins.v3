<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  initialConfig: { type: Object, default: () => ({}) },
  api: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['save', 'layout', 'switch', 'close'])

const modes = [
  { value: 'record', title: '仅删除转移记录' },
  { value: 'source', title: '删除转移记录和源文件' },
  { value: 'destination', title: '删除转移记录和媒体库文件' },
  { value: 'all', title: '删除转移记录、源文件和媒体库文件' },
]
const config = props.initialConfig || {}
const options = ref(Array.isArray(config.error_options) ? config.error_options : [])
const warning = ref(config.error_warning || '')
const enabled = ref(Boolean(config.enabled))
const cron = ref(config.cron || '0 3 * * *')
const retainHours = ref(Number(config.retain_hours || 0))
const rules = ref(normalizeRules(config.rules))
const error = ref('')

function normalizeRules(value) {
  if (!Array.isArray(value)) return []
  return value.filter(rule => rule && modes.some(mode => mode.value === rule.delete_mode))
    .map(rule => ({ delete_mode: rule.delete_mode, error_types: Array.isArray(rule.error_types) ? [...rule.error_types] : [] }))
}
function addRule() {
  rules.value.push({ delete_mode: 'record', error_types: [] })
}
function removeRule(index) {
  rules.value.splice(index, 1)
}
function selectedElsewhere(index, type) {
  return rules.value.some((rule, ruleIndex) => ruleIndex !== index && rule.error_types.includes(type))
}
function validate() {
  const owners = new Map()
  for (let index = 0; index < rules.value.length; index += 1) {
    for (const type of rules.value[index].error_types) {
      if (owners.has(type)) return `错误类型“${type}”已分配到规则 ${owners.get(type) + 1}，不能重复分配`
      owners.set(type, index)
    }
  }
  return ''
}
function save() {
  error.value = validate()
  if (error.value) return
  emit('save', {
    enabled: enabled.value,
    cron: cron.value,
    retain_hours: Math.max(0, Number(retainHours.value) || 0),
    rules: rules.value.map(rule => ({ delete_mode: rule.delete_mode, error_types: [...rule.error_types] })),
  })
}
async function refreshOptions() {
  try {
    const result = await props.api.get('plugin/TransferFailureCleaner/options')
    if (result?.error_options) options.value = result.error_options
    warning.value = result?.error_warning || ''
  } catch (refreshError) {
    warning.value = '读取失败提示失败，请稍后重试。'
  }
}

emit('layout', { maxWidth: '60rem' })
if (!rules.value.length) addRule()
</script>

<template>
  <div class="pa-4">
    <v-alert type="warning" variant="tonal" class="mb-5" style="line-height: 1.7">
      文件删除不可恢复，请先预览。删除源文件会联动删除对应下载任务。
    </v-alert>

    <v-card v-for="(rule, index) in rules" :key="index" variant="outlined" class="mb-4">
      <v-card-text class="pa-4">
        <div class="d-flex align-center mb-4">
          <div class="text-subtitle-1 font-weight-medium flex-grow-1">规则 {{ index + 1 }}</div>
          <v-btn v-if="rules.length > 1" icon="mdi-minus" size="small" variant="tonal" color="error"
            aria-label="删除规则" @click="removeRule(index)" />
        </div>
        <v-select v-model="rule.delete_mode" :items="modes" item-title="title" item-value="value"
          label="删除方式" density="comfortable" class="mb-5" hide-details />
        <v-select v-model="rule.error_types" :items="options" item-title="title" item-value="value"
          label="此规则清理的错误类型" multiple chips closable-chips density="comfortable"
          hint="可选多个类型；每个错误类型只能分配到一条规则。" persistent-hint>
          <template #item="{ props: itemProps, item }">
            <v-list-item v-bind="itemProps" :disabled="selectedElsewhere(index, item.value)" />
          </template>
        </v-select>
      </v-card-text>
    </v-card>

    <v-btn prepend-icon="mdi-plus" variant="tonal" color="primary" class="mb-5" @click="addRule">
      添加规则
    </v-btn>

    <v-card variant="outlined" class="mb-5">
      <v-card-text class="pa-4">
        <div class="text-subtitle-1 font-weight-medium mb-3">公共设置（适用于所有规则）</div>
        <v-switch v-model="enabled" label="启用定时清理" class="mb-3" hide-details />
        <v-text-field v-model="cron" label="执行周期（Cron）" density="comfortable" class="mb-5"
          hint="默认每天 03:00，使用 MP 时区。" persistent-hint />
        <v-text-field v-model.number="retainHours" label="保留最近多少小时" density="comfortable" type="number" min="0"
          hint="0 不限制；设置保留时间后，时间无效的记录会跳过。" persistent-hint />
      </v-card-text>
    </v-card>

    <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>
    <v-alert v-else-if="warning" type="info" variant="tonal" class="mb-4" style="line-height: 1.7">{{ warning }}</v-alert>
    <v-alert type="info" variant="tonal" style="line-height: 1.7">
      保存后到“查看数据”按规则预览或清理。留空规则不执行。
    </v-alert>

    <div class="d-flex justify-end mt-5">
      <v-btn prepend-icon="mdi-refresh" variant="text" class="mr-2" @click="refreshOptions">刷新错误类型</v-btn>
      <v-btn prepend-icon="mdi-content-save" color="primary" variant="flat" @click="save">保存</v-btn>
    </div>
  </div>
</template>
