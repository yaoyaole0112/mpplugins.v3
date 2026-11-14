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
            await dialog.get_by_text("Film Resource", exact=True).wait_for()
            await dialog.get_by_role("tab", name="身份待核验", exact=True).click()
            await dialog.get_by_text("Unknown Resource", exact=True).wait_for()
            await dialog.get_by_role("tab", name="长期账本", exact=True).click()
            await dialog.get_by_text("Film Resource", exact=True).wait_for()
            await dialog.get_by_label("启用频道监听", exact=True).uncheck()
            assert await dialog.get_by_label("分辨率", exact=True).is_enabled()
            assert await dialog.get_by_label("排除已入队的同规格资源", exact=True).is_enabled()
            await dialog.get_by_label("排除已入队的同规格资源", exact=True).uncheck()
            await dialog.get_by_label("排除同一种子包内的同规格重复文件", exact=True).uncheck()
            await dialog.locator(".v-select").filter(
                has_text="整包处理方式"
            ).locator(".v-field").click()
            await page.get_by_role("option", name="存在不合格文件则排除整包", exact=True).click()
            await dialog.locator(".v-select").filter(
                has_text="资源身份未确认时"
            ).locator(".v-field").click()
            await page.get_by_role("option", name="按未命中放行", exact=True).click()
            await dialog.get_by_label("文件大小", exact=True).check()
            size = dialog.get_by_label("单集文件大小容差", exact=True)
            assert await size.is_enabled()
            await size.fill("3")
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await page.wait_for_function("""window.brushflowRequests.some(item =>
                item.method === 'POST' && item.path.endsWith('/settings')
                && item.body.enabled === false && item.body.size_tolerance_pct === 3
                && item.body.inflight_enabled === false && item.body.pack_dedup_enabled === false
                && item.body.pack_policy === 'exclude' && item.body.identity_policy === 'allow'
                && item.body.attributes.includes('size'))""")
            assert not await dialog.get_by_label("排除已入队的同规格资源", exact=True).is_checked()
            assert not await dialog.get_by_label("排除同一种子包内的同规格重复文件", exact=True).is_checked()
            await dialog.get_by_label("排除已入队的同规格资源", exact=True).check()
            await dialog.get_by_label("排除同一种子包内的同规格重复文件", exact=True).check()
            await dialog.locator(".v-select").filter(
                has_text="整包处理方式"
            ).locator(".v-field").click()
            await page.get_by_role("option", name="逐文件排除，仅下载剩余文件", exact=True).click()
            await dialog.locator(".v-select").filter(
                has_text="资源身份未确认时"
            ).locator(".v-field").click()
            await page.get_by_role("option", name="等待核验，不下载", exact=True).click()
            policy = dialog.locator(".v-select").filter(
                has_text="候选缺少勾选规格时"
            ).locator(".v-field")
            await policy.click()
            await page.get_by_role("option", name="按未命中放行（兼容旧版）", exact=True).click()
            aliases = dialog.get_by_label("剧名别名绑定（可选）", exact=True)
            await aliases.fill("Short Roman Title = 313346")
            await dialog.get_by_label("电影别名绑定（可选）", exact=True).fill("Film = 123")
            for label in ("特效", "编码", "来源", "帧率", "文件大小"):
                checkbox = dialog.get_by_label(label, exact=True)
                if await checkbox.is_checked():
                    await checkbox.uncheck()
            await dialog.get_by_label("分辨率", exact=True).check()
            await dialog.get_by_label("启用频道监听", exact=True).check()
            await dialog.get_by_role("button", name="保存设置", exact=True).click()
            await page.wait_for_function("""window.brushflowRequests.some(item =>
                item.method === 'POST' && item.path.endsWith('/settings')
                && item.body.attributes.length === 1 && item.body.attributes[0] === 'resolution'
                && item.body.unknown_policy === 'allow'
                && item.body.movie_identity_aliases === 'Film = 123'
                && item.body.identity_aliases === 'Short Roman Title = 313346')""")
            await aliases.wait_for()
            assert await aliases.input_value() == "Short Roman Title = 313346"
            await dialog.get_by_label("排除同一种子包内的同规格重复文件", exact=True).scroll_into_view_if_needed()
            await page.screenshot(path=str(output / f"resource-rules-{width}.png"), full_page=True)
            clipped = await dialog.locator(
                ".v-selection-control .v-label, .v-select__selection-text"
            ).evaluate_all("""elements =>
                elements.filter(element => element.scrollWidth > element.clientWidth + 1).length""")
            assert clipped == 0, (width, clipped)
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
            await page.get_by_role("button", name="创建第一个任务", exact=True).click()
            editor = page.get_by_role("dialog")
            assert await editor.get_by_text("按集 / 电影去重模式", exact=True).count() == 0
            assert await editor.get_by_label("分享任务", exact=True).count() == 0
            assert await editor.get_by_text("资源身份未确认时", exact=True).count() == 0
            dedup = editor.get_by_label("规格去重", exact=True)
            assert await dedup.is_checked()
            await dedup.uncheck()
            assert await editor.get_by_text("资源身份未确认时", exact=True).count() == 0
            assert await editor.get_by_role("button", name="设置全局去重规则", exact=True).count() == 0
            await page.screenshot(path=str(output / f"task-dedup-off-{width}.png"), full_page=True)
            await editor.get_by_label("任务名称", exact=True).fill("【分享】Actually Traffic")
            for label, option in (("站点", "Test Site"), ("下载器", "QB")):
                await editor.locator(".v-select").filter(has_text=label).first.locator(".v-field").click()
                await page.get_by_role("option", name=option, exact=True).click()
            await editor.get_by_role("button", name="保存任务", exact=True).click()
            await page.wait_for_function("""window.brushflowRequests.some(item => item.path.endsWith('/tasks')
                && item.body.name === '【分享】Actually Traffic'
                && item.body.spec_dedup_enabled === false
                && !('purpose' in item.body) && !('resource_dedup_mode' in item.body)
                && !('identity_policy' in item.body))""")
            await editor.wait_for(state="hidden")
            await page.get_by_role("button", name="创建第一个任务", exact=True).click()
            editor = page.get_by_role("dialog")
            dedup = editor.get_by_label("规格去重", exact=True)
            await dedup.uncheck()
            await dedup.check()
            await editor.get_by_label("任务名称", exact=True).fill("Test Share")
            await editor.get_by_role("button", name="设置全局去重规则", exact=True).click()
            field_dialog = page.get_by_role("dialog").filter(has=page.locator(".channel-dedup__title"))
            await field_dialog.get_by_text("全局去重字段", exact=True).wait_for()
            assert await field_dialog.get_by_label("分辨率", exact=True).is_checked()
            assert await field_dialog.get_by_label("排除已入队的同规格资源", exact=True).is_checked()
            await field_dialog.locator(".v-select").filter(has_text="资源身份未确认时").wait_for()
            await field_dialog.get_by_role("button", name="关闭", exact=True).click()
            await field_dialog.wait_for(state="hidden")
            assert await editor.get_by_label("任务名称", exact=True).input_value() == "Test Share"
            for label, option in (("站点", "Test Site"), ("下载器", "QB")):
                await editor.locator(".v-select").filter(has_text=label).first.locator(".v-field").click()
                await page.get_by_role("option", name=option, exact=True).click()
            await page.wait_for_timeout(400)
            await editor.locator(".v-card-text").first.evaluate("element => { element.scrollTop = 0 }")
            await page.screenshot(path=str(output / f"task-dedup-on-{width}.png"), full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            await editor.get_by_role("button", name="保存任务", exact=True).click()
            await page.wait_for_function("""window.brushflowRequests.some(item => item.path.endsWith('/tasks')
                && item.body.spec_dedup_enabled === true
                && !('purpose' in item.body) && !('resource_dedup_mode' in item.body)
                && !('identity_policy' in item.body))""")
            await editor.wait_for(state="hidden")
            assert not errors, errors
            await page.goto(f"{base_url}/tests/channel-dedup-smoke.html?task=traffic")
            await page.wait_for_function("window.brushflowReady === true")
            await page.locator(".brushflow-task-head__actions button").filter(
                has=page.locator(".mdi-pencil-outline")
            ).click()
            editor = page.get_by_role("dialog")
            dedup = editor.get_by_label("规格去重", exact=True)
            assert not await dedup.is_checked()
            await dedup.check()
            await editor.get_by_role("button", name="设置全局去重规则", exact=True).wait_for()
            await editor.get_by_role("button", name="保存任务", exact=True).click()
            await page.wait_for_function("""window.brushflowRequests.some(item => item.method === 'PUT'
                && item.body.id === 'saved-traffic' && item.body.spec_dedup_enabled === true)""")
            await editor.wait_for(state="hidden")
            await page.screenshot(path=str(output / f"task-edited-{width}.png"), full_page=True)
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
