<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import './mobile-layout.css'

const props = defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'Ed2kFolderWatcher' } })
const emit = defineEmits(['close'])
const busy = ref(false)
const loaded = ref(false)
const error = ref('')
const notice = ref('')
const settings = reactive({ telegram_mode: 'user', cloud_enabled: false, cloud_scan_enabled: false, cloud_storage: '115网盘Plus', cloud_folder: '', cloud_recursive: true, cloud_cron: '*/10 * * * *', cloud_read_timeout: 300, cloud_monitor_enabled: false, cloud_life_source: 'shared' })
const cloud = reactive({ storages: [], folders: [], browsing: '', scanning: false, pending: 0, file: '', bytes_read: 0, size: 0, error: '' })
const account = reactive({ api_id: '', api_hash: '', phone: '', code: '', password: '' })
const auth = reactive({ logged_in: false, pending: false, password_required: false, api_hash_configured: false, expires_in: 0, retry_in: 0 })
let timer
let savedDelete = false
const base = computed(() => `plugin/${props.pluginId}`)
const disabled = computed(() => busy.value || !loaded.value)
const configFields = ['enabled', 'folder', 'recursive', 'monitor_enabled', 'output_enabled', 'output', 'extensions', 'min_age', 'delete_source', 'scan_enabled', 'cron', 'telegram_enabled', 'telegram_mode', 'telegram_chat_id', 'telegram_thread_id', 'cloud_enabled', 'cloud_scan_enabled', 'cloud_storage', 'cloud_folder', 'cloud_recursive', 'cloud_cron', 'cloud_read_timeout', 'cloud_monitor_enabled', 'cloud_life_source']
const textFields = [{ key: 'extensions', label: '扩展名过滤', hint: '本地与网盘共用；留空处理全部文件，例如 mkv,mp4' }, { key: 'min_age', label: '文件稳定等待秒数', type: 'number', hint: '本地与网盘共用；稳定等待不能保证上传已经完成，请选择完成目录' }]

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
async function browseCloud(path = settings.cloud_folder || '/') {
  await work(async () => {
    const result = await request('cloud/browse', { storage: settings.cloud_storage, path })
    if (!result.ok) throw new Error(result.message || '目录读取失败')
    cloud.folders = result.folders
    cloud.browsing = result.path
  })
}
async function runCloud() {
  if (!window.confirm('将按已保存的网盘目录增量读取完整文件内容。不会删除云端文件，但会消耗下载流量，确定继续？')) return
  await work(async () => {
    const result = await request('cloud/run', {})
    if (!result.ok) throw new Error(result.message || '网盘扫描失败')
    notice.value = result.message
    await refreshCloud()
  })
}
async function saveSettings() {
  if (auth.pending) { error.value = '请先完成或取消验证码登录，再保存其他设置'; return }
  if (settings.delete_source && !savedDelete && !window.confirm('确定启用生成成功后删除源文件？请先使用测试目录验证。')) return
  await work(async () => {
    const result = await request('settings', Object.fromEntries(configFields.map(key => [key, settings[key] ?? ''])))
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
  <section class="ed2k-panel ed2k-mobile-page" :aria-busy="busy">
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
      <div class="switches"><label class="switch"><input v-model="settings.enabled" type="checkbox" :disabled="disabled">启用插件</label><label class="switch"><input v-model="settings.output_enabled" type="checkbox" :disabled="disabled">生成本地 TXT</label></div>
      <div class="fields"><label v-for="field in textFields" :key="field.key">{{ field.label }}<input v-model="settings[field.key]" :type="field.type || 'text'" :min="field.type === 'number' ? 0 : undefined" :disabled="disabled"><small>{{ field.hint }}</small></label></div>
      <div class="fields"><label>TXT 输出路径<input v-model="settings.output" :disabled="disabled || !settings.output_enabled" placeholder="留空自动选择 ed2k.txt 路径"><small>本地与网盘链接合并输出；有本地目录时默认写入该目录，仅网盘时写入插件数据目录。关闭 TXT 不删除已有文件。</small></label></div>
      <p>Telegram 发送与个人账号授权也适用于两种来源，统一在下方配置；两种来源共用一个计算线程。</p>
    </article>

    <article class="card" data-testid="local-source">
      <h3>本地监控</h3>
      <div class="switches"><label class="switch"><input v-model="settings.monitor_enabled" type="checkbox" :disabled="disabled">实时监控文件</label><label class="switch"><input v-model="settings.recursive" type="checkbox" :disabled="disabled">包含本地子目录</label><label class="switch"><input v-model="settings.delete_source" type="checkbox" :disabled="disabled">成功后删除本地源文件</label><label class="switch"><input v-model="settings.scan_enabled" type="checkbox" :disabled="disabled">本地定时增量补扫</label></div>
      <div class="fields"><label>本地监控目录<input v-model="settings.folder" :disabled="disabled" placeholder="/media/movies"><small>MoviePilot 容器内路径；留空可仅处理网盘来源。</small></label><label>本地补扫计划<input v-model="settings.cron" :disabled="disabled || !settings.scan_enabled" placeholder="*/10 * * * *"><small>默认关闭，网络挂载漏事件时可启用兜底。</small></label></div>
      <p v-if="settings.delete_source" class="warning">已开启本地删除源文件：仅在所选输出成功且文件未变化时删除；两种输出都关闭时不删除。此开关对网盘无效，网盘文件始终保留。</p>
    </article>

    <article class="card" data-testid="cloud-source">
      <h3>网盘监控</h3>
      <p>无需将网盘挂载为本地目录；复用已登录的存储插件，完整数据经网络分块读取计算，不保存完整临时文件。本地和网盘共用一个工作线程；不会并发计算多个文件，也不会删除云端文件。</p>
      <div class="switches"><label class="switch"><input v-model="settings.cloud_enabled" type="checkbox" :disabled="disabled">启用网盘增量生成</label><label class="switch"><input v-model="settings.cloud_recursive" type="checkbox" :disabled="disabled">包含网盘子目录</label><label class="switch"><input v-model="settings.cloud_scan_enabled" type="checkbox" :disabled="disabled">网盘定时增量补扫</label><label class="switch"><input v-model="settings.cloud_monitor_enabled" type="checkbox" :disabled="disabled || settings.cloud_storage !== '115网盘Plus'">115生活事件监控（近实时）</label></div>
      <div class="fields"><label>网盘存储<select v-model="settings.cloud_storage" :disabled="disabled" @change="cloud.folders = []; cloud.browsing = ''; if (settings.cloud_storage !== '115网盘Plus') settings.cloud_monitor_enabled = false"><option value="115网盘Plus">115网盘Plus</option><option value="CloudDrive储存">CloudDrive储存</option></select><small>当前已配置：{{ cloud.storages.map(item => item.name).join('、') || '未检测到支持的存储，请先启用并登录对应插件' }}</small></label><label>网盘监控目录<input v-model.trim="settings.cloud_folder" :disabled="disabled" placeholder="/待生成链接"><small>网盘内的绝对路径，不是容器本地路径。首次扫描处理目录内现有文件，建议先选小目录测试。</small></label><label>网盘补扫计划<input v-model.trim="settings.cloud_cron" :disabled="disabled || !settings.cloud_scan_enabled" placeholder="*/10 * * * *"><small>仅开启“网盘定时增量补扫”后生效；关闭不影响手动补扫和生活事件触发。默认计划每 10 分钟。</small></label><label>网盘读取超时（秒）<input v-model="settings.cloud_read_timeout" type="number" min="30" max="1800" :disabled="disabled"><small>单次网络读取等待上限，默认 300 秒，不是整个文件计算时限。临时断线最多重连两次；服务提供强校验标识或SHA1时尝试安全续读。</small></label><label v-if="settings.cloud_monitor_enabled">115生活事件来源<select v-model="settings.cloud_life_source" :disabled="disabled"><option value="shared">复用115插件监听（不额外请求生活接口）</option><option value="direct">独立轮询（每分钟一次生活接口）</option></select><small>复用模式要求115插件生活事件监听正在运行，且与当前存储登录同一账号；不会擅自切换为独立请求。</small></label></div>
      <p v-if="settings.cloud_monitor_enabled">每分钟检查一次开启后产生的新生活事件，只将事件对应且位于监控目录内的单个文件加入队列，不扫描或补算目录中的已有文件。新增事件文件尚未整理到监控目录时，按文件ID保留最多15分钟、最多10次复查；后续改名事件仅唤醒已有候选，不处理单独改名的历史文件。每轮最多检查8个文件；保存配置或重启清空候选，漏报文件可手动或定时补扫。</p>
      <div class="actions"><button type="button" class="secondary" :disabled="disabled" @click="browseCloud()">浏览网盘目录</button><button type="button" class="secondary" :disabled="disabled || !settings.enabled || !settings.cloud_enabled" @click="runCloud">立即网盘增量扫描／补发</button><button type="button" class="secondary" :disabled="disabled" @click="work(refreshCloud)">刷新网盘进度</button></div>
      <div v-if="cloud.browsing" class="cloud-browser"><p>当前浏览：{{ cloud.browsing }}</p><div class="actions"><button type="button" class="secondary" :disabled="disabled" @click="browseCloud(cloud.browsing.split('/').slice(0, -1).join('/') || '/')">上一级</button><button type="button" :disabled="disabled" @click="settings.cloud_folder = cloud.browsing">选择此目录</button></div><div class="cloud-folders"><button v-for="folder in cloud.folders" :key="folder.path" type="button" class="secondary" :disabled="disabled" @click="browseCloud(folder.path)">{{ folder.name }}</button><small v-if="!cloud.folders.length">此目录下没有子目录。</small></div></div>
      <p role="status">{{ cloud.scanning ? '正在发现文件' : '等待下一次扫描' }} · 待处理 {{ cloud.pending }} 个<span v-if="cloud.file"> · {{ cloud.reconnecting ? '断线重连／续读中' : '正在计算' }}：{{ cloud.file }}（{{ cloud.size ? Math.floor(cloud.bytes_read / cloud.size * 100) : 0 }}%）<span v-if="cloud.stream_retries"> · 已重连 {{ cloud.stream_retries }} 次</span></span></p>
      <p v-if="cloud.error" class="warning">{{ cloud.error }}</p>
      <p v-if="cloud.retry_at" class="warning">失败任务下次允许重试：{{ new Date(cloud.retry_at * 1000).toLocaleString() }}；连续失败按 5、10、20…分钟退避，最长一小时。</p>
      <p v-if="cloud.life_enabled">生活事件已启用 · {{ cloud.life_source === 'shared' ? '复用115插件' : '独立轮询' }} · 已排队 {{ cloud.life_events || 0 }} 个新增文件<span v-if="cloud.life_last_poll"> · 最近检查 {{ new Date(cloud.life_last_poll * 1000).toLocaleString() }}</span></p>
      <p v-if="cloud.life_error" class="warning">{{ cloud.life_error }}</p>
      <p v-if="cloud.life_retry_at" class="warning">生活事件下次允许检查：{{ new Date(cloud.life_retry_at * 1000).toLocaleString() }}；失败时自动退避，不持续请求。</p>
      <p v-if="cloud.life_truncated" class="warning">本次生活事件达到64条上限，可能遗漏更早事件；请保留定时兜底，必要时手动补扫。</p>
      <small>网盘复用“通用设置”及“ED2K链接自动转发”设置。修改后先点右上角“保存插件设置”。关闭定时补扫不会取消已排队任务或失败重试。续读只保留内存中的哈希状态，不保存完整临时文件；服务不支持安全续读、重连耗尽或插件重启后，下次任务仍需从头读取，不生成不完整链接。</small>
    </article>

    <article class="card">
      <h3>ED2K链接自动转发</h3>
      <div class="switches"><label class="switch"><input v-model="settings.telegram_enabled" type="checkbox" :disabled="disabled">发送 ED2K 链接到 Telegram</label></div>
      <!-- <select v-model="settings.telegram_mode"><option value="bot">Bot Token（机器人）</option></select> -->
      <!-- <label>发送机器人 Token<input v-model="settings.telegram_token" type="password" autocomplete="new-password"></label> -->
      <div class="fields telegram-targets"><label>接收目标（支持多个）<textarea v-model="settings.telegram_chat_id" :disabled="disabled" rows="1" placeholder="@接收机器人, @频道用户名, -1001234567890" /><small>每行一个，或用逗号、分号分隔；支持 @用户名、数字 Chat ID、公开 t.me 链接，最多 50 个。重复目标自动合并。</small><small>单独指定群组话题：@群组用户名|123；末尾加 | 可不使用默认话题。</small></label><label>默认话题线程 ID（可选）<input v-model="settings.telegram_thread_id" :disabled="disabled" placeholder="普通私聊、频道留空"><small>作用于未单独指定话题的目标；混合发送频道、机器人、群组时建议留空。</small></label></div>
      <p>逐个发送，每次间隔至少 3 秒；仅补发失败或新增目标。启用删源时，所有目标发送成功后才删除源文件。</p>
      <p>发送方式：个人账号。只发送链接，不加标题；请在下方完成账号授权。修改接收目标后请点击“保存插件设置”。</p>
    </article>

    <article class="card" data-testid="telegram-account">
      <div class="card-header"><h3>Telegram 账号</h3><button type="button" :disabled="disabled || auth.pending" @click="accountAction('save', { api_id: account.api_id, api_hash: account.api_hash })">保存账号设置</button></div>
      <p>使用你自己的 Telegram 账号发送。请从 my.telegram.org 的 API development tools 获取 API ID 和 API Hash。无需终端命令。</p>
      <div class="fields"><label>API ID<input v-model.trim="account.api_id" inputmode="numeric" :disabled="disabled || auth.pending" autocomplete="off"></label><label>API Hash<input v-model.trim="account.api_hash" type="password" :disabled="disabled || auth.pending" autocomplete="new-password" :placeholder="auth.api_hash_configured ? '已配置；留空保持不变' : '32 位字符串'"></label></div>
      <p class="account-status" role="status">{{ auth.logged_in ? 'Telegram 已登录 · 可以发送链接' : auth.pending ? (auth.password_required ? '等待两步验证密码' : '等待登录验证码') : 'Telegram 尚未登录' }}<span v-if="auth.pending"> · 剩余 {{ auth.expires_in }} 秒</span></p>
      <div v-if="!auth.logged_in" class="fields"><label v-if="!auth.password_required">手机号（国际格式）<input v-model.trim="account.phone" type="tel" :disabled="disabled || auth.pending" placeholder="+8613800000000" autocomplete="off"></label><label v-if="auth.pending && !auth.password_required">验证码<input v-model="account.code" inputmode="numeric" type="password" autocomplete="off" :disabled="disabled" placeholder="查看 Telegram 官方服务消息"></label><label v-if="auth.password_required">两步验证密码<input v-model="account.password" type="password" autocomplete="off" :disabled="disabled" placeholder="仅本次登录使用，不保存"></label></div>
      <div class="actions"><button v-if="!auth.logged_in && !auth.pending" type="button" :disabled="disabled || !auth.api_hash_configured || auth.retry_in > 0 || !account.phone" @click="accountAction('send_code', { phone: account.phone })">{{ auth.retry_in > 0 ? `请等待 ${auth.retry_in} 秒` : '发送验证码' }}</button><button v-if="auth.pending" type="button" :disabled="disabled || (auth.password_required ? !account.password : !account.code)" @click="accountAction('sign_in', { code: account.code, password: account.password })">{{ auth.password_required ? '验证密码并登录' : '验证并登录' }}</button><button v-if="auth.pending" type="button" class="secondary" :disabled="disabled" @click="accountAction('cancel')">取消本次登录</button><button v-if="auth.logged_in || auth.session_present" type="button" class="danger" :disabled="disabled" @click="logout">退出登录</button><button type="button" class="secondary" :disabled="disabled" @click="work(refreshAuth)">刷新登录状态</button></div>
      <small>API Hash 保存后不回显；验证码和密码仅用于本次登录，不写入普通插件配置或日志。Session 只保存在本地，请通过可信网络或 HTTPS 使用本页面。</small>
    </article>
  </section>
</template>

<style scoped>
.cloud-browser{border:1px solid rgba(var(--v-theme-on-surface),.16);border-radius:10px;padding:12px}.cloud-folders{display:flex;flex-wrap:wrap;gap:10px;max-height:220px;overflow:auto}.cloud-folders button{overflow-wrap:anywhere;max-width:100%}
.fields.telegram-targets textarea{line-height:20px;overflow:hidden;scrollbar-width:none}.telegram-targets textarea::-webkit-scrollbar{display:none}
.fields textarea{width:100%;border:1px solid rgba(var(--v-theme-on-surface),.25);border-radius:9px;padding:13px 15px;background:rgba(var(--v-theme-on-surface),.025);color:inherit;font:inherit;resize:vertical}.telegram-targets textarea,.telegram-targets input{height:48px;min-height:48px;box-sizing:border-box}.fields textarea:focus{outline:2px solid rgb(var(--v-theme-primary));outline-offset:2px}
.header-actions{display:flex;align-items:center;justify-content:flex-end;gap:12px;flex-wrap:wrap;margin-left:auto}.header-actions button{white-space:nowrap}
.ed2k-panel{padding:24px;max-width:1220px;margin:auto;color:rgb(var(--v-theme-on-surface));overflow:auto;max-height:85dvh;font-size:15px}.ed2k-header,.card-header{display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap}.ed2k-header{margin-bottom:24px}h2,h3{margin:0;font-weight:650}h2{font-size:24px}h3{font-size:20px}p{margin:16px 0;color:rgba(var(--v-theme-on-surface),.72);line-height:1.7}.card{padding:26px;margin-bottom:22px;border:1px solid rgba(var(--v-theme-on-surface),.16);border-radius:15px;background:rgba(var(--v-theme-on-surface),.025)}.fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px;margin:22px 0}.fields label{display:flex;flex-direction:column;gap:10px;min-width:0}.fields input,.fields select{width:100%;border:1px solid rgba(var(--v-theme-on-surface),.25);border-radius:9px;padding:13px 15px;min-height:48px;background:rgba(var(--v-theme-on-surface),.025);color:inherit;font:inherit}.fields select option{background:rgb(var(--v-theme-surface));color:rgb(var(--v-theme-on-surface))}.fields input:focus,.fields select:focus{outline:2px solid rgb(var(--v-theme-primary));outline-offset:2px}small{color:rgba(var(--v-theme-on-surface),.62);line-height:1.7}.switches{display:flex;flex-wrap:wrap;gap:20px;margin:22px 0}.switch{display:flex;align-items:center;gap:10px;cursor:pointer}.switch input{width:18px;height:18px;accent-color:rgb(var(--v-theme-primary))}.actions{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}button{padding:12px 20px;border:0;border-radius:9px;color:rgb(var(--v-theme-on-primary));background:rgb(var(--v-theme-primary));font:inherit;cursor:pointer;min-height:44px}.secondary{background:rgba(var(--v-theme-primary),.13);color:rgb(var(--v-theme-primary))}.danger{color:#ef8585;background:rgba(220,60,60,.15)}button:disabled{opacity:.45;cursor:not-allowed}.message{padding:16px 20px;border-radius:10px;margin:16px 0;overflow-wrap:anywhere}.error{background:rgba(230,65,65,.14);color:#ed9696}.success{background:rgba(60,180,110,.14);color:#79c99e}.warning{color:#e7b36a}.account-status{font-weight:600}@media(max-width:650px){.ed2k-panel{padding:14px}.card{padding:18px}.fields{grid-template-columns:1fr;gap:20px}.card-header{align-items:flex-start}button{padding:11px 15px}}
</style>
