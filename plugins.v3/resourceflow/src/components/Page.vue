<script setup>
import ResourceFlowWorkbench from './ResourceFlowWorkbench.vue'

defineProps({
  api: { type: Object, default: () => ({}) },
})

defineEmits(['action', 'close'])
</script>

<template>
  <ResourceFlowWorkbench
    :api="api"
    plugin-id="ResourceFlow"
    show-close
    compact
    @action="$emit('action')"
    @close="$emit('close')"
  />
</template>
