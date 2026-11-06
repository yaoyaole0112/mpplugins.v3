import { onMounted, onUnmounted } from 'vue'

let iosPageCount = 0

export function isIosAppWebView(userAgent = globalThis.navigator?.userAgent || '') {
  return /(?:iPhone|iPad|iPod)/i.test(userAgent)
    && /AppleWebKit/i.test(userAgent)
    && !/(?:Safari|CriOS|FxiOS|EdgiOS|OPiOS)/i.test(userAgent)
}

export function useMobilePage(root) {
  onMounted(() => {
    const iosApp = isIosAppWebView()
    if (iosApp) {
      iosPageCount += 1
      document.documentElement.classList.add('ed2k-ios-app')
      document.body.classList.add('ed2k-ios-app')
    }
    if (window.matchMedia('(max-width: 959px)').matches) {
      root.value?.scrollIntoView({ block: 'start', behavior: 'instant' })
    }
  })
  onUnmounted(() => {
    if (!isIosAppWebView()) return
    iosPageCount = Math.max(0, iosPageCount - 1)
    if (!iosPageCount) {
      document.documentElement.classList.remove('ed2k-ios-app')
      document.body.classList.remove('ed2k-ios-app')
    }
  })
}
