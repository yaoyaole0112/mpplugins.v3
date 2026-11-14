<script setup>
import { onMounted } from 'vue'
import ResourceFlowWorkbench from './ResourceFlowWorkbench.vue'

defineProps({
  initialConfig: { type: Object, default: () => ({}) },
  api: { type: Object, default: () => ({}) },
})

const emit = defineEmits(['layout', 'close'])

onMounted(() => {
  emit('layout', { maxWidth: '80rem' })
})
</script>

<template>
  <ResourceFlowWorkbench
    :api="api"
    plugin-id="ResourceFlow"
    initial-tab="config"
    show-close
    compact
    @close="$emit('close')"
  />
</template>
