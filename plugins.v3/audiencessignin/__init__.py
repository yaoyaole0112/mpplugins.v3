import re
import time
import traceback
from datetime import datetime
from threading import Lock, Thread
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils

try:
    from app.helper.cloudflare import under_challenge
except Exception:
    def under_challenge(_html: str) -> bool:
        return False

try:
    from app.helper.browser import cookie_parse
except Exception:
    cookie_parse = None

try:
    from app.utils.site import SiteUtils
except Exception:
    SiteUtils = None

try:
    from app.db.site_oper import SiteOper
except Exception:
    SiteOper = None

try:
    from app.helper.sites import SitesHelper
except Exception:
    SitesHelper = None


PLUGIN_NAME = "观众签到"
SITE_NAME = "观众"
SITE_DOMAIN = "audiences.me"
ATTENDANCE_PATH = "/attendance.php"
DEFAULT_SITEKEY = "0x4AAAAAABfcR5-BOyur3FT4"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
)
DEFAULT_CRON = "35 8 * * *"

SUCCESS_RE = re.compile(
    r"签到成功|已经签到|今日已签|签到已得|本次签到获得|连续签到\s*\d+\s*天",
    re.I,
)
BONUS_RE = re.compile(r"(?:获得|奖励|签到已得)\s*([\d.]+)\s*(?:粒)?爆米花")
STREAK_RE = re.compile(r"连续签到\s*(\d+)\s*天")
SITEKEY_RE = re.compile(r'data-sitekey=["\']([^"\']+)["\']', re.I)
NEED_VERIFY_HINTS = ("cf-turnstile", "人机验证", "请验证您是真人")
LOGIN_FAIL_HINTS = ("login.php", "userdetails.php?id=0")

_SIGN_LOCK = Lock()


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _html_has_turnstile(html: str) -> bool:
    text = html or ""
    return any(hint in text for hint in NEED_VERIFY_HINTS)


def _html_signed(html: str) -> bool:
    if not html:
        return False
    if SUCCESS_RE.search(html):
        return True
    if "attendance-card--verify" in html or _html_has_turnstile(html):
        return False
    if "attendance-card--success" in html or "attendance-card--done" in html:
        return True
    return False


def _html_logged_in(html: str) -> bool:
    if not html:
        return False
    if SiteUtils:
        try:
            return bool(SiteUtils.is_logged_in(html))
        except Exception:
            pass
    if "logout.php" in html or "userdetails.php" in html:
        return True
    return "c_secure_uid" not in html and "login.php" not in html[:2000]


def _extract_detail(html: str) -> str:
    if not html:
        return ""
    bonus = BONUS_RE.search(html)
    streak = STREAK_RE.search(html)
    parts = []
    if bonus:
        parts.append(f"获得 {bonus.group(1)} 粒爆米花")
    if streak:
        parts.append(f"连续签到 {streak.group(1)} 天")
    if parts:
        return "，".join(parts)
    match = SUCCESS_RE.search(html)
    return match.group(0) if match else ""


