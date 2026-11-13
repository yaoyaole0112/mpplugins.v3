<script setup>
import { onMounted, ref } from 'vue'
import { readRules, unwrap } from './form.js'

const props = defineProps({
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'DirectoryRename' },
})
const emit = defineEmits(['close', 'layout'])
const rules = ref([])
const error = ref('')
const enabled = ref(false)
onMounted(async () => {
  emit('layout', { maxWidth: '60rem' })
  try {
    const config = unwrap(await props.api.get(`plugin/${props.pluginId}`))
    rules.value = readRules(config)
    enabled.value = Boolean(config.enabled)
  } catch (failure) {
    error.value = failure.message || '读取规则失败'
  }
})
</script>

<template>
  <div class="pa-5">
    <div class="d-flex align-center ga-3 mb-4">
      <span class="text-subtitle-1 flex-grow-1">目标目录重命名</span>
      <v-chip :color="enabled ? 'success' : undefined" size="small">{{ enabled ? '已启用' : '已停用' }}</v-chip>
      <v-btn icon="mdi-close" variant="text" size="small" aria-label="关闭" @click="emit('close')" />
    </div>
    <v-alert v-if="error" type="error" variant="tonal">{{ error }}</v-alert>
    <div v-for="rule in rules" :key="rule._key" class="saved-rule">
      <div class="font-weight-medium">{{ rule.name || '未命名规则' }}</div>
      <div class="text-body-2 text-medium-emphasis mt-1">{{ rule.storage }} · {{ rule.path }}</div>
      <div class="text-caption mt-2">{{ [rule.tv ? '电视剧' : '', rule.movie ? '电影' : ''].filter(Boolean).join('、') }}</div>
    </div>
  </div>
</template>

<style scoped>
.saved-rule { padding: 16px 0; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); overflow-wrap: anywhere; }
</style>
