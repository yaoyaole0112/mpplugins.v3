"""Shared settings browser checks against a mock API, without real approval traffic."""

import argparse
import asyncio
from pathlib import Path
import re

from playwright.async_api import async_playwright


BADGE_COLORS = {
    'success': ('rgb(87, 185, 133)', 'rgba(60, 180, 110, 0.13)'),
    'error': ('rgb(223, 123, 123)', 'rgba(220, 60, 60, 0.13)'),
    'info': ('rgb(86, 160, 220)', 'rgba(60, 140, 220, 0.13)'),
    'warning': ('rgb(219, 163, 75)', 'rgba(220, 150, 60, 0.13)'),
    'secondary': ('rgb(139, 145, 155)', 'rgba(139, 145, 155, 0.13)'),
}


async def verify_badge_colors(dialog, all_tones=True):
    badges = await dialog.locator('.shared-settings__badge').evaluate_all('''elements =>
        elements.map(element => ({tone: element.dataset.tone, text: element.textContent,
            color: getComputedStyle(element).color, background: getComputedStyle(element).backgroundColor,
            border: getComputedStyle(element).borderTopWidth, radius: getComputedStyle(element).borderRadius}))''')
    if all_tones:
        assert set(BADGE_COLORS).issubset({badge['tone'] for badge in badges}), badges
    for badge in badges:
        assert (badge['color'], badge['background']) == BADGE_COLORS[badge['tone']], badge
        assert badge['border'] == '0px' and badge['radius'] == '7px', badge


async def verify_record_layout(dialog):
    records = await dialog.locator('.shared-settings__record').evaluate_all('''elements => elements.map(element => {
        const head = element.querySelector('.shared-settings__record-head')
        const title = head.querySelector('.shared-settings__heading, strong').getBoundingClientRect()
        const badge = head.querySelector('.shared-settings__badge').getBoundingClientRect()
        const bounds = head.getBoundingClientRect()
        return {single: element.querySelectorAll('.shared-settings__badge').length === 1,
            right: Math.abs(badge.right - bounds.right) < 2, top: Math.abs(badge.top - bounds.top) < 2,
            separate: title.right <= badge.left, fits: element.scrollWidth <= element.clientWidth + 1}
    })''')
    assert records and all(all(record.values()) for record in records), records
    infos = await dialog.locator('.shared-settings__info').evaluate_all('''elements => elements.map(element => ({
        background: getComputedStyle(element).backgroundColor,
        padding: getComputedStyle(element).padding, radius: getComputedStyle(element).borderRadius,
        fits: element.scrollWidth <= element.clientWidth + 1,
        contained: element.getBoundingClientRect().right <= element.closest('article').getBoundingClientRect().right + 1
    }))''')
    assert infos, infos
    for info in infos:
        assert info['background'] in ('rgba(0, 0, 0, 0.06)', 'rgba(255, 255, 255, 0.06)'), info
        assert info['padding'] == '7px 10px' and info['radius'] == '7px', info
        assert info['fits'] and info['contained'], info


async def verify_settings_layout(dialog, width):
    toggles = await dialog.locator('.global-settings__toggles').evaluate('''element => {
        const boxes = [...element.children].map(child => child.getBoundingClientRect())
        return {inline: boxes.every((box, index) => !index || box.left >= boxes[index - 1].right),
            aligned: boxes.every(box => Math.abs(box.top - boxes[0].top) < 2),
            stacked: boxes.every((box, index) => !index || box.top >= boxes[index - 1].bottom - 1),
            fits: element.scrollWidth <= element.clientWidth + 1}
    }''')
    assert toggles['fits'] and (
        (toggles['inline'] and toggles['aligned']) or (width < 360 and toggles['stacked'])
    ), (width, toggles)
    assert await dialog.locator('.global-settings__toggles label').all_text_contents() == [
        '启用插件', '显示侧栏入口']
    for selector in ('.global-settings__aliases', '.shared-settings__grid:has(textarea)'):
        layout = await dialog.locator(selector).evaluate('''element => {
            const [first, second] = [...element.children].map(child => child.getBoundingClientRect())
            return {inline: second.left >= first.right && Math.abs(first.top - second.top) < 2,
                stacked: second.top >= first.bottom, fits: element.scrollWidth <= element.clientWidth + 1}
        }''')
        assert layout['inline' if width >= 600 else 'stacked'] and layout['fits'], (width, selector, layout)
    assert await dialog.locator('.global-settings__rules').count() == 0
    assert await dialog.locator('.global-settings__progress').count() == 1
    limits = await dialog.locator('.global-settings__limits').evaluate('''element =>
        [...element.children].map(child => {
            const box = child.getBoundingClientRect()
            return {top: box.top, width: box.width}
        })''')
    rows = len({round(item['top']) for item in limits})
    assert rows == (2 if width >= 960 else 3 if width >= 600 else 6), (width, limits)
    if width >= 960:
        assert all(item['width'] < 320 for item in limits), limits
    heights = await dialog.locator('.global-settings__limits .v-field').evaluate_all(
        'elements => elements.map(element => element.getBoundingClientRect().height)')
    assert all(value >= 48 for value in heights), (width, heights)
    delete_policy = await dialog.locator('.global-settings__delete-policy').evaluate('''element => {
        const toggle = element.querySelector('.v-selection-control').getBoundingClientRect()
        const limits = element.nextElementSibling.getBoundingClientRect()
        return {above: toggle.bottom <= limits.top + 1, fits: element.scrollWidth <= element.clientWidth + 1}
    }''')
    assert delete_policy['above'] and delete_policy['fits'], (width, delete_policy)
    recent_date = await dialog.locator('.global-settings__recent-date').evaluate('''element => {
        const field = element.querySelector('.v-field').getBoundingClientRect()
        const toggle = element.querySelector('.v-selection-control').getBoundingClientRect()
        return {centered: Math.abs(field.top + field.height / 2 - toggle.top - toggle.height / 2) < 3,
            fits: element.scrollWidth <= element.clientWidth + 1}
    }''')
    assert recent_date['fits'] and (width < 600 or recent_date['centered']), (width, recent_date)
    status = await dialog.locator('.shared-settings__status').evaluate('''element =>
        [...element.children].slice(0, 3).map(child => child.getBoundingClientRect().top)''')
    if width >= 600:
        assert max(status) - min(status) < 2, (width, status)
    credentials = await dialog.locator('.shared-settings__credentials').evaluate('''element => {
        const bounds = element.getBoundingClientRect()
        const [secret, status] = [...element.children].map(child => child.getBoundingClientRect())
        return {sideBySide: status.left >= secret.right && Math.abs(secret.top - status.top) < 3,
            stacked: status.top >= secret.bottom - 1, fits: element.scrollWidth <= element.clientWidth + 1,
            width: bounds.width}
    }''')
    assert credentials['fits'] and (
        (credentials['width'] >= 920 and credentials['sideBySide'])
        or (credentials['width'] < 920 and credentials['stacked'])
    ), credentials
    identity = await dialog.locator('.shared-settings__grid--identity').evaluate('''element => {
        const boxes = [...element.children].map(child => child.getBoundingClientRect())
        return {width: element.getBoundingClientRect().width,
            columns: boxes.filter(box => Math.abs(box.top - boxes[0].top) < 3).length,
            fits: element.scrollWidth <= element.clientWidth + 1}
    }''')
    assert identity['fits'] and identity['columns'] == (
        3 if identity['width'] >= 920 else 2 if identity['width'] >= 480 else 1
    ), identity
    specs = await dialog.locator('.global-settings__spec-row').evaluate('''element => {
        const attributes = element.querySelector('.global-settings__attributes').getBoundingClientRect()
        const policy = element.querySelector('.global-settings__unknown').getBoundingClientRect()
        return {inline: policy.left >= attributes.right,
            centered: Math.abs(policy.top + policy.height / 2 - attributes.top - attributes.height / 2) < 2,
            fits: element.scrollWidth <= element.clientWidth + 1}
    }''')
    assert specs['fits'] and (width < 960 or all(specs.values())), (width, specs)
    footer = await dialog.locator('.global-settings__footer').evaluate('''element => {
        const buttons = [...element.querySelectorAll('button')].map(button => button.getBoundingClientRect())
        const groups = [...element.children].map(child => child.getBoundingClientRect())
        const bounds = element.getBoundingClientRect()
        const style = getComputedStyle(element)
        return {count: buttons.length, aligned: buttons.every(box =>
            Math.abs(box.top + box.height / 2 - buttons[0].top - buttons[0].height / 2) < 2),
            separate: groups[0].right <= groups[1].left,
            left: Math.abs(groups[0].left - bounds.left - parseFloat(style.paddingLeft)) < 2,
            right: Math.abs(bounds.right - groups[1].right - parseFloat(style.paddingRight)) < 2,
            fits: element.scrollWidth <= element.clientWidth + 1}
    }''')
    assert footer['count'] == 4 and all(footer[key] for key in ('aligned', 'separate', 'left', 'right', 'fits')), (width, footer)
    if await dialog.locator('.global-settings__test-feedback').count():
        feedback = await dialog.locator('.global-settings__test-feedback').bounding_box()
        assert feedback and feedback['width'] > 0 and feedback['height'] > 0
    assert await dialog.locator('.global-settings__body button[aria-label="测试审批连接"]').count() == 0
    assert await dialog.locator('.global-settings__footer button[aria-label="测试审批连接"] .mdi-connection').count() == 1
    assert await dialog.locator('.v-card-text').evaluate(
        'element => element.scrollWidth <= element.clientWidth + 1')


