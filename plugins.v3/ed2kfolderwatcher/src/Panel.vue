<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import './mobile-layout.css'
import { useMobilePage } from './useMobilePage'

const props = defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'Ed2kFolderWatcher' } })
const emit = defineEmits(['close'])
const panelRoot = ref(null)
useMobilePage(panelRoot)
const busy = ref(false)
const loaded = ref(false)
const error = ref('')
const notice = ref('')
const settings = reactive({ telegram_mode: 'user', telegram_aggregate_window: 600, cloud_enabled: false, cloud_scan_enabled: false, cloud_storage: '115网盘Plus', cloud_folder: '', cloud_recursive: true, cloud_cron: '*/10 * * * *', cloud_read_timeout: 300, cloud_monitor_enabled: false, cloud_life_source: 'shared' })
const cloud = reactive({ storages: [], folders: [], browsing: '', scanning: false, pending: 0, file: '', bytes_read: 0, size: 0, error: '' })
const account = reactive({ api_id: '', api_hash: '', phone: '', code: '', password: '' })
const auth = reactive({ logged_in: false, pending: false, password_required: false, api_hash_configured: false, expires_in: 0, retry_in: 0 })
let timer
let savedDelete = false
let savedDeleteSmall = false
const base = computed(() => `plugin/${props.pluginId}`)
const disabled = computed(() => busy.value || !loaded.value)
const configFields = ['enabled', 'folder', 'include_subdirectories', 'monitor_enabled', 'output_enabled', 'output', 'extensions', 'min_age', 'min_file_size_gb', 'delete_source', 'delete_small_files', 'cloud_delete_enabled', 'scan_enabled', 'cron', 'telegram_enabled', 'telegram_mode', 'telegram_chat_id', 'telegram_thread_id', 'telegram_aggregate', 'telegram_aggregate_window', 'cloud_enabled', 'cloud_scan_enabled', 'cloud_storage', 'cloud_folder', 'cloud_cron', 'cloud_read_timeout', 'cloud_monitor_enabled', 'cloud_life_source']
const textFields = [{ key: 'extensions', label: '扩展名过滤', hint: '留空处理全部文件，例如 mkv,mp4' }, { key: 'min_file_size_gb', label: '生成链接最小文件大小（GB）', type: 'number', step: 'any', placeholder: '例如0.5、1、1.5（GB）', hint: '小于此值不生成、不转发；留空或0不限，单位GB。' }]
const timingFields = [{ key: 'min_age', label: '文件稳定等待秒数', type: 'number', hint: '文件停止变化后等待时间，请监控完成目录。' }]

