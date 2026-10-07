<script setup>
import { onMounted, ref } from 'vue'
import Workbench from './Workbench.vue'
defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'EmeTools' } })
const hostRoot = ref(null)
onMounted(() => {
  let node = hostRoot.value?.parentElement
  for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
    if (!node.classList?.contains('v-overlay__content') && !node.classList?.contains('v-dialog')) continue
    node.style.setProperty('width', '92vw', 'important')
    node.style.setProperty('max-width', '1900px', 'important')
    node.style.setProperty('height', '90dvh', 'important')
    node.style.setProperty('max-height', '90dvh', 'important')
    node.style.setProperty('margin', '0', 'important')
    break
  }
})
</script>

<template><div ref="hostRoot" class="eme-app-page-host"><Workbench :api="api" :plugin-id="pluginId" app-page /></div></template>
