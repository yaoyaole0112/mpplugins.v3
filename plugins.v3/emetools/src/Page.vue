<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import Workbench from './Workbench.vue'
defineProps({ api: { type: Object, default: () => ({}) } })
const emit = defineEmits(['close'])
const pageRoot = ref(null)
let dialogHost = null
let originalHostStyle = null
let originalPositionStyles = null
let mobileDialogQuery = null
const dialogWrappers = []
function syncDialogViewport() {
  if (!dialogHost) return
  const viewport = window.visualViewport
  if (mobileDialogQuery?.matches) {
    const height = Math.max(0, (viewport?.height || window.innerHeight) - 16)
    dialogHost.style.setProperty('position', 'fixed', 'important')
    dialogHost.style.setProperty('top', `${(viewport?.offsetTop || 0) + 8}px`, 'important')
    dialogHost.style.setProperty('left', '8px', 'important')
    dialogHost.style.setProperty('width', 'calc(100vw - 16px)', 'important')
    dialogHost.style.setProperty('max-width', 'calc(100vw - 16px)', 'important')
    dialogHost.style.setProperty('height', `${height}px`, 'important')
    dialogHost.style.setProperty('max-height', `${height}px`, 'important')
    dialogHost.style.setProperty('margin', '0', 'important')
    dialogHost.style.setProperty('overflow', 'hidden', 'important')
    return
  }
  for (const [property, value, priority] of originalPositionStyles || []) {
    if (value) dialogHost.style.setProperty(property, value, priority)
    else dialogHost.style.removeProperty(property)
  }
  dialogHost.style.setProperty('width', '94vw', 'important')
  dialogHost.style.setProperty('max-width', '1900px', 'important')
  dialogHost.style.setProperty('height', '92dvh', 'important')
  dialogHost.style.setProperty('max-height', '92dvh', 'important')
  dialogHost.style.setProperty('margin', '0 auto', 'important')
  dialogHost.style.setProperty('overflow', 'hidden', 'important')
}
onMounted(() => {
  mobileDialogQuery = window.matchMedia('(max-width: 760px)')
  let node = pageRoot.value?.parentElement
  const ancestors = []
  for (let depth = 0; node && depth < 16; depth += 1, node = node.parentElement) {
    ancestors.push(node)
    if (!node.matches?.('.v-overlay__content')) continue
    dialogHost = node
    break
  }
  dialogHost ||= ancestors.find(element => element.matches?.('.v-dialog, [role="dialog"]'))
  if (dialogHost) {
    node = dialogHost
    originalHostStyle = node.getAttribute('style')
    originalPositionStyles = ['position', 'top', 'left'].map(property => [property, node.style.getPropertyValue(property), node.style.getPropertyPriority(property)])
    node.style.setProperty('width', '94vw', 'important')
    node.style.setProperty('max-width', '1900px', 'important')
    node.style.setProperty('height', '92dvh', 'important')
    node.style.setProperty('max-height', '92dvh', 'important')
    node.style.setProperty('margin', '0 auto', 'important')
    node.style.setProperty('background', 'var(--eme-dialog-background, rgb(var(--v-theme-background)))', 'important')
    node.style.setProperty('overflow', 'hidden', 'important')
    node.classList.add('eme-mobile-dialog-host')
    for (const wrapper of ancestors) {
      if (wrapper === node) break
      wrapper.classList.add('eme-mobile-dialog-wrapper')
      dialogWrappers.push(wrapper)
    }
    syncDialogViewport()
    window.addEventListener('resize', syncDialogViewport)
    mobileDialogQuery.addEventListener('change', syncDialogViewport)
    window.visualViewport?.addEventListener('resize', syncDialogViewport)
    window.visualViewport?.addEventListener('scroll', syncDialogViewport)
  }
})
onUnmounted(() => {
  window.removeEventListener('resize', syncDialogViewport)
  mobileDialogQuery?.removeEventListener('change', syncDialogViewport)
  window.visualViewport?.removeEventListener('resize', syncDialogViewport)
  window.visualViewport?.removeEventListener('scroll', syncDialogViewport)
  dialogWrappers.forEach(wrapper => wrapper.classList.remove('eme-mobile-dialog-wrapper'))
  if (!dialogHost) return
  dialogHost.classList.remove('eme-mobile-dialog-host')
  if (originalHostStyle === null) dialogHost.removeAttribute('style')
  else dialogHost.setAttribute('style', originalHostStyle)
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
@media(max-width:760px){
  .eme-mobile-dialog-host{display:flex!important;flex-direction:column!important}
  .eme-mobile-dialog-wrapper{box-sizing:border-box!important;display:flex!important;flex-direction:column!important;flex:1 1 0!important;width:100%!important;min-width:0!important;height:100%!important;min-height:0!important;max-height:100%!important;margin:0!important;padding:0!important;overflow:hidden!important}
  .eme-dialog-page-root{display:block;min-height:0;height:100%;padding:0;overflow-x:hidden;overflow-y:auto;overscroll-behavior-y:auto;scrollbar-gutter:auto;-webkit-overflow-scrolling:touch}
  .eme-mobile-dialog-host .eme-dialog-page-root{flex:1 1 0;min-height:0;max-height:100%}
}
</style>
