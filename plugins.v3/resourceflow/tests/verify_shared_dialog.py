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
        const title = head.querySelector('strong').getBoundingClientRect()
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


async def verify(base_url, output, executable_path, host_css=None):
    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, executable_path=executable_path,
                                                   args=["--no-sandbox"])
        for width, height in ((1440, 1000), (390, 844), (320, 720)):
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
            assert await dialog.get_by_label("启用共享下载审批", exact=True).is_checked()
            assert await dialog.get_by_label("参与跨实例共享审批", exact=True).count() == 0
            bounds = await dialog.bounding_box()
            assert bounds["width"] >= (1100 if width >= 1000 else width - 2), bounds
            assert bounds["height"] >= height * 0.88, bounds
            await page.wait_for_timeout(300)
            card_bounds = await dialog.locator('.channel-dedup__card').bounding_box()
            assert card_bounds["width"] >= (1100 if width >= 1000 else width - 2), card_bounds
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
            release_text = (await release.inner_text()).removeprefix('首文件 · ')
            assert release_text.startswith('玫瑰战争 · TMDB 123 · S01E05 · 分辨率 1080p'), release_text
            assert '23.976 FPS' in await first.inner_text()
            assert await first.locator('.shared-settings__meta').evaluate(
                "element => getComputedStyle(element).color") == 'rgba(0, 0, 0, 0.72)'
            assert await dialog.locator('.shared-settings__badge[data-tone="success"]').count() >= 1
            assert await dialog.locator('.shared-settings__badge[data-tone="error"]').count() >= 1
            assert await first.locator('details').count() == 0
            assert await first.locator('.shared-settings__id').count() == 1
            assert '申请编号：' in await first.inner_text()
            await verify_badge_colors(dialog)
            await verify_record_layout(dialog)
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
            assert await dialog.get_by_label("协调 Bot 用户名", exact=True).input_value() == "@testbot"
            assert await dialog.get_by_text("全局去重字段", exact=True).count() == 0
            assert await dialog.get_by_text("资源去重规则", exact=True).count() == 0
            await dialog.get_by_label("本机名称", exact=True).fill("NAS A 修改后")
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path.endsWith('/shared/settings') && item.body.instance_label === 'NAS A 修改后')""")
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            await page.get_by_role("button", name="共享下载审批", exact=True).click()
            await page.wait_for_function("document.querySelector('.shared-settings__instance input')?.value")
            assert await dialog.get_by_label("本机名称", exact=True).input_value() == "NAS A 修改后"
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
            for label in ("特效", "编码", "来源", "帧率", "文件大小"):
                assert not await dialog.get_by_label(label, exact=True).is_checked()
            token = dialog.get_by_label("协调 Bot Token（已保存）", exact=True)
            assert await token.get_attribute("type") == "password"
            await token.fill("123456:FAKE_TOKEN_FOR_UI_TEST_ONLY_12345")
            await dialog.get_by_label("允许的个人账号 ID", exact=True).fill("11\n22")
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path.endsWith('/shared/settings') && item.body.host_coordinator === true
                && item.body.allowed_users.join(',') === '11,22')""")
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path.endsWith('/channel-dedup/settings') && item.body.attributes?.length === 1
                && item.body.attributes[0] === 'resolution' && item.body.enabled === false
                && item.body.channels === '' && item.body.size_tolerance_pct === 3)""")
            await page.wait_for_function("document.querySelector('input[type=password]').value === ''")
            await dialog.get_by_role("tab", name="共享占位记录", exact=True).click()
            await dialog.get_by_text("朋友A", exact=False).wait_for()
            claim = dialog.locator('.shared-settings__records article').first
            assert '占位时间' in await claim.inner_text()
            assert '最近更新' in await claim.inner_text()
            assert '下载方：朋友A' in await claim.inner_text()
            assert '占位编号：' in await claim.inner_text()
            claim_identity = await claim.locator('.shared-settings__claim-info').inner_text()
            assert '玫瑰战争 · TMDB 123 · S01E05 · 分辨率 1080p' in claim_identity
            assert await claim.locator('.shared-settings__claim-info .shared-settings__info').count() == 2
            for label in ('已完成', '已入队', '已释放'):
                assert await records.locator('.shared-settings__record-head .shared-settings__badge').filter(has_text=label).count() == 1
            await verify_badge_colors(dialog, all_tones=False)
            await verify_record_layout(dialog)
            assert await claim.locator('.shared-settings__claim-meta').evaluate(
                "element => getComputedStyle(element).display") == 'grid'
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
            await dialog.get_by_role("tab", name="本机共享申请", exact=True).click()
            await dialog.get_by_role("button", name="核验未完成申请", exact=True).click()
            await dialog.locator(".v-card-text").evaluate("element => { element.scrollTop = 0 }")
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(output / f"shared-settings-{width}.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            clipped = await dialog.locator(".v-label, .v-btn__content").evaluate_all("""elements =>
                elements.filter(item => item.getBoundingClientRect().width > 0
                    && item.scrollWidth > item.clientWidth + 2).map(item => item.textContent)""")
            assert not clipped, (width, clipped)
            await page.evaluate("window.resourceflowFailSave = true")
            before = await page.evaluate("""window.resourceflowRequests.filter(item =>
                item.path.endsWith('/channel-dedup/settings')).length""")
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await dialog.get_by_text("模拟共享保存失败", exact=True).first.wait_for()
            after = await page.evaluate("""window.resourceflowRequests.filter(item =>
                item.path.endsWith('/channel-dedup/settings')).length""")
            assert before == after
            assert await dialog.is_visible()
            assert not errors, errors
            assert not await page.evaluate("window.resourceflowRequests.some(item => item.path.endsWith('/poll'))")
            await page.goto(f"{base_url}/tests/channel-dedup-smoke.html?task=traffic")
            await page.wait_for_function("window.resourceflowReady === true")
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
            recent_switches = editor.locator('.editor-switches').filter(has_text='仅下载最近播出剧集')
            assert await recent_switches.count() == 1
            assert await recent_switches.evaluate(
                "element => element.nextElementSibling?.querySelector('input[type=number]') !== null")
            await editor.get_by_role('button', name='保存任务', exact=True).click()
            await page.wait_for_function('''window.resourceflowRequests.some(item =>
                item.method === 'PUT' && item.body.spec_dedup_enabled === true)''')
            await editor.wait_for(state='hidden')
            await page.locator('button:has(.mdi-pencil-outline)').click()
            assert await editor.get_by_label('共享下载审批', exact=True).is_checked()
            assert not errors, errors
            await page.close()
        await browser.close()
    print("Shared settings and task approval verified: 1440, 390 and 320; top-right tonal badges; responsive identity/owner backgrounds; no channel polling")
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
