"""Small OpenAI-compatible client used for independently configured date checks."""

import asyncio
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RecentAirAIError(RuntimeError):
    """Safe-to-log AI connection or response error."""


def chat_completions_url(base_url):
    base = str(base_url or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def request_recent_air_dates(base_url, model, api_key, system_prompt, user_prompt, timeout=45):
    """Call a configured OpenAI-compatible Chat Completions endpoint."""
    body = json.dumps({
        "model": str(model),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(chat_completions_url(base_url), data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as err:
        raise RecentAirAIError(f"AI API 返回 HTTP {err.code}") from None
    except URLError as err:
        reason = type(getattr(err, "reason", None)).__name__ or "连接失败"
        raise RecentAirAIError(f"AI API 网络连接失败（{reason}）") from None
    except TimeoutError:
        raise RecentAirAIError("AI API 请求超时") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RecentAirAIError("AI API 返回内容不是有效 JSON") from None

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RecentAirAIError("AI API 响应缺少 choices[0].message.content") from None
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "")
            for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str) or not content.strip():
        raise RecentAirAIError("AI API 未返回文本内容")
    return content


class OpenAICompatibleChatModel:
    """Minimal async adapter matching the message interface used by the verifier."""

    def __init__(self, base_url, model, api_key):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key

    async def ainvoke(self, messages):
        system_prompt, user_prompt = "", ""
        for message in messages:
            role = getattr(message, "type", None) or getattr(message, "role", None)
            content = getattr(message, "content", "")
            if role in {"system", "system_message"}:
                system_prompt = str(content)
            else:
                user_prompt = str(content)
        content = await asyncio.to_thread(
            request_recent_air_dates,
            self.base_url,
            self.model,
            self.api_key,
            system_prompt,
            user_prompt,
        )
        return SimpleNamespace(content=content)
