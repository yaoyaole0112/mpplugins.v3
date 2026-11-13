import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { createRequire } from 'node:module'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright')
const base = process.env.MOVIEPILOT_URL || 'http://127.0.0.1:3000'
const output = process.env.UI_OUTPUT_DIR || '/tmp/directoryrename-ui'
await mkdir(output, { recursive: true })
const credentials = JSON.parse(execFileSync('docker', [
  'exec', 'moviepilot', 'python', '-c',
  "import json,os; from dotenv import dotenv_values; c=dotenv_values('/config/app.env'); print(json.dumps({k:os.getenv(k) or c.get(k) for k in ('SUPERUSER','SUPERUSER_PASSWORD','API_TOKEN')}))",
], { encoding: 'utf8' }))

async function savedConfig() {
  const response = await fetch(`${base}/api/v1/plugin/DirectoryRename`, {
    headers: { 'X-API-KEY': credentials.API_TOKEN },
  })
  assert.equal(response.status, 200)
  const data = await response.json()
  return data.data
}

const before = await savedConfig()
const originalRules = JSON.parse(before.rules_json)
const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH || '/opt/google/chrome/chrome',
  headless: true,
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
})
const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } })
const failures = []
try {
  const login = await context.request.post(`${base}/api/v1/login/access-token`, {
    form: { username: credentials.SUPERUSER, password: credentials.SUPERUSER_PASSWORD },
  })
  assert.equal(login.status(), 200, 'Browser login failed')
  const result = await login.json()
  const token = result.access_token || result.data?.access_token
  assert.ok(token, 'Login did not return an access token')
  await context.addInitScript(({ token, user }) => {
    localStorage.setItem('auth', JSON.stringify({ token, remember: false, originalPath: null }))
    localStorage.setItem('user', JSON.stringify(user))
    localStorage.setItem('MP_LOCALE', 'zh-CN')
  }, {
    token,
    user: {
      superUser: result.super_user,
      userID: result.user_id,
      userName: '',
      avatar: '',
      level: result.level,
      permissions: result.permissions,
    },
  })
  const page = await context.newPage()
  page.on('pageerror', error => failures.push(error.message))
  page.on('response', response => {
    if (response.url().includes('/DirectoryRename/') && response.status() >= 400) {
      failures.push(`${response.status()} ${response.url().split('?')[0]}`)
    }
  })
  const ui = page.locator('.directory-rename-config')
  async function checkActionLayout() {
    const layout = await ui.locator('.rule-footer').evaluate(element => {
      const buttons = [...element.querySelectorAll('button')].map(button => {
        const rect = button.getBoundingClientRect()
        return { x: rect.x, right: rect.right, centerY: rect.y + rect.height / 2 }
      })
      return { background: getComputedStyle(element).backgroundColor, buttons }
    })
    assert.equal(layout.background, 'rgba(0, 0, 0, 0)', 'Footer must stay transparent')
    assert.equal(layout.buttons.length, 3)
    const [add, cancel, save] = layout.buttons
    assert.ok(Math.abs(add.centerY - cancel.centerY) <= 1, JSON.stringify(layout))
    assert.ok(Math.abs(add.centerY - save.centerY) <= 1, JSON.stringify(layout))
    assert.ok(add.right <= cancel.x && cancel.right <= save.x, JSON.stringify(layout))
    assert.ok(add.x >= 0 && save.right <= page.viewportSize().width, JSON.stringify(layout))
    const corners = await ui.locator('.v-expansion-panel').evaluateAll(elements =>
      elements.map(element => {
        const style = getComputedStyle(element)
        return [style.borderTopLeftRadius, style.borderTopRightRadius,
          style.borderBottomLeftRadius, style.borderBottomRightRadius]
      }))
    assert.ok(corners.every(values => values.every(value => value === '0px')),
      `Rule panel corners: ${JSON.stringify(corners)}`)
  }
  async function openConfig() {
    if (await ui.isVisible()) return
    await page.goto(`${base}/#/plugins`, { waitUntil: 'domcontentloaded' })
    const card = page.locator('[data-plugin-id="DirectoryRename"]').first()
    await card.waitFor({ timeout: 45000 })
    const menu = card.locator('button[aria-haspopup="menu"]').first()
    await menu.click()
    await page.getByText('设置', { exact: true }).last().click()
    await ui.waitFor({ timeout: 30000 })
    await ui.locator('.v-expansion-panel').first().getByRole('textbox', { name: '电视剧重命名模板' }).waitFor()
  }
  await openConfig()
  const panels = ui.locator('.v-expansion-panel')
  assert.equal(await panels.count(), originalRules.length)
  assert.equal(await ui.locator('textarea').first().inputValue(), originalRules[0].tv)
  await ui.getByRole('tab', { name: '电影模板' }).click()
  assert.equal(await ui.locator('textarea').first().inputValue(), originalRules[0].movie)
  await ui.getByRole('tab', { name: '电视剧模板' }).click()
  await checkActionLayout()
  await ui.screenshot({ path: path.join(output, 'desktop.png') })
  const darkTheme = await page.evaluate(() => {
    const provides = document.querySelector('#app')?.__vue_app__?._context.provides
    const key = provides && Reflect.ownKeys(provides).find(key => key.description === 'vuetify:theme')
    const theme = key && provides[key]
    if (!theme?.global?.name) return false
    theme.global.name.value = 'dark'
    return true
  })
  assert.ok(darkTheme, 'Native dark theme was not available')
  await ui.locator('.v-expansion-panel-title').first().click()
  await page.waitForTimeout(400)
  await checkActionLayout()
  await ui.screenshot({ path: path.join(output, 'desktop-dark-collapsed.png') })
  await ui.locator('.v-expansion-panel-title').first().click()

  const saveResponse = page.waitForResponse(response =>
    response.url().split('?')[0].endsWith('/api/v1/plugin/DirectoryRename')
      && response.request().method() === 'PUT')
  await ui.getByRole('button', { name: '保存', exact: true }).click()
  assert.equal((await saveResponse).status(), 200)
  assert.deepEqual(await savedConfig(), before, 'Saving untouched form changed configuration')
  console.log('unchanged-form-save: OK')
  await openConfig()

  let writes = 0
  page.on('request', request => {
    if (request.method() === 'PUT' && request.url().endsWith('/plugin/DirectoryRename')) writes += 1
  })
  await ui.getByRole('button', { name: '复制规则', exact: true }).first().click()
  assert.equal(await panels.count(), originalRules.length + 1)
  await ui.getByRole('button', { name: '保存', exact: true }).click()
  await ui.getByRole('alert').filter({ hasText: '重复' }).waitFor()
  assert.equal(writes, 0, 'Duplicate rule reached the save endpoint')
  await ui.getByRole('button', { name: '删除规则', exact: true }).last().click()
  const confirmation = page.getByRole('dialog').filter({ hasText: '删除规则' }).last()
  await confirmation.getByRole('button', { name: '取消', exact: true }).click()
  assert.equal(await panels.count(), originalRules.length + 1)
  await ui.getByRole('button', { name: '删除规则', exact: true }).last().click()
  await confirmation.getByRole('button', { name: '删除', exact: true }).click()
  assert.equal(await panels.count(), originalRules.length)
  console.log('copy-duplicate-validation-delete-cancel-confirm: OK')

  await ui.getByRole('button', { name: '添加规则' }).click()
  assert.equal(await panels.count(), originalRules.length + 1)
  const newPanel = panels.last()
  await newPanel.getByLabel('规则名称', { exact: true }).fill('界面验证（不保存）')
  await ui.getByRole('button', { name: '保存', exact: true }).click()
  await ui.getByRole('alert').filter({ hasText: '绝对路径' }).waitFor()
  assert.equal(writes, 0, 'Incomplete rule reached the save endpoint')
  const directory = newPanel.getByLabel('目标目录', { exact: true })
  await directory.click()
  await page.getByRole('option').filter({ hasText: '/影视资源/已整理/电影/' }).last().click()
  assert.equal(await directory.inputValue(), '/影视资源/已整理/电影/')
  await newPanel.getByLabel('电视剧重命名模板', { exact: true }).fill(originalRules[0].tv)
  await ui.getByRole('button', { name: '删除规则', exact: true }).last().click()
  await confirmation.getByRole('button', { name: '删除', exact: true }).click()
  console.log('add-path-choice-incomplete-validation: OK')

  await page.setViewportSize({ width: 390, height: 844 })
  await page.waitForTimeout(400)
  await checkActionLayout()
  await ui.screenshot({ path: path.join(output, 'mobile-dark-collapsed.png') })
  await ui.locator('.v-expansion-panel-title').first().click()
  await ui.locator('textarea').first().waitFor()
  await page.waitForTimeout(400)
  const layout = await ui.evaluate(element => ({
    scrollWidth: element.scrollWidth,
    clientWidth: element.clientWidth,
    viewport: window.innerWidth,
    bounds: element.getBoundingClientRect().toJSON(),
  }))
  assert.ok(layout.scrollWidth <= layout.clientWidth + 1, JSON.stringify(layout))
  assert.ok(layout.bounds.width <= layout.viewport, JSON.stringify(layout))
  await page.screenshot({ path: path.join(output, 'mobile.png') })
  await page.setViewportSize({ width: 320, height: 740 })
  await ui.locator('.v-expansion-panel-title').first().click()
  await page.waitForTimeout(400)
  await checkActionLayout()
  await ui.screenshot({ path: path.join(output, 'mobile-320.png') })
  assert.deepEqual(await savedConfig(), before, 'Unsaved UI operations changed saved rules')
  assert.deepEqual(failures.filter(message => /DirectoryRename|directoryrename|federation|resolve component/i.test(message)), [])
  console.log('mobile-layout-and-original-config: OK')
  console.log(`screenshots: ${output}`)
} catch (error) {
  const pages = context.pages()
  if (pages[0]) {
    await pages[0].screenshot({ path: path.join(output, 'failure.png') }).catch(() => {})
    console.error('page:', pages[0].url())
    console.error('visible buttons:', await pages[0].getByRole('button').allTextContents().catch(() => []))
  }
  throw error
} finally {
  await browser.close()
}
