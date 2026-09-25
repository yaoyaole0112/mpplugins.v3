<script setup>
import { onMounted } from 'vue'
import Workbench from './Workbench.vue'
defineProps({ api: { type: Object, default: () => ({}) } })
const emit = defineEmits(['layout', 'close'])
onMounted(() => emit('layout', { maxWidth: '80rem' }))
</script>

<template><Workbench :api="api" @close="emit('close')" /></template>