async def verify_record_search(page, dialog, tab, initial_height, output, width, host_css=None):
    selector = '.shared-settings__record'
    await page.evaluate('window.resourceflowExtraRecords = 125')
    await dialog.get_by_role('button', name='刷新资源记录', exact=True).click()
    await page.wait_for_function("""selector =>
        [...document.querySelectorAll(selector)].filter(item => item.getBoundingClientRect().width > 0).length === 20
    """, arg=selector)
    expected_total = {'本机下载申请': 130, '共享下载记录': 129}[tab]
    total = dialog.locator('.shared-settings__pagination .shared-settings__total')
    assert await total.inner_text() == f'共 {expected_total} 条'
    search = dialog.get_by_role('textbox', name='搜索记录')
    await search.scroll_into_view_if_needed()
    await dialog.locator('.v-pagination:visible .v-pagination__item button').nth(1).click()
    await page.wait_for_timeout(400)
    prefix = "Search Record item-"
    await search.fill(f'{prefix}124')
    await dialog.get_by_text(f'{prefix}124', exact=True).wait_for()
    assert await dialog.locator(f'{selector}:visible').count() == 1
    assert await dialog.locator('.v-pagination:visible').count() == 0
    assert await total.inner_text() == '共 1 条'
    toolbar = dialog.locator('.shared-settings__search:visible')
    owner_select, type_select = toolbar.locator('.v-select').nth(0), toolbar.locator('.v-select').nth(1)
    await owner_select.click()
    await page.get_by_role('option', name='朋友A', exact=True).click()
    await dialog.get_by_text('无匹配记录', exact=True).wait_for()
    await owner_select.click()
    await page.get_by_role('option', name='搜索下载方', exact=True).click()
    await dialog.get_by_text(f'{prefix}124', exact=True).wait_for()
    await type_select.click()
    await page.get_by_role('option', name='电影', exact=True).click()
    await dialog.get_by_text('无匹配记录', exact=True).wait_for()
    await type_select.click()
    await page.get_by_role('option', name='剧集', exact=True).click()
    await dialog.get_by_text(f'{prefix}124', exact=True).wait_for()
    await type_select.click()
    await page.get_by_role('option', name='全部类型', exact=True).click()
    await owner_select.click()
    await page.get_by_role('option', name='全部下载方', exact=True).click()
    await search.fill('not-a-record-[]_%')
    await dialog.get_by_text('无匹配记录', exact=True).wait_for()
    bounds = await dialog.locator('.channel-dedup__card').bounding_box()
    assert abs(bounds['height'] - initial_height) <= 1, (tab, width, bounds)
    await page.screenshot(path=str(output / f'search-empty-{tab}-{width}.png'), full_page=True)
    await page.evaluate('window.resourceflowSlowSearch = true')
    await search.fill('Slow')
    await page.wait_for_timeout(450)
    await search.fill(f'{prefix}124')
    await dialog.get_by_text(f'{prefix}124', exact=True).wait_for()
    await page.wait_for_timeout(1100)
    assert await dialog.get_by_text(f'{prefix}124', exact=True).is_visible()
    await page.evaluate('window.resourceflowSlowSearch = false; window.resourceflowExtraRecords = 0')
    await search.fill('')
    toolbar = dialog.locator('.shared-settings__search:visible')
    status_select = toolbar.locator('.v-select').last
    value = '已过滤' if tab == '本机下载申请' else '已完成'
    await status_select.click()
    if tab == '本机下载申请':
        await page.get_by_role('option', name='已过滤', exact=True).wait_for()
        options = await page.get_by_role('option').all_text_contents()
        assert {option.strip() for option in options} == {
            '全部状态', '已完成', '已入队', '已释放', '已过滤'}, options
    await page.get_by_role('option', name=value, exact=True).click()
    await page.wait_for_function("""selector =>
        [...document.querySelectorAll(selector)].filter(item => item.getBoundingClientRect().width > 0).length === 1
    """, arg=selector)
    await search.fill('not-a-record')
    await dialog.get_by_text('无匹配记录', exact=True).wait_for()
    await status_select.click()
    await page.get_by_role('option', name="全部状态", exact=True).click()
    await search.fill('')
    await page.wait_for_timeout(600)
    bounds = await dialog.locator('.channel-dedup__card').bounding_box()
    assert abs(bounds['height'] - initial_height) <= 1, (tab, width, bounds)
    assert await dialog.locator('.v-card-text').evaluate(
        'element => element.scrollWidth <= element.clientWidth + 1')


