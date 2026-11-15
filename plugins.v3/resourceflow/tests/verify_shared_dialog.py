"""Shared settings browser checks against a mock API, without real Telegram traffic."""

import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def verify(base_url, output, executable_path):
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
            assert '最近更新' in await first.inner_text()
            assert 'TMDB 123 · S01E05' in await first.inner_text()
            assert '23.976 FPS' in await first.inner_text()
            assert await first.locator('.shared-settings__meta').evaluate(
                "element => getComputedStyle(element).color") == 'rgba(0, 0, 0, 0.72)'
            assert await first.locator('.v-chip.text-success').count() >= 1
            assert await first.locator('.v-chip.text-error').count() >= 1
            await first.locator('summary').click()
            await first.get_by_text('Show.S01E06.2026.1080p.WEB-DL.H264.AAC.mkv', exact=True).wait_for()
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
            assert 'TMDB 123 · S01E05' in await claim.inner_text()
            assert '23.976 FPS' in await claim.inner_text()
            await claim.scroll_into_view_if_needed()
            await page.screenshot(path=str(output / f"shared-claims-{width}.png"), full_page=True)
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
            await page.close()
        await browser.close()
    print("Shared settings verified: desktop 1440, mobile 390 and 320; no channel polling")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable-path", default="/opt/google/chrome/chrome")
    args = parser.parse_args()
    asyncio.run(verify(args.base_url, args.output, args.executable_path))
