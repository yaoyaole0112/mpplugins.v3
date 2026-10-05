"""通过 Telegram 用户会话执行需人工确认的缺集补全任务。"""

import asyncio
import copy
import hashlib
import re
import time
import uuid


INTERACTIVE = {"searching", "choose_series", "resources", "submitting"}
PENDING = {"awaiting_verify", "uncertain", "verifying"}


def record_key(record):
    """以服务器、媒体库、TMDB 与季号区分任务，不受缺失集数变化影响。"""
    return [str(record.get(key) or "") for key in
            ("ServerName", "LibraryName", "TmdbId", "SeasonNum")]


def series_matches(label, record):
    """仅允许剧名、年份均明确一致的已入库选项自动匹配。"""
    match = re.fullmatch(r".*?\[已入库\]\s*(.*?)\s*[（(]((?:19|20)\d{2})[）)]\s*", label)
    normalize = lambda text: re.sub(r"\s+", "", str(text)).casefold()
    return bool(match and normalize(match[1]) == normalize(record.get("SeriesName", ""))
                and match[2] == str(record.get("Year", "")))


def resource_info(label, record):
    """解析资源描述；季集合不能当成确定的分集范围。"""
    season = int(record["SeasonNum"])
    missing = set(record["MissingEpisodeNumbers"])
    text = label.upper()
    points = re.search(r"(\d+)\s*积分", text)
    free = re.search(r"[\[【（(]\s*(?:免费|免积分)\s*[\]】）)]", text)
    seasons = set()
    ranges = {}
    for match in re.finditer(r"S(\d{1,2})(?:\s*[-–~]\s*S(\d{1,2}))?", text):
        first, last = int(match[1]), int(match[2] or match[1])
        if first <= last <= 99:
            seasons.update(range(first, last + 1))
    for match in re.finditer(r"(?<![\d.])(\d{1,2})\s*[-–~]\s*(\d{1,2})\s*季", text):
        first, last = int(match[1]), int(match[2])
        if first <= last <= 99:
            seasons.update(range(first, last + 1))
    for match in re.finditer(r"第?\s*(\d{1,2})\s*季", text):
        seasons.add(int(match[1]))
    for match in re.finditer(r"S(\d{1,2})\s*E(\d{1,4})(?:\s*[-–~]\s*(?:S\d{1,2})?E?(\d{1,4}))?", text):
        first, last = int(match[2]), int(match[3] or match[2])
        if first <= last <= 9999:
            ranges.setdefault(int(match[1]), set()).update(range(first, last + 1))
    updated = re.search(r"更新至\s*E(\d{1,4})", text)
    if updated and len(seasons) == 1:
        ranges[next(iter(seasons))] = set(range(1, int(updated[1]) + 1))
    explicit = season in ranges
    covered = sorted(missing & ranges[season]) if explicit else []
    possible = not explicit and (season in seasons or not seasons)
    return {"points": int(points[1]) if points else 0 if free else None, "covered": covered,
            "coverage": "明确覆盖" if covered else "可能覆盖（需确认）" if possible else "不覆盖",
            "eligible": bool(covered or possible), "ambiguous": possible}


def message_resource_points(label, text):
    number = re.match(r"\s*(\d+)[.、．]\s*(.+)", label)
    if not number:
        return None
    headings = list(re.finditer(r"(?m)^\s*(\d+)[.、．]\s*(?=\S)", text))
    matches = [index for index, heading in enumerate(headings) if int(heading[1]) == int(number[1])]
    if len(matches) != 1:
        return None
    index = matches[0]
    section = text[headings[index].end():headings[index + 1].start() if index + 1 < len(headings) else len(text)]
    prefix = re.split(r"\.{3,}|…+", number[2], maxsplit=1)[0]
    normalize = lambda value: re.sub(r"\s+", "", value).upper()
    if not normalize(prefix) or not normalize(section).startswith(normalize(prefix)):
        return None
    costs = re.findall(r"(?m)^\s*💰\s*(\d+\s*积分|免费|免积分)\s*(?=[|｜]|$)", section)
    if len(costs) != 1:
        return None
    return int(re.search(r"\d+", costs[0])[0]) if re.search(r"\d+", costs[0]) else 0


