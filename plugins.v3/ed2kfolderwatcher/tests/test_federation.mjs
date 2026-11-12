import assert from 'node:assert/strict'
import { readFile, access } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import * as vue from 'vue'

const entryUrl = new URL('../dist/assets/remoteEntry.js', import.meta.url)
const styles = []
globalThis.document = {
  createElement: () => ({}),
  head: { appendChild: element => styles.push(element.href) },
}

test('queue cancellation only exposes waiting task IDs and does not delete files', async () => {
  const source = await readFile(new URL('../src/Page.vue', import.meta.url), 'utf8')
  assert.match(source, /v-if="task.status === 'queued' && task.can_cancel"/)
  const cancel = source.match(/async function cancelTask\(task\) \{[\s\S]*?\n\}/)?.[0]
  assert.ok(cancel)
  assert.match(cancel, /tasks\/cancel/)
  assert.match(cancel, /\{ id: task.id \}/)
  assert.equal(cancel.includes('delete-source'), false)
})

test('queue rows display file sizes and No. badges without eligibility timestamps', async () => {
  const source = await readFile(new URL('../src/Page.vue', import.meta.url), 'utf8')
  const queue = source.split('<ol v-else class="progress-list">')[1].split('</ol>')[0]
  assert.match(queue, /class="queue-position">\{\{ task\.source === '本地上传' \? '上传 No\.' : 'No\.' \}\}\{\{ task\.queue_position \}\}/)
  assert.match(queue, /v-else-if="task\.status === 'queued'">文件大小：/)
  assert.match(queue, /formatBytes\(task\.size\)/)
  assert.equal(queue.includes('最早可处理'), false)
  assert.equal(queue.includes('队列第'), false)
  assert.equal(queue.includes('task.due_at'), false)
})

