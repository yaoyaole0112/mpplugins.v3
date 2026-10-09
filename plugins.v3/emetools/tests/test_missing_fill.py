"""离线模拟 Telegram 菜单编辑、确认边界与恢复，不访问真实 Bot。"""

import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock


SPEC = importlib.util.spec_from_file_location("missing_fill", Path(__file__).resolve().parents[1] / "missing_fill.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
MissingFill, record_key = MODULE.MissingFill, MODULE.record_key

RECORD = {"ServerName": "Emby", "LibraryName": "剧集", "SeriesName": "半熟恋人", "Year": "2021",
          "TmdbId": "123", "SeasonNum": 5, "SeasonFormatted": "S5", "MissingEpisodes": "12-13",
          "MissingEpisodeNumbers": [12, 13]}


class Menu:
    def __init__(self, client, text, labels, message_id=101, outgoing=False):
        self.client, self.raw_text, self.id, self.out = client, text, message_id, outgoing
        self.buttons = [[SimpleNamespace(text=label, data=f"{index}:{label}".encode()) for index, label in enumerate(labels)]]

    async def click(self, row, column):
        label = self.buttons[row][column].text
        self.client.clicks.append(label)
        if self.client.resource_pages and label in {"下一页", "上一页"}:
            self.client.resource_page += 1 if label == "下一页" else -1
            self.client.set_resource_page(self.client.resource_page)
        else:
            self.client.advance()
        return SimpleNamespace(message="")


class Client:
    def __init__(self):
        self.stage, self.clicks, self.commands = 0, [], []
        self.messages = []
        self.extra_outgoing = False
        self.series_labels = ["📺 剧集 | [已入库]半熟恋人 (2021)", "📺 剧集 | [未入库]半熟恋人 (2012)"]
        self.resource_labels = ["1. S05+S00 4K [4积分]", "2. S05E01-E28 4K [4积分]", "3. S04 更新至E26 [4积分]"]
        self.resource_text = "115 资源"
        self.resource_pages = None
        self.resource_page = 1

    def advance(self):
        self.stage += 1
        if self.stage == 1:
            self.menu = Menu(self, "搜索结果", self.series_labels)
        elif self.stage == 2:
            self.menu = Menu(self, "请选择网盘", ["123", "115", "夸克"])
        elif self.stage == 3:
            if self.resource_pages:
                self.resource_page = 1
                self.set_resource_page(1)
                return
            self.menu = Menu(self, self.resource_text, self.resource_labels)
        else:
            self.menu = Menu(self, "115 转存成功，成功 1 个，失败 0 个", [], message_id=102)
        self.messages = [self.menu]

    def set_resource_page(self, page):
        labels, text = self.resource_pages[page - 1]
        self.menu = Menu(self, text, labels, message_id=101)
        self.messages = [self.menu]

    async def send_message(self, bot, text):
        self.commands.append(text)
        self.stage = 0
        self.advance()
        return SimpleNamespace(id=100)

    async def get_messages(self, bot, ids=None, limit=None):
        if ids is not None:
            return self.menu if self.menu.id == ids else None
        messages = list(self.messages)
        if self.extra_outgoing:
            messages.append(Menu(self, "手动搜索", [], message_id=104, outgoing=True))
        return messages


class ParsingTests(unittest.TestCase):
    def test_series_identity_requires_exact_title_year_and_library_marker(self):
        for label, expected in [("📺 剧集 | [已入库]半熟恋人 (2021)", True),
                                ("[已入库]我的半熟恋人 (2021)", False),
                                ("[已入库]半熟恋人 (2012)", False),
                                ("[未入库]半熟恋人 (2021)", False), ("[已入库]半熟恋人", False)]:
            with self.subTest(label=label):
                self.assertEqual(MODULE.series_matches(label, RECORD), expected)

    def test_resource_ranges_and_ambiguous_season_packs(self):
        for label, coverage, covered in [("S05E01-E28 [8积分]", "明确覆盖", [12, 13]),
                                         ("S05E12 [4积分]", "明确覆盖", [12]),
                                         ("S05E01-E11 [4积分]", "不覆盖", []),
                                         ("S05 更新至E11 [4积分]", "不覆盖", []),
                                         ("S05 更新至E13 [4积分]", "明确覆盖", [12, 13]),
                                         ("S05+S00 [4积分]", "可能覆盖（需确认）", []),
                                         ("S00-S04 [4积分]", "不覆盖", []),
                                         ("1-4季已整理 [10积分]", "不覆盖", []),
                                         ("第5季 全集 [4积分]", "可能覆盖（需确认）", []),
                                         ("未知描述", "可能覆盖（需确认）", [])]:
            with self.subTest(label=label):
                info = MODULE.resource_info(label, RECORD)
                self.assertEqual(info["coverage"], coverage)
                self.assertEqual(info["covered"], covered)

    def test_free_markers_and_unknown_cost(self):
        for label, expected in [("S00-S06 4K [UBWEB] [免费]", 0),
                                ("S05E01-E28 【免费】", 0),
                                ("S05 （ 免费 ）", 0),
                                ("S05 (免费)", 0),
                                ("S05 [免积分]", 0),
                                ("S05 [0积分]", 0),
                                ("S05 [免费] [8积分]", 8),
                                ("免费剧场 S05", None),
                                ("S05 [非免费]", None),
                                ("S05 [免费试看]", None),
                                ("S05 4K", None)]:
            with self.subTest(label=label):
                self.assertEqual(MODULE.resource_info(label, RECORD)["points"], expected)

    def test_message_cost_matches_number_and_truncated_title(self):
        text = "115 资源 第1/2页\n1. S05 全集 [120人解锁]\n💰 8积分 | 1.10TB\n5. 五十公里桃花坞 · 城市角落 - 4K.SDR S06E01-S06E24 - 已更新至\n2026-07-17 第10期下 [47人解锁]\n💰 4积分 | 137.89GB\n"
        label = "5. 五十公里桃花坞 · 城市角落 - 4K.SDR S06E01-S06E24 - 已更新至 2026-07-17 第1..."
        self.assertEqual(MODULE.message_resource_points(label, text), 4)
        self.assertEqual(MODULE.message_resource_points(label, text.replace("4积分", "免费")), 0)
        self.assertEqual(MODULE.message_resource_points(label, text.replace("4积分", "0积分")), 0)
        self.assertIsNone(MODULE.message_resource_points(label.replace("5.", "6."), text))
        self.assertIsNone(MODULE.message_resource_points("5. 其他剧集...", text))
        self.assertIsNone(MODULE.message_resource_points(label, text + "5. 重复编号\n💰 8积分 | 1GB"))
        self.assertIsNone(MODULE.message_resource_points(label, text.replace("💰 4积分 | 137.89GB", "137.89GB")))
        self.assertIsNone(MODULE.message_resource_points(label, text + "💰 8积分 | 1GB"))
        self.assertIsNone(MODULE.message_resource_points(label, text.replace("4积分", "积分未知")))

    def test_message_resource_size_matches_number_and_truncated_title(self):
        text = "115 资源 第1/2页\n3. 种地吧 (2023) - S03E01-E80(完结) 2160p 📦 文件: 80 个 💾 大小: 455.03 GB [32人解锁]\n💰 4积分 | 455.03 GB | 4K\n"
        label = "3. 种地吧 (2023) - S03E01-E80(完结) 2160p 📦 文件: 80 个 💾 大小:..."
        self.assertEqual(MODULE.message_resource_size(label, text), "455.03 GB")
        self.assertIsNone(MODULE.message_resource_size("4. 其他资源...", text))

    def test_message_resource_size_reads_short_units_from_cost_line(self):
        text = """1. S01-S11 1080P&4K WEB-DL AAC [115人解锁]
💰 4积分 | 1.19TB | WEB-DL/WEBRip
3. 明星大侦探 1-11季全 [61人解锁]
💰 8积分 | 1.29T | 1080P | WEB-DL/WEBRip | 简中
5. 大侦探·拾光季（第十季 25 集全）  [87人解锁]
💰 2积分 | 128.43G | 4K | 简中
"""
        self.assertEqual(MODULE.message_resource_size("1. S01-S11 1080P&4K WEB-DL AAC [4积分]", text), "1.19TB")
        self.assertEqual(MODULE.message_resource_size("3. 明星大侦探 1-11季全 [8积分]", text), "1.29T")
        self.assertEqual(MODULE.message_resource_size("5. 大侦探·拾光季（第十季 25 集全） [2积分]", text), "128.43G")

    def test_url_buttons_are_not_clickable(self):
        message = SimpleNamespace(buttons=[[SimpleNamespace(text="URL", data=None),
                                            SimpleNamespace(text="115", data=b"callback")]])
        self.assertEqual([item["label"] for item in MODULE.buttons(message)], ["115"])


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = {}
        self.client = Client()
        self.plugin = SimpleNamespace(_enabled=True, _tg_session="fake-session", _tg_forward_token="fake-token",
                                      _missing=SimpleNamespace(_results=[copy.deepcopy(RECORD)], verify_inventory=MagicMock(return_value=[])))
        self.plugin.get_data = lambda key: copy.deepcopy(self.saved.get(key))
        self.plugin.save_data = lambda key, value: self.saved.update({key: copy.deepcopy(value)})
        self.plugin._monitor = SimpleNamespace(client=self.client, _authorized=AsyncMock(return_value=self.client),
                                              resolve_forward_bot=AsyncMock(return_value=SimpleNamespace(id=42)))
        self.fill = MissingFill(self.plugin)

    async def asyncTearDown(self):
        await self.fill.shutdown()

    async def ready(self):
        await self.fill.action({"operation": "start", "key": record_key(RECORD)})
        await self.fill.worker
        return self.fill.tasks[-1]

    def confirm(self, task, **changes):
        return {"operation": "confirm", "task_id": task["id"], "option_id": task["options"][0]["id"],
                "confirmed": True, **changes}

    async def test_search_edits_same_message_and_never_clicks_resource_before_confirmation(self):
        task = await self.ready()
        self.assertEqual(task["state"], "resources")
        self.assertEqual(self.client.commands, ["/re0 半熟恋人"])
        self.assertEqual(self.client.clicks, [self.client.series_labels[0], "115"])
        self.assertEqual(task["options"][0]["covered"], [12, 13])
        self.assertTrue(task["options"][1]["ambiguous"])
        self.assertFalse(task["options"][2]["eligible"])
        self.assertNotIn("data", repr(await self.fill.status()))
        self.assertNotIn("fake-session", repr(self.saved))

    async def test_confirm_clicks_once_and_completion_requires_inventory(self):
        task = await self.ready()
        await self.fill.action(self.confirm(task))
        await self.fill.worker
        self.assertEqual(task["state"], "awaiting_verify")
        self.assertTrue(task["submitted"])
        self.assertEqual(len(self.client.clicks), 3)
        await self.fill.action({"operation": "verify", "task_id": task["id"]})
        await asyncio.gather(*[worker for worker in self.fill.verifiers if worker.get_coro().__name__ == "_verify"])
        self.assertEqual(task["state"], "complete")

    async def test_all_resource_pages_are_collected_and_target_page_is_restored(self):
        self.client.resource_pages = [
            (["1. S05E01-E28 [4积分]", "下一页"], "115 资源 第1/2页\n1. S05E01-E28\n💰 4积分 | 12GB"),
            (["2. S04 更新至E26 [4积分]", "上一页"], "115 资源 第2/2页\n2. S04 更新至E26\n💰 4积分 | 8GB"),
        ]
        task = await self.ready()
        self.assertEqual([option["label"] for option in task["options"]], self.client.resource_pages[0][0][:1] + self.client.resource_pages[1][0][:1])
        self.assertEqual(task["options"][0]["size"], "12GB")
        await self.fill.action(self.confirm(task, option_id=task["options"][0]["id"]))
        await self.fill.worker
        self.assertEqual(self.client.clicks[-3:], ["下一页", "上一页", self.client.resource_pages[0][0][0]])

    async def test_double_confirmation_is_rejected(self):
        task = await self.ready()
        results = await asyncio.gather(self.fill.action(self.confirm(task)), self.fill.action(self.confirm(task)), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        await self.fill.worker
        self.assertEqual(len(self.client.clicks), 3)

    async def test_points_limit_unknown_points_and_explicit_consent(self):
        task = await self.ready()
        with self.assertRaises(ValueError):
            await self.fill.action(self.confirm(task, confirmed=False))
        await self.fill.action({"operation": "save", "max_points": 0})
        with self.assertRaises(ValueError):
            await self.fill.action(self.confirm(task))
        self.fill.options[0]["points"] = None
        with self.assertRaises(ValueError):
            await self.fill.action(self.confirm(task))
        self.assertEqual(len(self.client.clicks), 2)

    async def test_free_resource_requires_confirmation_and_allows_zero_limit(self):
        self.client.resource_labels = ["1. S05E01-E28 [免费]"]
        await self.fill.action({"operation": "save", "max_points": 0})
        task = await self.ready()
        self.assertEqual(task["options"][0]["points"], 0)
        self.assertEqual(len(self.client.clicks), 2)
        with self.assertRaises(ValueError):
            await self.fill.action(self.confirm(task, confirmed=False))
        await self.fill.action(self.confirm(task))
        await self.fill.worker
        self.assertEqual(len(self.client.clicks), 3)
        self.assertEqual(task["selected"]["points"], 0)
        self.assertEqual(task["state"], "awaiting_verify")

    async def test_message_cost_fallback_preserves_coverage_and_button_cost(self):
        self.client.resource_labels = ["5. S06E01-E24 长标题...", "2. S05E01-E28 [免费]"]
        self.client.resource_text = "115 资源\n2. S05E01-E28\n💰 8积分 | 1GB\n5. S06E01-E24 长标题已更新\n💰 4积分 | 137.89GB"
        task = await self.ready()
        options = {item["label"]: item for item in task["options"]}
        fifth = options[self.client.resource_labels[0]]
        self.assertEqual(fifth["points"], 4)
        self.assertFalse(fifth["eligible"])
        self.assertEqual(options[self.client.resource_labels[1]]["points"], 0)
        with self.assertRaises(ValueError):
            await self.fill.action(self.confirm(task, option_id=fifth["id"]))
        self.assertEqual(len(self.client.clicks), 2)

    async def test_message_free_cost_requires_confirmation(self):
        self.client.resource_labels = ["5. S05E01-E28 长标题..."]
        self.client.resource_text = "115 资源\n5. S05E01-E28 长标题已更新\n💰 免费 | 137.89GB"
        await self.fill.action({"operation": "save", "max_points": 0})
        task = await self.ready()
        self.assertEqual(task["options"][0]["points"], 0)
        self.assertEqual(len(self.client.clicks), 2)
        await self.fill.action(self.confirm(task))
        await self.fill.worker
        self.assertEqual(task["state"], "awaiting_verify")
        self.assertEqual(len(self.client.clicks), 3)

    async def test_noncovering_resource_is_rejected(self):
        task = await self.ready()
        with self.assertRaises(ValueError):
            await self.fill.action(self.confirm(task, option_id=task["options"][2]["id"]))

    async def test_clear_history_removes_finished_tasks_only(self):
        self.fill.tasks = [
            {"id": "done", "state": "complete"},
            {"id": "cancelled", "state": "cancelled"},
            {"id": "expired", "state": "expired"},
            {"id": "failed", "state": "failed"},
            {"id": "closed", "state": "closed"},
            {"id": "pending", "state": "awaiting_verify"},
            {"id": "active", "state": "resources"},
        ]
        self.fill.active = self.fill.tasks[-1]
        await self.fill.action({"operation": "clear_history"})
        self.assertEqual([task["id"] for task in self.fill.tasks], ["pending", "active"])
        with self.assertRaises(ValueError):
            await self.fill.action({"operation": "clear_history"})

    async def test_cancel_before_submit_does_not_spend(self):
        task = await self.ready()
        await self.fill.action({"operation": "cancel", "task_id": task["id"]})
        self.assertEqual(task["state"], "cancelled")
        self.assertFalse(self.fill.busy)
        self.assertFalse(self.fill.spent)
        with self.assertRaises(ValueError):
            await self.fill.action(self.confirm(task))

    async def test_duplicate_season_and_serial_bot_guard(self):
        task = await self.ready()
        with self.assertRaises(ValueError):
            await self.fill.action({"operation": "start", "key": record_key(RECORD)})
        await self.fill.action(self.confirm(task))
        await self.fill.worker
        with self.assertRaises(ValueError):
            await self.fill.action({"operation": "start", "key": record_key(RECORD)})

    async def test_manual_bot_message_stops_before_resource_click(self):
        task = await self.ready()
        self.client.extra_outgoing = True
        await self.fill.action(self.confirm(task))
        await self.fill.worker
        self.assertEqual(task["state"], "failed")
        self.assertFalse(task["submitted"])
        self.assertEqual(len(self.client.clicks), 2)

    async def test_changed_menu_stops_before_resource_click(self):
        task = await self.ready()
        self.client.menu = Menu(self.client, "其他菜单", ["另一资源"])
        self.client.messages = [self.client.menu]
        await self.fill.action(self.confirm(task))
        await self.fill.worker
        self.assertEqual(task["state"], "failed")
        self.assertFalse(task["submitted"])

    async def test_submit_timeout_retains_spent_marker_and_does_not_retry(self):
        task = await self.ready()
        self.client.menu.click = AsyncMock(side_effect=TimeoutError("回调超时"))
        await self.fill.action(self.confirm(task))
        await self.fill.worker
        self.assertEqual(task["state"], "uncertain")
        self.assertTrue(task["submitted"])
        restored = MissingFill(self.plugin)
        self.assertTrue(restored.spent)
        self.assertEqual(restored.tasks[-1]["state"], "uncertain")
        with self.assertRaises(ValueError):
            await restored.action({"operation": "start", "key": record_key(RECORD)})

    async def test_restart_expires_unconfirmed_menu(self):
        await self.ready()
        restored = MissingFill(self.plugin)
        self.assertEqual(restored.tasks[-1]["state"], "expired")
        self.assertFalse(restored.busy)

    async def test_ambiguous_series_requires_user_selection(self):
        self.client.series_labels = ["[已入库]半熟恋人 (2012)", "[未入库]半熟恋人 (2021)"]
        task = await self.ready()
        self.assertEqual(task["state"], "choose_series")
        self.assertFalse(self.client.clicks)
        await self.fill.action({"operation": "choose_series", "task_id": task["id"], "option_id": "1"})
        await self.fill.worker
        self.assertEqual(task["state"], "resources")

    async def test_failed_verification_cannot_report_complete(self):
        task = await self.ready()
        await self.fill.action(self.confirm(task))
        await self.fill.worker
        self.plugin._missing.verify_inventory.side_effect = ValueError("网络失败")
        await self.fill._verify(task)
        self.assertEqual(task["state"], "awaiting_verify")
        self.assertIn("不视为补全", task["message"])

    async def test_manual_close_keeps_resource_repeat_protection(self):
        task = await self.ready()
        await self.fill.action(self.confirm(task))
        await self.fill.worker
        await self.fill.action({"operation": "close", "task_id": task["id"], "confirmed": True})
        next_task = await self.ready()
        self.assertTrue(next_task["options"][0]["previously_submitted"])
        with self.assertRaises(ValueError):
            await self.fill.action(self.confirm(next_task))