function unpack(response) {
  const result = response?.success === undefined && response?.data?.success !== undefined ? response.data : response
  if (result?.success === false) throw new Error(result.message || '请求失败')
  return result?.success !== undefined ? result.data : result?.data ?? result
}
async function request(path, body) {
  const result = unpack(body === undefined ? await props.api.get(`${base.value}/${path}`) : await props.api.post(`${base.value}/${path}`, body))
  return result || {}
}
async function work(callback) {
  if (busy.value) return
  busy.value = true
  error.value = notice.value = ''
  try { await callback() } catch (failure) { error.value = failure?.message || '请求失败，请稍后重试' } finally { busy.value = false }
}
function applyAuth(result) {
  Object.assign(auth, result)
  if (result.api_id !== undefined) account.api_id = String(result.api_id || '')
}
function applySettings(result) {
  if (!result.ok) throw new Error(result.message || '读取或保存设置失败')
  Object.assign(settings, result.settings)
  settings.telegram_mode = 'user'
  savedDelete = !!settings.delete_source
  savedDeleteSmall = !!settings.delete_small_files
}
async function refreshAuth() {
  const result = await request('telegram/status')
  applyAuth(result)
  if (!result.ok) throw new Error(result.message || '状态查询失败')
}
async function refreshCloud() {
  const result = await request('cloud/status')
  if (!result.ok) throw new Error(result.message || '网盘状态读取失败')
  Object.assign(cloud, result)
}
function cloudRoots() {
  return [...new Set(String(settings.cloud_folder || '').split(/[\r\n,]+/).map(path => path.trim()).filter(Boolean))]
}
function addCloudRoot() {
  settings.cloud_folder = [...new Set([...cloudRoots(), cloud.browsing])].join(',')
}
async function browseCloud(path = cloudRoots()[0] || '/') {
  await work(async () => {
    const result = await request('cloud/browse', { storage: settings.cloud_storage, path })
    if (!result.ok) throw new Error(result.message || '目录读取失败')
    cloud.folders = result.folders
    cloud.browsing = result.path
  })
}
async function runCloud() {
  if (!window.confirm('将按已保存的全部网盘目录增量读取完整文件内容，会消耗下载流量。启用并授权完成后删源时，符合条件的新记录可能删除源文件，确定继续？')) return
  await work(async () => {
    const result = await request('cloud/run', {})
    if (!result.ok) throw new Error(result.message || '网盘扫描失败')
    notice.value = result.message
    await refreshCloud()
  })
}
async function saveSettings() {
  if (auth.pending) { error.value = '请先完成或取消验证码登录，再保存其他设置'; return }
  if (typeof settings.include_subdirectories !== 'boolean') { error.value = '原本地与网盘子目录设置不一致，请先明确选择是否包含子目录，再保存'; return }
  if (settings.delete_source && !savedDelete && !window.confirm('确定启用完成后删除源文件？本地按原规则处理；115网盘还需单独授权。请先使用测试目录验证。')) return
  if (settings.delete_small_files && !savedDeleteSmall && !window.confirm('确定启用低于阈值文件自动删源？稳定等待后的非空本地文件小于阈值时将直接删除，不生成链接、不等待Telegram或TXT成功，删除不可恢复。请仅使用下载完成目录，并先用测试文件验证。')) return
  let confirmCloudDelete = false
  if (settings.cloud_delete_enabled) {
    if (!settings.delete_source || !settings.cloud_enabled || settings.cloud_storage !== '115网盘Plus') { error.value = '网盘删源仅支持115网盘Plus，请同时开启完成后删除源文件与网盘增量生成；CloudDrive仍只读'; return }
    confirmCloudDelete = window.confirm('确认授权当前115账号、网盘监控目录及子目录范围完成后删源？仅处理授权时新生成并完成所选输出的记录，历史链接不追溯删除。TG发送成功不代表接收机器人已转存，请自行确认备份策略；删除结果不明会暂停，不会自动重删。每次保存开启此项均需确认。')
    if (!confirmCloudDelete) return
  }
  await work(async () => {
    const result = await request('settings', { ...Object.fromEntries(configFields.map(key => [key, settings[key] ?? ''])), confirm_cloud_delete: confirmCloudDelete })
    applySettings(result)
    notice.value = result.message
  })
}
async function accountAction(operation, values = {}) {
  await work(async () => {
    try {
      const result = await request('telegram/action', { operation, values })
      applyAuth(result)
      if (!result.ok) throw new Error(result.message || '账号操作失败')
      notice.value = result.message
      if (result.logged_in || operation === 'logout' || operation === 'cancel') {
        account.phone = account.code = account.password = ''
      }
    } finally {
      account.code = account.password = ''
      if (operation === 'save') account.api_hash = ''
    }
  })
}
async function logout() {
  if (window.confirm('确定退出本插件的 Telegram 会话？这会撤销本插件授权，不会退出你手机上的账号。')) await accountAction('logout')
}
async function run() {
  if (settings.delete_source && !window.confirm('当前启用了成功后删除源文件，确定执行增量补扫和补发？')) return
  await work(async () => {
    const result = await request('run', {})
    if (!result.ok) throw new Error(result.message || '补扫失败')
    notice.value = result.message
  })
}
onMounted(() => {
  work(async () => {
    applySettings(await request('settings'))
    loaded.value = true
    await refreshAuth()
    await refreshCloud()
  })
  timer = setInterval(() => {
    auth.retry_in = Math.max(0, (auth.retry_in || 0) - 1)
    if (auth.pending) {
      auth.expires_in = Math.max(0, (auth.expires_in || 0) - 1)
      if (!auth.expires_in) { auth.pending = auth.password_required = false; account.code = account.password = ''; notice.value = '登录流程已过期，请重新发送验证码' }
    }
  }, 1000)
  cloudTimer = setInterval(() => { if (!busy.value && loaded.value) refreshCloud().catch(() => {}) }, 5000)
})
let cloudTimer
onUnmounted(() => {
  clearInterval(timer)
  clearInterval(cloudTimer)
  account.api_hash = account.phone = account.code = account.password = ''
})
</script>