async def verify(base_url, output, executable_path, host_css=None):
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=executable_path,
                                                   args=["--no-sandbox"])
        for width, height in ((1440, 1000), (768, 1024), (390, 844), (320, 720)):
            page = await browser.new_page(viewport={"width": width, "height": height}, locale="zh-CN",
                                          timezone_id="Asia/Shanghai")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(f"{base_url}/tests/channel-dedup-smoke.html")
            await page.wait_for_function("window.resourceflowReady === true")
            await page.evaluate("document.fonts.ready")
            await page.get_by_role("button", name="共享下载审批", exact=True).click()
            dialog = page.get_by_role("dialog")
            await dialog.get_by_text("Show S01E05", exact=True).wait_for()
            assert await dialog.get_by_label("启用共享下载审批", exact=True).count() == 0
            assert await dialog.get_by_label("参与跨实例共享审批", exact=True).count() == 0
            assert await dialog.get_by_role("button", name="保存设置", exact=True).count() == 0
            assert await dialog.get_by_role("tab").all_text_contents() == [
                '本机下载申请', '共享下载记录']
            bounds = await dialog.bounding_box()
            assert bounds["width"] >= (1100 if width >= 1000 else width * 0.9 if width >= 600 else width - 2), bounds
            assert bounds["height"] >= height * 0.88, bounds
            await page.wait_for_timeout(300)
            card_bounds = await dialog.locator('.channel-dedup__card').bounding_box()
            close_button = dialog.get_by_role("button", name="关闭共享下载审批", exact=True)
            close_bounds = await close_button.bounding_box()
            assert close_bounds["y"] < card_bounds["y"] + 70, close_bounds
            assert close_bounds["x"] > card_bounds["x"] + card_bounds["width"] - 80, close_bounds
            assert await dialog.locator('.v-card-actions').count() == 0
            assert await dialog.locator('.channel-dedup__status').count() == 0
            refresh_bounds = await dialog.get_by_role('button', name='刷新资源记录', exact=True).bounding_box()
            assert refresh_bounds['x'] + refresh_bounds['width'] <= close_bounds['x'], refresh_bounds
            assert abs(refresh_bounds['y'] - close_bounds['y']) < 2, (refresh_bounds, close_bounds)
            assert await dialog.get_by_text('去重依据', exact=True).count() == 0
            assert card_bounds["width"] >= (1100 if width >= 1000 else width * 0.9 if width >= 600 else width - 2), card_bounds
            records = dialog.locator('.shared-settings__records')
            first = records.locator('article').first
            await first.get_by_text("申请时间", exact=False).wait_for()
            search_box = dialog.locator('.approval-search:visible input').first
            search_position = await search_box.bounding_box()
            assert '2026' in await first.inner_text()
            assert '11:00:00' in await first.inner_text()
            assert '申请时间：' in await first.inner_text()
            assert '最近更新：' in await first.inner_text()
            assert '站点：Test Site' in await first.inner_text()
            assert '任务：' not in await first.inner_text()
            assert '下载器：' not in await first.inner_text()
            assert '最近更新' in await first.inner_text()
            assert '已入队（1）' in await first.locator('.shared-settings__record-head').inner_text()
            assert await first.locator('.shared-settings__record-head .shared-settings__badge').count() == 1
            assert '文件 2' not in await first.inner_text()
            assert '完成 0' not in await first.inner_text()
            release = first.locator('.shared-settings__preview span')
            release_text = await release.inner_text()
            assert release_text.startswith('玫瑰战争 · S01E05-06 · TMDB 123 · 分辨率 1080p'), release_text
            assert release_text.endswith('大小 2.3 GB'), release_text
            assert '首文件' not in release_text
            assert await first.evaluate('''element => {
                const summary = element.querySelector('.shared-settings__preview').getBoundingClientRect()
                const title = element.querySelector('.shared-settings__torrent-title').getBoundingClientRect()
                return summary.bottom <= title.top
            }''')
            assert '23.976 FPS' in await first.inner_text()
            assert await first.locator('.shared-settings__meta').evaluate(
                "element => getComputedStyle(element).color") == 'rgba(0, 0, 0, 0.72)'
            assert await dialog.locator('.shared-settings__badge[data-tone="success"]').count() >= 1
            assert await dialog.locator('.shared-settings__badge[data-tone="error"]').count() >= 1
            assert await first.locator('details').count() == 0
            assert await first.locator('.shared-settings__id').count() == 1
            assert '申请编号：' in await first.inner_text()
            assert await first.locator('.shared-settings__reason').all_text_contents() == [
                '命中记录：已完成 · 下载方：朋友A']
            assert '过滤原因' not in await first.inner_text()
            assert '命中占位' not in await first.inner_text()
            assert '占位方' not in await first.inner_text()
            assert await dialog.get_by_role("button", name="核验未完成申请", exact=True).count() == 0
            await verify_badge_colors(dialog)
            await verify_record_layout(dialog)
            await verify_record_search(page, dialog, '本机下载申请', card_bounds['height'], output, width)
            if host_css:
                stylesheet = await page.add_style_tag(path=str(host_css))
                for scheme in ('light', 'dark'):
                    await page.evaluate('scheme => window.resourceflowSetTheme(scheme)', scheme)
                    await page.evaluate('''scheme => {
                        for (const element of [document.documentElement, document.body]) {
                            element.dataset.theme = 'glass'
                            element.dataset.glassColorScheme = scheme
                        }
                    }''', scheme)
                    await verify_badge_colors(dialog)
                    await verify_record_layout(dialog)
                    await first.scroll_into_view_if_needed()
                    await page.screenshot(path=str(output / f'shared-glass-{scheme}-{width}.png'), full_page=True)
                await page.evaluate('() => window.resourceflowSetTheme("light")')
                await page.evaluate('''() => {
                    for (const element of [document.documentElement, document.body]) {
                        delete element.dataset.theme
                        delete element.dataset.glassColorScheme
                    }
                }''')
                await stylesheet.evaluate('element => element.remove()')
            await first.scroll_into_view_if_needed()
            await page.screenshot(path=str(output / f"shared-requests-{width}.png"), full_page=True)
            assert await records.evaluate('element => element.clientHeight >= element.scrollHeight - 1')
            assert await dialog.get_by_label("启用频道监听", exact=True).count() == 0
            assert await dialog.get_by_role("button", name="立即同步", exact=True).count() == 0
            assert await dialog.get_by_label("协调 Bot 用户名", exact=True).count() == 0
            assert await dialog.get_by_text("全局去重字段", exact=True).count() == 0
            assert await dialog.get_by_text("资源去重规则", exact=True).count() == 0
            assert await dialog.get_by_label("剧集身份绑定", exact=True).count() == 0
            assert await dialog.get_by_role("tab", name="长期账本", exact=True).count() == 0
            assert await dialog.get_by_role("tab", name="共享已完成", exact=True).count() == 0
            assert await dialog.get_by_role("tab", name="下载待核验", exact=True).count() == 0
            assert await close_button.is_visible()
            await close_button.click()
            assert not await page.evaluate("window.resourceflowRequests.some(item => item.path.includes('/resource-ledger'))")
            await page.get_by_role("button", name="全局设置", exact=True).click()
            await dialog.get_by_label("本机名称", exact=True).wait_for()
            await page.wait_for_function("document.querySelector('.shared-settings__instance input')?.value")
            assert await dialog.get_by_label("启用共享下载审批", exact=True).is_checked()
            assert await dialog.get_by_text("共享下载审批", exact=True).count() >= 1
            assert await dialog.get_by_text("Tailscale 私网 HTTPS", exact=True).count() == 0
            assert await dialog.get_by_text(re.compile(r"Telegram|协调 Bot")).count() == 0
            assert await dialog.get_by_label("协调端 Tailscale HTTPS 域名", exact=True).input_value() == "approval.example.ts.net"
            assert await dialog.get_by_role("tab").count() == 0
            assert await dialog.locator('.shared-settings__records').count() == 0
            global_bounds = await dialog.bounding_box()
            assert abs(global_bounds['width'] - bounds['width']) <= 1
            assert abs(global_bounds['height'] - bounds['height']) <= 1
            assert await dialog.get_by_label('启用插件', exact=True).is_checked()
            assert await dialog.get_by_label('显示侧栏入口', exact=True).is_checked()
            assert await dialog.get_by_label('全局保种体积（GB）', exact=True).count() == 1
            assert await dialog.get_by_label('全局同时下载任务数', exact=True).count() == 1
            assert await dialog.get_by_text("共享规格去重字段", exact=True).count() == 0
            assert await dialog.get_by_text("资源去重规则", exact=True).count() == 0
            assert await dialog.locator('.channel-dedup__body').evaluate('''element =>
                element.querySelector('input[type=number]').compareDocumentPosition(
                    element.querySelector('.shared-settings')) & Node.DOCUMENT_POSITION_FOLLOWING''')
            await dialog.get_by_label("本机名称", exact=True).fill("NAS A 修改后")
            await dialog.get_by_label('全局保种体积（GB）', exact=True).fill('500')
            await dialog.get_by_label('种子黑名单', exact=True).check()
            series_bindings = dialog.get_by_label('剧集身份绑定', exact=True)
            assert await series_bindings.is_disabled()
            if width == 1440:
                await page.wait_for_timeout(31000)
                assert await dialog.get_by_label('全局保种体积（GB）', exact=True).input_value() == '500'
                assert await series_bindings.is_disabled()
                assert await dialog.get_by_label("本机名称", exact=True).input_value() == "NAS A 修改后"
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path === 'plugin/ResourceFlow/settings' && item.body.global_disksize === 500
                && item.body.global_torrent_blacklist_enabled === true)""")
            await page.wait_for_function("!document.querySelector('button.v-btn--loading')")
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            await page.get_by_role("button", name="全局设置", exact=True).click()
            await page.wait_for_function("document.querySelector('.shared-settings__instance input')?.value")
            assert await dialog.get_by_label("本机名称", exact=True).input_value() == "NAS A 修改后"
            assert await dialog.get_by_label('全局保种体积（GB）', exact=True).input_value() == '500'
            assert await dialog.get_by_label('种子黑名单', exact=True).is_checked()
            assert await dialog.get_by_label('剧集身份绑定', exact=True).is_disabled()
            client_saves = await page.evaluate("""window.resourceflowRequests.filter(item =>
                item.path.endsWith('/channel-dedup/settings'))""")
            assert client_saves and all("attributes" not in row["body"] for row in client_saves)
            await dialog.get_by_role("button", name="测试审批连接", exact=True).click()
            await page.wait_for_function("""window.resourceflowRequests.some(item => item.path.endsWith('/shared/test'))""")
            await dialog.get_by_text("审批连接测试成功，规则版本已同步", exact=True).wait_for()
            await page.evaluate("window.resourceflowFailSharedTest = true")
            await dialog.get_by_role("button", name="测试审批连接", exact=True).click()
            await dialog.get_by_text("审批连接测试失败：模拟审批测试失败", exact=True).wait_for()
            await page.evaluate("window.resourceflowFailSharedTest = false")
            assert await dialog.get_by_role("button", name="采用协调端匹配字段", exact=True).count() == 0
            await dialog.get_by_label("在本机运行协调端", exact=True).check()
            await dialog.get_by_label("提供私网 HTTPS 审批入口", exact=True).check()
            await dialog.get_by_text("共享规格去重字段", exact=True).wait_for()
            await dialog.get_by_label("剧集身份绑定", exact=True).fill("玫瑰 = 123\nTMDB 336181 = 336292")
            assert await dialog.get_by_label("分辨率", exact=True).is_checked()
            for label in ("特效", "编码", "来源", "帧率", "大小"):
                assert not await dialog.get_by_label(label, exact=True).is_checked()
            token = dialog.get_by_label("本机私网审批密钥（已保存）", exact=True)
            assert await token.get_attribute("type") == "password"
            await token.fill("A" * 43)
            users = dialog.get_by_label("允许的私网设备", exact=True)
            for invalid in ('11;abc', f'{"a" * 32} -22 {"b" * 64}'):
                await users.fill(invalid)
                before = await page.evaluate("window.resourceflowRequests.filter(item => item.method === 'POST').length")
                await dialog.get_by_role('button', name='保存设置', exact=True).click()
                await dialog.get_by_text(re.compile('全局设置未全部保存：私网设备格式无效')).wait_for()
                assert before == await page.evaluate("window.resourceflowRequests.filter(item => item.method === 'POST').length")
            mixed_users = f'{"a" * 32} 11 {"b" * 64}'
            await users.fill(mixed_users)
            await dialog.get_by_role('button', name='刷新共享状态', exact=True).click()
            await page.wait_for_function("!document.querySelector('.global-settings__footer button:disabled')")
            assert await users.input_value() == mixed_users
            assert await token.input_value() == "A" * 43
            await dialog.get_by_label('全局同时下载任务数', exact=True).fill('6')
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path.endsWith('/shared/settings') && item.body.host_coordinator === true
                && item.body.https_peers[0].sender === 11
                && !('bot_username' in item.body) && !('transport' in item.body))""")
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path.endsWith('/channel-dedup/settings') && item.body.attributes?.length === 1
                && item.body.attributes[0] === 'resolution' && item.body.enabled === false
                && item.body.channels === '' && item.body.size_tolerance_pct === 3
                && item.body.identity_aliases.includes('TMDB 336181 = 336292'))""")
            await page.wait_for_function("document.querySelector('input[type=password]').value === ''")
            await page.wait_for_function("!document.querySelector('button.v-btn--loading')")
            assert await users.input_value() == mixed_users
            assert await dialog.get_by_label('全局同时下载任务数', exact=True).input_value() == '6'
            await verify_settings_layout(dialog, width)
            await dialog.get_by_label('全局动态删种', exact=True).check()
            await dialog.get_by_label('全局动态删种阈值（GB）', exact=True).fill('50-100')
            await dialog.get_by_label('全局动态删种', exact=True).uncheck()
            assert await dialog.get_by_label('全局动态删种阈值（GB）', exact=True).count() == 0
            await dialog.get_by_label('大小', exact=True).check()
            await dialog.get_by_label('单集文件大小容差', exact=True).wait_for()
            await dialog.get_by_label('大小', exact=True).uncheck()
            await dialog.locator(".v-card-text").evaluate("element => { element.scrollTop = 0 }")
            await page.screenshot(path=str(output / f"global-settings-{width}.png"), full_page=True)
            await dialog.get_by_label("本机名称", exact=True).scroll_into_view_if_needed()
            await page.screenshot(path=str(output / f"global-shared-{width}.png"), full_page=True)
            await dialog.get_by_text("共享规格去重字段", exact=True).scroll_into_view_if_needed()
            await page.screenshot(path=str(output / f"global-policy-{width}.png"), full_page=True)
            await dialog.locator('.global-settings__aliases').scroll_into_view_if_needed()
            await page.screenshot(path=str(output / f"global-aliases-{width}.png"), full_page=True)
            if host_css:
                stylesheet = await page.add_style_tag(path=str(host_css))
                for scheme in ('light', 'dark'):
                    await page.evaluate('scheme => window.resourceflowSetTheme(scheme)', scheme)
                    await page.evaluate('''scheme => {
                        for (const element of [document.documentElement, document.body]) {
                            element.dataset.theme = 'glass'
                            element.dataset.glassColorScheme = scheme
                        }
                    }''', scheme)
                    await dialog.locator(".v-card-text").evaluate("element => { element.scrollTop = 0 }")
                    await page.screenshot(path=str(output / f'global-glass-{scheme}-{width}.png'), full_page=True)
                    await dialog.get_by_text("共享规格去重字段", exact=True).scroll_into_view_if_needed()
                    await page.screenshot(path=str(output / f'global-policy-glass-{scheme}-{width}.png'), full_page=True)
                    await verify_settings_layout(dialog, width)
                    assert await dialog.locator(".v-card-text").evaluate("element => element.scrollWidth <= element.clientWidth + 1")
                    clipped = await dialog.locator(".v-label, .v-btn__content").evaluate_all("""elements =>
                        elements.filter(item => item.getBoundingClientRect().width > 0
                            && item.scrollWidth > item.clientWidth + 2).map(item => item.textContent)""")
                    assert not clipped, (width, scheme, clipped)
                await page.evaluate('() => window.resourceflowSetTheme("light")')
                await page.evaluate('''() => {
                    for (const element of [document.documentElement, document.body]) {
                        delete element.dataset.theme
                        delete element.dataset.glassColorScheme
                    }
                }''')
                await stylesheet.evaluate('element => element.remove()')
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            await page.get_by_role("button", name="共享下载审批", exact=True).click()
            await dialog.get_by_role("tab", name="共享下载记录", exact=True).wait_for()
            assert await dialog.get_by_role("tab").all_text_contents() == [
                '本机下载申请', '共享下载记录']
            await dialog.get_by_role("tab", name="共享下载记录", exact=True).click()
            await dialog.get_by_text("朋友A", exact=False).wait_for()
            shared_position = await search_box.bounding_box()
            for axis in ('x', 'y'):
                assert abs(shared_position[axis] - search_position[axis]) <= 1, (search_position, shared_position)
            claim = dialog.locator('.shared-settings__records article').first
            assert '下载时间：' in await claim.inner_text()
            assert '最近更新' in await claim.inner_text()
            assert '下载方：朋友A' in await claim.inner_text()
            metadata = await claim.locator('.shared-settings__claim-meta > span').all_text_contents()
            assert metadata[-2:] == ['站点：Test Site', '下载方：朋友A'], metadata
            assert '占位编号：' in await claim.inner_text()
            claim_identity = await claim.locator('.shared-settings__preview').inner_text()
            assert '玫瑰战争 · S01E05 · TMDB 123 · 分辨率 1080p' in claim_identity
            assert await claim.locator('.shared-settings__claim-info .shared-settings__info').count() == 0
            assert await claim.locator('.shared-settings__claim-owner').inner_text() == '下载方：朋友A'
            assert await claim.locator('.shared-settings__preview.shared-settings__info').count() == 1
            for label in ('已完成', '已入队', '已释放'):
                assert await records.locator('.shared-settings__record-head .shared-settings__badge').filter(has_text=label).count() == 1
            await verify_badge_colors(dialog, all_tones=False)
            await verify_record_layout(dialog)
            await verify_record_search(page, dialog, '共享下载记录', card_bounds['height'], output, width)
            assert await claim.locator('.shared-settings__claim-meta').evaluate(
                "element => getComputedStyle(element).display") == 'flex'
            assert '23.976 FPS' in await claim.inner_text()
            await claim.scroll_into_view_if_needed()
            await page.screenshot(path=str(output / f"shared-claims-{width}.png"), full_page=True)
            if host_css:
                stylesheet = await page.add_style_tag(path=str(host_css))
                for scheme in ('light', 'dark'):
                    await page.evaluate('scheme => window.resourceflowSetTheme(scheme)', scheme)
                    await page.evaluate('''scheme => {
                        for (const element of [document.documentElement, document.body]) {
                            element.dataset.theme = 'glass'
                            element.dataset.glassColorScheme = scheme
                        }
                    }''', scheme)
                    await verify_badge_colors(dialog, all_tones=False)
                    await verify_record_layout(dialog)
                    await claim.scroll_into_view_if_needed()
                    await page.screenshot(path=str(output / f'shared-claims-glass-{scheme}-{width}.png'), full_page=True)
                await page.evaluate('() => window.resourceflowSetTheme("light")')
                await page.evaluate('''() => {
                    for (const element of [document.documentElement, document.body]) {
                        delete element.dataset.theme
                        delete element.dataset.glassColorScheme
                    }
                }''')
                await stylesheet.evaluate('element => element.remove()')
            await dialog.get_by_role("tab", name="本机下载申请", exact=True).click()
            await dialog.locator(".v-card-text").evaluate("element => { element.scrollTop = 0 }")
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(output / f"shared-settings-{width}.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            clipped = await dialog.locator(".v-label, .v-btn__content").evaluate_all("""elements =>
                elements.filter(item => item.getBoundingClientRect().width > 0
                    && item.scrollWidth > item.clientWidth + 2).map(item => item.textContent)""")
            assert not clipped, (width, clipped)
            await close_button.click()
            await page.get_by_role("button", name="全局设置", exact=True).click()
            await page.wait_for_function("document.querySelector('.shared-settings__instance input')?.value")
            await page.evaluate("window.resourceflowFailSave = true")
            before = await page.evaluate("""window.resourceflowRequests.filter(item =>
                item.path.endsWith('/channel-dedup/settings')).length""")
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await dialog.get_by_text("模拟共享保存失败", exact=True).first.wait_for()
            after = await page.evaluate("""window.resourceflowRequests.filter(item =>
                item.path.endsWith('/channel-dedup/settings')).length""")
            assert before == after
            assert await dialog.is_visible()
            await page.evaluate("window.resourceflowFailSave = false")
            for stage, message in (('policy', '模拟规则保存失败'), ('global', '模拟全局保存失败')):
                await page.evaluate("stage => window.resourceflowFailStage = stage", stage)
                before = await page.evaluate("""window.resourceflowRequests.filter(item =>
                    item.path === 'plugin/ResourceFlow/settings').length""")
                await dialog.get_by_role("button", name="保存设置", exact=True).click()
                await dialog.get_by_text(f"全局设置未全部保存：{message}", exact=True).wait_for()
                assert await dialog.is_visible()
                after = await page.evaluate("""window.resourceflowRequests.filter(item =>
                    item.path === 'plugin/ResourceFlow/settings').length""")
                assert after == before + (stage == 'global'), (stage, before, after)
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            await page.evaluate("window.resourceflowFailStage = ''; window.resourceflowFailSharedLoad = true")
            await page.get_by_role("button", name="全局设置", exact=True).click()
            await dialog.get_by_text('模拟共享读取失败', exact=True).wait_for()
            before = await page.evaluate("window.resourceflowRequests.filter(item => item.method === 'POST').length")
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await dialog.get_by_text('全局设置未全部保存：共享连接尚未加载，请关闭后重新打开全局设置', exact=True).wait_for()
            assert before == await page.evaluate("window.resourceflowRequests.filter(item => item.method === 'POST').length")
            assert not errors, errors
            assert not await page.evaluate("window.resourceflowRequests.some(item => item.path.endsWith('/poll'))")
            await page.goto(f"{base_url}/tests/channel-dedup-smoke.html?task=traffic")
            await page.wait_for_function("window.resourceflowReady === true")
            approval_badge = page.locator('.resourceflow-task-head__title').get_by_text('下载审批', exact=True)
            task_item = page.locator('.resourceflow-task-item').filter(has_text='Saved Traffic')
            await page.locator('.resourceflow-task-head__title').get_by_role('heading', name='【分享】Saved Traffic').wait_for()
            assert await approval_badge.count() == 0
            assert await task_item.get_by_text('Test Site · QB', exact=True).count() == 1
            assert await page.get_by_text('关闭去重', exact=False).count() == 0
            await page.locator('button:has(.mdi-pencil-outline)').click()
            editor = page.get_by_role('dialog')
            await editor.get_by_text('编辑刷流任务', exact=True).wait_for()
            approval = editor.get_by_label('共享下载审批', exact=True)
            assert not await approval.is_checked()
            assert await editor.get_by_label('规格去重', exact=True).count() == 0
            switches = await editor.locator('.editor-switches label').all_text_contents()
            assert switches.index('共享下载审批') > switches.index('发送通知'), switches
            await approval.check()
            await approval.scroll_into_view_if_needed()
            await page.screenshot(path=str(output / f'task-approval-{width}.png'), full_page=True)
            await editor.get_by_role('tab', name='选种规则').click()
            assert await editor.get_by_text('当前沿用旧下载类型：剧集', exact=True).count() == 1
            assert await editor.get_by_label('种子黑名单', exact=True).count() == 0
            await editor.get_by_label('打开 MoviePilot 整理分类', exact=True).press('Enter')
            await page.get_by_role('option', name='电影 · 精选/独立制作', exact=True).click()
            await page.keyboard.press('Escape')
            recent = editor.get_by_label('仅下载最近播出剧集', exact=True)
            await recent.check()
            recent_row = editor.locator('.editor-recent-rule')
            assert await recent_row.count() == 1
            days = editor.get_by_label('最近播出天数', exact=True)
            await days.fill('7')
            await recent_row.scroll_into_view_if_needed()
            assert await recent_row.evaluate('''element => {
                const toggle = element.querySelector('.editor-recent-rule__switch').getBoundingClientRect()
                const days = element.querySelector('.editor-recent-rule__days').getBoundingClientRect()
                return days.left >= toggle.right && Math.abs(days.top + days.height / 2 - toggle.top - toggle.height / 2) < 2
                    && element.scrollWidth <= element.clientWidth + 1
            }''')
            await page.screenshot(path=str(output / f'task-recent-days-{width}.png'), full_page=True)
            await recent.uncheck()
            assert await days.count() == 0
            await recent.check()
            assert await days.input_value() == '7'
            await editor.get_by_role('button', name='保存任务', exact=True).click()
            await page.wait_for_function('''window.resourceflowRequests.some(item =>
                item.method === 'PUT' && item.body.spec_dedup_enabled === true
                && item.body.recent_episodes_days === 7
                && item.body.download_categories.join() === 'movie:精选/独立制作'
                && !('blacklist_site_ids' in item.body))''')
            await editor.wait_for(state='hidden')
            await approval_badge.wait_for()
            assert await task_item.get_by_text('下载审批 · Test Site · QB', exact=True).count() == 1
            assert await page.get_by_text('开启去重', exact=False).count() == 0
            await page.locator('button:has(.mdi-pencil-outline)').click()
            assert await editor.get_by_label('共享下载审批', exact=True).is_checked()
            await editor.get_by_role('button', name='设置全局去重规则', exact=True).click()
            global_dialog = page.get_by_role('dialog').filter(has=page.get_by_label('启用插件', exact=True))
            await global_dialog.get_by_label('本机名称', exact=True).wait_for()
            assert await global_dialog.get_by_role('tab', name='本机下载申请', exact=True).count() == 0
            assert not errors, errors
            await page.close()
        for width, height in ((1440, 1000), (390, 844)):
            page = await browser.new_page(viewport={"width": width, "height": height}, locale="zh-CN")
            await page.goto(f"{base_url}/tests/channel-dedup-smoke.html?authority=1")
            await page.wait_for_function("window.resourceflowReady === true")
            await page.get_by_role("button", name="共享下载审批", exact=True).click()
            dialog = page.get_by_role("dialog")
            await dialog.get_by_text("Show S01E05", exact=True).wait_for()
            assert await dialog.get_by_text("全部参与设备的共享下载记录", exact=True).count() == 0
            assert await dialog.get_by_role("tab").all_text_contents() == [
                '本机下载申请', '共享下载记录']
            await dialog.get_by_text("共享占位关联异常 1 个，暂缓新增下载", exact=True).wait_for()
            await dialog.get_by_text("历史完成证据待同步：2 条；同步确认前暂缓新增下载。", exact=True).wait_for()
            assert await dialog.get_by_text("历史完成证据不完整", exact=False).count() == 0
            await dialog.get_by_text(f"关联占位：{'9' * 32}", exact=True).wait_for()
            assert "命中占位" not in await dialog.inner_text()
            await dialog.get_by_role("tab", name="共享下载记录", exact=True).click()
            await dialog.get_by_text(f"关联申请：{'b' * 32}", exact=True).wait_for()
            toolbar = dialog.locator('.shared-settings__search:visible')
            await toolbar.locator('.v-select').filter(has=page.locator('label', has_text='状态筛选')).click()
            await page.get_by_role('option', name='已完成', exact=True).click()
            await page.wait_for_function("""[...document.querySelectorAll('.shared-settings__record')]
                .filter(element => element.getBoundingClientRect().width > 0).length === 1""")
            await dialog.get_by_text("下载方：边城旧事", exact=True).wait_for()
            assert (await dialog.locator('.shared-settings__badge').inner_text()).strip() == '已完成'
            assert await dialog.get_by_role("tab", name="共享已完成", exact=True).count() == 0
            assert await dialog.get_by_role("button", name="移除长期证据", exact=True).count() == 0
            assert not await page.evaluate("""window.resourceflowRequests.some(item =>
                item.path.includes('/resource-ledger?') && !item.path.includes('state=pending'))""")
            assert await dialog.locator(".v-card-text").evaluate(
                "element => element.scrollWidth <= element.clientWidth + 1")
            await page.screenshot(path=str(output / f"shared-authority-{width}.png"), full_page=True)
            await page.close()
        for width, height in ((1440, 1000), (390, 844), (320, 720)):
            page = await browser.new_page(viewport={"width": width, "height": height}, locale="zh-CN")
            await page.goto(f"{base_url}/tests/channel-dedup-smoke.html?pack=1")
            await page.wait_for_function("window.resourceflowReady === true")
            await page.get_by_role("button", name="共享下载审批", exact=True).click()
            dialog = page.get_by_role("dialog")
            first = dialog.locator('.shared-settings__record').first
            await first.get_by_text('Phoenix Rising S01E01-E06', exact=True).wait_for()
            summary = first.locator('.shared-settings__preview')
            assert await summary.count() == 1
            text = await summary.inner_text()
            assert text.startswith('凤起微月 · S01E01-06 · TMDB 342129'), text
            assert text.endswith('大小 6.0 GB'), text
            assert await first.locator('.shared-settings__reason').all_text_contents() == [
                '命中记录：已完成 · 下载方：设备B']
            await first.get_by_text('已过滤（6）', exact=True).wait_for()
            assert '首文件' not in await first.inner_text()
            assert '命中占位' not in await first.inner_text()
            completed = dialog.locator('.shared-settings__record').filter(has_text='Lets Go S01 2026 2160p WEB-DL')
            assert await completed.count() == 1
            assert await completed.locator('.shared-settings__preview').all_text_contents() == [
                '走马上任 · S01E36-40 · TMDB 275297 · 分辨率 2160p · 特效 HDR · 编码 H.265 · 来源 WEB-DL · 大小 16.5 GB']
            await completed.get_by_text(f"关联占位：{'1' * 32} 等5个", exact=True).wait_for()
            assert '2' * 32 not in await completed.inner_text()
            await verify_record_layout(dialog)
            await page.screenshot(path=str(output / f'pack-summary-{width}.png'), full_page=True)
            await page.close()
        await browser.close()
    print("Verified 1440, 768, 390 and 320: two record tabs keep dialog height; completed filter replaces separate ledger tab; search, filters, pagination and stale-response guards; global settings and task editor regressions")
    if host_css:
        print('MoviePilot host stylesheet verified with glass light/dark overrides')


async def verify_selection_settings(base_url, output, executable_path, host_css=None):
    output.mkdir(exist_ok=True, parents=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=executable_path, args=["--no-sandbox"])
        for width in (1440, 768, 390, 320):
            page = await browser.new_page(viewport={"width": width, "height": 900}, locale="zh-CN")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(f"{base_url}/tests/channel-dedup-smoke.html?task=traffic")
            await page.wait_for_function("window.resourceflowReady === true")
            if host_css:
                await page.add_style_tag(path=str(host_css))
                await page.evaluate("""() => {
                    window.resourceflowSetTheme("dark")
                    for (const element of [document.documentElement, document.body]) {
                        element.dataset.theme = "glass"
                        element.dataset.glassColorScheme = "dark"
                    }
                }""")
            dialog = page.get_by_role("dialog")
            await page.get_by_role("button", name="全局设置", exact=True).click()
            await dialog.get_by_role("button", name="测试审批连接", exact=True).wait_for()
            await page.wait_for_function("document.querySelector('.shared-settings__instance input')?.value")
            assert await dialog.locator(".global-settings__section > h3").all_text_contents() == [
                "基础", "运行限额与删种", "缺失播出日期兜底", "选种与去重",
                "共享下载审批", "规范媒体身份绑定",
            ]
            assert await dialog.get_by_label("下载低速观察时长", exact=True).count() == 1
            assert await dialog.get_by_label("下载最低平均速度", exact=True).count() == 1
            assert await dialog.get_by_label("共享下载低速观察时长", exact=True).count() == 0
            assert await dialog.get_by_label("共享下载最低平均速度", exact=True).count() == 0
            assert await dialog.get_by_text(re.compile(r"Telegram|协调 Bot")).count() == 0
            assert await dialog.get_by_label("协调端 Tailscale HTTPS 域名").input_value() == "approval.example.ts.net"
            douban = dialog.locator(".global-settings__selection .v-select")
            assert await douban.count() == 1
            assert await dialog.locator(".global-settings__recent-date").get_by_label(
                "当仅豆瓣可识别时", exact=True).count() == 0
            assert await douban.get_by_text("排除过滤", exact=True).count() == 1
            await douban.click()
            await page.get_by_role("option", name="排除过滤", exact=True).wait_for()
            await page.get_by_role("option", name="执行下载", exact=True).click()
            await dialog.get_by_label("种子黑名单", exact=True).check()
            await dialog.get_by_label("国产剧仅下载 4K 及以上", exact=True).check()
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path === 'plugin/ResourceFlow/settings' && item.body.global_torrent_blacklist_enabled
                && item.body.douban_only_action === 'download' && item.body.domestic_drama_4k_only === true)""")
            assert await page.evaluate("""window.resourceflowRequests.filter(item =>
                item.path.endsWith('/shared/settings')).every(item =>
                !['bot_username', 'bot_token', 'allowed_users', 'transport'].some(key => key in item.body))""")
            assert await dialog.locator(".v-card-text").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            await dialog.get_by_label("在本机运行协调端", exact=True).check()
            await dialog.get_by_label("提供私网 HTTPS 审批入口", exact=True).check()
            specs = dialog.locator(".v-select").filter(has=page.locator("label", has_text="候选缺少勾选规格时"))
            await specs.locator(".v-field").click()
            await page.get_by_role("option", name="排除过滤", exact=True).wait_for()
            await page.get_by_role("option", name="执行下载", exact=True).wait_for()
            await page.keyboard.press("Escape")
            await verify_settings_layout(dialog, width)
            await dialog.locator(".v-card-text").evaluate("el => { el.scrollTop = 0 }")
            await page.screenshot(path=str(output / f"global-settings-{width}.png"))
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            await page.locator("button:has(.mdi-pencil-outline)").click()
            await dialog.get_by_role("tab", name="选种规则").click()
            assert await dialog.get_by_text("当前沿用旧下载类型：剧集", exact=True).count() == 1
            assert await dialog.get_by_label("种子黑名单", exact=True).count() == 0
            await dialog.get_by_label("打开 MoviePilot 整理分类", exact=True).press("Enter")
            await page.get_by_role("option", name="电影 · 精选/独立制作", exact=True).click()
            await page.keyboard.press("Escape")
            await dialog.get_by_text("电影 · 精选/独立制作", exact=True).wait_for()
            await page.screenshot(path=str(output / f"selection-{width}.png"))
            await dialog.get_by_role("button", name="保存任务", exact=True).click()
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.method === 'PUT' && item.body.download_categories.join() === 'movie:精选/独立制作'
                && !('blacklist_site_ids' in item.body))""")
            assert not errors, errors
            await page.close()
        await browser.close()
    print("Verified HTTPS-only settings and MoviePilot classification at 1440, 768, 390 and 320px")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable-path", default="/opt/google/chrome/chrome")
    parser.add_argument("--host-css", type=Path)
    parser.add_argument("--settings-only", action="store_true")
    args = parser.parse_args()
    check = verify_selection_settings if args.settings_only else verify
    asyncio.run(check(args.base_url, args.output, args.executable_path, args.host_css))
