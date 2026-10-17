<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

const props = defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'Ed2kFolderWatcher' } })
const emit = defineEmits(['close'])
const busy = ref(false)
const loaded = ref(false)
const error = ref('')
const notice = ref('')
const settings = reactive({ telegram_mode: 'user' })
const account = reactive({ api_id: '', api_hash: '', phone: '', code: '', password: '' })
const auth = reactive({ logged_in: false, pending: false, password_required: false, api_hash_configured: false, expires_in: 0, retry_in: 0 })
let timer
let savedDelete = false
const base = computed(() => `plugin/${props.pluginId}`)
const disabled = computed(() => busy.value || !loaded.value)
const configFields = ['enabled', 'folder', 'recursive', 'monitor_enabled', 'output_enabled', 'output', 'extensions', 'min_age', 'delete_source', 'scan_enabled', 'cron', 'telegram_enabled', 'telegram_mode', 'telegram_chat_id', 'telegram_thread_id']
const watchSwitches = [{ key: 'enabled', label: '启用插件' }, { key: 'monitor_enabled', label: '实时监控文件' }, { key: 'recursive', label: '包含子目录' }]
const textFields = [{ key: 'folder', label: '监控目录', hint: 'MoviePilot 容器内路径，例如 /media/movies' }, { key: 'extensions', label: '扩展名过滤', hint: '留空处理全部文件，例如 mkv,mp4' }, { key: 'min_age', label: '文件稳定等待秒数', type: 'number', hint: '持续写入时自动延后，避免处理未完成文件' }]

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
  })
  timer = setInterval(() => {
    auth.retry_in = Math.max(0, (auth.retry_in || 0) - 1)
    if (auth.pending) {
      auth.expires_in = Math.max(0, (auth.expires_in || 0) - 1)
      if (!auth.expires_in) { auth.pending = auth.password_required = false; account.code = account.password = ''; notice.value = '登录流程已过期，请重新发送验证码' }
    }
  }, 1000)
})
onUnmounted(() => {
  clearInterval(timer)
  account.api_hash = account.phone = account.code = account.password = ''
})
</script>

<template>
  <section class="ed2k-panel" :aria-busy="busy">
    <header class="ed2k-header"><div><h2>自动生成ED2K链接</h2><p>实时目录监控 · 增量生成 · Telegram 自动发送</p></div><button type="button" class="secondary" @click="emit('close')">关闭</button></header>
    <div v-if="error" role="alert" class="message error">{{ error }}</div>
    <div v-if="notice" role="status" class="message success">{{ notice }}</div>
    <p v-if="!loaded">正在加载设置…</p>

    <article class="card">
      <div class="card-header"><h3>目录监控与输出</h3><button type="button" :disabled="disabled || auth.pending" @click="saveSettings">保存插件设置</button></div>
      <div class="switches"><label v-for="field in watchSwitches" :key="field.key" class="switch"><input v-model="settings[field.key]" type="checkbox" :disabled="disabled">{{ field.label }}</label></div>
      <div class="fields"><label v-for="field in textFields" :key="field.key">{{ field.label }}<input v-model="settings[field.key]" :type="field.type || 'text'" :min="field.type === 'number' ? 0 : undefined" :disabled="disabled"><small>{{ field.hint }}</small></label></div>
      <div class="switches"><label class="switch"><input v-model="settings.output_enabled" type="checkbox" :disabled="disabled">生成本地 TXT</label><label class="switch"><input v-model="settings.delete_source" type="checkbox" :disabled="disabled">成功后删除源文件</label><label class="switch"><input v-model="settings.scan_enabled" type="checkbox" :disabled="disabled">定时增量补扫</label></div>
      <div class="fields"><label>TXT 输出路径<input v-model="settings.output" :disabled="disabled || !settings.output_enabled" placeholder="留空使用监控目录/ed2k.txt"><small>关闭 TXT 不会删除已有文件；内部缓存独立保存。</small></label><label>补扫计划<input v-model="settings.cron" :disabled="disabled || !settings.scan_enabled" placeholder="*/10 * * * *"><small>默认关闭，网络挂载漏事件时可启用兜底。</small></label></div>
      <p v-if="settings.delete_source" class="warning">已开启删除源文件：仅在所选输出成功且文件未变化时删除；两种输出都关闭时不删除。</p>
    </article>

    <article class="card">
      <h3>Telegram 发送设置</h3>
      <div class="switches"><label class="switch"><input v-model="settings.telegram_enabled" type="checkbox" :disabled="disabled">发送 ED2K 链接到 Telegram</label></div>
      <!-- <select v-model="settings.telegram_mode"><option value="bot">Bot Token（机器人）</option></select> -->
      <!-- <label>发送机器人 Token<input v-model="settings.telegram_token" type="password" autocomplete="new-password"></label> -->
      <div class="fields"><label>接收目标<input v-model="settings.telegram_chat_id" :disabled="disabled" placeholder="@Htrbgcbtbot"><small>支持 @用户名、数字 Chat ID 或公开 t.me 链接。</small></label><label>话题线程 ID（可选）<input v-model="settings.telegram_thread_id" :disabled="disabled" placeholder="普通私聊、频道留空"></label></div>
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
    <footer class="actions"><button type="button" :disabled="disabled || auth.pending" @click="saveSettings">保存插件设置</button><button type="button" class="secondary" :disabled="disabled || !settings.enabled || auth.pending" @click="run">增量补扫／补发链接</button></footer>
  </section>
