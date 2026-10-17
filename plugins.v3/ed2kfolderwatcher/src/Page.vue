<script setup>
import { onMounted, onUnmounted, ref } from 'vue'

const props = defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'Ed2kFolderWatcher' } })
const emit = defineEmits(['close', 'layout'])
const items = ref([])
const total = ref(0)
const deleteEnabled = ref(false)
const busy = ref(false)
const error = ref('')
const loaded = ref(false)
let timer
let disposed = false
const statuses = { deleted: '已删除源文件', retained: '未删除源文件', delete_failed: '删除失败，源文件保留', unknown: '历史记录：删源状态未记录' }

function formatTime(value) {
  return value ? new Date(value * 1000).toLocaleString() : '历史记录：生成时间未记录'
}
async function refresh() {
  if (busy.value || disposed) return
  busy.value = true
  try {
    const response = await props.api.get(`plugin/${props.pluginId}/records`)
    if (disposed) return
    const result = response?.success === undefined && response?.data?.success !== undefined ? response.data : response
    if (result?.success === false) throw new Error(result.message || '读取失败')
    const data = result?.success !== undefined ? result.data : result?.data ?? result
    if (!data?.ok) throw new Error(data?.message || '读取生成记录失败')
    items.value = data.items || []
    total.value = data.total || 0
    deleteEnabled.value = !!data.delete_source_enabled
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
  timer = setInterval(refresh, 15000)
})
onUnmounted(() => { disposed = true; clearInterval(timer) })
</script>

<template>
  <section class="records-page" :aria-busy="busy">
    <header class="records-header">
      <div><h2>自动生成ED2K链接</h2><p>最近生成记录 · 共 {{ total }} 条 · 显示最近 {{ items.length }} 条</p></div>
      <div class="records-actions"><button type="button" :disabled="busy" @click="refresh">刷新记录</button><button type="button" @click="emit('close')">关闭</button></div>
    </header>
    <p class="help">查看页每 15 秒刷新一次，不触发扫描或生成。修改设置请在插件卡片右上角「⋮ → 设置」进入配置页。</p>
    <p class="policy">当前删源开关：{{ deleteEnabled ? '开启（所选输出成功后才删除）' : '关闭（保留源文件）' }}。每条状态以处理时的实际记录为准，不代表源文件之后未被其他程序移动或删除。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="!loaded && busy" role="status">正在加载记录…</p>
    <p v-else-if="loaded && !items.length" class="empty">暂无生成记录。新增文件生成成功后会显示在这里。</p>
    <ol v-if="items.length" class="records-list">
      <li v-for="(item, index) in items" :key="`${item.link}-${index}`" class="record">
        <div class="record-heading"><h3>{{ item.name }}</h3><span class="source-status" :class="item.source_status">{{ statuses[item.source_status] || statuses.unknown }}</span></div>
        <p class="record-time">生成时间：{{ formatTime(item.generated_at) }}<span v-if="item.deleted_at"> · 删除时间：{{ formatTime(item.deleted_at) }}</span></p>
        <details><summary>查看 ED2K 链接</summary><pre>{{ item.link }}</pre></details>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.records-page{padding:24px;max-width:1220px;margin:auto;max-height:85dvh;overflow:auto;color:rgb(var(--v-theme-on-surface));font-size:15px}.records-header,.record-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}.records-actions{display:flex;gap:12px;margin-left:auto}h2,h3{margin:0}h2{font-size:24px}h3{font-size:17px;overflow-wrap:anywhere;flex:1;min-width:160px}p{line-height:1.7;margin:12px 0;color:rgba(var(--v-theme-on-surface),.7)}button{font:inherit;border:0;border-radius:9px;padding:12px 18px;cursor:pointer;background:rgba(var(--v-theme-primary),.13);color:rgb(var(--v-theme-primary))}button:disabled{opacity:.5;cursor:not-allowed}.policy,.help{padding:12px 16px;background:rgba(var(--v-theme-on-surface),.04);border-radius:10px}.records-list{list-style:none;padding:0;margin:20px 0}.record{padding:20px;margin-bottom:16px;border:1px solid rgba(var(--v-theme-on-surface),.16);border-radius:12px}.source-status{font-size:13px;border-radius:7px;padding:6px 10px;background:rgba(var(--v-theme-on-surface),.08)}.deleted{background:rgba(220,150,60,.15);color:#dba34b}.retained{background:rgba(60,180,110,.13);color:#57b985}.delete_failed,.error{color:#df7b7b}.unknown{color:rgba(var(--v-theme-on-surface),.6)}.record-time{font-size:13px}.empty{text-align:center;padding:40px 10px}summary{cursor:pointer;color:rgb(var(--v-theme-primary))}pre{white-space:pre-wrap;overflow-wrap:anywhere;user-select:text;font-size:13px;margin:14px 0 0}@media(max-width:650px){.records-page{padding:14px}.record{padding:16px}.records-actions{width:100%;justify-content:flex-end}}
</style>
