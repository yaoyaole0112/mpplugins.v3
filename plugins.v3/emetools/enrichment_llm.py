import asyncio
import json

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


async def search_portraits_with_moviepilot(item, actors):
    model = None
    try:
        from app.agent.llm.helper import LLMHelper
        from langchain_core.messages import HumanMessage, SystemMessage

        model = await asyncio.wait_for(LLMHelper.get_llm(
            streaming=False, thinking_level='off', web_search_mode='builtin'), timeout=30)
        query = {'title': item.get('Name'), 'year': item.get('ProductionYear'),
                 'type': item.get('Type'), 'provider_ids': item.get('ProviderIds'),
                 'actors': {str(index): {key: actor.get(key) for key in ('name', 'original_name', 'role', 'id')}
                            for index, actor in enumerate(actors)}}
        response = await asyncio.wait_for(model.ainvoke([
            SystemMessage(content='联网搜索下列作品演员的真实 TMDB 人物页，仅返回 JSON：'
                          '{"序号":{"tmdb_id":整数,"source":"https://www.themoviedb.org/person/ID"}}。'
                          '必须核对出演作品及人物身份，未找到不返回该项；禁止生成头像、猜测 ID 或图片 URL。'),
            HumanMessage(content=json.dumps(query, ensure_ascii=False))]), timeout=90)
        content = response.content
        if isinstance(content, list):
            content = ''.join(block.get('text', '') for block in content
                              if isinstance(block, dict) and block.get('type') == 'text')
        result = json.loads(content.strip().removeprefix('```json').removeprefix('```').removesuffix('```'))
        if not isinstance(result, dict):
            raise ValueError('头像检索未返回 JSON 对象')
        return result
    except Exception as error:
        raise ValueError(f'宿主联网头像检索不可用：{type(error).__name__}') from None
    finally:
        if model is not None:
            await LLMHelper.close_llm(model)
