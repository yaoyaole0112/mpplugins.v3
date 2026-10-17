<script setup>
import Page from './Page.vue'
defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'Ed2kFolderWatcher' } })
const emit = defineEmits(['close', 'layout'])
</script>
<template><Page :api="api" :plugin-id="pluginId" @close="emit('close')" @layout="emit('layout', $event)" /></template>
