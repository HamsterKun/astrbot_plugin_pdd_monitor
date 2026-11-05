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
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode, urlsplit

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
LOGIN_URL = "https://mobile.yangkeduo.com/login.html"

# 登录页的二维码选择器，从窄到宽。桌面版点开「扫码登录」后弹出的结构是
#   .qr-code-login-container > .qr-code-login-content > .qr-code-area
#                                          > .qr-code-wrapper > .qr-code-inner
# 真正的码在 `.qr-code-inner` 里（PDD 自己的扫码组件，canvas 或 img 都有可能）。
# 全都不中时退回整页截图，用来留档。
QR_SELECTORS = (
    ".qr-code-inner",
    ".qr-code-area",
    ".qr-code-inner canvas",
    ".qr-code-area canvas",
    ".qr-code-inner img",
    'img[src*="qrcode"]',
    'canvas[class*="qr"]',
    '[class*="qrcode"] img',
    'img[alt*="二维码"]',
    'img[src^="data:image"]',
)

# 「扫码登录」入口。桌面版是 React 现渲染的（body 一开始是空的），入口出现得晚，
# 所以按选择器轮询，超时前一直等。class 名最稳，文案只在 class 改版时兜底。
QR_ENTRY_SELS = (
    "div.qrcode-login",
    ".login-box .qrcode-login",
    "text=扫码登录",
    "text=扫一扫",
    "text=二维码登录",
)

# 点开入口后，这些元素出现就说明二维码弹层出来了
QR_READY_SELS = (".qr-code-inner", ".qr-code-area", ".qr-code-login-container")

# 登录页视口高度：至少 900（默认的 720 装不下那套「logo + 说明 + 二维码」），
# 页面更高就按页面高度撑开，最多 1200。
#
# 为什么不是「页面多高就多高」：有的页面用 100vh 布局，scrollHeight 恒等于视口高，
# 那种页面在 720 下就是把二维码压出屏幕，撑到 900 才装得下。
LOGIN_MIN_VIEWPORT_H = 900
LOGIN_MAX_VIEWPORT_H = 1200

# 页面实际有多高（含滚动部分）。
PAGE_HEIGHT_JS = r"""
() => {
  const de = document.documentElement;
  const b = document.body;
  return Math.max(
    de ? de.scrollHeight : 0,
    b ? b.scrollHeight : 0,
    b ? b.offsetHeight : 0
  );
}
"""

# 把二维码摆进画面。
#
# 为什么需要：登录页比视口高时，二维码常常压在下沿之外，而截图截的是视口，
# 于是推给面板的画面里只有半张码——手机根本扫不了。
#
# 三层兜底：
#   1) 已知的几个类名，取「面积最小」的那个——弹层容器往往包着整页，滚它等于没滚；
#   2) 类名全没命中时，找页面上最大的正方形 canvas/img。二维码就是方的，
#      登录页上不会有第二个这么大的正方块；
#   3) 还找不到就返回 ok:false，调用方会记日志，别再不声不响。
QR_VIEW_JS = r"""
() => {
  const sels = ['.qr-code-inner', '.qr-code-area canvas', '.qr-code-area img',
                '.qr-code-area', '.qr-code-wrapper', '.qr-code-login-container'];
  const cands = [];
  for (const s of sels) {
    for (const el of document.querySelectorAll(s)) {
      const r = el.getBoundingClientRect();
      if (!r || r.width < 40 || r.height < 40) continue;
      cands.push({ sel: s, r: r });
    }
  }
  if (!cands.length) {
    for (const el of document.querySelectorAll('canvas, img')) {
      const r = el.getBoundingClientRect();
      if (!r || r.width < 80 || r.height < 80) continue;
      if (Math.abs(r.width - r.height) > r.width * 0.2) continue;
      cands.push({ sel: '正方块兜底', r: r, score: -r.width });
    }
  }
  if (!cands.length) return { ok: false };
  cands.sort((a, b) => a.r.width * a.r.height - b.r.width * b.r.height);
  const c = cands[0];
  const pageY = c.r.top + window.scrollY;
  const top = c.r.height > window.innerHeight - 24
    ? pageY - 12
    : pageY - (window.innerHeight - c.r.height) / 2;
  window.scrollTo(0, Math.max(0, Math.round(top)));
  return {
    ok: true, sel: c.sel,
    h: Math.round(c.r.height),
    vh: window.innerHeight,
    y: Math.round(window.scrollY),
  };
}
"""

