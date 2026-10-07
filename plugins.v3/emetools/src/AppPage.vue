<script setup>
import { onMounted, ref } from 'vue'
import Workbench from './Workbench.vue'
defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'EmeTools' } })
const hostRoot = ref(null)
const resizeHostDialog = () => {
  let node = hostRoot.value?.parentElement
  for (let depth = 0; node && depth < 12; depth += 1, node = node.parentElement) {
    if (!node.matches?.('.v-overlay__content, .v-dialog, [role="dialog"]')) continue
    node.style.setProperty('width', '94vw', 'important')
    node.style.setProperty('max-width', '1900px', 'important')
    node.style.setProperty('height', '92dvh', 'important')
    node.style.setProperty('max-height', '92dvh', 'important')
    node.style.setProperty('margin', '0', 'important')
  }
}
onMounted(() => {
  resizeHostDialog()
  requestAnimationFrame(resizeHostDialog)
})
</script>

<template><div ref="hostRoot" class="eme-app-page-host"><Workbench :api="api" :plugin-id="pluginId" app-page /></div></template>
