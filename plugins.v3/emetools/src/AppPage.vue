<script setup>
import Workbench from './Workbench.vue'
defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'EmeTools' } })
</script>

<template><Workbench :api="api" :plugin-id="pluginId" /></template>
