<script setup>
import Workbench from './Workbench.vue'
defineProps({ api: { type: Object, default: () => ({}) } })
</script>

<template><Workbench :api="api" /></template>