# 扫码之后，拼多多有时会插一屏「安全验证」（让你从几个用过的收货地址/姓名里挑一个）。
# 这一步只能人来判断，所以把选项抛给用户，等一个序号回来再点。
VERIFY_HINTS = ("安全验证", "收货人姓名", "收货地址", "请选择您使用过的")

# 安全验证提交后，等多久还没拿到登录态就判失败。
# 卡住不动比报错更让人难受——用户会以为「没下文了」，只能干等 180 秒。
POST_VERIFY_GRACE = 90.0

# 把验证选项的文案读出来。优先原生 radio，读不到再按常见容器兜底；
# 只取「宽一点、窄高度、文案不长」的块，避免把标题和按钮也当成选项。
VERIFY_OPTIONS_JS = r"""
() => {
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim();
  const rows = [];
  document.querySelectorAll('input[type="radio"]').forEach((el) => {
    const row = el.closest("label") || el.parentElement || el;
    rows.push(clean(row.innerText).slice(0, 30));
  });
  if (rows.length) return rows;
  const seen = new Set();
  const out = [];
  document.querySelectorAll('[class*="option"], [class*="item"], li, [role="radio"]')
    .forEach((e) => {
      const r = e.getBoundingClientRect();
      const t = clean(e.innerText).slice(0, 30);
      if (r.width > 80 && r.height > 18 && r.height < 140 && t && !seen.has(t)) {
        seen.add(t);
        out.push(t);
      }
    });
  return out.slice(0, 8);
}
"""

# 点第 n 个选项，返回用哪种方式点中的
VERIFY_CLICK_JS = r"""
(n) => {
  const clean = (t) => (t || "").replace(/\s+/g, " ").trim();
  const ok = (e) => {
    const r = e.getBoundingClientRect();
    const t = clean(e.innerText);
    return r.width > 80 && r.height > 18 && r.height < 140 && t && t.length <= 30;
  };
  const radios = [...document.querySelectorAll('input[type="radio"]')];
  if (radios[n]) {
    const row = radios[n].closest("label") || radios[n].parentElement || radios[n];
    try { row.click(); } catch (e) {}
    try { radios[n].click(); } catch (e) {}
    return "radio";
  }
  const seen = new Set();
  const picked = [];
  document.querySelectorAll('[class*="option"], [class*="item"], li, [role="radio"]')
    .forEach((e) => {
      if (!ok(e)) return;
      const t = clean(e.innerText).slice(0, 30);
      if (seen.has(t)) return;
      seen.add(t);
      picked.push(e);
    });
  if (picked[n]) { picked[n].click(); return "row"; }
  return "";
}
"""

# 移动端环境三要素：iPhone UA + 440×956 视口 + is_mobile。少一个就会被判
# 「系统繁忙」—— 用桌面 UA 去访问搜索页必定失败。
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 "
             "Mobile/15E148 Safari/604.1")

VIEWPORT = {"width": 440, "height": 956}

# 扫码登录只在「桌面端」渲染。登录页那段代码里写着：
#   isPCPlatform && qrcodeLoginConfig.show && <div class="qrcode-login">扫码登录</div>
# 拿 iPhone UA 进去，「扫码登录」这个入口根本不会出现——之前截到「手机登录」
# 那一屏就是这个原因，不是选择器不准。
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# 只求「像桌面」，不求大：视口越大，渲染进程要的位图越大。900×720 已经够摆下
# 登录卡片和二维码弹层，配 2 倍缩放截出来的码也清楚。
# 登录页的桌面视口。高度给到 900：720 装不下「logo + 说明 + 二维码」那一屏，
# 二维码会被压出下沿，截出来的就是半张码。宽度不能降——「扫码登录」入口只
# 在桌面布局下渲染。
DESKTOP_VIEWPORT = {"width": 900, "height": 900}

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