def message_resource_size(label, text):
    """从资源正文中提取与编号对应的文件大小。"""
    number = re.match(r"\s*(\d+)[.、．]\s*(.+)", label)
    if not number:
        return None
    headings = list(re.finditer(r"(?m)^\s*(\d+)[.、．]\s*(?=\S)", text))
    matches = [index for index, heading in enumerate(headings) if int(heading[1]) == int(number[1])]
    if len(matches) != 1:
        return None
    index = matches[0]
    section = text[headings[index].end():headings[index + 1].start() if index + 1 < len(headings) else len(text)]
    prefix = re.split(r"\.{3,}|…+", number[2], maxsplit=1)[0]
    prefix = re.sub(r"\s*(?:\[[^]]*(?:积分|免费|免积分)[^]]*\]|【[^】]*(?:积分|免费|免积分)[^】]*】|（[^）]*(?:积分|免费|免积分)[^）]*）|\([^)]*(?:积分|免费|免积分)[^)]*\))\s*$", "", prefix)
    normalize = lambda value: re.sub(r"\s+", "", value).upper()
    if not normalize(prefix) or not normalize(section).startswith(normalize(prefix)):
        return None
    matches = re.findall(r"(?:大小\s*[:：]\s*|[|｜]\s*)(\d+(?:\.\d+)?\s*(?:TB|GB|MB|KB|B))", section, re.I)
    return matches[-1] if matches else None


def resource_page(message):
    """返回资源菜单当前页码和总页数；无法识别时按单页处理。"""
    match = re.search(r"第\s*(\d+)\s*/\s*(\d+)\s*页", message.raw_text or "")
    return (int(match[1]), int(match[2])) if match else (1, 1)


def buttons(message):
    """仅接受回调按钮，绝不打开 URL 或分享联系方式的按钮。"""
    return [{"row": row_index, "column": column_index, "label": button.text,
             "data": bytes(button.data)}
            for row_index, row in enumerate(message.buttons or [])
            for column_index, button in enumerate(row) if getattr(button, "data", None)]


def signature(message):
    return (message.id, message.raw_text or "", tuple(
        (item["row"], item["column"], item["label"], item["data"]) for item in buttons(message)))


