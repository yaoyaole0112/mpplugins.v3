import { onMounted } from 'vue'

export function useMobilePage(root) {
  onMounted(() => {
    if (window.matchMedia('(max-width: 959px)').matches) {
      root.value?.scrollIntoView({ block: 'start', behavior: 'instant' })
    }
  })
}
