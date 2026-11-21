"""Shared settings browser checks against a mock API, without real Telegram traffic."""

import argparse
import asyncio
from pathlib import Path

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
            fits: element.scrollWidth <= element.clientWidth + 1}
    }''')
    assert all(toggles.values()), (width, toggles)
    assert await dialog.locator('.global-settings__toggles label').all_text_contents() == [
        '启用插件', '显示侧栏入口', '全局动态删种']
    for selector in ('.global-settings__aliases', '.shared-settings__credentials'):
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
    assert rows == (1 if width >= 960 else 2 if width >= 600 else 4), (width, limits)
    if width >= 960:
        assert all(item['width'] < 320 for item in limits), limits
    heights = await dialog.locator('.global-settings__limits .v-field').evaluate_all(
        'elements => elements.map(element => element.getBoundingClientRect().height)')
    assert all(value >= 48 for value in heights), (width, heights)
    status = await dialog.locator('.shared-settings__status').evaluate('''element =>
        [...element.children].slice(0, 3).map(child => child.getBoundingClientRect().top)''')
    if width >= 600:
        assert max(status) - min(status) < 2, (width, status)
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
    assert await dialog.locator('.global-settings__body button[aria-label="测试审批连接"]').count() == 0
    assert await dialog.locator('.global-settings__footer button[aria-label="测试审批连接"] .mdi-connection').count() == 1
    assert await dialog.locator('.v-card-text').evaluate(
        'element => element.scrollWidth <= element.clientWidth + 1')


async def verify_record_search(page, dialog, tab, initial_height, output, width, host_css=None):
    ledger = tab == '下载待核验'
    selector = '.channel-dedup__record' if ledger else '.shared-settings__record'
    await page.evaluate('window.resourceflowExtraRecords = 125')
    await dialog.get_by_role('button', name='刷新资源记录', exact=True).click()
    await page.wait_for_function("""selector =>
        [...document.querySelectorAll(selector)].filter(item => item.getBoundingClientRect().width > 0).length === 20
    """, arg=selector)
    expected_total = {'本机下载申请': 130, '共享下载记录': 129, '下载待核验': 126}[tab]
    total = dialog.locator('.shared-settings__pagination .shared-settings__total')
    assert await total.inner_text() == f'共 {expected_total} 条'
    if ledger:
        assert await dialog.locator('.channel-dedup__records + .shared-settings__pagination .shared-settings__total').count() == 1
        assert await dialog.get_by_text(f'共 {expected_total} 条', exact=True).count() == 1
        layout = await dialog.locator('.channel-dedup__records').evaluate('''element => {
            const body = element.closest('.v-card-text')
            const style = getComputedStyle(element)
            return {max: style.maxHeight, overflow: style.overflowY,
                height: element.getBoundingClientRect().height,
                ownScroll: element.scrollHeight > element.clientHeight + 1,
                bodyScroll: body.scrollHeight > body.clientHeight + 1}
        }''')
        assert layout['max'] == 'none' and layout['overflow'] == 'visible', (tab, width, layout)
        assert layout['height'] > 400 and not layout['ownScroll'] and layout['bodyScroll'], (tab, width, layout)
        assert '测试剧集 · S01' in await dialog.locator('.channel-dedup__record-identity').nth(1).inner_text()
        await dialog.locator('.channel-dedup__record').last.scroll_into_view_if_needed()
        last = await dialog.locator('.channel-dedup__record').last.bounding_box()
        body = await dialog.locator('.v-card-text').bounding_box()
        assert last['y'] + last['height'] <= body['y'] + body['height'] + 1, (tab, width, last, body)
        await dialog.locator('.v-card-text').evaluate('element => { element.scrollTop = 0 }')
        await page.screenshot(path=str(output / f'ledger-many-{tab}-{width}.png'), full_page=True)
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
                assert await dialog.locator('.channel-dedup__records').evaluate('''element =>
                    getComputedStyle(element).maxHeight === 'none'
                    && getComputedStyle(element).overflowY === 'visible'
                    && element.scrollHeight <= element.clientHeight + 1''')
                await dialog.locator('.channel-dedup__record').last.scroll_into_view_if_needed()
                last = await dialog.locator('.channel-dedup__record').last.bounding_box()
                body = await dialog.locator('.v-card-text').bounding_box()
                assert last['y'] + last['height'] <= body['y'] + body['height'] + 1, (tab, width, scheme)
                await dialog.locator('.v-card-text').evaluate('element => { element.scrollTop = 0 }')
                await page.screenshot(path=str(output / f'ledger-many-{tab}-glass-{scheme}-{width}.png'), full_page=True)
            await page.evaluate('() => window.resourceflowSetTheme("light")')
            await page.evaluate('''() => {
                for (const element of [document.documentElement, document.body]) {
                    delete element.dataset.theme
                    delete element.dataset.glassColorScheme
                }
            }''')
            await stylesheet.evaluate('element => element.remove()')
    search = dialog.get_by_role('textbox', name='搜索记录')
    await search.scroll_into_view_if_needed()
    await dialog.locator('.v-pagination:visible .v-pagination__item button').nth(1).click()
    await page.wait_for_timeout(400)
    prefix = 'Search Ledger item-' if ledger else 'Search Record item-'
    await search.fill(f'{prefix}124')
    await dialog.get_by_text(f'{prefix}124', exact=True).wait_for()
    assert await dialog.locator(f'{selector}:visible').count() == 1
    assert await dialog.locator('.v-pagination:visible').count() == 0
    assert await total.inner_text() == '共 1 条'
    if not ledger:
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
    toolbar = dialog.locator('.channel-dedup__search:visible, .shared-settings__search:visible')
    status_select = toolbar.locator('.v-select').last
    value = '电影' if ledger else '已过滤' if tab == '本机下载申请' else '已完成'
    await status_select.click()
    if tab == '本机下载申请':
        await page.get_by_role('option', name='已过滤', exact=True).wait_for()
        options = await page.get_by_role('option').all_text_contents()
        assert {option.strip() for option in options} == {
            '全部状态', '已完成', '已入队', '等待 QB 文件清单', '已释放', '已过滤'}, options
    await page.get_by_role('option', name=value, exact=True).click()
    await page.wait_for_function("""selector =>
        [...document.querySelectorAll(selector)].filter(item => item.getBoundingClientRect().width > 0).length === 1
    """, arg=selector)
    await search.fill('not-a-record')
    await dialog.get_by_text('无匹配记录', exact=True).wait_for()
    await status_select.click()
    await page.get_by_role('option', name='全部类型' if ledger else '全部状态', exact=True).click()
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
                '本机下载申请', '共享下载记录', '下载待核验']
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
            assert '2026' in await first.inner_text()
            assert '11:00:00' in await first.inner_text()
            assert '申请时间：' in await first.inner_text()
            assert '最近更新：' in await first.inner_text()
            assert '任务：' in await first.inner_text()
            assert '下载器：' in await first.inner_text()
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
            assert await dialog.get_by_label("剧名别名绑定（可选）", exact=True).count() == 0
            assert await dialog.get_by_role("tab", name="长期账本", exact=True).count() == 0
            assert await dialog.get_by_role("tab", name="共享已完成", exact=True).count() == 0
            await dialog.get_by_role("tab", name="下载待核验", exact=True).click()
            await dialog.get_by_text("Unknown Resource", exact=True).wait_for()
            assert await dialog.get_by_text('待核验影片 · 电影 · 身份待确认 · 分辨率 2160p', exact=True).count() == 1
            assert await dialog.locator('.channel-dedup__badge').count() == 0
            action_layout = await dialog.locator('.channel-dedup__record-actions').first.evaluate("""element => {
                const button = element.querySelector('button').getBoundingClientRect()
                const record = element.closest('article').getBoundingClientRect()
                return {buttonHeight: Math.abs(button.height - 32) < 1,
                    contained: element.getBoundingClientRect().right <= record.right + 1,
                    fits: element.scrollWidth <= element.clientWidth + 1}
            }""")
            assert all(action_layout.values()), action_layout
            await dialog.get_by_role("button", name="核验并继续下载", exact=True).click()
            manual_dialog = page.get_by_role("dialog").filter(has=page.get_by_text("人工核验并继续下载", exact=True))
            await manual_dialog.get_by_label("TMDB ID").fill("123")
            await manual_dialog.get_by_label("我已核对以上媒体身份、季集号及规格").check()
            await manual_dialog.get_by_role("button", name="重新核验并提交", exact=True).click()
            await page.wait_for_function("""() => window.resourceflowRequests.some(item =>
                item.method === 'POST' && item.path.endsWith('/resource-ledger/pending/approve')
                && item.body.tmdb_id === 123 && item.body.confirmed === true)""")
            await verify_record_search(page, dialog, '下载待核验', card_bounds['height'], output, width, host_css)
            await page.screenshot(path=str(output / f'pending-{width}.png'), full_page=True)
            assert await close_button.is_visible()
            await close_button.click()
            assert await page.evaluate("""window.resourceflowRequests.filter(item =>
                item.method === 'POST').map(item => item.path)""") == [
                'plugin/ResourceFlow/resource-ledger/pending/approve']
            await page.get_by_role("button", name="全局设置", exact=True).click()
            await dialog.get_by_label("本机名称", exact=True).wait_for()
            await page.wait_for_function("document.querySelector('.shared-settings__instance input')?.value")
            assert await dialog.get_by_label("启用共享下载审批", exact=True).is_checked()
            assert await dialog.get_by_label("协调 Bot 用户名", exact=True).input_value() == "@testbot"
            assert await dialog.get_by_role("tab").count() == 0
            assert await dialog.locator('.shared-settings__records').count() == 0
            global_bounds = await dialog.bounding_box()
            assert abs(global_bounds['width'] - bounds['width']) <= 1
            assert abs(global_bounds['height'] - bounds['height']) <= 1
            assert await dialog.get_by_label('启用插件', exact=True).is_checked()
            assert await dialog.get_by_label('显示侧栏入口', exact=True).is_checked()
            assert await dialog.get_by_label('全局保种体积（GB）', exact=True).count() == 1
            assert await dialog.get_by_label('全局同时下载任务数', exact=True).count() == 1
            assert await dialog.get_by_text("全局去重字段", exact=True).count() == 0
            assert await dialog.get_by_text("资源去重规则", exact=True).count() == 0
            assert await dialog.locator('.channel-dedup__body').evaluate('''element =>
                element.querySelector('input[type=number]').compareDocumentPosition(
                    element.querySelector('.shared-settings')) & Node.DOCUMENT_POSITION_FOLLOWING''')
            await dialog.get_by_label("本机名称", exact=True).fill("NAS A 修改后")
            await dialog.get_by_label('全局保种体积（GB）', exact=True).fill('500')
            await dialog.get_by_label('剧名别名绑定（可选）', exact=True).fill('玫瑰 = 123')
            if width == 1440:
                await page.wait_for_timeout(31000)
                assert await dialog.get_by_label('全局保种体积（GB）', exact=True).input_value() == '500'
                assert await dialog.get_by_label('剧名别名绑定（可选）', exact=True).input_value() == '玫瑰 = 123'
                assert await dialog.get_by_label("本机名称", exact=True).input_value() == "NAS A 修改后"
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path === 'plugin/ResourceFlow/settings' && item.body.global_disksize === 500)""")
            await page.wait_for_function("!document.querySelector('button.v-btn--loading')")
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            await page.get_by_role("button", name="全局设置", exact=True).click()
            await page.wait_for_function("document.querySelector('.shared-settings__instance input')?.value")
            assert await dialog.get_by_label("本机名称", exact=True).input_value() == "NAS A 修改后"
            assert await dialog.get_by_label('全局保种体积（GB）', exact=True).input_value() == '500'
            assert await dialog.get_by_label('剧名别名绑定（可选）', exact=True).input_value() == '玫瑰 = 123'
            client_saves = await page.evaluate("""window.resourceflowRequests.filter(item =>
                item.path.endsWith('/channel-dedup/settings'))""")
            assert client_saves and all("attributes" not in row["body"] for row in client_saves)
            await dialog.get_by_role("button", name="测试审批连接", exact=True).click()
            await dialog.get_by_text("本机 Telegram ID", exact=True).wait_for()
            assert await dialog.get_by_role("button", name="采用协调端匹配字段", exact=True).count() == 0
            await dialog.get_by_label("在本机运行协调端", exact=True).check()
            await dialog.get_by_text("全局去重字段", exact=True).wait_for()
            await dialog.get_by_text("资源去重规则", exact=True).wait_for()
            assert await dialog.get_by_label("分辨率", exact=True).is_checked()
            for label in ("特效", "编码", "来源", "帧率", "大小"):
                assert not await dialog.get_by_label(label, exact=True).is_checked()
            token = dialog.get_by_label("协调 Bot Token（已保存）", exact=True)
            assert await token.get_attribute("type") == "password"
            await token.fill("123456:FAKE_TOKEN_FOR_UI_TEST_ONLY_12345")
            users = dialog.get_by_label("允许的个人账号 ID", exact=True)
            for invalid in ('11;abc', '-22', '1.5', '9007199254740992'):
                await users.fill(invalid)
                before = await page.evaluate("window.resourceflowRequests.filter(item => item.method === 'POST').length")
                await dialog.get_by_role('button', name='保存设置', exact=True).click()
                await dialog.get_by_text('全局设置未全部保存：白名单须为个人账号数字 ID', exact=True).wait_for()
                assert before == await page.evaluate("window.resourceflowRequests.filter(item => item.method === 'POST').length")
            mixed_users = "11,22;11；22、11 22\n11\t22，11"
            await users.fill(mixed_users)
            await dialog.get_by_role('button', name='刷新共享状态', exact=True).click()
            await page.wait_for_function("!document.querySelector('.global-settings__footer button:disabled')")
            assert await users.input_value() == mixed_users
            assert await token.input_value() == '123456:FAKE_TOKEN_FOR_UI_TEST_ONLY_12345'
            await dialog.get_by_label('全局同时下载任务数', exact=True).fill('6')
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path.endsWith('/shared/settings') && item.body.host_coordinator === true
                && item.body.allowed_users.join(',') === '11,22')""")
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path.endsWith('/channel-dedup/settings') && item.body.attributes?.length === 1
                && item.body.attributes[0] === 'resolution' && item.body.enabled === false
                && item.body.channels === '' && item.body.size_tolerance_pct === 3)""")
            await page.wait_for_function("document.querySelector('input[type=password]').value === ''")
            await page.wait_for_function("!document.querySelector('button.v-btn--loading')")
            assert await users.input_value() == '11, 22'
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
            await dialog.get_by_text("全局去重字段", exact=True).scroll_into_view_if_needed()
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
                    await dialog.get_by_text("全局去重字段", exact=True).scroll_into_view_if_needed()
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
                '本机下载申请', '共享下载记录', '下载待核验']
            await dialog.get_by_role("tab", name="共享下载记录", exact=True).click()
            await dialog.get_by_text("朋友A", exact=False).wait_for()
            claim = dialog.locator('.shared-settings__records article').first
            assert '下载时间：' in await claim.inner_text()
            assert '最近更新' in await claim.inner_text()
            assert '下载方：朋友A' in await claim.inner_text()
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
                && item.body.recent_episodes_days === 7)''')
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
                '本机下载申请', '共享下载记录', '下载待核验']
            await dialog.get_by_text("共享占位关联异常 1 个，暂缓新增下载", exact=True).wait_for()
            await dialog.get_by_text("历史完成证据待同步：2 条；同步确认前暂缓新增下载。", exact=True).wait_for()
            assert await dialog.get_by_text("历史完成证据不完整", exact=False).count() == 0
            await dialog.get_by_text(f"关联占位：{'9' * 32}", exact=True).wait_for()
            assert "命中占位" not in await dialog.inner_text()
            await dialog.get_by_role("tab", name="下载待核验", exact=True).click()
            assert await dialog.get_by_text("历史完成证据不完整", exact=False).count() == 0
            await dialog.get_by_text("历史完成证据待同步：2 条；同步确认前暂缓新增下载。", exact=True).wait_for()
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
        for width in (1440, 390):
            page = await browser.new_page(viewport={"width": width, "height": 900}, locale="zh-CN")
            await page.goto(f"{base_url}/tests/channel-dedup-smoke.html?yearSeason=1")
            await page.wait_for_function("window.resourceflowReady === true")
            await page.evaluate("document.fonts.ready")
            await page.get_by_role("button", name="共享下载审批", exact=True).click()
            await page.get_by_role("tab", name="下载待核验", exact=True).click()
            await page.get_by_role("button", name="核验并继续下载", exact=True).click()
            manual = page.get_by_role("dialog").filter(has=page.get_by_text("人工核验并继续下载", exact=True))
            assert await manual.get_by_label("季号", exact=True).input_value() == ""
            assert await manual.get_by_label("集号", exact=True).input_value() == "45"
            await manual.get_by_role("button", name="按名称查询媒体编号", exact=True).click()
            await manual.get_by_text("剧集 · 2022 · TMDB 154770", exact=True).click()
            await page.wait_for_function('document.querySelector("input[max=\\"999\\"]")?.value === "5"')
            assert await manual.get_by_label("TMDB ID").input_value() == "154770"
            await manual.get_by_label("我已核对以上媒体身份、季集号及规格").check()
            await manual.get_by_role("button", name="重新核验并提交", exact=True).click()
            await page.wait_for_function("""() => window.resourceflowRequests.some(item =>
                item.method === 'POST' && item.path.endsWith('/resource-ledger/pending/approve')
                && item.body.tmdb_id === 154770 && item.body.media_type === 'tv' && item.body.season === 5
                && item.body.episodes.length === 1 && item.body.episodes[0] === 45)""")
            await page.close()
        await browser.close()
    print("Verified 1440, 768, 390 and 320: three record tabs keep dialog height; completed filter replaces separate ledger tab; pending search, filters, pagination and stale-response guards; global settings and task editor regressions")
    if host_css:
        print('MoviePilot host stylesheet verified with glass light/dark overrides')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable-path", default="/opt/google/chrome/chrome")
    parser.add_argument("--host-css", type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.base_url, args.output, args.executable_path, args.host_css))
