<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import Panel from './Panel.vue'
import './mobile-layout.css'
import { useMobilePage } from './useMobilePage'

const props = defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'Ed2kFolderWatcher' } })
const emit = defineEmits(['close', 'layout'])
const pageRoot = ref(null)
useMobilePage(pageRoot)
const items = ref([])
const progress = ref([])
const total = ref(0)
const deleteEnabled = ref(false)
const cloudDeleteEnabled = ref(false)
const workerAlive = ref(false)
const busy = ref(false)
const error = ref('')
const loaded = ref(false)
const configOpen = ref(false)
const deleting = ref('')
const notice = ref('')
const copiedId = ref('')
const copyFailedId = ref('')
let timer
let disposed = false
const statuses = { deleted: '已删除源文件', retained: '未删除源文件', delete_failed: '删除失败，源文件保留', delete_uncertain: '删除结果未确认', unknown: '历史记录：删源状态未记录' }

function formatTime(value) {
  return value ? new Date(value * 1000).toLocaleString() : '历史记录：生成时间未记录'
}
function formatBytes(value) {
  const bytes = Number(value) || 0
  if (bytes < 1024 ** 2) return `${bytes} B`
  const unit = bytes < 1024 ** 3 ? 'MB' : 'GB'
  return `${(bytes / (unit === 'MB' ? 1024 ** 2 : 1024 ** 3)).toFixed(1)} ${unit}`
}
function progressPercent(item) {
  return item.size ? Math.min(100, Math.floor((item.bytes_read || 0) / item.size * 100)) : 0
}
function openSettings() {
  clearInterval(timer)
  configOpen.value = true
}
function unpack(response) {
  const result = response?.success === undefined && response?.data?.success !== undefined ? response.data : response
  if (result?.success === false) throw new Error(result.message || '请求失败')
  return result?.success !== undefined ? result.data : result?.data ?? result
}
async function copyLink(item, event) {
  if (item.link_valid === false) return
  const source = event.currentTarget
  copiedId.value = copyFailedId.value = ''
  try {
    if (!copyFallback(item.link, source)) {
      if (!navigator.clipboard?.writeText) throw new Error('剪贴板不可用')
      await navigator.clipboard.writeText(item.link)
    }
    copiedId.value = item.id || item.link
  } catch {
    copyFailedId.value = item.id || item.link
    const selection = window.getSelection()
    if (selection) {
      const range = document.createRange()
      range.selectNodeContents(source)
      selection.removeAllRanges()
      selection.addRange(range)
    }
  }
}
function copyFallback(link, source) {
  const previous = document.activeElement
  const field = document.createElement('textarea')
  field.value = link
  field.readOnly = true
  field.tabIndex = -1
  field.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;padding:0;border:0;opacity:0;font-size:16px'
  try {
    source.closest('.records-page').appendChild(field)
    field.focus({ preventScroll: true })
    field.select()
    field.setSelectionRange(0, link.length)
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    field.remove()
    previous?.focus?.({ preventScroll: true })
  }
}
async function deleteSource(item) {
  if (deleting.value || !item.can_delete) return
  if (!window.confirm(`确定永久删除源文件“${item.name}”？\n将先校验文件内容，删除后不可恢复；ED2K 链接和记录保留。`)) return
  deleting.value = item.id
  notice.value = error.value = ''
  try {
    const result = unpack(await props.api.post(`plugin/${props.pluginId}/records/delete-source`, { id: item.id, confirm: true }))
    if (!result?.ok) throw new Error(result?.message || '删除失败')
    notice.value = result.message
    item.can_delete = false
    item.source_status = 'deleted'
  } catch (failure) {
    error.value = failure?.message || '删除请求失败，请刷新核对实际状态'
  } finally {
    deleting.value = ''
  }
}
async function refresh() {
  if (busy.value || disposed || configOpen.value || deleting.value) return
  busy.value = true
  try {
    const response = await props.api.get(`plugin/${props.pluginId}/records`)
    if (disposed) return
    const result = response?.success === undefined && response?.data?.success !== undefined ? response.data : response
    if (result?.success === false) throw new Error(result.message || '读取失败')
    const data = result?.success !== undefined ? result.data : result?.data ?? result
    if (!data?.ok) throw new Error(data?.message || '读取生成记录失败')
    items.value = data.items || []
    progress.value = data.progress || []
    total.value = data.total || 0
    deleteEnabled.value = !!data.delete_source_enabled
    cloudDeleteEnabled.value = !!data.cloud_delete_enabled
    workerAlive.value = !!data.worker_alive
    loaded.value = true
    error.value = ''
  } catch (failure) {
    if (!disposed) error.value = failure?.message || '读取生成记录失败，请稍后刷新'
  } finally {
    if (!disposed) busy.value = false
  }
}
onMounted(() => {
  emit('layout', { maxWidth: '78rem' })
  refresh()
  timer = setInterval(refresh, 5000)
})
onUnmounted(() => { disposed = true; clearInterval(timer) })
</script>

