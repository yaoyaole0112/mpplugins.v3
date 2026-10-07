<script setup>
import { onMounted, ref } from 'vue'
import Workbench from './Workbench.vue'
defineProps({ api: { type: Object, default: () => ({}) } })
const pageRoot = ref(null)
onMounted(() => {
  let node = pageRoot.value?.parentElement
  for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
    if (!node.matches?.('.v-overlay__content, .v-dialog, [role="dialog"]')) continue
    node.style.setProperty('width', '94vw', 'important')
    node.style.setProperty('max-width', '1900px', 'important')
    node.style.setProperty('height', '92dvh', 'important')
    node.style.setProperty('max-height', '92dvh', 'important')
    node.style.setProperty('margin', '0 auto', 'important')
    break
  }
})
</script>

<template><div ref="pageRoot" class="eme-dialog-page-root"><Workbench :api="api" dialog-page /></div></template>
