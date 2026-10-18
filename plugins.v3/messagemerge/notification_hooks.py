"""
通知发送共享拦截链

多个插件可在 NotificationMixin.post_message / async_post_message 上注册
拦截处理器，实现消息发送前的拦截（如消息屏蔽、消息合并）。
处理器按优先级顺序执行（数值小者优先），任一处理器返回 True 表示该消息
已被拦截（不再发送）；返回 False 表示放行交给下一个处理器；所有处理器
都放行后调用原始发送方法。

- register(name, handler, priority): 注册拦截处理器（同名重复注册会替换）
- unregister(name): 注销拦截处理器；无处理器时自动还原原始发送方法
- deliver_original(chain, message, **kwargs): 绕过所有处理器直接调用原始
  发送方法（用于合并/转发等需要最终发送的场景）

handler 签名：
    handler(chain, message, meta, mediainfo, torrentinfo, transferinfo, kwargs) -> bool
支持同步函数或异步协程函数。
"""

import asyncio
import threading
from typing import Callable, List, Optional, Tuple

from app.chain import _messaging as _msg_mod
from app.log import logger

_lock = threading.Lock()
_handlers: List[Tuple[int, str, Callable]] = []
_patched = False
_original_post = None
_original_async_post = None


def register(name: str, handler: Callable, priority: int = 100) -> None:
    """注册拦截处理器；同名插件重复注册时替换处理器。"""
    global _patched, _original_post, _original_async_post
    with _lock:
        _handlers[:] = [h for h in _handlers if h[1] != name]
        _handlers.append((priority, name, handler))
        _handlers.sort(key=lambda x: x[0])
        if not _patched:
            _original_post = _msg_mod.NotificationMixin.post_message
            _original_async_post = _msg_mod.NotificationMixin.async_post_message
            _msg_mod.NotificationMixin.post_message = _wrapped_post_message
            _msg_mod.NotificationMixin.async_post_message = _wrapped_async_post_message
            _patched = True
            logger.info("通知拦截链: 已接管消息发送入口")


def unregister(name: str) -> None:
    """注销拦截处理器；无处理器时还原消息发送入口。"""
    global _patched
    with _lock:
        _handlers[:] = [h for h in _handlers if h[1] != name]
        if not _handlers and _patched:
            _msg_mod.NotificationMixin.post_message = _original_post
            _msg_mod.NotificationMixin.async_post_message = _original_async_post
            _patched = False
            logger.info("通知拦截链: 已还原消息发送入口")


def deliver_original(chain, message, **kwargs) -> None:
    """绕过处理器直接调用原始发送方法（用于合并/转发消息的最终发送）。"""
    return _original_post(chain, message=message, **kwargs)


def _wrapped_post_message(self, message=None, meta=None, mediainfo=None,
                          torrentinfo=None, transferinfo=None, **kwargs):
    with _lock:
        handlers = list(_handlers)
    for _priority, name, handler in handlers:
        try:
            if handler(self, message, meta, mediainfo, torrentinfo, transferinfo, kwargs):
                return None
        except Exception as err:
            logger.error(f"通知拦截链: 处理器 {name} 执行出错 {err}")
    return _original_post(self, message=message, meta=meta, mediainfo=mediainfo,
                          torrentinfo=torrentinfo, transferinfo=transferinfo, **kwargs)


async def _wrapped_async_post_message(self, message=None, meta=None, mediainfo=None,
                                      torrentinfo=None, transferinfo=None, **kwargs):
    with _lock:
        handlers = list(_handlers)
    for _priority, name, handler in handlers:
        try:
            result = handler(self, message, meta, mediainfo, torrentinfo, transferinfo, kwargs)
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return None
        except Exception as err:
            logger.error(f"通知拦截链: 处理器 {name} 执行出错 {err}")
    return await _original_async_post(self, message=message, meta=meta, mediainfo=mediainfo,
                                      torrentinfo=torrentinfo, transferinfo=transferinfo, **kwargs)
