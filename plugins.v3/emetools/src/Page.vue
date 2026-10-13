<script setup>
import { onMounted, ref } from 'vue'
import Workbench from './Workbench.vue'
defineProps({ api: { type: Object, default: () => ({}) } })
const emit = defineEmits(['close'])
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
    node.style.setProperty('background', 'rgb(var(--v-theme-background))', 'important')
    node.style.setProperty('overflow', 'hidden', 'important')
    break
  }
})
</script>

<template>
  <div ref="pageRoot" class="eme-dialog-page-root">
    <Workbench :api="api" dialog-page @close="emit('close')" />
    <button type="button" class="eme-dialog-page-close" aria-label="关闭窗口" title="关闭窗口" @click="emit('close')">
      <span aria-hidden="true">×</span>
    </button>
  </div>
</template>

<style>
.eme-dialog-page-root {
  position: relative;
  box-sizing: border-box;
  display: flex;
  width: 100%;
  height: 100%;
  min-height: 100%;
  overflow: hidden;
  background: rgb(var(--v-theme-background));
}
.eme-dialog-page-close {
  position: absolute;
  top: 12px;
  right: 16px;
  z-index: 10;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  padding: 0;
  border: 0;
  border-radius: 9px;
  background: rgb(var(--v-theme-surface));
  color: rgb(var(--v-theme-on-surface));
  font-size: 28px;
  line-height: 1;
  cursor: pointer;
}
.eme-dialog-page-close:hover {
  background: rgba(var(--v-theme-primary), .15);
  color: rgb(var(--v-theme-primary));
}
.eme-dialog-page-close:focus-visible {
  outline: 2px solid rgb(var(--v-theme-primary));
  outline-offset: 2px;
}
</style>