test('common settings put toggles before deletion and pair inputs without an empty half-row', async () => {
  const source = await readFile(new URL('../src/Panel.vue', import.meta.url), 'utf8')
  const common = source.split('data-testid="common-settings"')[1].split('</article>')[0]
  for (const name of ['enabled', 'output_enabled', 'include_subdirectories', 'movie_series_parallel']) {
    assert.ok(common.includes(`v-model="settings.${name}"`), name)
  }
  assert.equal(common.includes('settings.delete_source'), false)
  assert.equal(common.includes('settings.local_delete_source'), false)
  assert.match(common, /电影\/剧集两路并发/)
  assert.match(source, /const configFields = \[[^\n]*'movie_series_parallel'/)
  assert.match(common, /v-model.number="settings.retry_limit"[\s\S]*?v-model="settings.min_age"[\s\S]*?<\/div>/)
  assert.match(common, /<label>TXT 输出路径/)
  assert.match(source, /\.fields \.full-width\{grid-column:1 \/ -1\}/)
  assert.equal(source.includes('共用一个计算线程'), false)
})

test('local upload card precedes local monitoring and offers MP and direct modes', async () => {
  const source = await readFile(new URL('../src/Panel.vue', import.meta.url), 'utf8')
  assert.ok(source.indexOf('data-testid="local-upload"') < source.indexOf('data-testid="local-source"'))
  const upload = source.split('data-testid="local-upload"')[1].split('</article>')[0]
  assert.match(upload, /本地计算，上传115/)
  for (const name of ['local_upload_enabled', 'local_upload_mode', 'local_upload_folder', 'local_upload_target', 'local_upload_delete_source', 'local_upload_delete_cloud_copy']) {
    assert.ok(upload.includes(`v-model="settings.${name}"`), name)
    assert.ok(source.includes(`'${name}'`), name)
  }
  assert.match(upload, /value="mp"/)
  assert.match(upload, /value="direct"/)
  assert.match(upload, /复制上传/)
  assert.match(upload, /独立上传通道/)
  assert.match(source, /request\('local-upload\/run'/)
  assert.match(upload, /v-model.number="settings.local_upload_rate"/)
  assert.match(upload, /默认8，0不限速/)
  const fields = upload.split('<div class="fields">')[1].split('</div>')[0]
  const positions = ['上传方式', '直传速度上限（MiB/s）', '本地计算上传目录', '115上传目标目录'].map(label => fields.indexOf(label))
  assert.ok(positions.every(position => position >= 0))
  assert.deepEqual(positions, [...positions].sort((left, right) => left - right))
  assert.equal(fields.includes('115上传目标目录<input class="full-width"'), false)
  assert.equal(upload.includes('v-if="settings.local_upload_mode === \'direct\'" class="fields"'), false)
})

test('upload progress and independent delete settings are exposed', async () => {
  const page = await readFile(new URL('../src/Page.vue', import.meta.url), 'utf8')
  assert.match(page, /task\.progress_known !== false/)
  assert.match(page, /暂未返回字节进度/)
  assert.match(page, /task\.upload_speed/)
  assert.match(page, /首次失败：\{\{ item\.upload_first_error \}\}/)
  assert.match(page, /本地／网盘队列表示 ED2K 尚未计算/)
  assert.match(page, /upload_verified: 'ED2K已计算，115内容已确认'/)
  const panel = await readFile(new URL('../src/Panel.vue', import.meta.url), 'utf8')
  const save = panel.match(/async function saveSettings\(\) \{([\s\S]*?)\n\}/)?.[1]
  assert.match(save, /settings\.local_upload_delete_cloud_copy/)
  assert.match(panel, /输出成功后删除网盘源文件/)
  const common = panel.split('data-testid="common-settings"')[1].split('</article>')[0]
  assert.equal(common.includes('完成后删除源文件'), false)
  const upload = panel.split('data-testid="local-upload"')[1].split('</article>')[0]
  const cloud = panel.split('data-testid="cloud-source"')[1].split('</article>')[0]
  assert.match(upload, /网盘副本删除/)
  assert.equal(cloud.includes('单独授权115网盘删源'), false)
  assert.match(cloud, /开启后将使用当前115账号，并把授权限定在已配置监控目录和子目录范围/)
  assert.match(save, /values\.cloud_delete_enabled = settings\.cloud_delete_source \|\| settings\.local_upload_delete_cloud_copy/)
  assert.equal(cloud.includes('（默认关闭）'), false)
})

test('MP upload mode explains receipt-driven calculation and recovery', async () => {
  const source = await readFile(new URL('../src/Panel.vue', import.meta.url), 'utf8')
  const upload = source.split('data-testid="local-upload"')[1].split('</article>')[0]
  assert.match(upload, /MP上传成功后才本地计算/)
  assert.match(upload, /不监控新增文件/)
  assert.match(upload, /本插件不发起MP整理或115上传/)
  assert.match(upload, /v-if="settings\.local_upload_mode === 'direct'" class="actions"/)
  assert.equal(upload.includes('立即回执补扫'), false)
  for (const selector of ['data-testid="local-upload"', 'data-testid="local-source"', 'data-testid="cloud-source"']) {
    const card = source.split(selector)[1].split('</article>')[0]
    const switches = card.match(/<div class="switches">([\s\S]*?)<\/div>/)?.[1] || ''
    assert.ok((switches.match(/class="switch"/g) || []).length >= 3, selector)
  }
  const page = await readFile(new URL('../src/Page.vue', import.meta.url), 'utf8')
  assert.match(page, /MP回执后处理/)
  assert.match(page, /MP整理上传成功，ED2K已计算并校验/)
  assert.match(page, /item\.upload_mode === 'mp'/)
})

test('built federation entry resolves all public components and styles', async () => {
  const source = await readFile(entryUrl, 'utf8')
  assert.equal(source.includes('${__federation_expose_'), false)
  const remote = await import(entryUrl.href)
  remote.init({ vue: { [vue.version]: { get: () => () => vue, loaded: true } } })
  for (const name of ['./Page', './Config', './AppPage']) {
    const factory = await remote.get(name)
    const component = factory()
    assert.equal(typeof component.setup, 'function', name)
  }
  assert.ok(styles.length > 0)
  for (const style of styles) await access(fileURLToPath(style))
})

test('saving settings does not prompt, while destructive actions retain in-page confirmation', async () => {
  const source = await readFile(new URL('../src/Panel.vue', import.meta.url), 'utf8')
  assert.match(source, /role="alertdialog"/)
  assert.match(source, /function askConfirmation\(/)
  const saveSettings = source.match(/async function saveSettings\(\) \{([\s\S]*?)\n\}/)?.[1]
  assert.ok(saveSettings)
  assert.equal(saveSettings.includes('askConfirmation('), false)
  assert.equal(source.includes('window.confirm('), false)
})
