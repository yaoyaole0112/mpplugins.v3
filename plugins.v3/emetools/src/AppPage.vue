<script setup>
import { onMounted, onUnmounted } from 'vue'
import Workbench from './Workbench.vue'
defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'EmeTools' } })

const lockedAncestors = []
onMounted(() => {
  let node = document.querySelector('.eme-shell--app')?.parentElement
  for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
    node.classList.add('eme-app-host-lock')
    lockedAncestors.push(node)
  }
  document.documentElement.classList.add('eme-app-document-lock')
  document.body.classList.add('eme-app-document-lock')
})
onUnmounted(() => {
  lockedAncestors.forEach(node => node.classList.remove('eme-app-host-lock'))
  document.documentElement.classList.remove('eme-app-document-lock')
  document.body.classList.remove('eme-app-document-lock')
})
</script>

<template><Workbench :api="api" :plugin-id="pluginId" app-page /></template>
