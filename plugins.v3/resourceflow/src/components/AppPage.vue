<script setup>
import ResourceFlowWorkbench from './ResourceFlowWorkbench.vue'

defineProps({
  api: { type: Object, default: () => ({}) },
  navKey: { type: String, default: 'main' },
  pluginId: { type: String, default: 'ResourceFlow' },
})

defineEmits(['action'])
</script>

<template>
  <ResourceFlowWorkbench :api="api" :plugin-id="pluginId" @action="$emit('action')" />
</template>
