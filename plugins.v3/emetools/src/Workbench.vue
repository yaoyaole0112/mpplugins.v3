<script setup>
import { computed, onMounted, reactive, ref } from 'vue'

const props = defineProps({
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'EmeTools' },
})
const emit = defineEmits(['close'])
const sections = [
  { key: 'subscription', title: '订阅监控', icon: 'mdi-television-play', detail: 'MoviePilot 订阅与 Telegram 频道监控' },
  { key: 'invalid', title: '清理无效数据', icon: 'mdi-folder-search-outline', detail: '扫描与隔离 STRM 孤立资料' },
  { key: 'cleanup', title: '清理文件', icon: 'mdi-folder-remove-outline', detail: '115 文件夹清理' },
  { key: 'trash', title: '清空 115 回收站', icon: 'mdi-delete-alert-outline', detail: '不可恢复的彻底删除' },
  { key: 'move', title: '文件转存', icon: 'mdi-folder-swap-outline', detail: '115 文件夹监控转存' },
  { key: 'settings', title: '设置', icon: 'mdi-cog-outline', detail: '基础设置与 Telegram 账号' },
]
const active = ref('subscription')
const busy = ref(false)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const settings = reactive({ enabled: true, show_sidebar_nav: true, strm_root: '/strm',
  cookie_configured: false, rb_password: '', rb_password_configured: false })
const schedule = reactive({
  tools: { enabled: false, cron: '0 3 * * *', path: '/strm', auto_delete: true, confirm_cleanup: false },
  p115_cleanup: { enabled: false, cron: '0 */2 * * *', dir_ids: [], dir_names: [] },
  p115_trash: { enabled: false, cron: '0 3 * * *' },
  p115_move: { enabled: false, check_interval: 120, rules: [] },
})
const scanPath = ref('/strm')
const scan = ref(null)
const selected = ref([])
const preview = ref(null)
const cleanupToken = ref('')
const pendingDeleteToken = ref('')
const pendingJobs = ref([])
const trash = ref(null)
const moveInfo = ref(null)
const folder = reactive({ open: false, type: '', index: 0, cid: '0', path: '/', name: '根目录', trail: [], dirs: [], ready: false, error: '' })
const base = computed(() => `plugin/${props.pluginId}`)
const current = computed(() => sections.find(section => section.key === active.value))
const rules = computed(() => schedule.p115_move.rules || [])
const cleanupDirs = ref([])
const monitor = reactive({ configured: false, logged_in: false, dependency_ready: false, hits: [], last_error: '', last_event: '', listening_channels: {}, subscription_count: 0,
  sub: { enabled: false, channels: [], keywords: [], blacklist: [] }, kw: { enabled: false, channels: [], keywords: [], blacklist: [] } })
const drafts = reactive({ sub: { channels: [], keywords: [], blacklist: [] }, kw: { channels: [], keywords: [], blacklist: [] } })
const entry = reactive({ sub: { channels: '' }, kw: { channels: '', keywords: '', blacklist: '' } })
const telegram = reactive({ api_id: '', api_hash: '', forward_token: '', phone: '', code: '', password: '', password_required: false })

