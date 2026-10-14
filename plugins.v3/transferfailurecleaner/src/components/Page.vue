<script setup>
import { computed, onMounted, ref } from 'vue'

const props = defineProps({ api: { type: Object, default: () => ({}) } })
const emit = defineEmits(['close'])
const busy = ref(false)
const error = ref('')
const status = ref({ config: { rules: [] }, last_result: {} })
const modes = {
  record: '仅删除转移记录',
  source: '删除转移记录和源文件',
  destination: '删除转移记录和媒体库文件',
  all: '删除转移记录、源文件和媒体库文件',
}
const details = computed(() => status.value.last_result?.data?.rule_details || [])
const unwrap = result => result?.data && typeof result.success === 'boolean' ? result.data : result

async function refresh() {
  busy.value = true
  error.value = ''
  try {
    status.value = unwrap(await props.api.get('plugin/TransferFailureCleaner/status'))
  } catch (failure) {
    error.value = failure?.message || '读取执行记录失败，请重试'
  } finally {
    busy.value = false
  }
}

async function run(preview) {
  if (!preview && !window.confirm('将按已保存规则执行清理，涉及文件删除时不可恢复。确认继续？')) return
  busy.value = true
  error.value = ''
  try {
    const result = await props.api.post(`plugin/TransferFailureCleaner/${preview ? 'preview' : 'run'}`, {})
    if (result?.success === false) error.value = result.message || '执行失败，请查看日志'
    status.value = unwrap(await props.api.get('plugin/TransferFailureCleaner/status'))
  } catch (failure) {
    error.value = failure?.message || '执行失败，请查看日志'
  } finally {
    busy.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <v-card>
    <v-card-title class="d-flex align-center">
      <span class="flex-grow-1">失败整理记录清理</span>
      <v-btn icon="mdi-close" variant="text" aria-label="关闭" @click="emit('close')" />
    </v-card-title>
    <v-card-text>
      <v-alert type="warning" variant="tonal" class="mb-4">
        以下操作使用已保存规则。涉及文件删除时不可恢复，请先预览。
      </v-alert>
      <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>
      <v-card v-for="(rule, index) in status.config?.rules || []" :key="index" variant="outlined" class="mb-3">
        <v-card-text>
          <div class="font-weight-medium mb-2">规则 {{ index + 1 }} · {{ modes[rule.delete_mode] }}</div>
          <div>{{ rule.error_types?.join('；') || '未选择错误类型，不执行' }}</div>
        </v-card-text>
      </v-card>
      <v-alert v-if="!status.config?.rules?.length" type="info" variant="tonal" class="mb-4">
        尚无清理规则，请先在插件设置中添加规则并保存。
      </v-alert>
      <div class="d-flex flex-wrap ga-2 my-4">
        <v-btn color="primary" variant="tonal" :disabled="busy" @click="run(true)">预览匹配记录（不删除）</v-btn>
        <v-btn color="warning" :disabled="busy" @click="run(false)">立即清理</v-btn>
        <v-btn variant="text" :disabled="busy" @click="refresh">刷新</v-btn>
      </div>
      <v-progress-linear v-if="busy" indeterminate class="mb-3" />
      <v-alert type="info" variant="tonal" class="mb-4">
        {{ status.last_result?.time || '' }} {{ status.last_result?.message || '尚无执行记录' }}
      </v-alert>
      <v-card v-for="detail in details" :key="detail.number" variant="outlined" class="mb-3">
        <v-card-text>
          <div class="font-weight-medium mb-2">规则 {{ detail.number }} · {{ detail.label }}</div>
          <div>匹配 {{ detail.matched }} 条，删除 {{ detail.deleted }} 条，跳过 {{ detail.skipped }} 条，保留 {{ detail.retained }} 条，失败 {{ detail.failed }} 条</div>
          <div v-for="(count, type) in detail.types" :key="type" class="mt-2">{{ type }}：{{ count }} 条</div>
        </v-card-text>
      </v-card>
    </v-card-text>
  </v-card>
</template>
