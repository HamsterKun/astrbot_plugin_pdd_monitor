# -*- coding: utf-8 -*-
"""可视化登录：把服务器上那台 Chromium 的画面搬进 WebUI。

为什么不在聊天里点
    拼多多的登录流程会插「安全验证」——选择用过的收货地址，严重点还有滑块。
    这类交互没法在聊天窗口里完成。所以换个路子：插件在 WebUI 里开一块画布，
    按帧把登录页截成 JPEG 推过去，鼠标点击、拖动、键盘输入原样转发回页面。

好处
    浏览器始终跑在服务器上，cookie 直接落在服务器，不经过任何人的浏览器；
    用户看到的就是拼多多那个登录页，扫码、滑块、验证码都按原样操作。

内存
    一个会话就是一台 Chromium（几百兆）。所以有硬上限：十分钟没人管就自己收，
    拿到 cookie 五秒后也收，页面关掉再退进程——Chromium 只有进程退出才还内存。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from astrbot.api import logger

try:  # 插件按包加载
    from .pdd_browser import (
        DESKTOP_VIEWPORT,
        STEALTH_DESKTOP_JS,
        BrowserFetcher,
        ensure_qr_visible,
        fit_viewport_to_page,
    )
except ImportError:  # pragma: no cover - AstrBot 按文件加载插件时没有包上下文
    from pdd_browser import (  # type: ignore
        DESKTOP_VIEWPORT,
        STEALTH_DESKTOP_JS,
        BrowserFetcher,
        ensure_qr_visible,
        fit_viewport_to_page,
    )

# 一次会话最多活多久。十分钟够扫个码、点个验证；再久就是没人管了，退进程省内存。
SESSION_TIMEOUT = 600.0
# 帧的 JPEG 质量。55 在手机上看着清楚，一帧三四十 KB，比 PNG 小一个数量级。
FRAME_QUALITY = 55


class RemoteLogin:
    """一次「在 WebUI 里操作登录页」的会话。"""

    def __init__(self, fetcher: Any, on_success: Callable[[str], None],
                 timeout: float = SESSION_TIMEOUT):
        self.fetcher = fetcher
        self.on_success = on_success
        self.timeout = max(60.0, float(timeout))
        self.ctx = None
        self.page = None
        # starting | ready | success | error | closed
        self.status = "starting"
        self.message = "正在打开登录页…"
        self.started_at = time.time()
        # 实际视口尺寸。登录页比默认视口高时会把高度撑开，前端换算点击坐标要用真值
        self.vw = int(DESKTOP_VIEWPORT["width"])
        self.vh = int(DESKTOP_VIEWPORT["height"])
        self._poll_task: Optional[asyncio.Task] = None
        self._warm_task: Optional[asyncio.Task] = None
        self._closed = False

    # ---------------------------------------------------------------- 生命周期
    def alive(self) -> bool:
        return not self._closed and self.status in ("starting", "ready")

    async def start(self) -> None:
        """开浏览器、开登录页。

        这里只做到「页面打开了」就返回——画面要尽快出现在面板上。点开「扫码登录」
        那个 tab 要等 React 渲染，慢起来十几秒，所以扔到后台（_warm）里做，
        否则面板会先空白十几秒，看着像卡死。
        """
        await self.fetcher.start()
        self.ctx = await self.fetcher._new_context(mobile=False)
        self.page = await self.ctx.new_page()
        await self.page.add_init_script(STEALTH_DESKTOP_JS)
        await self.fetcher._open_login_page(self.page)
        # 先量一次页面高度：装不下就把视口撑高，别让二维码掉出画面
        await self._fit()
        self.status = "ready"
        self.message = "登录页已打开，正在点开扫码…"
        self._poll_task = asyncio.create_task(self._poll())
        self._warm_task = asyncio.create_task(self._warm())

    async def _fit(self) -> None:
        """把视口撑到能装下整页。失败不致命，后面还有滚动兜底。"""
        page = self.page
        if page is None or self._closed:
            return
        h = await fit_viewport_to_page(
            page, lambda m: logger.info(f"[pdd] {m}"))
        if h:
            self.vh = int(h)

    async def _warm(self) -> None:
        """后台把「扫码登录」点出来，再让整张码落在画面里。失败不算事：用户自己点也行。"""
        page = self.page
        try:
            await self.fetcher._switch_to_qr_tab(
                page, timeout=12.0, popup_timeout=10.0)
        except Exception as e:
            logger.debug(f"[pdd] 自动点开扫码失败（用户可以手点）：{e}")

        if self.status == "ready":
            self.message = "用拼多多 App 扫上面的码；要换登录方式就在画面上点"

        # 二维码组件是点开之后再去请求、再渲染的，页面高度这时才定型。
        # 网慢时它可能好几秒才出来，位置也会再跳一次，所以至少核对两遍；
        # 还对不上就每隔一阵再试，最多约十五秒。
        ok_passes = 0
        for attempt in range(7):
            try:
                if self._closed:
                    return
                await asyncio.sleep(1.2 if attempt == 0 else 1.8)
                if self._closed:
                    return
                await self._fit()
                got = await ensure_qr_visible(
                    page, lambda m: logger.info(f"[pdd] {m}"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"[pdd] 二维码摆进画面失败（不影响手点）：{e}")
                return
            if got:
                ok_passes += 1
                if ok_passes >= 2:
                    return

        # 再不啃声就没法查了：至少让日志里留下「没找到码」这件事
        logger.warning(
            "[pdd] 画面里没找到二维码元素（类名和正方块兜底都没命中），"
            "码可能仍然只露一半")

    async def _poll(self) -> None:
        """每秒看一次有没有 PDDAccessToken。有就落配置，然后收摊。"""
        while not self._closed:
            try:
                header, fields = await BrowserFetcher._logged_cookie(self.ctx)
                if header:
                    ok = True
                    try:
                        self.on_success(header)
                    except Exception as e:
                        ok = False
                        logger.error(f"[pdd] 登录 cookie 写入失败：{e}")
                    if ok:
                        self.status = "success"
                        self.message = f"登录成功，已写入 {len(fields)} 个字段"
                    else:
                        self.status = "error"
                        self.message = "拿到 cookie 了，但写配置失败，看日志"
                    await asyncio.sleep(5)      # 让页面把「成功」显示完
                    await self.close()
                    return
            except Exception as e:
                logger.debug(f"[pdd] 登录轮询出错（继续）：{e}")

            if time.time() - self.started_at > self.timeout:
                self.status = "error"
                self.message = f"会话超过 {int(self.timeout / 60)} 分钟，已关掉浏览器"
                await self.close()
                return
            await asyncio.sleep(1.0)

    async def close(self) -> None:
        """收掉页面、上下文、浏览器。幂等。"""
        if self._closed:
            return
        self._closed = True
        if self.status in ("starting", "ready"):
            self.status = "closed"
        for task in (self._poll_task, self._warm_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        await self.fetcher._close_quietly(self.page)
        await self.fetcher._close_quietly(self.ctx)
        try:
            await self.fetcher.close()
        except Exception as e:      # pragma: no cover - 收尾不抛
            logger.debug(f"[pdd] 关浏览器出错（忽略）：{e}")
        logger.info("[pdd] 可视化登录会话已结束")

    # -------------------------------------------------------------------- 画面
    async def frame(self) -> Optional[bytes]:
        """截一帧 JPEG。返回 None 表示现在没画面。"""
        if self.page is None or self._closed:
            return None
        try:
            return await asyncio.wait_for(
                self.page.screenshot(type="jpeg", quality=FRAME_QUALITY),
                timeout=8.0,
            )
        except Exception as e:
            logger.debug(f"[pdd] 截帧失败：{e}")
            return None

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "w": int(self.vw),
            "h": int(self.vh),
            "left": max(0, int(self.timeout - (time.time() - self.started_at))),
            "alive": self.alive(),
            # 浏览器进程是否已经退了。前端靠它决定是说「正在关」还是「已经关了」
            "closed": self._closed,
        }

    # -------------------------------------------------------------------- 输入
    async def send(self, ev: dict) -> bool:
        """把画布上的操作翻译成 Playwright 的鼠标/键盘动作。

        拖动要拆成 down → move → up：滑块验证码就是靠中间那一串 move 判定的，
        只发 down/up 会因为「没有轨迹」被判成机器。
        """
        page = self.page
        if page is None or self._closed:
            return False
        kind = str(ev.get("t") or "")
        try:
            x = float(ev.get("x") or 0)
            y = float(ev.get("y") or 0)
        except (TypeError, ValueError):
            x = y = 0.0

        try:
            if kind == "down":
                await page.mouse.move(x, y)
                await page.mouse.down()
            elif kind == "move":
                await page.mouse.move(x, y, steps=2)
            elif kind == "up":
                await page.mouse.up()
            elif kind == "click":
                await page.mouse.click(x, y)
            elif kind == "wheel":
                await page.mouse.wheel(0, float(ev.get("dy") or 0))
            elif kind == "type":
                await page.keyboard.type(str(ev.get("text") or ""), delay=30)
            elif kind == "key":
                await page.keyboard.press(str(ev.get("key") or "Enter"))
            elif kind == "reload":
                await page.reload(wait_until="domcontentloaded")
                # 刷新回到了页面顶部，二维码又跑到下沿之外了：重新量高度、点开、再摆一次
                if self._warm_task is not None and not self._warm_task.done():
                    self._warm_task.cancel()
                self._warm_task = asyncio.create_task(self._warm())
            else:
                return False
        except Exception as e:
            logger.debug(f"[pdd] 转发输入失败（{kind}）：{e}")
            return False
        return True