function unpack(response) {
  if (response && Object.prototype.hasOwnProperty.call(response, 'success')) {
    if (!response.success) throw new Error(response.message || '请求失败')
    return response.data
  }
  return response?.data ?? response
}
async function get(path) { return unpack(await props.api.get(`${base.value}/${path}`)) }
async function post(path, body) { return unpack(await props.api.post(`${base.value}/${path}`, body)) }
async function execute(operation, body = {}) {
  const result = await post('action', { operation, ...body })
  if (result?.ok === false) throw new Error(result.message || '操作失败')
  return result
}
async function work(callback) {
  busy.value = true
  error.value = ''
  notice.value = ''
  try { await callback() } catch (err) { error.value = err?.message || '请求失败' } finally { busy.value = false }
}
function applyStatus(data) {
  Object.assign(settings, data.settings || {})
  settings.rb_password = ''
  telegram.api_id = settings.tg_api_id || ''
  telegram.api_hash = ''
  telegram.forward_token = ''
  for (const key of Object.keys(schedule)) Object.assign(schedule[key], data.schedule?.[key] || {})
  scanPath.value = schedule.tools.path || '/strm'
  cleanupDirs.value = (schedule.p115_cleanup.dir_ids || []).map((cid, index) => ({
    cid, name: schedule.p115_cleanup.dir_names?.[index] || '',
  }))
  schedule.p115_move.rules = (schedule.p115_move.rules || []).map(rule => ({ ...rule }))
}
async function load() {
  loading.value = true
  try { applyStatus(await get('status')); await getPending(); await loadMonitor() } catch (err) { error.value = err?.message || '加载工具配置失败' }
  finally { loading.value = false }
}
async function getPending() {
  const result = await execute('pending')
  pendingJobs.value = result.pending || []
}
async function saveBasicSettings() {
  await work(async () => {
    const { enabled, show_sidebar_nav, strm_root, rb_password } = settings
    const result = await post('settings', { enabled, show_sidebar_nav, strm_root, rb_password })
    Object.assign(settings, result.settings)
    settings.rb_password = ''
    const status = await get('status')
    schedule.tools.path = status.schedule?.tools?.path || strm_root
    scanPath.value = schedule.tools.path
    notice.value = '基础设置已保存'
  })
}
async function saveTelegramSettings() {
  await work(async () => {
    const result = await post('settings', {
      tg_api_id: telegram.api_id, tg_api_hash: telegram.api_hash,
      tg_forward_token: telegram.forward_token,
    })
    settings.tg_api_id = result.settings.tg_api_id
    settings.tg_api_hash_configured = result.settings.tg_api_hash_configured
    settings.tg_forward_token_configured = result.settings.tg_forward_token_configured
    telegram.api_hash = ''
    telegram.forward_token = ''
    notice.value = 'Telegram 账号设置已保存'
  })
}
async function loadMonitor() {
  const status = await get('monitor/status')
  Object.assign(monitor, status)
  for (const scope of ['sub', 'kw']) {
    drafts[scope].channels = [...(status[scope]?.channels || [])]
    drafts[scope].keywords = [...(status[scope]?.keywords || [])]
    drafts[scope].blacklist = [...(status[scope]?.blacklist || [])]
  }
}
async function monitorCommand(operation, scope = 'sub', extra = {}) {
  await work(async () => {
    const result = await post('monitor/action', { operation, scope, ...extra })
    if (result.password_required) telegram.password_required = true
    notice.value = result.message || (operation === 'save' ? '监控设置已保存' : '操作成功')
    await loadMonitor()
  })
}
function addEntry(scope, field) {
  const value = (entry[scope][field] || '').trim()
  if (value && !drafts[scope][field].includes(value)) drafts[scope][field].push(value)
  entry[scope][field] = ''
}
async function saveMonitor(scope) {
  await monitorCommand('save', scope, { channels: drafts[scope].channels, keywords: drafts[scope].keywords, blacklist: drafts[scope].blacklist })
}
async function toggleMonitor(scope) {
  if (!monitor[scope].enabled && (JSON.stringify(drafts[scope].channels) !== JSON.stringify(monitor[scope].channels) ||
    JSON.stringify(drafts[scope].keywords) !== JSON.stringify(monitor[scope].keywords) ||
    JSON.stringify(drafts[scope].blacklist) !== JSON.stringify(monitor[scope].blacklist))) {
    error.value = '请先保存监控配置，再启动'
    return
  }
  await monitorCommand(monitor[scope].enabled ? 'stop' : 'start', scope)
}
function logoutTelegram() {
  if (window.confirm('退出 Telegram 并停止两种监控？')) monitorCommand('logout')
}
async function saveSchedule(section) {
  await work(async () => {
    let value = { ...schedule[section] }
    if (section === 'tools') value = { enabled: value.enabled, cron: value.cron, path: value.path, auto_delete: value.auto_delete, confirm_cleanup: value.confirm_cleanup }
    if (section === 'p115_cleanup') {
      const dirs = cleanupDirs.value.filter(item => String(item.cid || '').trim())
      value = { enabled: value.enabled, cron: value.cron, dir_ids: dirs.map(item => item.cid), dir_names: dirs.map(item => item.name) }
    }
    if (section === 'p115_trash') value = { enabled: value.enabled, cron: value.cron }
    if (section === 'p115_move') {
      if (rules.value.some(rule => !String(rule.src_id || '').trim() || !String(rule.dst_id || '').trim())) throw new Error('请填写每条转存规则的源目录与目标目录')
      value = { enabled: value.enabled, check_interval: Math.max(60, Number(value.check_interval) || 120), rules: rules.value }
    }
    const result = await post('schedule', { section, settings: value })
    if (section === 'p115_cleanup') { preview.value = null; cleanupToken.value = '' }
    if (section === 'p115_move') moveInfo.value = null
    notice.value = result.message || '配置已保存'
  })
}
async function startScan() {
  scan.value = null
  selected.value = []
  pendingDeleteToken.value = ''
  await work(async () => {
    scan.value = await execute('scan', { path: scanPath.value })
    selected.value = (scan.value.items || []).map(item => item.path)
    notice.value = `扫描完成：发现 ${scan.value.count || 0} 项可清理数据`
  })
}
async function deleteSelected() {
  if (!scan.value?.scan_token || !selected.value.length) return
  const needsConfirmation = schedule.tools.confirm_cleanup
  if (!needsConfirmation && !window.confirm(`将 ${selected.value.length} 项无效数据移入插件隔离区？清理前会再次核验。`)) return
  await work(async () => {
    const result = await execute(needsConfirmation ? 'request_delete' : 'delete', {
      scan_token: scan.value.scan_token, paths: selected.value, path: scan.value.root || scanPath.value,
    })
    notice.value = result.message || (needsConfirmation ? '待确认' : `已隔离 ${(result.deleted || []).length} 项`)
    if (needsConfirmation) pendingDeleteToken.value = result.token
    else { scan.value = null; selected.value = [] }
  })
}
async function confirmDelete() {
  if (!window.confirm('确定隔离选中的无效数据？清理前会再次核验。')) return
  await work(async () => {
    const result = await execute('confirm_delete', { token: pendingDeleteToken.value })
    notice.value = `已隔离 ${(result.deleted || []).length} 项，跳过 ${(result.failed || []).length} 项`
    pendingDeleteToken.value = ''
    scan.value = null
    selected.value = []
  })
}
async function confirmScheduled(token) {
  if (!window.confirm('确定隔离这批定时扫描发现的数据？清理前会再次核验。')) return
  await work(async () => {
    const result = await execute('confirm_scheduled', { token })
    notice.value = `已隔离 ${(result.deleted || []).length} 项，跳过 ${(result.failed || []).length} 项`
    await getPending()
  })
}
async function getPreview() {
  cleanupToken.value = ''
  await work(async () => { preview.value = await execute('cleanup_preview') })
}
async function requestCleanup() {
  if (!preview.value) return
  await work(async () => {
    const result = await execute('cleanup_request')
    cleanupToken.value = result.token || ''
    notice.value = result.message || '等待页面确认'
  })
}
async function confirmCleanup() {
  if (!window.confirm('确认删除预览中的 115 文件和文件夹？删除后会进入 115 回收站。')) return
  await work(async () => {
    const result = await execute('cleanup_confirm', { token: cleanupToken.value })
    cleanupToken.value = ''
    preview.value = null
    notice.value = result.message || '清理完成'
  })
}
async function getTrash() {
  await work(async () => { trash.value = await execute('trash_info') })
}
async function clearTrash() {
  if (!trash.value?.ok || !window.confirm(`确定彻底清空 115 回收站的 ${trash.value.count || 0} 个文件？此操作不可恢复。`)) return
  await work(async () => {
    const result = await execute('trash_clear', { token: trash.value.token })
    notice.value = result.message || '回收站已清空'
    trash.value = null
  })
}
async function getMove() {
  await work(async () => { moveInfo.value = await execute('move_info') })
}
async function runMove() {
  if (!window.confirm('按插件中已保存的转存规则立即移动源目录内容？未保存的修改不会生效。')) return
  await work(async () => {
    const result = await execute('move_run')
    notice.value = result.message || '转存完成'
    moveInfo.value = null
  })
}
async function browse(type, index = 0) {
  folder.open = true
  folder.type = type
  folder.index = index
  folder.cid = '0'
  folder.path = type === 'root' ? (settings.strm_root || '/strm') : type === 'local' ? (scanPath.value || '/strm') : '/'
  folder.name = '根目录'
  folder.trail = []
  if (type === 'root') {
    let parent = folder.path.replace(/\/+$/, '') || '/'
    while (parent !== '/') {
      parent = parent.slice(0, parent.lastIndexOf('/')) || '/'
      folder.trail.unshift({ cid: '0', path: parent, name: parent === '/' ? '根目录' : parent.split('/').pop() })
    }
  }
  await loadFolder()
}
async function loadFolder() {
  folder.ready = false
  folder.error = ''
  try {
    const local = folder.type === 'local' || folder.type === 'root'
    const result = await execute(local ? (folder.type === 'root' ? 'root_dirs' : 'dirs') : 'p115_dirs', local ? { path: folder.path } : { cid: folder.cid })
    folder.dirs = local ? (result.dirs || []).map(name => ({ name, path: `${result.path.replace(/\/$/, '')}/${name}` })) : (result.dirs || [])
    if (local) folder.path = result.path
    folder.ready = true
  } catch (err) { folder.error = err?.message || '读取目录失败'; folder.dirs = [] }
}
async function enterFolder(item) {
  folder.trail.push({ cid: folder.cid, path: folder.path, name: folder.name })
  if (folder.type === 'local' || folder.type === 'root') folder.path = item.path
  else folder.cid = String(item.cid)
  folder.name = item.name
  await loadFolder()
}
async function parentFolder() {
  const previous = folder.trail.pop()
  if (!previous) return
  folder.cid = previous.cid
  folder.path = previous.path
  folder.name = previous.name
  await loadFolder()
}
function selectFolder() {
  if (!folder.ready) return
  const { type, index, cid, path } = folder
  if (type === 'local') scanPath.value = path
  else if (type === 'root') settings.strm_root = path
  else if (type === 'cleanup') cleanupDirs.value[index] = { cid, name: folder.name }
  else if (type === 'src' || type === 'dst') {
    rules.value[index][`${type}_id`] = cid
    rules.value[index][`${type}_name`] = folder.name
  }
  folder.open = false
}
function chooseSection(key) {
  active.value = key
  notice.value = ''
  error.value = ''
}
onMounted(load)
</script>

