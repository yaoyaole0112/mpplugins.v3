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
            page = await browser.new_page(viewport={"width": width, "height": height}, locale="zh-CN")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(f"{base_url}/tests/channel-dedup-smoke.html")
            await page.wait_for_function("window.resourceflowReady === true")
            await page.evaluate("document.fonts.ready")
            await page.get_by_role("button", name="共享审批与同规格去重", exact=True).click()
            dialog = page.get_by_role("dialog")
            await dialog.get_by_text("Show S01E05", exact=True).wait_for()
            assert await dialog.get_by_label("启用频道监听", exact=True).count() == 0
            assert await dialog.get_by_role("button", name="立即同步", exact=True).count() == 0
            assert await dialog.get_by_label("协调 Bot 用户名", exact=True).input_value() == "@testbot"
            await dialog.get_by_role("button", name="测试审批连接", exact=True).click()
            await dialog.get_by_text("本机 Telegram ID", exact=True).wait_for()
            await dialog.get_by_role("button", name="采用协调端匹配字段", exact=True).click()
            assert await dialog.get_by_label("分辨率", exact=True).is_checked()
            for label in ("特效", "编码", "来源", "帧率", "文件大小"):
                assert not await dialog.get_by_label(label, exact=True).is_checked()
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path.endsWith('/channel-dedup/settings') && item.body.attributes.length === 1
                && item.body.attributes[0] === 'resolution' && item.body.enabled === false
                && item.body.channels === '' && item.body.size_tolerance_pct === 3)""")
            await dialog.get_by_label("在本机运行协调端", exact=True).check()
            token = dialog.get_by_label("协调 Bot Token（已保存）", exact=True)
            assert await token.get_attribute("type") == "password"
            await token.fill("123456:FAKE_TOKEN_FOR_UI_TEST_ONLY_12345")
            await dialog.get_by_label("允许的个人账号 ID", exact=True).fill("11\n22")
            await dialog.get_by_role("button", name="保存共享连接", exact=True).click()
            await page.wait_for_function("""window.resourceflowRequests.some(item =>
                item.path.endsWith('/shared/settings') && item.body.host_coordinator === true
                && item.body.allowed_users.join(',') === '11,22')""")
            await page.wait_for_function("document.querySelector('input[type=password]').value === ''")
            await dialog.get_by_role("tab", name="共享占位记录", exact=True).click()
            await dialog.get_by_text("朋友A", exact=False).wait_for()
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
            await dialog.get_by_role("button", name="保存共享连接", exact=True).click()
            await dialog.get_by_text("模拟共享保存失败", exact=True).wait_for()
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
