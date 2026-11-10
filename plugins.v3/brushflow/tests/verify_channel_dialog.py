"""Browser smoke checks against tests/channel-dedup-smoke.html, with mock API only."""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def verify(base_url, output, executable_path=None):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, args=["--no-sandbox"], executable_path=executable_path,
        )
        for width, height in ((1440, 1000), (390, 844), (320, 720)):
            page = await browser.new_page(
                viewport={"width": width, "height": height}, locale="zh-CN", timezone_id="Asia/Shanghai",
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(f"{base_url}/tests/channel-dedup-smoke.html")
            await page.wait_for_function("window.brushflowReady === true")
            await page.evaluate("document.fonts.ready")
            output.mkdir(parents=True, exist_ok=True)
            opener = page.get_by_role("button", name="频道监听与同规格去重", exact=True)
            actions = page.locator(".brushflow-page__actions")
            assert await actions.evaluate("""element =>
                element.children[0].classList.contains('v-chip') &&
                element.children[1].getAttribute('aria-label') === '频道监听与同规格去重' &&
                element.children[2].getAttribute('aria-label') === '全局设置'""")
            assert await opener.locator(".mdi-send").evaluate("""element =>
                getComputedStyle(element, '::before').content !== 'none'""")
            await page.screenshot(path=str(output / f"channel-header-{width}.png"), full_page=True)
            await opener.click()
            dialog = page.get_by_role("dialog")
            await dialog.get_by_text("PSYREN 决战游戏", exact=True).wait_for()
            await dialog.get_by_text("5 分钟", exact=True).wait_for()
            await dialog.get_by_text("下次同步", exact=True).wait_for()
            await dialog.get_by_label("文件大小", exact=True).check()
            size = dialog.get_by_label("单集文件大小容差", exact=True)
            await size.fill("3")
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await page.wait_for_function("""window.brushflowRequests.some(item =>
                item.method === 'POST' && item.path.endsWith('/settings')
                && item.body.size_tolerance_pct === 3 && item.body.attributes.includes('size'))""")
            await dialog.get_by_role("button", name="立即同步", exact=True).click()
            await page.wait_for_function("""window.brushflowRequests.some(item =>
                item.method === 'POST' && item.path.endsWith('/poll'))""")
            await dialog.locator(".v-card-text").evaluate("element => { element.scrollTop = 0 }")
            await page.wait_for_timeout(300)
            await page.screenshot(path=str(output / f"channel-dialog-{width}.png"), full_page=True)
            await page.evaluate("window.brushflowFailSave = true")
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await dialog.get_by_text("模拟保存失败", exact=True).wait_for()
            assert await dialog.is_visible()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            overflowing = await dialog.evaluate("""element => [...element.querySelectorAll('button, input, textarea')]
                .filter(item => item.getBoundingClientRect().width > 0)
                .filter(item => item.getBoundingClientRect().left < -1 ||
                    item.getBoundingClientRect().right > innerWidth + 1).length""")
            assert overflowing == 0, (width, overflowing)
            await page.screenshot(path=str(output / f"channel-dialog-error-{width}.png"), full_page=True)
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            await dialog.wait_for(state="hidden")
            assert not errors, errors
            print(json.dumps({"width": width, "height": height, "passed": True}))
            await page.close()
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5176")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", help="Use an existing Chromium executable")
    arguments = parser.parse_args()
    asyncio.run(verify(arguments.url, arguments.output, arguments.browser))