<template>
  <div class="eme-shell">
    <aside class="eme-sidebar">
      <div class="eme-brand"><span class="eme-brand-icon">✦</span><div><strong>订阅清理转存</strong><small>MoviePilot 独立插件</small></div></div>
      <div class="eme-nav-label">工具</div>
      <button v-for="section in sections" :key="section.key" type="button" class="eme-nav" :class="{ selected: active === section.key }" @click="chooseSection(section.key)">
        <i :class="`mdi ${section.icon}`" /><span><strong>{{ section.title }}</strong><small>{{ section.detail }}</small></span><i class="mdi mdi-chevron-right eme-chevron" />
      </button>
      <div class="eme-sidebar-footer"><span class="eme-dot" />由 MoviePilot 独立执行和调度</div>
    </aside>
    <main class="eme-main">
      <header class="eme-header">
        <div><span class="eme-kicker">{{ active === 'settings' ? '插件配置' : '工具配置' }}</span><h2>{{ current.title }}</h2><p>{{ current.detail }}</p></div>
      </header>
      <div v-if="loading" class="eme-message">正在加载插件配置…</div>
      <div v-if="error" class="eme-message eme-error" role="alert">{{ error }}</div>
      <div v-if="notice" class="eme-message eme-success" role="status">{{ notice }}</div>
      <section v-if="active === 'settings'" class="eme-card">
        <div class="eme-card-heading"><h3>基础设置</h3><button class="eme-button primary" :disabled="busy" @click="saveBasicSettings">保存设置</button></div>
        <div class="eme-options eme-settings-switches">
          <label class="eme-switch-label"><input v-model="settings.enabled" class="eme-switch-input" type="checkbox" role="switch" /><span class="eme-switch-track" aria-hidden="true" /><span>启用插件</span></label>
          <label class="eme-switch-label"><input v-model="settings.show_sidebar_nav" class="eme-switch-input" type="checkbox" role="switch" /><span class="eme-switch-track" aria-hidden="true" /><span>显示 MP 侧栏入口</span></label>
        </div>
        <div class="eme-settings-fields"><label>STRM 根目录（MoviePilot 容器内）<div class="eme-inline"><input :value="settings.strm_root" readonly /><button class="eme-button secondary" type="button" :disabled="busy" @click="browse('root')">浏览选择</button></div></label><label>115 回收站安全密钥 <input v-model.trim="settings.rb_password" type="password" autocomplete="off" :placeholder="settings.rb_password_configured ? '已配置；留空保持不变' : '默认 000000；留空保持不变'" /></label></div>
        <p class="eme-hint">115 Cookie：{{ settings.cookie_configured ? '已从 115 网盘 STRM 助手读取，更新后自动同步' : '未读取到，请先在 115 网盘 STRM 助手中配置 Cookie' }}。115 连接沿用 MoviePilot 容器的网络环境。</p>
      </section>
      <section v-if="active === 'settings'" class="eme-card">
        <div class="eme-card-heading"><h3>Telegram 账号</h3><button class="eme-button primary" :disabled="busy" @click="saveTelegramSettings">保存设置</button></div>
        <p class="eme-hint">使用你自己的 Telegram 账号监听频道。请从 my.telegram.org 的 API development tools 获取 API ID 和 API Hash；此插件不会读取 MediaEnhance 的账号或会话。</p>
        <div class="eme-settings-fields"><label>API ID<input v-model.trim="telegram.api_id" inputmode="numeric" placeholder="Telegram API ID" /></label>
          <label>API Hash<input v-model.trim="telegram.api_hash" type="password" autocomplete="new-password" :placeholder="settings.tg_api_hash_configured ? '已配置；留空保持不变' : '32 位字符串'" /></label>
          <label>转发 Bot Token<input v-model.trim="telegram.forward_token" type="password" autocomplete="new-password" :placeholder="settings.tg_forward_token_configured ? '已配置；留空保持不变' : '命中时将原消息转发给此 Bot'" /></label></div>
        <p class="eme-hint">{{ monitor.logged_in ? 'Telegram 已登录' : 'Telegram 未登录' }} · {{ monitor.dependency_ready ? '监控依赖已就绪' : '缺少 telethon 依赖' }}</p>
        <div v-if="!monitor.logged_in" class="eme-fields"><label>手机号（国际格式）<input v-model.trim="telegram.phone" placeholder="+8613800000000" autocomplete="tel" /></label><label>验证码<input v-model.trim="telegram.code" placeholder="Telegram 收到的验证码" autocomplete="one-time-code" /></label><label v-if="telegram.password_required">二步验证密码<input v-model="telegram.password" type="password" autocomplete="off" /></label></div>
        <div class="eme-actions"><button v-if="!monitor.logged_in" class="eme-button secondary" :disabled="busy || !monitor.dependency_ready" @click="monitorCommand('send_code', 'sub', { phone: telegram.phone })">发送验证码</button>
          <button v-if="!monitor.logged_in" class="eme-button primary" :disabled="busy || !monitor.dependency_ready" @click="monitorCommand('sign_in', 'sub', { code: telegram.code, password: telegram.password })">登录 Telegram</button>
          <button v-else class="eme-button danger" :disabled="busy" @click="logoutTelegram">退出登录</button></div>
      </section>
      <template v-if="active === 'subscription'">
        <section v-for="scope in ['sub', 'kw']" :key="scope" class="eme-card"><div class="eme-card-heading"><div><h3>{{ scope === 'sub' ? '订阅监控' : '关键词监控' }}</h3><p>{{ scope === 'sub' ? '按订阅名称、TMDB ID、年份、类型和季号校验频道消息。' : '按自定义关键词及黑名单筛选频道消息。' }}命中后原样转发给设置中的 Bot。</p></div><div class="eme-inline"><button class="eme-button secondary" :disabled="busy || monitor[scope].enabled" @click="saveMonitor(scope)">保存</button><button class="eme-button primary" :disabled="busy || (!monitor.logged_in && !monitor[scope].enabled)" @click="toggleMonitor(scope)">{{ monitor[scope].enabled ? '停止监控' : '启动监控' }}</button></div></div>
          <p class="eme-hint">状态：{{ monitor[scope].enabled ? (monitor.logged_in && monitor.listening_channels?.[scope] ? '运行中' : '等待连接') : '已停止' }} · 已监听 {{ monitor.listening_channels?.[scope] || 0 }} / {{ drafts[scope].channels.length }} 个频道<span v-if="scope === 'sub'"> · 已读取 {{ monitor.subscription_count || 0 }} 条 MP 订阅</span><span v-if="monitor.last_poll"> · 最近检查频道 {{ monitor.last_poll }}</span><span v-if="monitor.last_event"> · 最近收到消息 {{ monitor.last_event }}</span></p>
          <p v-if="monitor.last_error" class="eme-message eme-error">{{ monitor.last_error }}</p>
          <label>监控频道（公开频道 @用户名或 t.me/链接）<div class="eme-inline"><input v-model.trim="entry[scope].channels" :disabled="monitor[scope].enabled" placeholder="@channelname" @keyup.enter="addEntry(scope, 'channels')" /><button class="eme-button secondary" :disabled="monitor[scope].enabled" @click="addEntry(scope, 'channels')">添加</button></div></label>
          <div class="eme-chips"><span v-for="(value, index) in drafts[scope].channels" :key="value" class="eme-chip">{{ value }}<button :disabled="monitor[scope].enabled" @click="drafts[scope].channels.splice(index, 1)">×</button></span></div>
          <template v-if="scope === 'kw'"><div v-for="field in ['keywords', 'blacklist']" :key="field"><label>{{ field === 'keywords' ? '匹配关键词' : '排除关键词（黑名单）' }}（支持正则）<div class="eme-inline"><input v-model.trim="entry.kw[field]" :disabled="monitor.kw.enabled" :placeholder="field === 'keywords' ? '添加匹配关键词' : '添加排除关键词'" @keyup.enter="addEntry('kw', field)" /><button class="eme-button secondary" :disabled="monitor.kw.enabled" @click="addEntry('kw', field)">添加</button></div></label><div class="eme-chips"><span v-for="(value, index) in drafts.kw[field]" :key="value" class="eme-chip">{{ value }}<button :disabled="monitor.kw.enabled" @click="drafts.kw[field].splice(index, 1)">×</button></span></div></div></template>
          <p v-else class="eme-hint">订阅名称和媒体资料自动从 MoviePilot「我的订阅」读取，每 5 分钟更新一次；订阅列表请在 MoviePilot 中查看。</p>
        </section>
        <section v-if="monitor.hits?.length" class="eme-card"><h3>最近命中</h3><div v-for="(hit, index) in monitor.hits" :key="index" class="eme-result">{{ hit.time }} · {{ hit.channel }} · {{ hit.matches?.join('、') }}</div></section>
      </template>
      <template v-if="active === 'invalid'">
        <section class="eme-card">
          <div class="eme-card-heading"><div><h3>扫描无效数据</h3><p>只扫描 MP 插件设置的 STRM 根目录；清理时复核并移入 .mp-emetools-trash，可通过 manifest.json 恢复。</p></div><button class="eme-button primary" :disabled="busy" @click="startScan">开始扫描</button></div>
          <label>扫描目录<div class="eme-inline"><input v-model.trim="scanPath" placeholder="/strm" /><button class="eme-button secondary" :disabled="busy" @click="browse('local')">浏览</button></div></label>
          <template v-if="scan"><p class="eme-hint">孤立资料 {{ scan.metadata_count || 0 }} 项 · 无 STRM 子目录 {{ scan.directory_count || 0 }} 项；请核对勾选内容。</p>
            <div class="eme-results"><label v-for="item in scan.items || []" :key="item.path" class="eme-result"><input v-model="selected" type="checkbox" :disabled="!!pendingDeleteToken" :value="item.path" /><span><strong>{{ item.path }}</strong><small>{{ item.reason || item.kind || '无效数据' }}</small></span></label><p v-if="!scan.items?.length">未发现需要清理的内容。</p></div>
            <div class="eme-actions"><button class="eme-button danger" :disabled="busy || !selected.length || !!pendingDeleteToken" @click="deleteSelected">清理选中 {{ selected.length }} 项</button><button v-if="pendingDeleteToken" class="eme-button danger" :disabled="busy" @click="confirmDelete">确认隔离选中项目</button></div>
          </template>
        </section>
        <section class="eme-card"><div class="eme-card-heading"><div><h3>定时清理</h3><p>由 MoviePilot 调度；需要确认时先生成待办并发送 MP 通知，不直接清理。</p></div><button class="eme-button primary" :disabled="busy" @click="saveSchedule('tools')">保存任务</button></div>
          <label class="eme-check"><input v-model="schedule.tools.enabled" type="checkbox" /> 启用定时任务</label>
          <div class="eme-fields"><label>cron 表达式<input v-model.trim="schedule.tools.cron" placeholder="0 3 * * *" /></label><label>扫描目录<input v-model.trim="schedule.tools.path" placeholder="/strm" /></label></div>
          <div class="eme-options"><label><input v-model="schedule.tools.auto_delete" type="checkbox" /> 自动隔离清理</label><label><input v-model="schedule.tools.confirm_cleanup" type="checkbox" /> 清理前在 MP 页面确认</label></div>
          <div v-if="pendingJobs.length" class="eme-actions"><span>待确认的定时扫描：</span><button v-for="job in pendingJobs" :key="job.token" class="eme-button danger" :disabled="busy" @click="confirmScheduled(job.token)">确认隔离 {{ job.count }} 项</button><button class="eme-button secondary" @click="getPending">刷新待办</button></div>
        </section>
      </template>
      <template v-if="active === 'cleanup'">
        <section class="eme-card"><div class="eme-card-heading"><div><h3>115 清理目录</h3><p>目录内的文件与子文件夹删除后进入 115 回收站。</p></div><div class="eme-inline"><button class="eme-button secondary" @click="cleanupDirs.push({ cid: '', name: '' })">添加目录</button><button class="eme-button primary" :disabled="busy" @click="saveSchedule('p115_cleanup')">保存配置</button></div></div>
          <div class="eme-cleanup-grid"><div v-for="(item, index) in cleanupDirs" :key="index" class="eme-cleanup-item"><button class="eme-button secondary eme-folder-choice" type="button" :title="item.name || '选择清理目录'" @click="browse('cleanup', index)">{{ item.name || '选择清理目录' }}</button><button class="eme-button text eme-remove" type="button" :aria-label="`移除清理目录 ${item.name || index + 1}`" @click="cleanupDirs.splice(index, 1)">✕</button></div></div><p v-if="!cleanupDirs.length" class="eme-hint">还没有清理目录。请添加并保存后再预览。</p>
          <div class="eme-actions"><button class="eme-button secondary" :disabled="busy" @click="getPreview">预览待清理内容</button><template v-if="preview"><span>文件 {{ preview.file_count || 0 }} 个 · 文件夹 {{ preview.dir_count || 0 }} 个</span><button class="eme-button danger" :disabled="busy || !!cleanupToken || !(preview.file_count || preview.dir_count)" @click="requestCleanup">生成清理确认</button><button v-if="cleanupToken" class="eme-button danger" :disabled="busy" @click="confirmCleanup">确认清理文件</button></template></div>
          <p class="eme-hint">预览与清理仅针对已保存的目录，确认时将重新检查目录内容。</p>
        </section>
        <section class="eme-card"><div class="eme-card-heading"><h3>定时清理文件</h3><button class="eme-button primary" :disabled="busy" @click="saveSchedule('p115_cleanup')">保存任务</button></div><div class="eme-options"><label><input v-model="schedule.p115_cleanup.enabled" type="checkbox" /> 启用</label></div><label>cron 表达式<input v-model.trim="schedule.p115_cleanup.cron" placeholder="0 */2 * * *" /></label></section>
      </template>
      <template v-if="active === 'trash'">
        <section class="eme-card"><div class="eme-card-heading"><div><h3>115 回收站</h3><p>清空是彻底删除，不能恢复。执行前必须核对当前数量。</p></div><button class="eme-button secondary" :disabled="busy" @click="getTrash">查询状态</button></div><div v-if="trash" class="eme-stat">当前 {{ trash.count || 0 }} 个文件 <button class="eme-button danger" :disabled="busy || !trash.count" @click="clearTrash">立即清空</button></div><p v-else class="eme-hint">请先查询回收站状态。</p></section>
        <section class="eme-card"><div class="eme-card-heading"><h3>定时清空</h3><button class="eme-button primary" :disabled="busy" @click="saveSchedule('p115_trash')">保存任务</button></div><label class="eme-check"><input v-model="schedule.p115_trash.enabled" type="checkbox" /> 启用（将彻底删除，无法恢复）</label><label>cron 表达式<input v-model.trim="schedule.p115_trash.cron" placeholder="0 3 * * *" /></label></section>
      </template>
      <template v-if="active === 'move'">
        <section class="eme-card"><div class="eme-card-heading"><div><h3>文件转存规则</h3><p>每条规则分别将源目录中的文件及子文件夹移动到对应目标目录。</p></div><button class="eme-button secondary" @click="rules.push({ src_id: '', src_name: '', dst_id: '', dst_name: '' })">添加规则</button></div>
          <div v-for="(rule, index) in rules" :key="index" class="eme-move-row"><span class="eme-move-label">源</span><button class="eme-button secondary eme-folder-choice" type="button" :title="rule.src_name || '选择源目录'" @click="browse('src', index)">{{ rule.src_name || '选择源目录' }}</button><span class="eme-move-arrow" aria-hidden="true">→</span><span class="eme-move-label">目标</span><button class="eme-button secondary eme-folder-choice" type="button" :title="rule.dst_name || '选择目标目录'" @click="browse('dst', index)">{{ rule.dst_name || '选择目标目录' }}</button><button class="eme-button text eme-remove" type="button" :aria-label="`移除转存规则 ${index + 1}`" @click="rules.splice(index, 1)">✕</button></div>
          <div class="eme-actions"><button class="eme-button primary" :disabled="busy" @click="saveSchedule('p115_move')">保存规则与监控</button><button class="eme-button secondary" :disabled="busy" @click="getMove">查看待转存</button><button class="eme-button secondary" :disabled="busy || !rules.length" @click="runMove">立即执行已保存规则</button></div>
          <p v-if="moveInfo" class="eme-hint">上次执行：{{ moveInfo.last_run || '暂无' }}；<span v-for="(item, key) in moveInfo.pending || {}" :key="key">{{ item.name }}：{{ item.count < 0 ? item.error : `${item.count} 项` }}；</span></p>
        </section>
        <section class="eme-card"><div class="eme-card-heading"><h3>实时监控</h3><button class="eme-button primary" :disabled="busy" @click="saveSchedule('p115_move')">保存监控</button></div><label class="eme-check"><input v-model="schedule.p115_move.enabled" type="checkbox" /> 启用监控</label><label>检查间隔（秒，最少 60）<input v-model.number="schedule.p115_move.check_interval" type="number" min="60" step="60" /></label></section>
      </template>
    </main>
      <div v-if="folder.open" class="eme-overlay" @click.self="folder.open = false">
        <div class="eme-dialog" role="dialog" aria-modal="true" aria-label="选择目录">
          <div class="eme-card-heading"><h3>选择{{ folder.type === 'root' ? 'STRM 根目录' : folder.type === 'local' ? '扫描目录' : '115 文件夹' }}</h3><button class="eme-button text" @click="folder.open = false">关闭</button></div>
          <p class="eme-hint">{{ folder.type === 'root' || folder.type === 'local' ? folder.path : `CID: ${folder.cid}` }}</p>
          <p v-if="folder.error" class="eme-message eme-error">{{ folder.error }}</p>
          <div class="eme-actions"><button class="eme-button secondary" :disabled="!folder.trail.length" @click="parentFolder">上一级</button><button class="eme-button primary" :disabled="!folder.ready || (folder.type === 'root' ? folder.path === '/' : folder.type !== 'local' && folder.cid === '0')" @click="selectFolder">选择当前目录</button></div>
          <div class="eme-folder-list"><button v-for="item in folder.dirs" :key="item.path || item.cid" class="eme-folder" @click="enterFolder(item)">📁 {{ item.name }}</button><p v-if="folder.ready && !folder.dirs.length" class="eme-hint">没有子目录</p></div>
        </div>
      </div>
  </div>