class MissingFill:
    """在监控器事件循环中串行操作 Bot，持久化确认与提交边界。"""

    def __init__(self, plugin):
        self.plugin = plugin
        saved = plugin.get_data("missing_fill") or {}
        self.max_points = int(saved.get("max_points", 4))
        self.tasks = saved.get("tasks", [])
        self.spent = set(saved.get("spent", []))
        for task in self.tasks:
            if task["state"] in INTERACTIVE | {"verifying"}:
                task["state"] = "uncertain" if task.get("submitted") else "expired"
                task["message"] = "服务重启：已提交任务请复查，未提交任务需重新搜索。"
        self.active = None
        self.worker = None
        self.expiry = None
        self.verifiers = set()
        self.bot = None
        self.command_id = 0
        self.menu = None
        self.options = []
        self._persist()

    @property
    def busy(self):
        return self.active is not None

    def _persist(self):
        self.plugin.save_data("missing_fill", {"max_points": self.max_points,
                                               "tasks": copy.deepcopy(self.tasks), "spent": sorted(self.spent)})

    def _set(self, task, state, message):
        task.update(state=state, message=message, updated_at=time.time())
        task.setdefault("log", []).append({"time": task["updated_at"], "message": message})
        task["log"] = task["log"][-20:]
        self._persist()

    async def status(self):
        """返回可展示的任务信息，不暴露回调数据、会话或资源链接。"""
        return {"max_points": self.max_points, "busy": self.busy,
                "tasks": copy.deepcopy(list(reversed(self.tasks)))}

    async def action(self, action):
        """接收搜索、选剧、确认、取消、复查与已核实关闭操作。"""
        operation = action.get("operation")
        if operation == "save":
            maximum = action.get("max_points")
            if type(maximum) is not int or not 0 <= maximum <= 100:
                raise ValueError("单次积分上限必须是 0–100 的整数")
            self.max_points = maximum
            self._persist()
        elif operation == "start":
            if not self.plugin._enabled or not self.plugin._tg_session or not self.plugin._tg_forward_token:
                raise ValueError("请启用插件、登录 Telegram 用户账号并配置转发 Bot Token")
            if self.busy:
                raise ValueError("已有 Bot 交互任务，请先确认或取消")
            record = next((item for item in self.plugin._missing._results
                           if record_key(item) == action.get("key")), None)
            if not record or not record.get("MissingEpisodeNumbers"):
                raise ValueError("缺集记录已变化，请刷新检测结果")
            if any(record_key(task["record"]) == record_key(record)
                   and task["state"] in INTERACTIVE | PENDING for task in self.tasks):
                raise ValueError("该缺失季已有未核实的任务，请先复查或人工核实关闭")
            if len(self.tasks) >= 50:
                removable = next((task for task in self.tasks if task["state"] not in INTERACTIVE | PENDING), None)
                if removable is None:
                    raise ValueError("待核实任务过多，请先处理已有任务")
                self.tasks.remove(removable)
            task = {"id": uuid.uuid4().hex, "record": copy.deepcopy(record), "created_at": time.time(),
                    "options": [], "submitted": False, "remaining": list(record["MissingEpisodeNumbers"])}
            self.tasks.append(task)
            self.active = task
            self._set(task, "searching", "正在发送 /re0 搜索；期间请勿手动操作此 Bot。")
            self.worker = asyncio.create_task(self._run(task, self._search(task)))
            self.expiry = asyncio.create_task(self._expire(task))
        else:
            task = next((item for item in self.tasks if item["id"] == action.get("task_id")), None)
            if not task:
                raise ValueError("任务不存在")
            if operation in {"choose_series", "confirm"}:
                expected = "choose_series" if operation == "choose_series" else "resources"
                if task is not self.active or task["state"] != expected:
                    raise ValueError("任务状态已变化，请刷新页面")
                selected = next((item for item in self.options if item["id"] == action.get("option_id")), None)
                if not selected:
                    raise ValueError("选项已失效，请重新搜索")
                if operation == "confirm":
                    if not action.get("confirmed"):
                        raise ValueError("必须明确确认积分扣除和整包转存")
                    if selected["points"] is None or selected["points"] > self.max_points:
                        raise ValueError("积分未知或超过上限，拒绝点击")
                    if not selected["eligible"]:
                        raise ValueError("该资源未覆盖缺集")
                    if selected["fingerprint"] in self.spent:
                        raise ValueError("该资源已有提交记录，禁止重复点击；请在 Bot 中人工核实")
                    task["selected"] = {key: selected[key] for key in ("label", "points", "size", "coverage", "covered")}
                    self._set(task, "submitting", "已确认，正在提交转存；此后不自动重试。")
                    self.worker = asyncio.create_task(self._run(task, self._submit(task, selected)))
                else:
                    self._set(task, "searching", "已人工选剧，正在选择 115。")
                    self.worker = asyncio.create_task(self._run(task, self._resources(task, selected)))
            elif operation == "cancel":
                if task is not self.active or task["state"] == "submitting" or task.get("submitted"):
                    raise ValueError("提交后的任务不能取消，请复查或人工核实")
                if self.worker and not self.worker.done():
                    self.worker.cancel()
                    await asyncio.gather(self.worker, return_exceptions=True)
                self._set(task, "cancelled", "已取消；没有点击转存资源。")
                self._release(task)
            elif operation == "verify":
                if task["state"] not in {"awaiting_verify", "uncertain"}:
                    raise ValueError("当前任务不能复查")
                task["state_before_verify"] = task["state"]
                self._set(task, "verifying", "正在只读查询 Emby 实际分集。")
                self._spawn_verifier(self._verify(task))
            elif operation == "close":
                if task["state"] not in {"awaiting_verify", "uncertain"} or not action.get("confirmed"):
                    raise ValueError("请先人工核实 Bot 和网盘，并确认关闭")
                self._set(task, "closed", "用户已人工核实并关闭；不表示已补全，同一资源仍禁止重复提交。")
            else:
                raise ValueError("未知的补全操作")
        return {"ok": True, **await self.status()}

    def _spawn_verifier(self, coroutine):
        worker = asyncio.create_task(coroutine)
        self.verifiers.add(worker)
        worker.add_done_callback(self.verifiers.discard)

    def _release(self, task):
        if self.active is task:
            self.active = None
            self.menu = None
            self.options = []
            if self.expiry and self.expiry is not asyncio.current_task():
                self.expiry.cancel()

    async def _expire(self, task):
        await asyncio.sleep(600)
        if self.active is task and task["state"] in {"choose_series", "resources"}:
            self._set(task, "expired", "确认窗口已过期，请重新搜索；没有提交转存。")
            self._release(task)

    async def _run(self, task, coroutine):
        try:
            await coroutine
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state = "uncertain" if task.get("submitted") else "failed"
            message = str(error) if isinstance(error, (ValueError, TimeoutError)) else f"Telegram 操作失败：{type(error).__name__}"
            self._set(task, state, message[:200])
            self._release(task)

    async def _messages(self):
        messages = await asyncio.wait_for(self.plugin._monitor.client.get_messages(self.bot, limit=30), timeout=15)
        if any(message.out and message.id > self.command_id for message in messages):
            raise ValueError("检测到此 Bot 的其他账号操作，已停止以避免菜单串线")
        return messages

    async def _wait(self, snapshot, predicate, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in reversed(await self._messages()):
                if message.out or message.id <= self.command_id:
                    continue
                if snapshot.get(message.id) != signature(message) and predicate(message):
                    return message
            await asyncio.sleep(1.5)
        raise TimeoutError("等待 Bot 回复超时；如已提交，请人工核实，不要重复点击")

    async def _snapshot(self):
        return {message.id: signature(message) for message in await self._messages() if not message.out}

    async def _click(self, selected, task=None):
        if selected.get("page") and selected["page"] != resource_page(self.menu)[0]:
            await self._go_to_resource_page(selected["page"])
        await self._messages()
        current = await asyncio.wait_for(self.plugin._monitor.client.get_messages(self.bot, ids=self.menu.id), timeout=15)
        if not current or signature(current) != signature(self.menu):
            raise ValueError("Bot 菜单已变化，拒绝使用旧按钮，请重新搜索")
        snapshot = await self._snapshot()
        if task is not None:
            task["submitted"] = True
            self.spent.add(selected["fingerprint"])
            self._persist()
        result = await asyncio.wait_for(current.click(selected["row"], selected["column"]), timeout=20)
        return snapshot, getattr(result, "message", "") or ""

    async def _go_to_resource_page(self, target_page):
        current_page, total_pages = resource_page(self.menu)
        if not 1 <= target_page <= total_pages:
            raise ValueError("资源页码已失效，请重新搜索")
        while current_page != target_page:
            wanted = "下一页" if current_page < target_page else "上一页"
            button = next((item for item in buttons(self.menu) if wanted in item["label"]), None)
            if not button:
                raise ValueError("无法切换到资源所在页，请重新搜索")
            snapshot, _ = await self._click(button)
            self.menu = await self._wait(
                snapshot,
                lambda message: resource_page(message)[0] != current_page,
            )
            current_page, total_pages = resource_page(self.menu)

    def _publish(self, task, message, items, state, description):
        self.menu, self.options = message, items
        task["options"] = [{key: value for key, value in item.items()
                            if key not in {"data", "row", "column", "fingerprint"}} for item in items]
        self._set(task, state, description)

    async def _search(self, task):
        client = await asyncio.wait_for(self.plugin._monitor._authorized(), timeout=20)
        self.bot = await asyncio.wait_for(self.plugin._monitor.resolve_forward_bot(), timeout=25)
        title = re.sub(r"[\r\n\x00-\x1f]", " ", task["record"]["SeriesName"]).strip()[:100]
        command = await asyncio.wait_for(client.send_message(self.bot, f"/re0 {title}"), timeout=20)
        self.command_id = command.id
        menu = await self._wait({}, lambda message: any("[已入库]" in item["label"] or "[未入库]" in item["label"]
                                                        for item in buttons(message)))
        choices = [{**item, "id": str(index), "matched": series_matches(item["label"], task["record"])}
                   for index, item in enumerate(buttons(menu))
                   if "[已入库]" in item["label"] or "[未入库]" in item["label"]]
        matched = [item for item in choices if item["matched"]]
        self.menu, self.options = menu, choices
        if len(matched) == 1:
            self._set(task, "searching", "已匹配同名同年份的已入库剧集，正在选择 115。")
            await self._resources(task, matched[0])
        else:
            self._publish(task, menu, choices, "choose_series", "无法唯一匹配已入库剧集，请核对名称和年份后人工选择。")

    async def _resources(self, task, selected):
        snapshot, _ = await self._click(selected)
        menu = await self._wait(snapshot, lambda message: any(item["label"].strip() == "115" for item in buttons(message)))
        self.menu = menu
        disk = next(item for item in buttons(menu) if item["label"].strip() == "115")
        snapshot, _ = await self._click(disk)
        menu = await self._wait(snapshot, lambda message: bool(buttons(message)) and "115" in (message.raw_text or "")
                               and any(re.match(r"\s*\d+[.、．]", item["label"]) for item in buttons(message)))
        self.menu = menu
        resources = []
        visited = set()
        while True:
            current_page, total_pages = resource_page(menu)
            if current_page in visited:
                raise ValueError("Bot 资源分页循环异常，请重新搜索")
            visited.add(current_page)
            for index, item in enumerate(buttons(menu)):
                if not re.match(r"\s*\d+[.、．]", item["label"]):
                    continue
                info = resource_info(item["label"], task["record"])
                if info["points"] is None:
                    info["points"] = message_resource_points(item["label"], menu.raw_text or "")
                info["size"] = message_resource_size(item["label"], menu.raw_text or "")
                stable_label = re.sub(r"^\s*\d+[.、．]\s*", "", item["label"])
                fingerprint = hashlib.sha256(repr((record_key(task["record"]), int(self.bot.id), stable_label)).encode()).hexdigest()
                resources.append({**item, **info, "id": f"{current_page}-{index}", "page": current_page,
                                  "fingerprint": fingerprint, "previously_submitted": fingerprint in self.spent})
            if current_page >= total_pages:
                break
            next_button = next((item for item in buttons(menu) if "下一页" in item["label"]), None)
            if not next_button:
                raise ValueError("Bot 资源分页缺少下一页按钮，请重新搜索")
            snapshot, _ = await self._click(next_button)
            menu = await self._wait(snapshot, lambda message: resource_page(message)[0] == current_page + 1)
            self.menu = menu
        resources.sort(key=lambda item: (not item["eligible"], item["ambiguous"], -len(item["covered"]),
                                        item["points"] if item["points"] is not None else 999))
        self.menu = menu
        self._publish(task, menu, resources, "resources", f"已读取全部 {len(resources)} 个资源；请选择并确认，季包仅可能覆盖缺集，可能转存已有集数。确认窗口 10 分钟。")

    async def _submit(self, task, selected):
        snapshot, callback = await self._click(selected, task)
        response = await self._wait(snapshot, lambda message: bool(message.raw_text), timeout=90)
        texts = [callback, response.raw_text or ""]
        if any(re.search(r"积分不足|余额不足|转存失败|链接失效|验证码|确认.*(?:支付|扣除)", text) for text in texts):
            self._set(task, "uncertain", "Bot 提示失败或额外验证，请到 Telegram 人工核实；不会自动重试。")
        else:
            success = any(re.search(r"转存成功|成功\s*[1-9]\d*\s*个.*失败\s*0", text, re.S) for text in texts)
            self._set(task, "awaiting_verify", "Bot 已反馈转存成功，等待 MP 整理与 Emby 入库。" if success else
                      "已提交资源按钮，尚未确认转存成功；请核实 Bot 回复，后台将复查入库。")
            self._spawn_verifier(self._auto_verify(task))
        self._release(task)

    async def _auto_verify(self, task):
        for _ in range(3):
            await asyncio.sleep(90)
            if task["state"] != "awaiting_verify":
                return
            self._set(task, "verifying", "正在自动复查 Emby 实际分集。")
            await self._verify(task)

    async def _verify(self, task):
        previous = "uncertain" if task.get("state_before_verify") == "uncertain" else "awaiting_verify"
        try:
            remaining = await asyncio.to_thread(self.plugin._missing.verify_inventory, task["record"])
            task["remaining"] = remaining
            self._set(task, "complete" if not remaining else previous,
                      "已确认目标缺失集全部入库。" if not remaining else f"Emby 仍缺 {', '.join(map(str, remaining))}；请等待整理或人工核实。")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else type(error).__name__
            self._set(task, previous, f"入库复查未成功：{message[:140]}；不视为补全。")

    async def shutdown(self):
        """停止后台任务；提交后的不确定结果留待重启后人工复查。"""
        workers = [worker for worker in [self.worker, self.expiry, *self.verifiers] if worker and not worker.done()]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if self.active:
            self._set(self.active, "uncertain" if self.active.get("submitted") else "expired", "服务停止，请复查或重新搜索。")
            self._release(self.active)