<template>
  <section ref="panelRoot" class="ed2k-panel ed2k-mobile-page" :aria-busy="busy">
    <header class="ed2k-header">
      <div><h2>自动生成ED2K链接</h2><p>实时目录监控 · 增量生成 · Telegram 自动发送</p></div>
      <div class="header-actions">
        <button type="button" :disabled="disabled || auth.pending" @click="saveSettings">保存插件设置</button>
        <button type="button" class="secondary" :disabled="disabled || !settings.enabled || auth.pending" @click="run">增量补扫／补发链接</button>
        <button type="button" class="secondary" @click="emit('close')">关闭</button>
      </div>
    </header>
    <div v-if="error" role="alert" class="message error">{{ error }}</div>
    <div v-if="notice" role="status" class="message success">{{ notice }}</div>
    <p v-if="!loaded">正在加载设置…</p>

    <article class="card" data-testid="common-settings">
      <h3>通用设置</h3>
      <div class="switches"><label class="switch"><input v-model="settings.delete_source" type="checkbox" :disabled="disabled" @change="settings.delete_source || (settings.cloud_delete_enabled = false)">完成后删除源文件</label><label class="switch"><input v-model="settings.cloud_delete_enabled" type="checkbox" :disabled="disabled">单独授权115网盘删源（默认关闭）</label></div>
      <p v-if="settings.delete_source" class="warning">仅在输出全部成功且文件未变化后删除；115还需单独授权，CloudDrive只读。TG发送不代表接收方已备份。</p>
      <p v-if="settings.cloud_delete_enabled" class="warning">每次保存需确认115授权；账号、目录或子目录变化后需重新授权。历史记录不追删，结果不明会暂停。</p>
      <div class="switches"><label class="switch"><input v-model="settings.enabled" type="checkbox" :disabled="disabled">启用插件</label><label class="switch"><input v-model="settings.output_enabled" type="checkbox" :disabled="disabled">生成本地 TXT</label><label class="switch"><input v-model="settings.include_subdirectories" :indeterminate="settings.include_subdirectories === null" type="checkbox" :disabled="disabled">包含子目录</label></div>
      <p v-if="settings.include_subdirectories === null" class="warning">本地与网盘的子目录设置不一致，请选择后保存。</p>
      <div class="fields"><label v-for="field in textFields" :key="field.key">{{ field.label }}<input v-model="settings[field.key]" :type="field.type || 'text'" :step="field.step" :min="field.type === 'number' ? 0 : undefined" :placeholder="field.placeholder" :disabled="disabled"><small>{{ field.hint }}</small></label></div>
      <div class="fields timing-fields"><label v-for="field in timingFields" :key="field.key">{{ field.label }}<input v-model="settings[field.key]" :type="field.type || 'text'" :step="field.step" :min="field.type === 'number' ? 0 : undefined" :placeholder="field.placeholder" :disabled="disabled"><small>{{ field.hint }}</small></label><label>TXT 输出路径<input v-model="settings.output" :disabled="disabled || !settings.output_enabled" placeholder="留空自动选择 ed2k.txt 路径"><small>本地与网盘链接合并输出；留空自动选择路径。关闭后不删除已有文件。</small></label></div>
      <p>Telegram和个人账号授权在下方配置；本地与网盘共用一个计算线程。</p>
    </article>

    <article class="card" data-testid="local-source">
      <h3>本地监控</h3>
      <div class="switches"><label class="switch"><input v-model="settings.monitor_enabled" type="checkbox" :disabled="disabled">实时监控文件</label><label class="switch"><input v-model="settings.scan_enabled" type="checkbox" :disabled="disabled">本地定时增量补扫</label></div>
      <div class="fields"><label>本地监控目录<input v-model="settings.folder" class="monitor-roots" :disabled="disabled" placeholder="/media/movies,/media/tv"><small>容器内绝对路径，多个用英文逗号分隔，最多32个。</small></label><label>本地补扫计划<input v-model="settings.cron" :disabled="disabled || !settings.scan_enabled" placeholder="*/10 * * * *"><small>默认关闭，用于补漏。</small></label></div>
      <label class="switch"><input v-model="settings.delete_small_files" type="checkbox" :disabled="disabled">低于阈值文件自动删源</label>
      <p class="warning" v-if="settings.delete_small_files">低于阈值的非空本地文件会在稳定等待后直接删除，不生成、不转发，且不可恢复；空文件和网盘文件不处理。</p>
    </article>

    <article class="card" data-testid="cloud-source">
      <h3>网盘监控</h3>
      <p>无需挂载网盘；通过已登录的存储插件分块读取，不保存完整临时文件。与本地共用一个计算线程，默认保留云端文件。115删源需单独授权，CloudDrive只读。</p>
      <div class="switches"><label class="switch"><input v-model="settings.cloud_enabled" type="checkbox" :disabled="disabled">启用网盘增量生成</label><label class="switch"><input v-model="settings.cloud_monitor_enabled" type="checkbox" :disabled="disabled || settings.cloud_storage !== '115网盘Plus'">115生活事件监控（近实时）</label><label class="switch"><input v-model="settings.cloud_scan_enabled" type="checkbox" :disabled="disabled">网盘定时增量补扫</label></div>
      <div class="fields"><label>网盘存储<select v-model="settings.cloud_storage" :disabled="disabled" @change="cloud.folders = []; cloud.browsing = ''; if (settings.cloud_storage !== '115网盘Plus') { settings.cloud_monitor_enabled = false; settings.cloud_delete_enabled = false }"><option value="115网盘Plus">115网盘Plus</option><option value="CloudDrive储存">CloudDrive储存</option></select><small>已配置：{{ cloud.storages.map(item => item.name).join('、') || '未检测到支持的存储' }}</small></label><label>网盘监控目录<input v-model="settings.cloud_folder" class="monitor-roots" :disabled="disabled" placeholder="/电影,/电视剧"><small>同一存储内的绝对路径，多个用英文逗号分隔，最多32个。</small></label><label>网盘补扫计划<input v-model.trim="settings.cloud_cron" :disabled="disabled || !settings.cloud_scan_enabled" placeholder="*/10 * * * *"><small>开启定时补扫后生效，默认每10分钟。</small></label><label>网盘读取超时（秒）<input v-model="settings.cloud_read_timeout" type="number" min="30" max="1800" :disabled="disabled"><small>单次网络读取等待上限，默认300秒。</small></label><label v-if="settings.cloud_monitor_enabled">115生活事件来源<select v-model="settings.cloud_life_source" :disabled="disabled"><option value="shared">复用115插件监听（不额外请求生活接口）</option><option value="direct">独立轮询（每分钟一次生活接口）</option></select><small>复用模式需115插件监听和账号一致。</small></label></div>
      <p v-if="settings.cloud_monitor_enabled">只将事件对应且位于监控目录内的单个文件加入队列，不扫描或补算目录中的已有文件；每个事件最多复查10次。</p>
      <div class="actions"><button type="button" class="secondary" :disabled="disabled" @click="browseCloud()">浏览网盘目录</button><button type="button" class="secondary" :disabled="disabled || !settings.enabled || !settings.cloud_enabled" @click="runCloud">立即网盘增量扫描／补发</button><button type="button" class="secondary" :disabled="disabled" @click="work(refreshCloud)">刷新网盘进度</button></div>
      <div v-if="cloud.browsing" class="cloud-browser"><p>当前浏览：{{ cloud.browsing }}</p><div class="actions"><button type="button" class="secondary" :disabled="disabled" @click="browseCloud(cloud.browsing.split('/').slice(0, -1).join('/') || '/')">上一级</button><button type="button" :disabled="disabled" @click="addCloudRoot">添加此目录</button></div><div class="cloud-folders"><button v-for="folder in cloud.folders" :key="folder.path" type="button" class="secondary" :disabled="disabled" @click="browseCloud(folder.path)">{{ folder.name }}</button><small v-if="!cloud.folders.length">此目录下没有子目录。</small></div></div>
      <p role="status">{{ cloud.scanning ? '正在发现文件' : '等待下一次扫描' }} · 待处理 {{ cloud.pending }} 个<span v-if="cloud.file"> · {{ cloud.reconnecting ? '断线重连／续读中' : '正在计算' }}：{{ cloud.file }}（{{ cloud.size ? Math.floor(cloud.bytes_read / cloud.size * 100) : 0 }}%）<span v-if="cloud.stream_retries"> · 已重连 {{ cloud.stream_retries }} 次</span></span></p>
      <p v-if="cloud.error" class="warning">{{ cloud.error }}</p>
      <p v-if="cloud.retry_at" class="warning">失败任务下次允许重试：{{ new Date(cloud.retry_at * 1000).toLocaleString() }}；连续失败按 5、10、20…分钟退避，最长一小时。</p>
      <p v-if="cloud.life_enabled">生活事件已启用 · {{ cloud.life_source === 'shared' ? '复用115插件' : '独立轮询' }} · 已排队 {{ cloud.life_events || 0 }} 个新增文件<span v-if="cloud.life_last_poll"> · 最近检查 {{ new Date(cloud.life_last_poll * 1000).toLocaleString() }}</span></p>
      <p v-if="cloud.life_error" class="warning">{{ cloud.life_error }}</p>
      <p v-if="cloud.life_source === 'shared' && cloud.life_source_latest">复用事件库最新事件：{{ new Date(cloud.life_source_latest * 1000).toLocaleString() }}</p>
      <p v-if="cloud.life_source_notice" class="warning">{{ cloud.life_source_notice }}</p>
      <p v-if="cloud.life_retry_at" class="warning">生活事件下次允许检查：{{ new Date(cloud.life_retry_at * 1000).toLocaleString() }}；失败时自动退避，不持续请求。</p>
      <p v-if="cloud.life_truncated" class="warning">{{ cloud.life_source === 'shared' ? '本批读取64条新增事件，下轮按游标继续读取；不额外请求115。批量事件可能延迟，请保留定时兜底。' : '本次生活事件达到64条上限，可能遗漏更早事件；请保留定时兜底，必要时手动补扫。' }}</p>
      <small>网盘共用通用设置和转发设置。修改后点击右上角保存；关闭定时补扫不影响已排队任务。</small>
    </article>

    <article class="card">
      <h3>ED2K链接自动转发</h3>
      <div class="switches telegram-switches"><label class="switch"><input v-model="settings.telegram_enabled" type="checkbox" :disabled="disabled">发送 ED2K 链接到 Telegram</label><label class="switch"><input v-model="settings.telegram_aggregate" type="checkbox" :disabled="disabled || !settings.telegram_enabled">聚合转发</label></div>
      <small>同剧名同季数合并为一条消息。</small>
      <div v-if="settings.telegram_aggregate" class="fields telegram-wait-field"><label>聚合转发延迟等待秒数<input v-model.number="settings.telegram_aggregate_window" type="number" min="0" max="3600" :disabled="disabled"><small>批次完成后继续等待；期间发现的同剧同季文件会合并，默认600秒。</small></label></div>
      <!-- <select v-model="settings.telegram_mode"><option value="bot">Bot Token（机器人）</option></select> -->
      <!-- <label>发送机器人 Token<input v-model="settings.telegram_token" type="password" autocomplete="new-password"></label> -->
      <div class="fields telegram-targets"><label>接收目标（支持多个）<textarea v-model="settings.telegram_chat_id" :disabled="disabled" rows="1" placeholder="@接收机器人, @频道用户名, -1001234567890" /><small>支持用户名、Chat ID或 t.me 链接，最多50个；逗号、分号或换行分隔。</small><small>指定话题：@群组用户名|123。</small></label><label>默认话题线程 ID（可选）<input v-model="settings.telegram_thread_id" :disabled="disabled" placeholder="普通私聊、频道留空"><small>用于未单独指定话题的目标。</small></label></div>
      <p>逐个发送，间隔至少3秒；聚合时同剧同季链接换行合并，全部目标成功后才删源。</p>
      <p>使用个人账号发送，仅发送链接。修改目标后请保存设置。</p>
    </article>

    <article class="card" data-testid="telegram-account">
      <div class="card-header"><h3>Telegram 账号</h3><button type="button" :disabled="disabled || auth.pending" @click="accountAction('save', { api_id: account.api_id, api_hash: account.api_hash })">保存账号设置</button></div>
      <p>使用个人账号发送；API ID和API Hash可从 my.telegram.org 获取，无需终端命令。</p>
      <div class="fields"><label>API ID<input v-model.trim="account.api_id" inputmode="numeric" :disabled="disabled || auth.pending" autocomplete="off"></label><label>API Hash<input v-model.trim="account.api_hash" type="password" :disabled="disabled || auth.pending" autocomplete="new-password" :placeholder="auth.api_hash_configured ? '已配置；留空保持不变' : '32 位字符串'"></label></div>
      <p class="account-status" role="status">{{ auth.logged_in ? 'Telegram 已登录 · 可以发送链接' : auth.pending ? (auth.password_required ? '等待两步验证密码' : '等待登录验证码') : 'Telegram 尚未登录' }}<span v-if="auth.pending"> · 剩余 {{ auth.expires_in }} 秒</span></p>
      <div v-if="!auth.logged_in" class="fields"><label v-if="!auth.password_required">手机号（国际格式）<input v-model.trim="account.phone" type="tel" :disabled="disabled || auth.pending" placeholder="+8613800000000" autocomplete="off"></label><label v-if="auth.pending && !auth.password_required">验证码<input v-model="account.code" inputmode="numeric" type="password" autocomplete="off" :disabled="disabled" placeholder="查看 Telegram 官方服务消息"></label><label v-if="auth.password_required">两步验证密码<input v-model="account.password" type="password" autocomplete="off" :disabled="disabled" placeholder="仅本次登录使用，不保存"></label></div>
      <div class="actions"><button v-if="!auth.logged_in && !auth.pending" type="button" :disabled="disabled || !auth.api_hash_configured || auth.retry_in > 0 || !account.phone" @click="accountAction('send_code', { phone: account.phone })">{{ auth.retry_in > 0 ? `请等待 ${auth.retry_in} 秒` : '发送验证码' }}</button><button v-if="auth.pending" type="button" :disabled="disabled || (auth.password_required ? !account.password : !account.code)" @click="accountAction('sign_in', { code: account.code, password: account.password })">{{ auth.password_required ? '验证密码并登录' : '验证并登录' }}</button><button v-if="auth.pending" type="button" class="secondary" :disabled="disabled" @click="accountAction('cancel')">取消本次登录</button><button v-if="auth.logged_in || auth.session_present" type="button" class="danger" :disabled="disabled" @click="logout">退出登录</button><button type="button" class="secondary" :disabled="disabled" @click="work(refreshAuth)">刷新登录状态</button></div>
      <small>API Hash不回显；验证码和密码仅用于本次登录，Session只保存在本地。</small>
    </article>
  </section>
