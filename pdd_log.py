# -*- coding: utf-8 -*-
"""日志出口。

AstrBot 要求插件统一走宿主的 ``astrbot.api.logger``，不得自行初始化 Python
标准库的日志模块。但 ``pdd_browser`` 与 ``socks_relay`` 也能脱离插件单独运行
（调代理、跑中继排查），那种环境里没有 astrbot 模块。这里做一层薄封装：

- 插件上下文里 —— 直接用宿主 logger，级别、格式、落盘由宿主统一管理
- 独立脚本里 —— 打到标准输出的替身，只在 ``--verbose`` 时输出 debug

两个出口都提供 ``debug`` / ``info`` / ``warning`` / ``error``，调用点不必区分。
"""
from __future__ import annotations

import sys


class _Stdout:
    """没有宿主 logger 时的替身。只实现插件用到的那几个级别。"""

    verbose = False

    def _emit(self, level: str, msg: str) -> None:
        if level == "debug" and not self.verbose:
            return
        print(f"[{level}] {msg}", file=sys.stderr, flush=True)

    def debug(self, msg: str) -> None:
        self._emit("debug", msg)

    def info(self, msg: str) -> None:
        self._emit("info", msg)

    def warning(self, msg: str) -> None:
        self._emit("warn", msg)

    def error(self, msg: str) -> None:
        self._emit("error", msg)


FALLBACK = _Stdout()


def get_logger():
    """宿主 logger 优先；取不到时返回标准输出替身。"""
    try:
        from astrbot.api import logger  # 插件内运行时一定存在
    except Exception:
        return FALLBACK
    return logger


def set_verbose(on: bool) -> None:
    """独立脚本用：控制替身是否输出 debug。有宿主 logger 时无效果。"""
    FALLBACK.verbose = bool(on)
