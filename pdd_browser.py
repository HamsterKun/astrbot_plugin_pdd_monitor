# -*- coding: utf-8 -*-
"""浏览器取数：拼多多搜索页改成客户端渲染后，唯一能拿到商品数据的路子。

为什么必须开浏览器
    搜索页的 HTML 只是个空骨架（`<div id="root"></div>` 加几个 script），商品是
    浏览器执行 JS 后由 XHR 拉回来的：
      - 请求要 `anti_content` 签名（606 字符），由页面 JS 现算，带时效；
      - 响应体是 `rc-res-data` 网关密文（十万字符），解密器在按需加载的 chunk 里。
    签名和加密都在浏览器内部，纯 HTTP 复刻要逆向两套算法，所以让浏览器自己跑完。

怎么拿到明文
    在页面脚本执行前注入 hook，把 `JSON.parse` 和 `Response.prototype.json`
    包一层。解密后的对象一定会经过它们 —— 实测一次搜索能截到 20 条商品，
    字段是 `items[].item_data.goods_model`，有 goods_id / 价格原值 / 销量，
    比解析 DOM 干净得多，也不受改版换类名影响。

为什么还要一层本地中继
    Chromium 的 SOCKS5 握手只声明「免认证」（0x00），用户名密码根本不发。
    而家宽代理为了安全是强制认证的，两者对不上。所以在本地起一个免认证端口，
    由中继带账号密码去连上游 —— 公网暴露的那一端仍然有密码保护。

内核
    pip 只装 playwright 这个 Python 包，Chromium 内核要另外下载（一百多 MB）。
    首次取数发现内核缺失时会自动跑 `python -m playwright install chromium`
    （auto_install=False 可关掉），失败时把原因和处理命令一起抛出来。

典型用法
    f = BrowserFetcher(proxy="socks5://user:pass@frp:port", cookie="...")
    await f.start()
    try:
        payload = await f.search("9600X")          # 解密后的整份响应
    finally:
        await f.close()
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

try:  # 作为 AstrBot 插件的一部分加载
    from .pdd_log import get_logger
    from .socks_relay import Socks5Relay
except ImportError:  # 被当普通脚本 import 时没有包上下文
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pdd_log import get_logger
    from socks_relay import Socks5Relay

# 日志走宿主 logger（见 pdd_log）；独立运行时退回标准输出
log = get_logger()

SEARCH_URL = "https://mobile.yangkeduo.com/search_result.html"
HOME_URL = "https://mobile.yangkeduo.com/"

# 移动端环境三要素：iPhone UA + 440×956 视口 + is_mobile。少一个就会被判
# 「系统繁忙」—— 用桌面 UA 去访问搜索页必定失败。
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 "
             "Mobile/15E148 Safari/604.1")

VIEWPORT = {"width": 440, "height": 956}

# 无头浏览器最好认的几个特征：webdriver 为 true、语言集不对、platform 是 Linux。
# iOS 上 Safari 本来就没什么插件，所以只补这几项，不硬造 plugins 列表。
STEALTH_JS = r"""
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
    Object.defineProperty(navigator, 'platform', { get: () => 'iPhone' });
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 5 });
  } catch (e) {}
})();
"""

# 截获解密后的明文。两条路径都挂：axios 走 JSON.parse，fetch 走 Response.json。
# 只留「数组最长的那份」，一次搜索里热搜/灰度配置也会走 JSON.parse，不能留错对象。
HOOK_JS = r"""
(() => {
  if (window.__PDD_HOOKED__) return;
  window.__PDD_HOOKED__ = 1;
  window.__PDD_HITS__ = 0;

  const looksLikeGoods = (t) =>
    /goodsName|goods_name|min_group_price|thumb_url|sales_tip|goods_id|link_url/i.test(t);

  const pickBestArray = (r) => {
    let best = { n: 0, path: '' };
    const walk = (x, path, d) => {
      if (d > 5 || !x || typeof x !== 'object') return;
      for (const k in x) {
        const v = x[k];
        if (Array.isArray(v)) {
          if (v.length > best.n) best = { n: v.length, path: path ? path + '.' + k : k };
        } else if (v && typeof v === 'object') {
          walk(v, path ? path + '.' + k : k, d + 1);
        }
      }
    };
    walk(r, '', 0);
    return best;
  };

  const collect = (r, rawText) => {
    try {
      if (!r || typeof r !== 'object' || Array.isArray(r)) return;
      const text = rawText || JSON.stringify(r) || '';
      if (!looksLikeGoods(text.slice(0, 200000))) return;
      const best = pickBestArray(r);
      if (best.n < 1) return;
      window.__PDD_HITS__++;
      if (best.n >= (window.__PDD_BEST_N__ || 0)) {
        window.__PDD_BEST__ = r;
        window.__PDD_BEST_PATH__ = best.path;
        window.__PDD_BEST_N__ = best.n;
      }
    } catch (e) {}
  };

  const origParse = JSON.parse;
  JSON.parse = function (text, reviver) {
    const r = origParse.call(this, text, reviver);
    collect(r, typeof text === 'string' ? text : '');
    return r;
  };

  if (window.Response && Response.prototype.json) {
    const origJson = Response.prototype.json;
    Response.prototype.json = function () {
      return origJson.call(this).then((r) => { collect(r, ''); return r; });
    };
  }
})();
"""

# 轮询用：有没有截到 / 截到多大
POLL_JS = "() => window.__PDD_BEST__ ? (window.__PDD_BEST_N__ || 1) : 0"
DUMP_JS = ("() => { try { return JSON.stringify(window.__PDD_BEST__ || null); }"
           " catch (e) { return null; } }")
DIAG_JS = r"""
() => {
  let txt = '';
  try { txt = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 300); }
  catch (e) {}
  return {
    title: document.title || '',
    url: location.href,
    text: txt,
    hits: window.__PDD_HITS__ || 0,
    hasBest: !!window.__PDD_BEST__,
    prices: (txt.match(/[¥￥]\s*\d/g) || []).length,
  };
}
"""


class BrowserUnavailable(RuntimeError):
    """Playwright 或浏览器内核没装。"""


# 内核缺失时 Playwright 抛的原话。用它区分「缺内核」和「启动失败」两类错误：
# 前者可以自动补下载，后者补了也没用。
KERNEL_MISSING_HINTS = (
    "executable doesn't exist",
    "was just installed or updated",
    "please run the following command to download",
)

INSTALL_CMD = "python -m playwright install chromium"


def kernel_missing(err: BaseException) -> bool:
    """这个异常是不是「内核文件不存在」。"""
    text = str(err).lower()
    return any(h in text for h in KERNEL_MISSING_HINTS)


def _brief(err: BaseException, limit: int = 120) -> str:
    """异常压成一行：Playwright 的报错动辄十几行带方框，塞进聊天窗口不合适。"""
    text = " ".join(str(err).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class BrowserError(RuntimeError):
    """取数失败，message 里带可读原因。"""


def parse_cookie_header(raw: str) -> list[dict]:
    """把一整条 Cookie 头拆成 Playwright 认的 cookie 列表。"""
    text = (raw or "").strip().strip('"').strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    out = []
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if name:
            out.append({"name": name, "value": value,
                        "domain": ".yangkeduo.com", "path": "/"})
    return out


class BrowserFetcher:
    """一个长期存活的 Chromium，按次开上下文取一份搜索数据。

    Chromium 启动要一两秒，所以实例复用、上下文每次新建 —— 上下文里带 cookie
    和 UA，用完就关，避免页面之间互相影响。

    但进程本身必须定期退出。Chromium 关闭页面、关闭上下文都不会把内存还给系统：
    V8 堆、网络缓存、渲染进程的私有页一旦涨上去就贴着不动。长时间跑下来 RSS
    只增不减，机房小内存机器会被拖到换页。所以这里有两道回收：
    取数够 N 次回收一次，空闲超过 N 秒回收一次。回收 = 进程退出 = 内存全额归还。
    """

    def __init__(
        self,
        *,
        proxy: str = "",
        cookie: str = "",
        channel: str = "",
        headless: bool = True,
        user_agent: str = IPHONE_UA,
        timeout: float = 45.0,
        poll_interval: float = 0.4,
        auto_install: bool = True,
        install_timeout: float = 900.0,
        idle_timeout: float = 600.0,
        max_searches: int = 12,
        warm_home: bool = True,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.proxy = (proxy or "").strip()
        self.cookie = cookie or ""
        self.channel = channel or ""
        self.headless = headless
        self.user_agent = user_agent or IPHONE_UA
        self.timeout = max(5.0, float(timeout))
        self.poll_interval = max(0.1, float(poll_interval))
        self.auto_install = bool(auto_install)
        self.install_timeout = max(60.0, float(install_timeout))
        self.idle_timeout = max(0.0, float(idle_timeout))
        self.max_searches = max(0, int(max_searches))
        self.warm_home = bool(warm_home)
        self._log_fn = on_log

        self._pw = None
        self._browser = None
        self._relay = None
        self._lock = asyncio.Lock()
        self.last_diag: dict[str, Any] = {}
        # 内核状态：'' 未知 / 'installing' 下载中 / 'installed' 刚补上 / 'failed' 补失败
        self.kernel_state = ""
        self.kernel_error = ""
        # 回收计数：本进程实例已取数几次、最后一次用是什么时候
        self._searches = 0
        self._last_used = 0.0
        self.recycled = 0          # 累计回收次数，供诊断显示
        self._home_warmed = False  # 本浏览器进程是否已访问过首页

    # ---------------------------------------------------------------- 生命周期
    def _log(self, msg: str) -> None:
        """交出去记，没人接才自己记。

        插件里 main 会传一个回调（顺带给消息加 [pdd] 前缀），此时不必再写一遍，
        否则同一行日志出现两次。
        """
        if self._log_fn:
            try:
                self._log_fn(msg)
                return
            except Exception:
                pass
        log.debug(msg)

    async def start(self) -> None:
        if self._browser is not None:
            # 进程可能已经崩了（OOM、被系统杀）。只判 None 会让实例一辈子起不来，
            # 每轮都拿一个死句柄去开上下文，然后一直失败到重启插件为止。
            try:
                alive = self._browser.is_connected()
            except Exception:
                alive = False
            if alive:
                return
            self._log("浏览器进程已断开，重新拉起")
            await self._shutdown_pw()
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise BrowserUnavailable(
                "没装 playwright｜pip 依赖没装上｜执行 pip install playwright，"
                "再执行 python -m playwright install chromium"
            ) from e

        kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                # 无头取数用不到 GPU，省掉显存与共享内存映射
                "--disable-gpu",
                # 单页场景，限制渲染进程数，避免每个 iframe 各起一个进程
                "--renderer-process-limit=1",
                # 只渲染一页，浏览器其余功能全部关掉，减少常驻内存与后台请求
                "--mute-audio",
                "--no-first-run",
                "--disable-extensions",
                "--disable-sync",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-features=Translate,MediaRouter,OptimizationHints",
                # 磁盘缓存压到 1MB：每次都是同一个搜索页，缓不缓存区别不大
                "--disk-cache-size=1048576",
                # 给渲染进程的 V8 堆封顶。默认上限按物理内存算，小机器上会越吃越多
                "--js-flags=--max-old-space-size=512",
            ],
        }
        if self.channel:
            # 默认的 headless=True 走 chrome-headless-shell，那是个精简壳子，
            # 指纹比完整内核好认。填 chromium 用完整内核的新无头模式。
            kwargs["channel"] = self.channel

        if self.proxy:
            kwargs["proxy"] = self._build_proxy()

        self._pw = await async_playwright().start()
        try:
            self._browser = await self._launch(kwargs)
        except Exception:
            await self._shutdown_pw()
            raise
        # 新进程 = 新的浏览器存储，首页要重新访一次
        self._home_warmed = False
        self._log(f"浏览器已启动（headless={self.headless}"
                  f"{', channel=' + self.channel if self.channel else ''}）")

    async def _launch(self, kwargs: dict[str, Any]):
        """launch 一次；缺内核就先补下载再 launch。

        pip 只装 playwright 这个 Python 包，Chromium 内核是单独下载的（一百多 MB），
        首次安装插件时一定缺。这里补上，省掉手工执行一条命令的步骤。
        """
        try:
            return await self._pw.chromium.launch(**kwargs)
        except Exception as e:
            if not (kernel_missing(e) and self.auto_install):
                raise BrowserError(f"启动浏览器失败｜{_brief(e)}") from e
            if self.channel:
                # channel 用的是系统里已装的 Chrome/Edge，playwright install 装不到它
                raise BrowserUnavailable(
                    f"channel={self.channel} 找不到对应的浏览器｜"
                    f"browser_channel 填的是系统浏览器，playwright 不负责安装｜"
                    f"清空 browser_channel，或先装好 {self.channel}"
                ) from e
            await self._install_kernel()
            try:
                return await self._pw.chromium.launch(**kwargs)
            except Exception as e2:
                raise BrowserError(
                    f"内核下载完仍启动失败｜{_brief(e2)}｜检查磁盘空间与依赖库"
                    f"（python -m playwright install-deps chromium）"
                ) from e2

    async def _install_kernel(self) -> None:
        """跑 playwright install chromium，逐行读输出，失败抛 BrowserUnavailable。"""
        cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
        self.kernel_state = "installing"
        self._log(f"未找到浏览器内核，开始下载：{' '.join(cmd)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            self.kernel_state = "failed"
            self.kernel_error = _brief(e)
            raise BrowserUnavailable(
                f"内核下载起不来｜{_brief(e)}｜手工执行 {INSTALL_CMD}"
            ) from e

        tail: deque[str] = deque(maxlen=8)

        async def pump() -> None:
            try:
                async for raw in proc.stdout:            # type: ignore[union-attr]
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        tail.append(line)
                        self._log(f"install | {line}")
            except Exception:
                pass

        reader = asyncio.create_task(pump())
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.install_timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            self.kernel_state = "failed"
            self.kernel_error = "下载超时"
            raise BrowserUnavailable(
                f"内核下载超时（{int(self.install_timeout)} 秒）｜"
                f"网络慢或磁盘满｜手工执行 {INSTALL_CMD}，或调大 browser_timeout 后重试"
            )
        finally:
            if not reader.done():
                reader.cancel()

        if proc.returncode != 0:
            self.kernel_state = "failed"
            self.kernel_error = "；".join(list(tail)[-2:]) or f"退出码 {proc.returncode}"
            raise BrowserUnavailable(
                f"内核下载失败｜{self.kernel_error}｜手工执行 {INSTALL_CMD} 看完整输出"
            )
        self.kernel_state = "installed"
        self.kernel_error = ""
        self._log("浏览器内核已就绪")


    def _build_proxy(self) -> dict[str, str]:
        """把代理地址翻成 Chromium 认的写法。带密码的 SOCKS5 要过本地中继。"""
        url = self.proxy
        if "://" not in url:
            url = "socks5://" + url
        sp = urlsplit(url)
        scheme = (sp.scheme or "socks5").lower()
        has_auth = bool(sp.username)

        if scheme.startswith("socks") and has_auth:
            self._relay = Socks5Relay(url).start()
            self._log(f"已起本地中继 {self._relay.url} → 上游 {sp.hostname}:{sp.port}")
            return {"server": self._relay.url}

        server = f"{scheme}://{sp.hostname}:{sp.port or (1080 if scheme.startswith('socks') else 8080)}"
        conf = {"server": server}
        if has_auth:
            conf["username"] = sp.username or ""
            conf["password"] = sp.password or ""
        return conf

    async def close(self) -> None:
        await self._shutdown_pw()

    def _stop_relay(self) -> None:
        """收掉本地中继。

        必须跟浏览器一起收：每次 launch 都会 `_build_proxy()` 新建一个中继
        （新线程、新随机端口），旧的不停就漏一个线程和一个端口。回收重启会频繁
        走这条路，漏起来很快。
        """
        relay, self._relay = self._relay, None
        if relay is not None:
            try:
                relay.stop()
            except Exception:
                pass

    @staticmethod
    def _is_dead_browser(err: Exception) -> bool:
        """这个异常是不是「浏览器进程没了」这一类。

        进程崩掉时抛的是 Playwright 的 Error，不是我们自己的 BrowserError，
        文案里一定带 closed / crashed / disconnected。认出来才有机会原地重启，
        否则只能等下次重载插件。
        """
        text = str(err).lower()
        return any(k in text for k in ("browser has been closed",
                                       "target page, context or browser has been closed",
                                       "target crashed",
                                       "browser closed",
                                       "browser disconnected"))

    async def _shutdown_pw(self, timeout: float = 15.0) -> None:
        """关掉浏览器、驱动、中继。三条路都要收干净，否则重载插件会攒进程。"""
        browser, pw = self._browser, self._pw
        self._browser = None
        self._pw = None
        if browser is not None:
            try:
                # close 在浏览器卡住时会一直挂着，插件重载就卡在这里
                await asyncio.wait_for(browser.close(), timeout=timeout)
            except Exception:
                pass
        if pw is not None:
            try:
                # stop 会让 driver 退出，它下面的浏览器子进程随之被杀
                await asyncio.wait_for(pw.stop(), timeout=5.0)
            except Exception:
                pass
        self._stop_relay()

    async def _maybe_recycle(self) -> None:
        """判断该不该把整个浏览器进程换掉。

        Chromium 释放页面、释放上下文，都不会把内存还给系统。只有进程退出才可以。
        所以这里按「用得多」和「放得久」两个条件主动退一次：
          - 连续取数到 max_searches 次（一轮巡检里监控项多时会触发）
          - 距上次取数超过 idle_timeout 秒（巡检间隔 30 分钟，每轮开头必触发）
        """
        if self._browser is None:
            return
        reason = ""
        if self.max_searches and self._searches >= self.max_searches:
            reason = f"已取数 {self._searches} 次"
        elif self.idle_timeout and self._last_used:
            idle = time.time() - self._last_used
            if idle >= self.idle_timeout:
                reason = f"空闲 {int(idle // 60)} 分钟"
        if not reason:
            return
        self._log(f"回收浏览器（{reason}），释放内存")
        self.recycled += 1
        await self._shutdown_pw()
        self._searches = 0

    # -------------------------------------------------------------------- 取数
    async def search(self, keyword: str, sort: str = "price", page: int = 1) -> dict:
        """搜一个关键词，返回解密后的整份响应（dict）。

        返回的是原始结构，交给插件自己的 `_find_goods_list` 去认商品 ——
        这样哪天拼多多把商品又换个地方包一层，只改解析层就够了。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            raise BrowserError("关键词为空")

        url = (f"{SEARCH_URL}?search_key={keyword}"
               f"&sort_type={sort or 'price'}&page={int(page) or 1}")

        async with self._lock:          # 一个浏览器实例串行用，别并发抢页面
            # 回收和启动都放在锁里：并发调用时两边都判 browser is None 会起两个进程
            try:
                await self._maybe_recycle()
                await self.start()
            except BrowserError:
                raise
            try:
                return await self._search_locked(url, keyword)
            except BrowserError:
                raise
            except Exception as e:
                if not self._is_dead_browser(e):
                    raise
                # 进程在半路崩了：重启一次再取，别把这一轮判成「取不到价」
                self._log(f"浏览器进程异常退出（{e}），重启后重试一次")
                await self._shutdown_pw()
                try:
                    await self.start()
                except Exception as e2:
                    raise BrowserError(f"浏览器重启失败：{e2}") from e2
                return await self._search_locked(url, keyword)
            finally:
                # 失败也算一次：崩掉的那次一样占过内存
                self._searches += 1
                self._last_used = time.time()

    async def _warm_context(self, ctx) -> None:
        """用一张临时页访问首页，让服务端下发设备 cookie。

        单开一页而不是复用搜索页：hook 把 JSON.parse 的载荷存在 window 上，
        首页的数据混进来，后面轮询可能拿到首页的推荐列表当搜索结果。
        失败不报错——预热只是加分项，缺了顶多这一次取数白跑。
        """
        page = None
        try:
            page = await ctx.new_page()
            await page.goto(HOME_URL, wait_until="domcontentloaded",
                            timeout=min(self.timeout, 20.0) * 1000)
            self._home_warmed = True
            self._log("已访问首页，设备 cookie 就绪")
        except Exception as e:
            self._log(f"首页预热失败（继续取数）：{e}")
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _search_locked(self, url: str, keyword: str) -> dict:
        ctx = None
        page = None
        try:
            ctx = await self._browser.new_context(
                user_agent=self.user_agent,
                viewport=VIEWPORT,
                device_scale_factor=3,
                is_mobile=True,
                has_touch=True,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            cookies = parse_cookie_header(self.cookie)
            if cookies:
                try:
                    await ctx.add_cookies(cookies)
                except Exception as e:
                    self._log(f"注入 cookie 失败（继续跑）：{e}")

            # 本进程第一次取数：先用同一 context 访问首页，拿 api_uid 等设备 cookie。
            # 少了这一步，直接打开搜索页常常只得到一个不渲染的空壳（标题「拼多多」、
            # 正文空白），hook 自然截不到数据。放进 context 里做，cookie 才对搜索页生效。
            if self.warm_home and not self._home_warmed:
                await self._warm_context(ctx)

            page = await ctx.new_page()
            await page.add_init_script(STEALTH_JS)
            await page.add_init_script(HOOK_JS)

            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=self.timeout * 1000)
            except Exception as e:
                raise BrowserError(f"打开搜索页失败：{e}") from e

            payload = await self._wait_payload(page, keyword)
            return payload
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass

    async def _wait_payload(self, page, keyword: str) -> dict:
        """等 hook 截到明文。轮询而不是死等固定秒数，慢的时候不误杀、快的时候不浪费。"""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.timeout
        last_n = 0
        while loop.time() < deadline:
            try:
                n = await page.evaluate(POLL_JS)
            except Exception:
                n = 0
            if n:
                raw = await page.evaluate(DUMP_JS)
                if raw:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError as e:
                        raise BrowserError(f"截到的数据不是合法 JSON：{e}") from e
                    self._log(f"截到 {n} 条商品，JSON {len(raw)} 字节")
                    return data
            if n != last_n:
                last_n = n
            await asyncio.sleep(self.poll_interval)

        diag = await self._diagnose(page)
        raise BrowserError(self._explain(diag, keyword))

    async def _diagnose(self, page) -> dict:
        try:
            diag = await page.evaluate(DIAG_JS)
        except Exception:
            diag = {}
        self.last_diag = diag
        return diag

    @staticmethod
    def _explain(diag: dict, keyword: str) -> str:
        """把失败原因压成「现象｜原因｜处理」三段。三种情况长得像，处理办法完全不同。"""
        text = (diag.get("text") or "").strip()
        url = (diag.get("url") or "")
        title = (diag.get("title") or "")
        hits = diag.get("hits") or 0

        if "login" in url.lower() or title.strip() == "登录":
            return ("跳登录页｜cookie 缺失或已失效｜"
                    "重新复制整条 Cookie，须含 PDDAccessToken")
        if "系统繁忙" in text or "稍后再试" in text:
            if hits == 0:
                return ("页面报「系统繁忙」且未截到任何数据包｜出口 IP 被限流，或代理未生效｜"
                        "先用 /盯价 debug 看「代理」行；确认生效仍如此则换出口")
            return "页面报「系统繁忙」，但已截到数据包｜偶发限流｜重试一次"
        if diag.get("prices"):
            return (f"页面渲染出 {diag['prices']} 个价格，hook 未截到明文｜"
                    f"数据不再经 JSON.parse｜上报此条等待适配；临时可改 fetch_mode=http")
        if not text:
            return ("搜索页正文为空｜页面壳没渲染出来，多见于首次访问缺设备 cookie｜"
                    "重试一次；连续如此检查 cookie 与出口代理")
        return (f"未取到商品数据｜标题「{title}」，正文开头：{text[:80]}｜上报此条")