# 桌面形态的登录页要配套的指纹：上面那份把 platform 写成 iPhone，
# 配 Windows UA 就是自相矛盾，而且「扫码登录」入口本来就按桌面端判断。
STEALTH_DESKTOP_JS = r"""
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
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


async def _wait_qr_ready(page, timeout: float = 8.0) -> bool:
    """等二维码本身出现（不是弹层外壳，是里面那个码）。

    放模块级而不是方法：这个方法会被测试用假页面调用，不需要 fetcher 实例，
    挂在 self 上反而多一层「self 有没有这个属性」的隐患。
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(0.2, timeout)
    while True:
        for sel in QR_READY_SELS:
            try:
                if await page.locator(sel).first.count():
                    return True
            except Exception:
                pass
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.5)


async def fit_viewport_to_page(page, log=None,
                               max_h: int = LOGIN_MAX_VIEWPORT_H,
                               min_h: int = LOGIN_MIN_VIEWPORT_H) -> int:
    """把视口撑到能装下整页（不小于 min_h、不大于 max_h），返回新高度，失败返回 0。

    比「滚到二维码」更彻底：滚动之后页面顶上那些内容就看不见了，而且一旦页面
    重新渲染（切 tab、弹层加载完重排）滚动位置会被重置，用户下次取帧又是半张码。
    撑高视口没有这些问题——整页都在画面里。

    代价是这一帧的位图变大，只在登录会话期间，可以接受。
    """
    try:
        h = await page.evaluate(PAGE_HEIGHT_JS)
    except Exception as e:
        if log is not None:
            log(f"量页面高度失败（继续）：{_brief(e)}")
        return 0
    try:
        h = int(h or 0)
    except (TypeError, ValueError):
        return 0
    if h <= 0:
        return 0
    # 上下各留一点，别让最后一行贴着边；再跟下限取大
    target = max(int(min_h), min(int(max_h), h + 8))
    try:
        await page.set_viewport_size(
            {"width": int(DESKTOP_VIEWPORT["width"]), "height": target})
    except Exception as e:
        if log is not None:
            log(f"调整视口高度失败（继续）：{_brief(e)}")
        return 0
    if log is not None:
        log(f"登录页高 {h}，视口高度调到 {target}")
    return target


