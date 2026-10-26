import asyncio

from app.runtime.settings import get_runtime_setting
from app.sdk.config import settings


def llm_setting(name, default=""):
    return get_runtime_setting(name, getattr(settings, name, default))


def llm_available():
    return bool(llm_setting("LLM_MODEL") and llm_setting("LLM_API_KEY"))


async def translate_with_moviepilot(messages):
    from app.agent.llm.helper import LLMHelper
    from langchain_core.messages import HumanMessage, SystemMessage

    model = None
    try:
        model = await asyncio.wait_for(LLMHelper.get_llm(
            streaming=False, thinking_level="off", web_search_mode="disabled"), timeout=30)
        response = await asyncio.wait_for(model.ainvoke([
            SystemMessage(content=messages[0]["content"]),
            HumanMessage(content=messages[1]["content"])]), timeout=90)
        content = response.content
        if isinstance(content, list):
            content = "".join(block.get("text", "") for block in content
                              if isinstance(block, dict) and block.get("type") == "text")
        if not isinstance(content, str):
            raise ValueError("LLM 未返回文本")
        return content
    finally:
        if model is not None:
            await LLMHelper.close_llm(model)