</template>

<style scoped>
.cloud-browser{border:1px solid rgba(var(--v-theme-on-surface),.16);border-radius:10px;padding:12px}.cloud-folders{display:flex;flex-wrap:wrap;gap:10px;max-height:220px;overflow:auto}.cloud-folders button{overflow-wrap:anywhere;max-width:100%}
.fields.telegram-targets textarea{line-height:20px;overflow:hidden;scrollbar-width:none}.telegram-targets textarea::-webkit-scrollbar{display:none}.telegram-switches{align-items:center;flex-wrap:nowrap}
.fields textarea{width:100%;border:1px solid rgba(var(--v-theme-on-surface),.25);border-radius:9px;padding:13px 15px;background:rgba(var(--v-theme-on-surface),.025);color:inherit;font:inherit;resize:vertical}.telegram-targets textarea,.telegram-targets input{height:48px;min-height:48px;box-sizing:border-box}.fields textarea:focus{outline:2px solid rgb(var(--v-theme-primary));outline-offset:2px}.monitor-roots{height:48px;min-height:48px;box-sizing:border-box}.timing-fields{align-items:start}
.header-actions{display:flex;align-items:center;justify-content:flex-end;gap:12px;flex-wrap:wrap;margin-left:auto}.header-actions button{white-space:nowrap}
.ed2k-panel{padding:24px;max-width:1220px;margin:auto;color:rgb(var(--v-theme-on-surface));overflow:auto;max-height:85dvh;font-size:15px}.ed2k-header,.card-header{display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap}.ed2k-header{margin-bottom:24px}h2,h3{margin:0;font-weight:650}h2{font-size:24px}h3{font-size:20px}p{margin:16px 0;color:rgba(var(--v-theme-on-surface),.72);line-height:1.7}.card{padding:26px;margin-bottom:22px;border:1px solid rgba(var(--v-theme-on-surface),.16);border-radius:15px;background:rgba(var(--v-theme-on-surface),.025)}.fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px;margin:22px 0}.fields label{display:flex;flex-direction:column;gap:10px;min-width:0}.fields input,.fields select{width:100%;border:1px solid rgba(var(--v-theme-on-surface),.25);border-radius:9px;padding:13px 15px;min-height:48px;background:rgba(var(--v-theme-on-surface),.025);color:inherit;font:inherit}.fields select option{background:rgb(var(--v-theme-surface));color:rgb(var(--v-theme-on-surface))}.fields input:focus,.fields select:focus{outline:2px solid rgb(var(--v-theme-primary));outline-offset:2px}small{color:rgba(var(--v-theme-on-surface),.62);line-height:1.7}.switches{display:flex;flex-wrap:wrap;gap:20px;margin:22px 0}.switch{display:flex;align-items:center;gap:10px;cursor:pointer}.switch input{width:18px;height:18px;accent-color:rgb(var(--v-theme-primary))}.actions{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}button{padding:12px 20px;border:0;border-radius:9px;color:rgb(var(--v-theme-on-primary));background:rgb(var(--v-theme-primary));font:inherit;cursor:pointer;min-height:44px}.secondary{background:rgba(var(--v-theme-primary),.13);color:rgb(var(--v-theme-primary))}.danger{color:#ef8585;background:rgba(220,60,60,.15)}button:disabled{opacity:.45;cursor:not-allowed}.message{padding:16px 20px;border-radius:10px;margin:16px 0;overflow-wrap:anywhere}.error{background:rgba(230,65,65,.14);color:#ed9696}.success{background:rgba(60,180,110,.14);color:#79c99e}.warning{color:#e7b36a}.account-status{font-weight:600}@media(max-width:650px){.ed2k-panel{padding:14px}.card{padding:18px}.fields{grid-template-columns:1fr;gap:20px}.card-header{align-items:flex-start}.telegram-switches{gap:8px}.telegram-switches .switch{white-space:nowrap;font-size:12px}button{padding:11px 15px}}
</style>