async def ensure_qr_visible(page, log=None) -> bool:
    """把二维码滚进画面。

    登录页比视口高（900×720 装不下整页），二维码常常压在下沿之外，截出来的
    画面只有半张码——手机扫不了。这里按元素的真实位置算滚动量，比写死像素靠谱：
    弹层高度、有没有顶部广告都说不准。

    和 `_wait_qr_ready` 一样放模块级：会被假页面调用，不需要 fetcher 实例。
    """
    try:
        res = await page.evaluate(QR_VIEW_JS)
    except Exception as e:
        if log is not None:
            log(f"滚动到二维码失败（继续）：{_brief(e)}")
        return False
    if not isinstance(res, dict) or not res.get("ok"):
        return False
    if log is not None:
        log(f"已把二维码滚到画面里（码高 {res.get('h')}，视口 {res.get('vh')}）")
    return True


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
        # 上一轮有页面/上下文没关干净（关闭超时、进程卡住）。这种进程不能再信：
        # 关不掉的渲染进程会把内存一直占着，下一轮直接换新的。
        self._dirty = False
        self._release_task = None  # 收尾任务的引用，取消场景下靠它跑完

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
        self._dirty = False
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
        """外部收尾入口：插件重载、异常兜底都走这里。

        用 `_release_now` 而不是直接 `_shutdown_pw`：调用点往往在取消/异常路径上，
        直接 await 很可能半路被打断，进程就留下了。
        """
        await self._release_now()

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

    async def _close_quietly(self, obj, timeout: float = 8.0) -> bool:
        """关页面、关上下文，都带超时。

        `close()` 在页面还在导航、渲染进程卡死时会一直挂着：插件这头表现为
        「巡检停在半路」，机器那头表现为「一个早就用完的页面关十分钟还在」。
        超时就当它坏了，给浏览器记个脏标记，下一轮整个换掉——关不掉的进程
        只能靠退出收尸。
        """
        if obj is None:
            return True
        try:
            await asyncio.wait_for(obj.close(), timeout=timeout)
            return True
        except asyncio.CancelledError:
            # 取消也要留下痕迹：这个页面大概率没关成
            self._dirty = True
            raise
        except BaseException as e:      # noqa: BLE001 - 收尾不能抛
            self._dirty = True
            self._log(f"页面/上下文 {int(timeout)} 秒没关掉，下一轮换新进程：{_brief(e)}")
            return False

    async def _shutdown_pw(self, timeout: float = 15.0) -> None:
        """关掉浏览器、驱动、中继。三条路都要收干净，否则重载插件会攒进程。

        每一步都吞掉 BaseException：这里多半是在异常路径上被调用的，收尾途中
        再抛一次（比如协程已被取消），句柄就丢了，进程再没人管。
        """
        browser, pw = self._browser, self._pw
        self._browser = None
        self._pw = None
        if browser is not None:
            try:
                # close 在浏览器卡住时会一直挂着，插件重载就卡在这里
                await asyncio.wait_for(browser.close(), timeout=timeout)
            except BaseException:       # noqa: BLE001
                pass
        if pw is not None:
            try:
                # stop 会让 driver 退出，它下面的浏览器子进程随之被杀
                await asyncio.wait_for(pw.stop(), timeout=5.0)
            except BaseException:       # noqa: BLE001
                pass
        self._stop_relay()
        self._home_warmed = False
        self._dirty = False

    async def _release_now(self) -> None:
        """立刻退掉浏览器，被取消也要退。

        取消（插件重载、外层 wait_for 超时、任务被 abort）时，`finally` 里的
        `await` 会立刻再抛一次 CancelledError，收尾动作走不完，Chromium 就留在
        系统里了——这就解释了「明明报错了，内存却没降」。所以另起一个任务去收，
        它不挂在当前协程上，取消也带不走。
        """
        if self._browser is None and self._pw is None and self._relay is None:
            return
        task = asyncio.ensure_future(self._shutdown_pw())
        self._release_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 让它在后台跑完；引用留在 self._release_task 上，不会被回收
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            raise
        except BaseException:           # noqa: BLE001
            pass

    async def _maybe_recycle(self) -> None:
        """判断该不该把整个浏览器进程换掉。

        Chromium 释放页面、释放上下文，都不会把内存还给系统。只有进程退出才可以。
        所以这里按「用得多」和「放得久」两个条件主动退一次：
          - 连续取数到 max_searches 次（一轮巡检里监控项多时会触发）
          - 距上次取数超过 idle_timeout 秒（巡检间隔 30 分钟，每轮开头必触发）
        另外，上一轮有页面没关掉也直接换掉：那说明渲染进程已经卡住，留着只会涨。
        """
        if self._dirty and self._browser is not None:
            self._log("上一轮有页面没关干净，换一个新进程")
            self.recycled += 1
            await self._shutdown_pw()
            self._searches = 0
            return
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
    @staticmethod
    def _search_query(keyword: str, sort: str = "", page: int = 1) -> str:
        """拼查询串。关键词一律编码，空格用 %20。

        连续空白压成一个：中间的空格是「同时包含」的分词边界，要保留但不必留多个。
        """
        params = {"search_key": re.sub(r"\s+", " ", keyword or "").strip()}
        if sort:
            params["sort_type"] = str(sort)
        params["page"] = str(int(page) or 1)
        return urlencode(params, quote_via=quote)

    async def search(self, keyword: str, sort: str = "price", page: int = 1) -> dict:
        """搜一个关键词，返回解密后的整份响应（dict）。

        返回的是原始结构，交给插件自己的 `_find_goods_list` 去认商品 ——
        这样哪天拼多多把商品又换个地方包一层，只改解析层就够了。

        sort 传空串表示综合排序：不带 sort_type。综合排序是拼多多自己的推荐
        结果，相关性强，插件拿它当价格基准（见 main 里的 use_price_baseline）。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            raise BrowserError("关键词为空")

        # 关键词必须编码后再拼进 URL。带空格的词（「RTX5070ti ultra」）直接拼进去，
        # 空格会被当成 URL 的结束，服务端只收到前半截，页面是个空壳。
        # 空格要编成 %20 而不是 +：+ 代表空格只是表单编码的约定，前端按字面量解。
        url = f"{SEARCH_URL}?{self._search_query(keyword, sort, page)}"

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

    # ---------------------------------------------------------------- 扫码登录
    async def _new_context(self, mobile: bool = True):
        """新建上下文：UA、视口、语言、时区都得像一台真设备。

        mobile=False 给扫码登录用：桌面 UA 才会渲染「扫码登录」入口，这也是
        整条登录路唯一能拿到二维码的姿势。
        """
        return await self._browser.new_context(
            user_agent=self.user_agent if mobile else DESKTOP_UA,
            viewport=VIEWPORT if mobile else DESKTOP_VIEWPORT,
            device_scale_factor=3 if mobile else 2,   # 桌面 2 倍，二维码够清楚又不臃肿
            is_mobile=mobile,
            has_touch=mobile,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

    async def _inject_cookies(self, ctx) -> None:
        cookies = parse_cookie_header(self.cookie)
        if not cookies:
            return
        try:
            await ctx.add_cookies(cookies)
        except Exception as e:
            self._log(f"注入 cookie 失败（继续跑）：{e}")

    async def _block_heavy(self, ctx) -> None:
        """搜索页不下载图片、字体、音视频。

        取数靠的是页面里那段 JSON，缩略图纯粹是负担：一屏几十张，每张解码后
        都是几 MB 的位图，Chromium 关页面也不还，全靠进程退出回收。拦掉之后
        单个渲染进程的内存能降一大截，顺带省流量、跑得更快。

        只给取数用；登录页要显示二维码，不能拦。
        """
        heavy = {"image", "media", "font"}

        async def route(r):
            try:
                if r.request.resource_type in heavy:
                    await r.abort()
                else:
                    await r.continue_()
            except Exception:
                pass

        try:
            await ctx.route("**/*", route)
        except Exception as e:
            self._log(f"资源拦截没挂上（继续取数）：{e}")

    async def login_qr(self, on_qr, timeout: float = 180.0,
                       refresh_after: float = 95.0, on_miss=None,
                       ask=None) -> dict:
        """开登录页，把二维码交给 on_qr 发出去，再等用户扫码。

        on_qr(png: bytes, how: str) 每次拿到一张新二维码时调用一次。二维码会过期，
        所以隔一段时间重截一张再发，比让用户对着失效的码干等强。

        on_miss(html: str) 只在「选择器一个都没命中、只能退回整页截图」时调用一次，
        用来把页面源码留档——否则没法把选择器对准，只能靠猜。

        ask(png, options) -> int 用于扫码后的「安全验证」：那一步要人判断（比如从
        几个用过的收货人姓名里挑一个），所以把截图和选项交给会话，等一个序号回来
        再点。不传就当作没人能答，遇到验证直接报错。

        返回 {"cookie": "k=v; ...", "count": n, "fields": [...]}。
        超时抛 BrowserError，由调用方决定怎么提示。

        这里**不注入现有账号 cookie**：登录流程要能换号，带着旧凭证进去会直接
        跳过登录页（或者显示已登录）。设备 cookie 靠预热首页拿，不影响换号。

        退出时一定把浏览器关掉（见 finally）：登录是一次性的事，留着没有意义。
        登录页本身很重——一直在跑轮询、埋点、动态图，一个这样的页面就能占掉
        几百兆，而且 Chromium 关页面不还内存。之前这条路径失败后浏览器就这么
        挂在内存里，连试几次就把机器吃满了。
        """
        async with self._lock:
            try:
                await self._maybe_recycle()
                await self.start()
                return await self._login_locked(on_qr, timeout, refresh_after,
                                                on_miss, ask)
            finally:
                try:
                    await self._release_now()
                except asyncio.CancelledError:
                    raise
                except BaseException as e:      # noqa: BLE001 - 收尾不能盖掉原错误
                    self._log(f"登录结束关浏览器失败：{e}")

    async def _open_login_page(self, page) -> None:
        """打开登录页。

        只等「导航提交」不等 DOM：桌面版这个页面正文是空的，全靠 JS 现渲染，
        等 load 或 domcontentloaded 都可能卡在埋点上。页面拿到手就行，
        正文什么时候出来交给 `_switch_to_qr_tab` 去轮询。
        """
        try:
            await page.goto(LOGIN_URL, wait_until="commit",
                            timeout=self.timeout * 1000)
        except Exception as e:
            raise BrowserError(
                f"打开登录页失败｜{int(self.timeout)} 秒内导航都没提交，多为出口太慢或不通｜"
                f"{_brief(e)}"
            ) from e
        try:
            await page.wait_for_load_state("domcontentloaded",
                                           timeout=min(self.timeout, 20.0) * 1000)
        except Exception as e:
            self._log(f"登录页正文没在预期内就绪（继续）：{_brief(e)}")

    async def _switch_to_qr_tab(self, page, timeout: float = 25.0,
                                popup_timeout: float = 8.0) -> bool:
        """点开「扫码登录」，等二维码弹层出来。

        两个坑都在这里：
          1) 入口是 React 现渲染的。桌面版 body 一开始完全空，脚本跑完才有内容，
             所以不能「查一次没有就放弃」，得等到期限。
          2) 点开之后二维码还要再请求一次才画出来（`.qr-code-inner` 里是 PDD 自己的
             扫码组件），点完立刻截图通常是一块「二维码加载中」的遮罩。
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(1.0, timeout)
        while loop.time() < deadline:
            for sel in QR_ENTRY_SELS:
                try:
                    loc = page.locator(sel).first
                    if not await loc.count():
                        continue
                    await loc.click(timeout=2000)
                    self._log(f"已点开扫码登录（{sel}）")
                    if await _wait_qr_ready(page, popup_timeout):
                        await ensure_qr_visible(page, self._log)
                        return True
                except Exception:
                    continue
            await asyncio.sleep(1.0)
        self._log("没等到「扫码登录」入口")
        return False

    async def _ensure_qr_visible(self, page) -> bool:
        """把二维码滚进画面（WebUI 那条路要按 fetcher 调用，这里留个薄壳）。"""
        return await ensure_qr_visible(page, self._log)

    async def _login_locked(self, on_qr, timeout: float, refresh_after: float,
                            on_miss=None, ask=None) -> dict:
        ctx = None
        page = None
        sent_at = 0.0
        shots = 0
        verifies = 0
        try:
            # 桌面形态：只有桌面 UA 的登录页才有「扫码登录」这个入口
            ctx = await self._new_context(mobile=False)
            # 设备 cookie（api_uid 等）还是要的：没有它登录页本身也可能不渲染
            if self.warm_home and not self._home_warmed:
                await self._warm_context(ctx)

            page = await ctx.new_page()
            await page.add_init_script(STEALTH_DESKTOP_JS)
            await self._open_login_page(page)
            # 等入口渲染出来再点，点开再等二维码画出来。给的时间比一次普通导航宽，
            # 因为 JS 包要走代理下载；真等不到就立刻报，别让用户干等 180 秒。
            switched = await self._switch_to_qr_tab(
                page, timeout=min(max(15.0, self.timeout), 40.0), popup_timeout=15.0)
            if not switched:
                if on_miss is not None:
                    try:
                        await on_miss(await page.content())
                    except Exception as e:
                        self._log(f"存登录页快照失败：{e}")
                raise BrowserError(
                    "没找到「扫码登录」入口｜登录页没渲染出这个按钮，多为 JS 资源没加载完"
                    "（出口慢或被拦）｜重发 /盯价 login，仍不行把 browser_timeout 调大"
                )

            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            verify_at = 0.0
            while loop.time() < deadline:
                header, fields = await self._logged_cookie(ctx)
                if header:
                    self._log(f"扫码登录成功，拿到 {len(fields)} 个字段")
                    return {"cookie": header, "count": len(fields), "fields": fields}

                # 扫码之后常插一屏「安全验证」。这一步要人判断，题目交给会话，
                # 等一个序号回来再点；点完接着轮询 cookie。
                if await self._is_verify_panel(page):
                    if verifies >= 3:
                        raise BrowserError(
                            "验证没通过｜连续三轮都没被接受，或被风控按住了｜"
                            "重发 /盯价 login，不行就改用手工复制 Cookie"
                        )
                    verifies += 1
                    before = loop.time()
                    await self._solve_verify(page, ask)
                    # 等人回话花掉的时间不算进「扫码超时」，否则刚点完就判超时
                    deadline += max(0.0, loop.time() - before) + 2.0
                    verify_at = loop.time()
                    continue

                # 验证提交后迟迟拿不到登录态：九成是选错了，或者后面还藏着一道。
                # 不能就这么干等到 180 秒，用户会觉得「没下文」。
                if verify_at and (loop.time() - verify_at) > POST_VERIFY_GRACE:
                    excerpt = await self._page_excerpt(page)
                    raise BrowserError(
                        f"验证已提交，但没拿到登录态｜可能选错了，或后面还有一道验证｜"
                        f"页面现在是「{excerpt}」｜重发 /盯价 login"
                    )

                # 已经进过验证环节就别再发「二维码刷新」了：码那一步过去了
                if not verifies and (shots == 0 or (loop.time() - sent_at) >= refresh_after):
                    png, how = await self._grab_qr(page)
                    if png and how == "整页" and shots == 0:
                        # 整页说明没抠到二维码。再等一拍看是不是还在渲染
                        if await _wait_qr_ready(page, 3.0):
                            png2, how2 = await self._grab_qr(page)
                            if png2 and how2 != "整页":
                                png, how = png2, how2
                    if png:
                        # 退回整页截图说明选择器没命中，留一份源码好把选择器对准
                        if how == "整页" and shots == 0 and on_miss is not None:
                            try:
                                await on_miss(await page.content())
                            except Exception as e:
                                self._log(f"存登录页快照失败：{e}")
                        await on_qr(png, how)
                        shots += 1
                        sent_at = loop.time()
                        self._log(f"二维码已截（{how}），等扫码")
                await asyncio.sleep(2.0)

            raise BrowserError(
                f"扫码登录超时｜{int(timeout)} 秒内没扫，或二维码已过期｜"
                "重发 /盯价 login 换个新码"
            )
        finally:
            await self._close_quietly(page)
            await self._close_quietly(ctx)
            self._last_used = time.time()

    @staticmethod
    async def _is_verify_panel(page) -> bool:
        """当前页面是不是那屏「安全验证」。

        扫完码、风控觉得这台设备眼生时会出现，题目一般是「选择您使用过的收货人
        姓名」。只能人来答，所以认出来之后要把题目抛给会话。
        """
        try:
            text = await page.evaluate(
                "() => (document.body && document.body.innerText) || ''")
        except Exception:
            return False
        return any(h in (text or "") for h in VERIFY_HINTS)

    @staticmethod
    async def _page_excerpt(page, limit: int = 70) -> str:
        """页面上一屏文字的前几十个字，用来在回执里说明「现在停在哪一页」。"""
        try:
            text = await page.evaluate(
                "() => (document.body && document.body.innerText) || ''")
        except Exception:
            return "读不到页面内容"
        clean = " ".join((text or "").split())
        if not clean:
            return "页面是空的"
        return clean[:limit] + ("…" if len(clean) > limit else "")

    async def _solve_verify(self, page, ask) -> None:
        """把验证题目发出去，等用户选一个序号，点它、再点确认。"""
        if ask is None:
            raise BrowserError(
                "扫码后要做安全验证｜这步需要人来选，当前会话没法交互｜"
                "这次先改用方式二手工复制 Cookie"
            )

        options: list[str] = []
        try:
            for item in await page.evaluate(VERIFY_OPTIONS_JS) or []:
                text = str(item or "").strip()
                if text and text not in options:
                    options.append(text[:30])
        except Exception:
            pass

        try:
            png = await page.screenshot(type="png")
        except Exception:
            png = b""

        idx = await ask(png, options[:8])
        if idx is None or int(idx) < 0:
            raise BrowserError("安全验证已取消｜重发 /盯价 login 再来一次")

        try:
            how = await page.evaluate(VERIFY_CLICK_JS, int(idx))
        except Exception as e:
            raise BrowserError(f"点不动验证选项｜{_brief(e)}") from e
        if not how:
            raise BrowserError(
                "点不动验证选项｜页面结构和预期不同｜这次先改用方式二手工复制 Cookie"
            )
        self._log(f"已选中第 {int(idx) + 1} 项（{how}），点确认")
        await asyncio.sleep(0.4)

        clicked = False
        for label in ("确认", "确定", "提交", "下一步"):
            try:
                loc = page.get_by_text(label, exact=True).first
                if await loc.count():
                    await loc.click(timeout=3000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            self._log("没找到「确认」按钮，按页面当前状态继续")
        await asyncio.sleep(2.0)

    @staticmethod
    async def _logged_cookie(ctx) -> tuple[str, list[str]]:
        """从上下文里取登录后的 cookie。有 PDDAccessToken 才算登录成功。"""
        try:
            items = await ctx.cookies()
        except Exception:
            return "", []
        pairs = {c["name"]: c.get("value", "") for c in items or [] if c.get("name")}
        token = pairs.get("PDDAccessToken", "")
        if not token:
            return "", []
        fields = list(pairs)
        header = "; ".join(f"{k}={v}" for k, v in pairs.items())
        return header, fields

    @staticmethod
    async def _grab_qr(page) -> tuple[bytes, str]:
        """截二维码，返回 (png, 用的哪种方式)。

        点开弹层后二维码还要再请求一次才画出来，中间是「二维码加载中」的遮罩。
        所以先专门等 `.qr-code-inner`（码本身），等不到才往下退；退到外壳
        `.qr-code-area` 时还要确认没有遮罩，免得把「加载中」当成码发出去。
        全都不中退回整页截图，二维码在中间，用户照样扫得到，比报错强。
        """
        try:
            await page.locator(".qr-code-inner").first.wait_for(
                state="visible", timeout=10000)
        except Exception:
            pass
        for sel in QR_SELECTORS:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=2000)
                box = await loc.bounding_box()
                if not box or box.get("width", 0) < 60:
                    continue
                if not sel.endswith(".qr-code-inner"):
                    # 外壳里可能正盖着「加载中/已失效」的遮罩
                    mask = page.locator(".qr-code-area .qr-code-mask").first
                    if await mask.count():
                        continue
                png = await loc.screenshot(type="png")
                if png and len(png) > 800:
                    return png, sel
            except Exception:
                continue
        try:
            return await page.screenshot(type="png"), "整页"
        except Exception as e:
            raise BrowserError(f"截二维码失败｜页面可能已被关闭｜{_brief(e)}") from e

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
                await self._close_quietly(page)

    async def _search_locked(self, url: str, keyword: str) -> dict:
        ctx = None
        page = None
        try:
            ctx = await self._new_context()
            await self._inject_cookies(ctx)
            await self._block_heavy(ctx)

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
            await self._close_quietly(page)
            await self._close_quietly(ctx)

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