</template>

<style scoped>
.ed2k-panel{padding:24px;max-width:1220px;margin:auto;color:rgb(var(--v-theme-on-surface));overflow:auto;max-height:85dvh;font-size:15px}.ed2k-header,.card-header{display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap}.ed2k-header{margin-bottom:24px}h2,h3{margin:0;font-weight:650}h2{font-size:24px}h3{font-size:20px}p{margin:16px 0;color:rgba(var(--v-theme-on-surface),.72);line-height:1.7}.card{padding:26px;margin-bottom:22px;border:1px solid rgba(var(--v-theme-on-surface),.16);border-radius:15px;background:rgba(var(--v-theme-on-surface),.025)}.fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px;margin:22px 0}.fields label{display:flex;flex-direction:column;gap:10px;min-width:0}.fields input,.fields select{width:100%;border:1px solid rgba(var(--v-theme-on-surface),.25);border-radius:9px;padding:13px 15px;min-height:48px;background:rgba(var(--v-theme-on-surface),.025);color:inherit;font:inherit}.fields select option{background:rgb(var(--v-theme-surface));color:rgb(var(--v-theme-on-surface))}.fields input:focus,.fields select:focus{outline:2px solid rgb(var(--v-theme-primary));outline-offset:2px}small{color:rgba(var(--v-theme-on-surface),.62);line-height:1.7}.switches{display:flex;flex-wrap:wrap;gap:20px;margin:22px 0}.switch{display:flex;align-items:center;gap:10px;cursor:pointer}.switch input{width:18px;height:18px;accent-color:rgb(var(--v-theme-primary))}.actions{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}button{padding:12px 20px;border:0;border-radius:9px;color:rgb(var(--v-theme-on-primary));background:rgb(var(--v-theme-primary));font:inherit;cursor:pointer;min-height:44px}.secondary{background:rgba(var(--v-theme-primary),.13);color:rgb(var(--v-theme-primary))}.danger{color:#ef8585;background:rgba(220,60,60,.15)}button:disabled{opacity:.45;cursor:not-allowed}.message{padding:16px 20px;border-radius:10px;margin:16px 0;overflow-wrap:anywhere}.error{background:rgba(230,65,65,.14);color:#ed9696}.success{background:rgba(60,180,110,.14);color:#79c99e}.warning{color:#e7b36a}.account-status{font-weight:600}@media(max-width:650px){.ed2k-panel{padding:14px}.card{padding:18px}.fields{grid-template-columns:1fr;gap:20px}.card-header{align-items:flex-start}button{padding:11px 15px}}
</style>