<template>
  <Panel v-if="configOpen" :api="api" :plugin-id="pluginId" @close="emit('close')" />
  <section v-else ref="pageRoot" class="records-page ed2k-mobile-page" :aria-busy="busy">
    <header class="records-header">
      <div><h2>自动生成ED2K链接</h2><p>最近生成记录 · 共 {{ total }} 条 · 显示最近 {{ items.length }} 条</p></div>
      <div class="records-actions"><button type="button" :disabled="busy" @click="refresh">刷新记录</button><button type="button" @click="openSettings">设置</button><button type="button" @click="emit('close')">关闭</button></div>
    </header>
    <p class="help">每 5 秒刷新进度与记录；点击有效 ED2K 链接即可复制。“手动删除源文件”需确认并校验内容，不会自动执行。点击右上角“设置”进入配置页。</p>
    <p class="policy">当前删源开关：{{ deleteEnabled ? '开启（所选输出成功后才删除）' : '关闭（保留源文件）' }}；115网盘另行授权：{{ cloudDeleteEnabled ? '已授权（仍受账号、目录、SHA1与输出条件约束）' : '未授权，保留网盘文件' }}。每条状态以处理时的实际记录为准，不代表源文件之后未被其他程序移动或删除。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <section class="progress-section" aria-live="polite">
      <h3>正在处理与等待队列 <span class="worker-status" :class="workerAlive ? 'worker-on' : 'worker-off'">{{ workerAlive ? '工作线程运行中' : '工作线程未运行' }}</span></h3>
      <p v-if="!progress.length" class="progress-empty">当前没有本地或网盘文件等待处理。</p>
      <ol v-else class="progress-list">
        <li v-for="(task, index) in progress" :key="`${task.source}-${task.name}-${index}`" class="progress-item">
          <div class="progress-heading"><span class="source-tag">{{ task.source }}</span><strong>{{ task.name }}</strong><span class="progress-stage">{{ task.stage }}</span></div>
          <div v-if="task.status === 'active' && task.size" class="progress-track"><span :style="{ width: `${progressPercent(task)}%` }"></span></div>
          <p class="progress-detail">
            <span v-if="task.status === 'active' && task.size">{{ formatBytes(task.bytes_read) }} / {{ formatBytes(task.size) }}（{{ progressPercent(task) }}%）</span>
            <span v-else-if="task.status === 'queued' && task.due_at">预计复查：{{ formatTime(task.due_at) }}</span>
            <span v-else-if="task.status === 'active'">正在准备任务</span>
          </p>
        </li>
      </ol>
    </section>
    <p v-if="!loaded && busy" role="status">正在加载记录…</p>
    <p v-else-if="loaded && !items.length" class="empty">暂无生成记录。新增文件生成成功后会显示在这里。</p>
    <ol v-if="items.length" class="records-list" tabindex="0" aria-label="最近生成的 ED2K 链接记录">
      <li v-for="(item, index) in items" :key="`${item.link}-${index}`" class="record">
        <div class="record-heading"><h3>{{ item.name }}</h3><div class="record-actions"><button v-if="item.can_delete && item.source_status !== 'deleted'" type="button" class="delete-source" :disabled="!!deleting" @click="deleteSource(item)">{{ deleting === item.id ? '校验删除中…' : '手动删除源文件' }}</button><span class="source-status" :class="item.source_status">{{ statuses[item.source_status] || statuses.unknown }}</span></div></div>
        <p class="record-time">文件大小：{{ item.size === null || item.size === undefined ? '未知' : formatBytes(item.size) }} · 生成时间：{{ formatTime(item.generated_at) }}<span v-if="item.deleted_at"> · 删除时间：{{ formatTime(item.deleted_at) }}</span><span v-if="item.source_type === 'cloud'"> · 网盘：{{ item.storage }}<span v-if="item.cloud_delete_note"> · {{ item.cloud_delete_note }}</span><span v-else>（未记录自动删除）</span></span></p>
        <p v-if="item.link_valid === false" class="error">{{ item.link_error || '链接无效，需要完整源文件重新生成' }}</p>
        <pre v-if="item.link_valid === false">{{ item.link }}</pre><button v-else type="button" class="copy-link" title="点击复制 ED2K 链接" @click="copyLink(item, $event)">{{ item.link }}</button>
        <div v-if="item.link_valid !== false" class="copy-feedback" role="status" aria-live="polite"><span v-if="copiedId === (item.id || item.link)" class="notice">链接已复制</span><span v-else-if="copyFailedId === (item.id || item.link)" class="error">复制受限：Ctrl+C／⌘C 或长按</span></div>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.copy-feedback{height:20px;min-height:20px;font-size:12px;line-height:20px;overflow-wrap:anywhere;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.progress-section{flex:none;margin-top:14px;padding:14px 16px;border:1px solid rgba(var(--v-theme-on-surface),.16);border-radius:12px}.progress-section h3{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:16px}.worker-status,.source-tag{flex:none;border-radius:7px;padding:4px 8px;font-size:12px;background:rgba(var(--v-theme-on-surface),.08)}.worker-on{color:#57b985}.worker-off{color:#df7b7b}.progress-empty,.progress-detail{font-size:13px;margin:6px 0;color:rgba(var(--v-theme-on-surface),.68)}.progress-list{list-style:none;padding:0;margin:8px 0 0}.progress-item{padding:9px 0;border-top:1px solid rgba(var(--v-theme-on-surface),.1)}.progress-heading{display:flex;align-items:center;gap:8px;min-width:0}.progress-heading strong{min-width:0;overflow-wrap:anywhere;flex:1}.progress-stage{font-size:12px;color:rgba(var(--v-theme-on-surface),.65);text-align:right}.progress-track{height:6px;margin:8px 0 4px;background:rgba(var(--v-theme-on-surface),.1);border-radius:6px;overflow:hidden}.progress-track span{display:block;height:100%;background:rgb(var(--v-theme-primary));transition:width .2s ease}
.copy-link{display:block;width:100%;text-align:left;white-space:pre-wrap;overflow-wrap:anywhere;user-select:text;font-family:monospace;font-size:13px;background:transparent;padding:6px 0;color:inherit}.copy-link:hover{text-decoration:underline}.record-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap;margin-left:auto}.record-actions .delete-source,.record-actions .source-status{display:inline-flex;align-items:center;justify-content:center;min-width:120px;height:32px;padding:6px 10px;font-size:13px;line-height:20px;white-space:nowrap;box-sizing:border-box}.delete-source{background:rgba(220,60,60,.13);color:#df7b7b}.notice{color:#57b985}.records-actions{flex-wrap:wrap}
.records-page{display:flex;flex-direction:column;box-sizing:border-box;width:100%;padding:20px;max-width:1220px;margin:auto;max-height:none;overflow:visible;color:rgb(var(--v-theme-on-surface));font-size:15px}.records-header,.record-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}.records-actions{display:flex;gap:12px;margin-left:auto}h2,h3{margin:0}h2{font-size:24px}h3{font-size:17px;overflow-wrap:anywhere;flex:1;min-width:160px}p{line-height:1.7;margin:12px 0;color:rgba(var(--v-theme-on-surface),.7)}button{font:inherit;border:0;border-radius:9px;padding:12px 18px;cursor:pointer;background:rgba(var(--v-theme-primary),.13);color:rgb(var(--v-theme-primary))}button:disabled{opacity:.5;cursor:not-allowed}.policy,.help{padding:8px 12px;background:rgba(var(--v-theme-on-surface),.04);border-radius:10px}.records-list{flex:none;min-height:0;list-style:none;padding:0 8px 0 0;margin:12px 0 0;overflow:visible;scrollbar-gutter:auto;scrollbar-width:thin;scrollbar-color:rgba(var(--v-theme-on-surface),.3) transparent}.records-list:focus-visible{outline:2px solid rgb(var(--v-theme-primary));outline-offset:2px}.records-list::-webkit-scrollbar{width:6px}.records-list::-webkit-scrollbar-thumb{background:rgba(var(--v-theme-on-surface),.3);border-radius:6px}.record:last-child{margin-bottom:0}.record{padding:12px 16px;margin-bottom:10px;border:1px solid rgba(var(--v-theme-on-surface),.16);border-radius:12px}.source-status{font-size:13px;border-radius:7px;padding:6px 10px;background:rgba(var(--v-theme-on-surface),.08)}.deleted{background:rgba(220,150,60,.15);color:#dba34b}.retained{background:rgba(60,180,110,.13);color:#57b985}.delete_failed,.delete_uncertain,.error{color:#df7b7b}.unknown{color:rgba(var(--v-theme-on-surface),.6)}.record-time{font-size:13px;margin:6px 0;line-height:1.5}.empty{text-align:center;padding:40px 10px}summary{cursor:pointer;color:rgb(var(--v-theme-primary))}pre{white-space:pre-wrap;overflow-wrap:anywhere;user-select:text;font-size:13px;margin:14px 0 0}@media(max-width:650px){.records-page{padding:14px}.record{padding:12px}.records-actions{width:100%;justify-content:flex-end}}
</style>
