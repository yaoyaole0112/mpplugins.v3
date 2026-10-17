<script setup>
import { onMounted } from 'vue'
import Panel from './Panel.vue'
defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'Ed2kFolderWatcher' } })
const emit = defineEmits(['close', 'layout'])
onMounted(() => emit('layout', { maxWidth: '78rem' }))
</script>
<template><Panel :api="api" :plugin-id="pluginId" @close="emit('close')" /></template>