class AudiencesSignIn(_PluginBase):
    plugin_name = PLUGIN_NAME
    plugin_desc = "专为观众站 audiences.me 的 Cloudflare Turnstile 每日签到。"
    plugin_icon = "signin.png"
    plugin_version = "1.0.0"
    plugin_author = "helios"
    author_url = "https://github.com/yaoyaole0112"
    plugin_config_prefix = "audiencessignin_"
    plugin_order = 25
    auth_level = 2

    _enabled = False
    _notify = True
    _onlyonce = False
    _use_proxy = True
    _cron = DEFAULT_CRON
    _timeout = 90
    _retries = 2
    _retry_interval = 8
    _solver = "none"
    _solver_key = ""
    _cookie = ""
    _history_days = 30

    def init_plugin(self, config: dict = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce"))
        self._use_proxy = bool(config.get("use_proxy", True))
        self._cron = str(config.get("cron") or DEFAULT_CRON).strip()
        self._timeout = int(config.get("timeout") or 90)
        self._retries = int(config.get("retries") or 2)
        self._retry_interval = int(config.get("retry_interval") or 8)
        self._solver = str(config.get("solver") or "none").strip()
        self._solver_key = str(config.get("solver_key") or "").strip()
        self._cookie = str(config.get("cookie") or "").strip()
        self._history_days = int(config.get("history_days") or 30)
        try:
            CronTrigger.from_crontab(self._cron)
        except Exception as err:
            logger.error(f"{PLUGIN_NAME}: Cron 无效：{err}")
            self._cron = DEFAULT_CRON
        if self._onlyonce:
            self._onlyonce = False
            self._save_config()
            Thread(target=self.signin, name="audiences-signin-once", daemon=True).start()
            logger.info(f"{PLUGIN_NAME}: 已启动立即执行任务")

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self) -> None:
        return None

    def _save_config(self) -> None:
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "use_proxy": self._use_proxy,
            "cron": self._cron,
            "timeout": self._timeout,
            "retries": self._retries,
            "retry_interval": self._retry_interval,
            "solver": self._solver,
            "solver_key": self._solver_key,
            "cookie": self._cookie,
            "history_days": self._history_days,
        })

    def get_command(self) -> List[Dict[str, Any]]:
        return [{
            "cmd": "/audiences_signin",
            "event": EventType.PluginAction,
            "desc": "观众站点签到",
            "category": "站点",
            "data": {"action": "audiences_signin"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.signin,
            "methods": ["GET"],
            "summary": "立即执行观众签到",
            "description": "使用站点 Cookie 完成 audiences.me 的 Turnstile 签到",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [{
            "id": "AudiencesSignIn",
            "name": "观众站点自动签到",
            "trigger": CronTrigger.from_crontab(self._cron),
            "func": self.signin,
            "kwargs": {},
        }]

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event) -> None:
        data = event.event_data or {}
        if data.get("action") != "audiences_signin":
            return
        self.signin()

    def signin(self) -> Dict[str, Any]:
        if not _SIGN_LOCK.acquire(blocking=False):
            logger.warning(f"{PLUGIN_NAME}: 已有签到任务在执行，跳过")
            return {"success": False, "message": "已有签到任务在执行"}
        try:
            return self._signin_with_retry()
        finally:
            _SIGN_LOCK.release()

    def _signin_with_retry(self) -> Dict[str, Any]:
        last = {"success": False, "message": "未执行"}
        attempts = max(1, self._retries + 1)
        for index in range(attempts):
            try:
                last = self._signin_once()
            except Exception as err:
                last = {"success": False, "message": f"异常：{err}"}
                logger.error(f"{PLUGIN_NAME}: {err}\n{traceback.format_exc()}")
            if last.get("success"):
                break
            if index < attempts - 1:
                logger.warning(
                    f"{PLUGIN_NAME}: 第 {index + 1} 次失败，"
                    f"{self._retry_interval} 秒后重试：{last.get('message')}"
                )
                time.sleep(max(1, self._retry_interval))
        self._record(last)
        self._notify_result(last)
        return last

    def _signin_once(self) -> Dict[str, Any]:
        site = self._load_site()
        cookie = (self._cookie or site.get("cookie") or "").strip()
        ua = (site.get("ua") or DEFAULT_UA).strip()
        base_url = (site.get("url") or f"https://{SITE_DOMAIN}/").strip()
        if not base_url.endswith("/"):
            base_url += "/"
        attendance_url = urljoin(base_url, ATTENDANCE_PATH.lstrip("/"))
        if not cookie:
            return {"success": False, "message": "未找到观众站 Cookie，请在站点设置或插件中填写"}

        proxies = settings.PROXY if self._use_proxy else None
        timeout = max(30, self._timeout)
        logger.info(f"{PLUGIN_NAME}: 开始访问 {attendance_url}")

        html = self._http_get(attendance_url, cookie, ua, proxies, timeout)
        if html and under_challenge(html):
            logger.warning(f"{PLUGIN_NAME}: 普通请求命中 Cloudflare 防护，改用浏览器")
            html = ""
        if html:
            if not _html_logged_in(html):
                return {"success": False, "message": "Cookie 已失效，请更新观众站 Cookie 和 UA"}
            if _html_signed(html) and not _html_has_turnstile(html):
                detail = _extract_detail(html) or "今日已签到"
                return {"success": True, "message": detail, "already": True}

        browser_result = self._signin_with_browser(attendance_url, cookie, ua, timeout)
        if browser_result.get("success") or browser_result.get("token"):
            if browser_result.get("success"):
                return browser_result
            token = browser_result.get("token")
            posted = self._post_token(attendance_url, cookie, ua, proxies, timeout, token)
            if posted.get("success"):
                return posted

        if self._solver != "none" and self._solver_key:
            sitekey = browser_result.get("sitekey") or DEFAULT_SITEKEY
            if html:
                match = SITEKEY_RE.search(html)
                if match:
                    sitekey = match.group(1)
            logger.info(f"{PLUGIN_NAME}: 浏览器未能完成验证，改用 {self._solver} 获取 Token")
            token = self._solve_turnstile(sitekey, attendance_url)
            if not token:
                return {"success": False, "message": f"{self._solver} 未能获得 Turnstile Token"}
            posted = self._post_token(attendance_url, cookie, ua, proxies, timeout, token)
            if posted.get("success"):
                return posted
            return posted

        if browser_result.get("message"):
            return browser_result
        return {
            "success": False,
            "message": "未能通过人机验证。Docker 无头浏览器经常过不了 Turnstile，请在插件中配置 YesCaptcha / CapSolver / 2Captcha",
        }

    def _load_site(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "name": SITE_NAME,
            "domain": SITE_DOMAIN,
            "url": f"https://{SITE_DOMAIN}/",
            "cookie": "",
            "ua": DEFAULT_UA,
            "proxy": self._use_proxy,
        }
        if SiteOper:
            try:
                site = SiteOper().get_by_domain(SITE_DOMAIN)
                if site:
                    info.update({
                        "name": getattr(site, "name", SITE_NAME) or SITE_NAME,
                        "url": getattr(site, "url", info["url"]) or info["url"],
                        "cookie": getattr(site, "cookie", "") or "",
                        "ua": getattr(site, "ua", "") or DEFAULT_UA,
                        "proxy": bool(getattr(site, "proxy", False)),
                    })
                    return info
            except Exception as err:
                logger.debug(f"{PLUGIN_NAME}: SiteOper 读取失败：{err}")
        if SitesHelper:
            try:
                helper = SitesHelper()
                indexer = None
                if hasattr(helper, "get_indexer"):
                    indexer = helper.get_indexer(SITE_DOMAIN)
                if not indexer and hasattr(helper, "get_indexers"):
                    for item in helper.get_indexers() or []:
                        blob = f"{item.get('name','')} {item.get('domain','')} {item.get('url','')}"
                        if SITE_DOMAIN in blob.lower() or item.get("name") == SITE_NAME:
                            indexer = item
                            break
                if indexer:
                    info.update({
                        "name": indexer.get("name") or SITE_NAME,
                        "url": indexer.get("url") or info["url"],
                        "cookie": indexer.get("cookie") or "",
                        "ua": indexer.get("ua") or DEFAULT_UA,
                        "proxy": bool(indexer.get("proxy")),
                    })
            except Exception as err:
                logger.debug(f"{PLUGIN_NAME}: SitesHelper 读取失败：{err}")
        return info

    def _proxies_server(self) -> Optional[dict]:
        if not self._use_proxy:
            return None
        return getattr(settings, "PROXY_SERVER", None)

    def _http_get(self, url: str, cookie: str, ua: str, proxies, timeout: int) -> str:
        try:
            res = RequestUtils(cookies=cookie, ua=ua, proxies=proxies, timeout=min(timeout, 30)).get_res(url)
            if res is not None and res.status_code == 200:
                return res.text or ""
            logger.warning(f"{PLUGIN_NAME}: GET 失败 status={getattr(res, 'status_code', None)}")
        except Exception as err:
            logger.warning(f"{PLUGIN_NAME}: GET 异常：{err}")
        return ""

    def _post_token(self, url: str, cookie: str, ua: str, proxies, timeout: int, token: str) -> Dict[str, Any]:
        data = {
            "cf-turnstile-response": token,
            "cf-token": token,
        }
        try:
            res = RequestUtils(
                cookies=cookie,
                ua=ua,
                proxies=proxies,
                timeout=timeout,
                referer=url,
            ).post_res(url, data=data)
        except Exception as err:
            return {"success": False, "message": f"提交 Token 失败：{err}"}
        if res is None:
            return {"success": False, "message": "提交 Token 无响应"}
        html = res.text or ""
        if _html_signed(html) and not _html_has_turnstile(html):
            detail = _extract_detail(html) or "签到成功"
            logger.info(f"{PLUGIN_NAME}: {detail}")
            return {"success": True, "message": detail}
        if _html_has_turnstile(html):
            return {"success": False, "message": "Token 提交后仍需人机验证"}
        if res.status_code == 200 and _html_logged_in(html):
            detail = _extract_detail(html) or "签到成功"
            return {"success": True, "message": detail}
        return {"success": False, "message": f"提交 Token 后状态码 {res.status_code}"}

    def _signin_with_browser(self, url: str, cookie: str, ua: str, timeout: int) -> Dict[str, Any]:
        try:
            from cloakbrowser import launch_context
        except Exception as err:
            logger.error(f"{PLUGIN_NAME}: 无法导入 CloakBrowser：{err}")
            return {"success": False, "message": "当前环境没有 CloakBrowser"}

        context = None
        page = None
        result: Dict[str, Any] = {"success": False, "message": "浏览器签到未完成"}
        try:
            context = launch_context(
                headless=True,
                proxy=self._proxies_server(),
                user_agent=ua,
                humanize=True,
            )
            page = context.new_page()
            self._apply_cookies(context, page, cookie)
            page.set_extra_http_headers({
                "cookie": cookie,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            self._wait_cf_clearance(page, timeout=min(timeout, 30))
            html = page.content() or ""
            match = SITEKEY_RE.search(html)
            if match:
                result["sitekey"] = match.group(1)
            if not _html_logged_in(html):
                result["message"] = "浏览器打开签到页后未登录，Cookie 可能失效"
                return result
            if _html_signed(html) and not _html_has_turnstile(html):
                result["success"] = True
                result["message"] = _extract_detail(html) or "今日已签到"
                result["already"] = True
                return result

            token = self._wait_turnstile_token(page, timeout=max(20, timeout - 15))
            html = page.content() or ""
            if _html_signed(html) and not _html_has_turnstile(html):
                result["success"] = True
                result["message"] = _extract_detail(html) or "今日已签到"
                result["already"] = True
                return result
            if token:
                result["token"] = token
                self._submit_attendance_form(page, token)
                time.sleep(2)
                html = page.content() or ""
                if _html_signed(html) and not _html_has_turnstile(html):
                    result["success"] = True
                    result["message"] = _extract_detail(html) or "签到成功"
                    return result
                result["message"] = "已获得 Token，等待提交确认"
                return result
            result["message"] = "浏览器未能完成 Turnstile"
            return result
        except Exception as err:
            logger.error(f"{PLUGIN_NAME}: 浏览器签到失败：{err}")
            result["message"] = f"浏览器签到失败：{err}"
            return result
        finally:
            try:
                if page:
                    page.close()
            except Exception:
                pass
            try:
                if context:
                    context.close()
            except Exception:
                pass

    def _apply_cookies(self, context, page, cookie: str) -> None:
        if not cookie or cookie_parse is None:
            return
        try:
            items = cookie_parse(cookie, array=True) or []
            payload = []
            for item in items:
                payload.append({
                    "name": item.get("name"),
                    "value": item.get("value"),
                    "domain": SITE_DOMAIN,
                    "path": "/",
                })
            if payload:
                context.add_cookies(payload)
        except Exception as err:
            logger.debug(f"{PLUGIN_NAME}: add_cookies 失败，改用请求头：{err}")
            try:
                page.set_extra_http_headers({"cookie": cookie})
            except Exception:
                pass

    @staticmethod
    def _wait_cf_clearance(page, timeout: int = 20) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                title = (page.title() or "").strip().lower()
            except Exception:
                title = ""
            if title and not any(word in title for word in ("just a moment", "请稍候", "loading")):
                return
            time.sleep(1)

    def _wait_turnstile_token(self, page, timeout: int = 45) -> str:
        deadline = time.time() + timeout
        clicked = False
        while time.time() < deadline:
            try:
                html = page.content() or ""
                if _html_signed(html) and not _html_has_turnstile(html):
                    return ""
            except Exception:
                html = ""
            token = self._read_token(page)
            if token:
                return token
            if not clicked:
                self._click_turnstile(page)
                clicked = True
            elif int(time.time()) % 8 == 0:
                self._click_turnstile(page)
            time.sleep(1)
        return self._read_token(page)

    @staticmethod
    def _read_token(page) -> str:
        try:
            token = page.evaluate(
                """() => {
                    const response = document.querySelector("[name=cf-turnstile-response]");
                    const hidden = document.querySelector("#cf-token");
                    return (response && response.value) || (hidden && hidden.value) || "";
                }"""
            )
            return str(token or "").strip()
        except Exception:
            return ""

    def _click_turnstile(self, page) -> None:
        selectors = [
            ".cf-turnstile",
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='turnstile']",
        ]
        for selector in selectors:
            try:
                page.click(selector, timeout=1500)
                logger.debug(f"{PLUGIN_NAME}: 点击 {selector}")
                break
            except Exception:
                continue
        try:
            frame = page.query_selector("iframe[src*='challenges.cloudflare.com']")
            if frame:
                box = frame.bounding_box()
                if box and box.get("width", 0) > 10 and box.get("height", 0) > 10:
                    page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
        except Exception:
            pass
        try:
            for frame in getattr(page, "frames", []) or []:
                url = getattr(frame, "url", "") or ""
                if "challenges.cloudflare.com" not in url and "turnstile" not in url:
                    continue
                for selector in ("input[type=checkbox]", "label", "#cf-stage", "body"):
                    try:
                        frame.click(selector, timeout=1200)
                        break
                    except Exception:
                        continue
                for child in getattr(frame, "child_frames", []) or []:
                    for selector in ("input[type=checkbox]", "label", "body"):
                        try:
                            child.click(selector, timeout=800)
                            break
                        except Exception:
                            continue
        except Exception:
            pass

    @staticmethod
    def _submit_attendance_form(page, token: str) -> None:
        try:
            page.evaluate(
                """(tok) => {
                    const form = document.getElementById("attendance-form");
                    let response = document.querySelector("[name=cf-turnstile-response]");
                    if (!response && form) {
                        response = document.createElement("input");
                        response.type = "hidden";
                        response.name = "cf-turnstile-response";
                        form.appendChild(response);
                    }
                    if (response) response.value = tok;
                    const hidden = document.getElementById("cf-token");
                    if (hidden) hidden.value = tok;
                    if (form) form.submit();
                    return true;
                }""",
                token,
            )
        except Exception:
            pass

    def _solve_turnstile(self, sitekey: str, pageurl: str) -> str:
        solver = self._solver
        if solver in ("yescaptcha", "capsolver"):
            if solver == "yescaptcha":
                api = "https://api.yescaptcha.com"
                task_type = "TurnstileTaskProxyless"
            else:
                api = "https://api.capsolver.com"
                task_type = "AntiTurnstileTaskProxyLess"
            return self._solve_task_api(api, task_type, sitekey, pageurl)
        if solver in ("twocaptcha", "2captcha"):
            return self._solve_2captcha(sitekey, pageurl)
        logger.warning(f"{PLUGIN_NAME}: 未知打码平台 {solver}")
        return ""

    def _solve_task_api(self, api: str, task_type: str, sitekey: str, pageurl: str) -> str:
        proxies = settings.PROXY if self._use_proxy else None
        create = RequestUtils(proxies=proxies, timeout=30).post_res(
            f"{api}/createTask",
            json={
                "clientKey": self._solver_key,
                "task": {
                    "type": task_type,
                    "websiteURL": pageurl,
                    "websiteKey": sitekey,
                },
            },
        )
        if create is None:
            logger.error(f"{PLUGIN_NAME}: {api} createTask 无响应")
            return ""
        try:
            created = create.json()
        except Exception:
            logger.error(f"{PLUGIN_NAME}: {api} createTask 返回无法解析")
            return ""
        if created.get("errorId"):
            logger.error(f"{PLUGIN_NAME}: {api} 错误：{created.get('errorDescription') or created}")
            return ""
        task_id = created.get("taskId")
        if not task_id:
            logger.error(f"{PLUGIN_NAME}: {api} 未返回 taskId")
            return ""
        deadline = time.time() + max(60, self._timeout)
        while time.time() < deadline:
            time.sleep(3)
            poll = RequestUtils(proxies=proxies, timeout=30).post_res(
                f"{api}/getTaskResult",
                json={"clientKey": self._solver_key, "taskId": task_id},
            )
            if poll is None:
                continue
            try:
                data = poll.json()
            except Exception:
                continue
            status = data.get("status")
            if status == "ready":
                solution = data.get("solution") or {}
                token = solution.get("token") or solution.get("cf-turnstile-response") or ""
                if token:
                    logger.info(f"{PLUGIN_NAME}: {api} 已拿到 Token")
                    return token
                return ""
            if data.get("errorId"):
                logger.error(f"{PLUGIN_NAME}: {api} 取结果失败：{data.get('errorDescription') or data}")
                return ""
        logger.error(f"{PLUGIN_NAME}: {api} 等待 Token 超时")
        return ""

    def _solve_2captcha(self, sitekey: str, pageurl: str) -> str:
        proxies = settings.PROXY if self._use_proxy else None
        created = RequestUtils(proxies=proxies, timeout=30).post_res(
            "https://2captcha.com/in.php",
            data={
                "key": self._solver_key,
                "method": "turnstile",
                "sitekey": sitekey,
                "pageurl": pageurl,
                "json": 1,
            },
        )
        if created is None:
            return ""
        try:
            data = created.json()
        except Exception:
            return ""
        if data.get("status") != 1:
            logger.error(f"{PLUGIN_NAME}: 2Captcha 下单失败：{data}")
            return ""
        request_id = data.get("request")
        deadline = time.time() + max(60, self._timeout)
        while time.time() < deadline:
            time.sleep(5)
            poll = RequestUtils(proxies=proxies, timeout=30).get_res(
                "https://2captcha.com/res.php",
                params={
                    "key": self._solver_key,
                    "action": "get",
                    "id": request_id,
                    "json": 1,
                },
            )
            if poll is None:
                continue
            try:
                result = poll.json()
            except Exception:
                continue
            if result.get("status") == 1:
                return result.get("request") or ""
            if result.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                logger.error(f"{PLUGIN_NAME}: 2Captcha 失败：{result}")
                return ""
        return ""

    def _record(self, result: Dict[str, Any]) -> None:
        history = self.get_data("history") or []
        if not isinstance(history, list):
            history = [history]
        history.insert(0, {
            "date": _now_str(),
            "day": _today(),
            "success": bool(result.get("success")),
            "already": bool(result.get("already")),
            "message": result.get("message") or "",
        })
        keep = max(7, self._history_days)
        self.save_data("history", history[:keep])

    def _notify_result(self, result: Dict[str, Any]) -> None:
        success = bool(result.get("success"))
        message = result.get("message") or ""
        if success:
            title = f"{PLUGIN_NAME}成功"
            if result.get("already"):
                text = f"{SITE_NAME} 今日已签到。{message}".strip()
            else:
                text = f"{SITE_NAME} {message or '签到成功'}"
        else:
            title = f"{PLUGIN_NAME}失败"
            text = f"{SITE_NAME} 签到失败：{message}"
        logger.info(f"{PLUGIN_NAME}: {text}")
        if not self._notify:
            return
        try:
            self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)
        except Exception as err:
            logger.error(f"{PLUGIN_NAME}: 发送通知失败：{err}")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._switch("enabled", "启用插件"),
                            self._switch("notify", "签到后发送通知"),
                            self._switch("onlyonce", "立即运行一次"),
                            self._switch("use_proxy", "使用系统代理"),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._text("cron", "执行周期", "5 位 cron，默认每天 08:35", 6),
                            self._number("timeout", "超时(秒)", 3),
                            self._number("retries", "失败重试次数", 3),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "solver",
                                        "label": "Turnstile 打码平台（可选，推荐）",
                                        "items": [
                                            {"title": "不使用（仅浏览器尝试）", "value": "none"},
                                            {"title": "YesCaptcha", "value": "yescaptcha"},
                                            {"title": "CapSolver", "value": "capsolver"},
                                            {"title": "2Captcha", "value": "twocaptcha"},
                                        ],
                                    },
                                }],
                            },
                            self._text("solver_key", "打码平台 API Key", "Docker 无头环境过不了勾选框时需要", 6),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._text("cookie", "Cookie 覆盖（留空则用站点管理里的观众站 Cookie）", "", 12),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": (
                                            "观众站签到页会先走 Cloudflare Turnstile，验证通过后自动 POST attendance.php。"
                                            "官方「站点自动签到」只是打开页面，看到已登录就报成功，实际没勾验证。"
                                            "建议把观众从自动签到名单里去掉，避免假成功。"
                                            "本插件会读取站点管理中的观众 Cookie/UA；无头浏览器经常过不了勾选框，失败时请配置打码平台。"
                                        ),
                                    },
                                }],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "use_proxy": True,
            "cron": DEFAULT_CRON,
            "timeout": 90,
            "retries": 2,
            "retry_interval": 8,
            "solver": "none",
            "solver_key": "",
            "cookie": "",
            "history_days": 30,
        }

    @staticmethod
    def _switch(model: str, label: str) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 3},
            "content": [{
                "component": "VSwitch",
                "props": {"model": model, "label": label},
            }],
        }

    @staticmethod
    def _text(model: str, label: str, placeholder: str, md: int) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{
                "component": "VTextField",
                "props": {
                    "model": model,
                    "label": label,
                    "placeholder": placeholder,
                    "clearable": True,
                },
            }],
        }

    @staticmethod
    def _number(model: str, label: str, md: int) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{
                "component": "VTextField",
                "props": {"model": model, "label": label, "type": "number"},
            }],
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data("history") or []
        if not isinstance(historys, list):
            historys = [historys]
        if not historys:
            return [{
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": "暂无签到记录，启用插件后可立即运行一次。"},
            }]
        rows = []
        for item in historys[: self._history_days]:
            success = bool(item.get("success"))
            color = "success" if success else "error"
            status = "已签到" if item.get("already") else ("成功" if success else "失败")
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": item.get("date") or ""},
                    {
                        "component": "td",
                        "content": [{
                            "component": "VChip",
                            "props": {"color": color, "size": "small", "variant": "tonal"},
                            "text": status,
                        }],
                    },
                    {"component": "td", "text": item.get("message") or ""},
                ],
            })
        return [{
            "component": "VTable",
            "props": {"hover": True, "density": "comfortable"},
            "content": [
                {
                    "component": "thead",
                    "content": [{
                        "component": "tr",
                        "content": [
                            {"component": "th", "text": "时间"},
                            {"component": "th", "text": "结果"},
                            {"component": "th", "text": "详情"},
                        ],
                    }],
                },
                {"component": "tbody", "content": rows},
            ],
        }]