</template>

<style scoped>
.eme-shell{display:flex;min-height:680px;height:100%;color:rgb(var(--v-theme-on-surface));background:rgb(var(--v-theme-background));font-size:14px}.eme-sidebar{width:266px;flex-shrink:0;padding:26px 14px;border-right:1px solid rgba(var(--v-border-color),var(--v-border-opacity));display:flex;flex-direction:column;gap:5px}.eme-brand{display:flex;align-items:center;gap:12px;padding:0 12px 30px}.eme-brand-icon{width:38px;height:38px;background:rgba(var(--v-theme-primary),.15);color:rgb(var(--v-theme-primary));border-radius:12px;display:grid;place-items:center;font-size:22px}.eme-brand strong,.eme-brand small,.eme-nav strong,.eme-nav small{display:block}.eme-brand small,.eme-nav small,.eme-hint,.eme-header p,.eme-card-heading p{color:rgba(var(--v-theme-on-surface),.62);font-size:12px}.eme-nav-label,.eme-kicker{color:rgb(var(--v-theme-primary));font-weight:700;font-size:11px;letter-spacing:1px;padding:0 14px;margin-bottom:10px}.eme-nav{display:flex;align-items:center;gap:12px;border:0;background:transparent;color:inherit;text-align:left;border-radius:12px;padding:13px 12px;cursor:pointer;width:100%}.eme-nav:hover,.eme-nav.selected{background:rgba(var(--v-theme-primary),.11)}.eme-nav.selected{color:rgb(var(--v-theme-primary))}.eme-nav>i:first-child{font-size:23px}.eme-nav span{flex:1;min-width:0}.eme-nav small{margin-top:4px}.eme-chevron{opacity:.4}.eme-sidebar-footer{margin-top:auto;padding:14px 12px;font-size:11px;color:rgba(var(--v-theme-on-surface),.6)}.eme-dot{display:inline-block;width:7px;height:7px;background:#39b981;border-radius:100%;margin-right:7px}.eme-main{flex:1;min-width:0;overflow:auto;padding:28px clamp(18px,4%,50px) 55px}.eme-header,.eme-card-heading{display:flex;align-items:center;justify-content:space-between;gap:18px}.eme-header{margin-bottom:26px}.eme-header h2{font-size:26px;line-height:1.3;margin:4px 0}.eme-header p,.eme-card-heading p{margin:4px 0}.eme-header-actions,.eme-actions,.eme-inline,.eme-options{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.eme-kicker{padding:0}.eme-card{padding:22px;border-radius:16px;border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));background:rgb(var(--v-theme-surface));margin-bottom:16px}.eme-card h3{font-size:17px;margin:0}.eme-card-heading{margin-bottom:18px}.eme-card label:not(.eme-check):not(.eme-result){display:block;min-width:0}.eme-card label{font-size:13px}.eme-card input:not([type=checkbox]),.eme-actions input{box-sizing:border-box;min-width:0;width:100%;margin-top:7px;padding:10px 12px;background:rgb(var(--v-theme-background));color:inherit;border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));border-radius:9px;outline:none}.eme-card input:focus{border-color:rgb(var(--v-theme-primary))}.eme-inline input{flex:1}.eme-button{border:0;border-radius:9px;padding:9px 14px;cursor:pointer;font-size:13px;white-space:nowrap}.eme-button:disabled{opacity:.5;cursor:not-allowed}.eme-button.primary{background:rgb(var(--v-theme-primary));color:rgb(var(--v-theme-on-primary))}.eme-button.secondary{background:rgba(var(--v-theme-primary),.1);color:rgb(var(--v-theme-primary))}.eme-button.danger{background:rgba(225,75,75,.13);color:#e45c5c}.eme-button.text{background:transparent;color:rgba(var(--v-theme-on-surface),.7)}.eme-options{margin:16px 0}.eme-check{display:flex;align-items:center;gap:7px;margin:16px 0}.eme-fields{display:grid;grid-template-columns:1fr 1fr;gap:12px}.eme-rule{display:flex;align-items:center;gap:9px;margin:10px 0}.eme-rule>input,.eme-rule>label{flex:1;min-width:0}.eme-rule>label input{display:block}.eme-move-rule{padding:10px 14px;border:1px dashed rgba(var(--v-border-color),var(--v-border-opacity));border-radius:12px;margin-bottom:12px}.eme-actions{margin-top:18px}.eme-actions input{max-width:240px!important;margin:0!important}.eme-results{max-height:280px;overflow:auto;margin:14px 0}.eme-result{display:flex;align-items:flex-start;gap:10px;padding:9px;border-bottom:1px solid rgba(var(--v-border-color),var(--v-border-opacity));overflow-wrap:anywhere}.eme-result strong,.eme-result small{display:block}.eme-result small{opacity:.6;margin-top:4px}.eme-message{padding:12px 16px;border-radius:9px;margin-bottom:16px;background:rgba(var(--v-theme-primary),.1)}.eme-error{background:rgba(225,75,75,.13);color:#e45c5c}.eme-success{background:rgba(44,176,112,.12);color:#239a69}.eme-stat{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px;border-radius:10px;background:rgba(var(--v-theme-primary),.07)}.eme-overlay{position:fixed;inset:0;z-index:2000;display:grid;place-items:center;background:rgba(0,0,0,.55);padding:20px}.eme-dialog{width:min(500px,100%);max-height:85vh;display:flex;flex-direction:column;background:rgb(var(--v-theme-surface));border-radius:16px;padding:20px}.eme-folder-list{overflow:auto;margin-top:15px;min-height:150px}.eme-folder{display:block;width:100%;text-align:left;border:0;background:transparent;color:inherit;padding:10px;border-radius:8px;cursor:pointer}.eme-folder:hover{background:rgba(var(--v-theme-primary),.1)}@media(max-width:760px){.eme-shell{flex-direction:column}.eme-sidebar{width:auto;border-right:0;border-bottom:1px solid rgba(var(--v-border-color),var(--v-border-opacity));padding:12px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.eme-brand,.eme-nav-label,.eme-sidebar-footer{display:none}.eme-nav{padding:9px}.eme-nav small,.eme-chevron{display:none!important}.eme-main{padding:18px}.eme-header,.eme-card-heading{align-items:flex-start;flex-wrap:wrap}.eme-fields{grid-template-columns:1fr}.eme-rule{flex-wrap:wrap}}
.eme-shell{position:relative}
.eme-overlay{position:absolute;box-sizing:border-box;inset:0;z-index:10;display:flex;align-items:center;justify-content:center;overflow:hidden}
.eme-dialog{box-sizing:border-box;flex:none;width:min(500px,100%);height:min(480px,calc(100% - 64px));max-height:calc(100% - 32px);min-height:0;overflow:hidden;box-shadow:0 18px 50px rgba(0,0,0,.25)}
.eme-folder-list{flex:1 1 0;min-height:0;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable}
.eme-shell{height:min(840px,calc(100dvh - 88px));min-height:0;overflow:hidden}
.eme-card{padding-top:14px}
.eme-sidebar,.eme-main{min-height:0}
.eme-sidebar{overflow-y:auto}
.eme-header h2,.eme-card-heading h3{font-weight:700}
.eme-card label.eme-switch-label:not(.eme-check):not(.eme-result){display:inline-flex;align-items:center;gap:10px;cursor:pointer;position:relative}
.eme-switch-input{position:absolute;opacity:0;width:1px;height:1px;margin:0}
.eme-switch-track{box-sizing:border-box;display:inline-block;position:relative;width:44px;height:24px;flex:none;border-radius:12px;background:rgba(var(--v-theme-on-surface),.3);transition:background .2s}
.eme-switch-track::after{content:"";position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.2);transition:transform .2s}
.eme-switch-input:checked + .eme-switch-track{background:rgb(var(--v-theme-primary))}
.eme-switch-input:checked + .eme-switch-track::after{transform:translateX(20px)}
.eme-switch-input:focus-visible + .eme-switch-track{outline:2px solid rgb(var(--v-theme-primary));outline-offset:3px}
.eme-settings-switches{gap:24px;margin:4px 0 22px}
.eme-settings-fields{display:grid;gap:18px}
.eme-settings-fields>label{display:block;min-width:0}
.eme-cleanup-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:12px 0}
.eme-cleanup-item,.eme-move-row{display:flex;align-items:center;min-width:0;gap:8px;padding:8px 10px;border:1px solid rgba(var(--v-border-color),var(--v-border-opacity));border-radius:10px}
.eme-folder-choice{min-width:0;overflow:hidden;text-overflow:ellipsis;flex:1;text-align:center;font-weight:600}
.eme-remove{flex:none;color:#e45c5c!important;padding:8px 10px}
.eme-move-row{margin:10px 0}
.eme-move-row .eme-folder-choice{max-width:230px}
.eme-move-label,.eme-move-arrow{flex:none;font-weight:700;color:rgba(var(--v-theme-on-surface),.65)}
.eme-move-arrow{font-size:18px}
.eme-chips{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:12px 0}.eme-chip{padding:5px 8px;border-radius:9px;background:rgba(var(--v-theme-primary),.1);overflow-wrap:anywhere}.eme-chip button{border:0;background:transparent;color:#e45c5c;cursor:pointer;font-size:18px;margin-left:5px}
@media(max-width:760px){.eme-cleanup-grid{grid-template-columns:1fr}.eme-move-row{flex-wrap:wrap}.eme-move-row .eme-folder-choice{max-width:none;min-width:80px}}
</style>
