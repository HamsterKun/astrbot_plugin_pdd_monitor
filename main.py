# -*- coding: utf-8 -*-
"""astrbot_plugin_pdd_monitor

拼多多关键词价格监控插件（H5 搜索页版）。

/盯价 add 9700X —— 以 "9700X" 为关键词抓 mobile.yangkeduo.com 搜索页，
按价格排序后取列表里最便宜那件商品的「页面展示价」，作为该关键词的价格。
后台定时巡检，跌破历史最低价时主动推送到已绑定的会话（群）；
群里 @机器人 说「今日行情」，汇报所有关键词的今日价格与涨跌。

取价走网页搜索页，只需要一条登录态的 cookie；不依赖开放平台资质。
命中原生「系统繁忙 / 滑块验证」时按 1h→2h→4h→8h 指数退避，避免越撞越死。

为了降低被风控识别的概率，请求会尽量长得像一台真实手机：
先开首页拿设备 cookie，再带 Referer 去搜索；服务端轮换的设备/风控 cookie
会被收进会话并在下次请求带上；整轮的请求摊平到整个巡检周期里。
装了 curl_cffi 时连 TLS 与 HTTP/2 指纹也一并模拟（transport=auto 自动启用）。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:  # astrbot 不同版本对消息组件的导出路径保持一致，这里仍做一层保护
    import astrbot.api.message_components as Comp
except Exception:  # pragma: no cover
    Comp = None  # type: ignore

PLUGIN_NAME = "astrbot_plugin_pdd_monitor"
DATA_FILE = "pdd_price.json"
# 取价口径或数据源变化时递增，读到旧版本的数据文件会直接重置
# v3：取价从开放接口换回 H5 搜索页，价格口径从「券后到手价」变成「页面展示价」
DATA_VERSION = 3

GOODS_URL = "https://mobile.yangkeduo.com/goods.html?goods_id={}"
SEARCH_URL = "https://mobile.yangkeduo.com/search_result.html"
HOME_URL = "https://mobile.yangkeduo.com/"

# 默认 UA：iOS Safari。版本号与 Sec-Fetch 家族、Safari 的能力要对得上，
# 否则「UA 说是 Safari 18，请求头却是 IE 时代的样子」本身就是破绽。
DEFAULT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1"
)

# 登录态 cookie：始终以配置里粘贴的为准，绝不让服务端下发或旧 jar 覆盖。
# 其余（api_uid、njrpl、rvds、dilx 等）是设备/风控 cookie，服务端会轮换，
# 必须允许它们随时间演进，否则每次请求都一模一样，反而不像浏览器。
LOGIN_COOKIE_KEYS = ("PDDAccessToken", "pdd_user_id", "pdd_user_uin")

# 判断「这个 UA 是不是 iPhone Safari」，用于决定要不要发 Safari 才有的头
SAFARI_UA_RE = re.compile(r"Version/[\d.]+ (?:Mobile/\w+ )?Safari", re.I)


def build_headers(ua: str, referer: Optional[str] = None) -> dict[str, str]:
    """拼一套自洽的浏览器请求头。

    刻意不设 Accept-Encoding：交给底层库按已装的解码器决定（httpx 有 brotli 就发 br）。
    手动写死 `gzip, deflate` 而 UA 声称是 Safari，等于自报「我不是浏览器」——
    Safari 一定会发 br。
    """
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def parse_cookie_pairs(raw: str) -> dict[str, str]:
    """把 Cookie 头拆成键值对，保留原始大小写（服务端按名字精确匹配）。"""
    pairs: dict[str, str] = {}
    for chunk in (raw or "").split(";"):
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if name:
            pairs[name] = value.strip()
    return pairs


def merge_cookie_pairs(base: dict[str, str], incoming: dict[str, str]) -> dict[str, str]:
    """用 base 的登录态字段压过 incoming，其余以 incoming 为准。"""
    merged = dict(incoming)
    for key in LOGIN_COOKIE_KEYS:
        if base.get(key):
            merged[key] = base[key]
    return merged


def format_cookie(pairs: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in pairs.items() if v)


def parse_set_cookie(values: list[str]) -> dict[str, str]:
    """从 Set-Cookie 里只取 name=value。

    Path/Domain/Expires 一律不管：本插件只碰 yangkeduo.com 的首页和搜索页，
    不存在跨域和过期问题，多解析一步只会多出一个出错的地方。
    """
    out: dict[str, str] = {}
    for raw in values or []:
        chunk = str(raw).split(";", 1)[0].strip()
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


# 搜索页排序：price = 价格升序（最便宜的在第一页），_price = 价格降序
# 注意：本插件要取「最低价」，降序的第一页是最贵的一批，取出来的最低值会虚高，
# 所以默认用升序。真要倒序，把配置 search_sort 改成 _price。
SORT_ASC = "price"
SORT_DESC = "_price"

# 价格字段候选：整数分优先，字符串价（带 ¥ 或千分位）兜底
PRICE_KEYS = (
    "price",
    "groupPrice",
    "minGroupPrice",
    "minOnSaleGroupPrice",
    "priceInfo",
    "priceStr",
    "priceText",
)


# --------------------------------------------------------------------------- #
# 标题匹配：把关键词和商品标题都分词，取词集交集大小作为「重合度」
# --------------------------------------------------------------------------- #
try:  # jieba 是纯 Python 包，写在 requirements.txt 里；缺失时退化，不阻塞插件
    import jieba

    HAS_JIEBA = True
except Exception:  # pragma: no cover
    jieba = None  # type: ignore
    HAS_JIEBA = False

# 型号/英文/数字串：9700X、RTX4060、K845、iPhone15。jieba 会把它们切碎
# （9700X → 9700 + X），所以另外用正则整串补进词集。
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_.+/]{1,}")
# 词里至少要有一个「有意义」的字符，用来丢掉纯标点
WORD_RE = re.compile(r"[0-9a-z\u4e00-\u9fff]")
# 这些词出现在任何标题里都不代表匹配，参与打分只会添噪声
STOP_TOKENS = frozenset(
    "的 了 和 与 及 或 是 在 有 个 只 件 套 款 型 版 a an the for with and of to in on".split()
)

_jieba_ready = False


def _init_jieba() -> None:
    """第一次分词时再加载词典，别拖慢 AstrBot 启动。"""
    global _jieba_ready
    if _jieba_ready or not HAS_JIEBA:
        return
    _jieba_ready = True
    try:
        jieba.setLogLevel(60)  # 关掉 "Building prefix dict..." 之类的初始化日志
    except Exception:
        pass


def _keep_token(token: str) -> bool:
    token = (token or "").strip().lower()
    if not token or token in STOP_TOKENS:
        return False
    return bool(WORD_RE.search(token))


def tokenize(text: str) -> set[str]:
    """把一段文字切成词集，中文词由 jieba 切，型号/英文串另用正则整串补入。

    两侧（关键词、商品标题）都走这一个函数，口径才一致。
    """
    text = (text or "").strip().lower()
    if not text:
        return set()
    _init_jieba()

    tokens: set[str] = set()
    if HAS_JIEBA:
        try:
            for word in jieba.lcut(text):
                if _keep_token(word):
                    tokens.add(word.strip().lower())
        except Exception:
            pass

    # 型号整串补充：即便 jieba 把 9700X 拆成了 9700 + X，整串也进词集，
    # 这样 9700X 的标题能同时命中 9700X/9700/x，而 9700K 只会命中 9700/k
    for match in MODEL_RE.finditer(text):
        token = match.group(0).strip("-_./+")
        if len(token) >= 3 and _keep_token(token):
            tokens.add(token)
    return tokens


def overlap_score(keyword_tokens: set[str], title_tokens: set[str]) -> tuple[float, set[str]]:
    """重合度 = 命中词数 / 关键词词数，落在 0~1，方便直接显示成百分比。"""
    if not keyword_tokens:
        return 0.0, set()
    hit = keyword_tokens & title_tokens
    return len(hit) / len(keyword_tokens), hit


def normalize_cookie(raw: str) -> str:
    """容忍浏览器里粘出来的几种形态，统一成一行 Cookie 头。

    支持：纯值 / 带 `Cookie:` 前缀 / 多行请求头 / 折行被拼回来 / 首尾多余引号分号。
    """
    text = raw.strip()
    if not text:
        return ""
    # 多行：挑出含等号最多的那一行，或把整体按行拼起来
    lines = [x.strip() for x in text.replace("\r", "").split("\n") if x.strip()]
    if len(lines) > 1:
        best = max(lines, key=lambda x: x.count("="))
        if best.count("=") >= 2:
            text = best
        else:
            text = " ".join(lines)
    # 去掉 `Cookie:` 前缀（中英文冒号都见过）
    text = re.sub(r"^\s*cookie\s*[:：]\s*", "", text, flags=re.I)
    # 去掉整体包裹的引号
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        text = text[1:-1]
    text = text.replace("\r", "").replace("\n", "")
    # 规范分隔符与多余空白
    text = re.sub(r"\s*;\s*", "; ", text).strip()
    text = re.sub(r"^(?:;\s*)+", "", text)
    text = re.sub(r"(?:\s*;)+$", "", text)
    return text.strip()


def is_goods_item(obj: Any) -> bool:
    """判断一个 dict 是不是商品条目。

    商品名是必需特征；id 或价格有一个就够——只认价格的话，
    哪天拼多多把价格字段改名，整页都会被判成「没找到商品列表」，
    报错就会误导成「这个关键词没结果」。
    """
    if not isinstance(obj, dict):
        return False
    has_name = any(k in obj for k in ("goodsName", "goods_name"))
    if not has_name:
        return False
    has_id = any(k in obj for k in ("goodsID", "goods_id", "goodsSign", "goods_sign"))
    has_price = any(k in obj for k in PRICE_KEYS)
    return has_id or has_price

URL_RE = re.compile(r"https?://[^\s，,、；;\"'）)】\]】]+", re.I)

# 搜索页把整棵树塞在 window.rawData 里；匹配到等号后由 _extract_raw_data 做括号配对
RAW_DATA_RE = re.compile(r"window\s*\.\s*rawData\s*=(?!=)")

# 本插件自己发出的文案特征，用于识别平台回显、避免自问自答
SELF_MARKS = (
    "【今日行情】",
    "【史低提醒】",
    "【监控列表】",
)

# 指令名刻意用中文：QQ 客户端会把「/pd」这类前缀转成表情，英文缩写容易被打断
PRIMARY_CMD = "盯价"
CMD_ALIASES = ("多多", "拼多多", "价格监控", "比价")
CMD_NAMES = (PRIMARY_CMD,) + CMD_ALIASES

HELP_TEXT = (
    "【多多盯价】\n"
    "/盯价 add 9700X      加关键词开始监控\n"
    "/盯价 del 1          删第 1 项\n"
    "/盯价 list           监控列表\n"
    "/盯价 info 1         单项详情\n"
    "/盯价 check          立即巡检\n"
    "/盯价 debug 9700X    取价诊断\n"
    "/盯价 on / off       开 / 关本群推送\n"
    "\n@我 说「今日行情」看全部价格。"
)


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def fmt_yuan(cents: Optional[int]) -> str:
    if cents is None:
        return "--"
    return f"¥{cents / 100:.2f}"


def shorten(text: str, limit: int = 28) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def extract_first_url(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip("。.,;，") if m else None


def keyword_key(keyword: str) -> str:
    """关键词监控在 goods 表里的存储键。加前缀避免和 goods_id 撞车。"""
    return "kw:" + (keyword or "").strip().lower()


def is_keyword_item(item: Optional[dict]) -> bool:
    return bool(item) and item.get("type") == "keyword"


class PddFetchError(Exception):
    """取价失败：网络异常 / 风控 / 页面结构变化 / 商品下架。

    risk=True 表示这是平台侧的临时风控（系统繁忙、滑块验证），
    与本条关键词无关，此时应立即中止本轮巡检，避免继续请求加重风控。
    """

    def __init__(self, message: str = "", risk: bool = False):
        super().__init__(message)
        self.risk = risk


class PriceStore:
    """关键词监控表 + 价格历史，落盘为单个 JSON。

    数据结构：
    {
      "goods": {
        "kw:9700x": {
            "type": "keyword", "keyword": str, "title": str（当前最低价商品名）,
            "goods_id": str（该商品 id）, "share_link": str, "note": str,
            "samples": [{price_cents, title, goods_id}, ...],
            "added_at": str, "added_by": str,
            "last_price_cents": int, "last_check": str,
            "lowest_cents": int, "lowest_at": str,
            "daily": {"YYYY-MM-DD": cents},
            "fail_count": int, "last_error": str
        }
      },
      "sessions": ["aiocqhttp:GroupMessage:123456"],
      "meta": {"last_daily_report": "YYYY-MM-DD"}
    }

    键沿用 "goods" 字段名，只是为了兼容旧数据文件；内容现在全是关键词项。
    """

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, Any] = {"goods": {}, "sessions": [], "meta": {}}
        self.load()

    # ---------------- 持久化 ----------------
    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                if int(raw.get("data_version") or 0) != DATA_VERSION:
                    # 取价源和价格口径都变过，旧历史留着只会造成假涨跌，直接清空更干净
                    logger.warning("[pdd] 检测到旧版本数据文件，已清空监控与历史（新增关键词即可）")
                    self.data["goods"] = {}
                    self.data["sessions"] = [s for s in (raw.get("sessions") or []) if isinstance(s, str)]
                    self.data["meta"] = {}
                    self.data["data_version"] = DATA_VERSION
                    self.save()
                    return
                goods = raw.get("goods") or {}
                kept = {k: v for k, v in goods.items() if is_keyword_item(v)}
                dropped = len(goods) - len(kept)
                if dropped:
                    logger.info(f"[pdd] 已清理 {dropped} 个旧的单品监控项（现在只支持关键词监控）")
                self.data["goods"] = kept
                self.data["sessions"] = [s for s in (raw.get("sessions") or []) if isinstance(s, str)]
                self.data["meta"] = raw.get("meta") or {}
        except Exception as e:  # 数据损坏时不阻塞插件启动
            logger.error(f"[pdd] 读取数据文件失败，将使用空数据：{e}")

    def save(self) -> None:
        tmp = self.path + ".tmp"
        self.data["data_version"] = DATA_VERSION
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.error(f"[pdd] 保存数据文件失败：{e}")

    # ---------------- 关键词监控项 ----------------
    @property
    def goods(self) -> dict[str, dict]:
        return self.data["goods"]

    def add_keyword(self, keyword: str, note: str = "", added_by: str = "") -> dict:
        key = keyword_key(keyword)
        item = self.goods.get(key) or {"daily": {}, "fail_count": 0}
        item.update(
            {
                "type": "keyword",
                "keyword": keyword.strip(),
                "title": item.get("title") or keyword.strip(),
                "note": note or item.get("note", ""),
                "added_at": item.get("added_at") or now_str(),
                "added_by": item.get("added_by") or added_by,
                "daily": item.get("daily") or {},
            }
        )
        self.goods[key] = item
        return item

    def find_keyword(self, keyword: str) -> Optional[tuple[str, dict]]:
        key = keyword_key(keyword)
        if key in self.goods and is_keyword_item(self.goods[key]):
            return key, self.goods[key]
        return None

    def get_by_index(self, index: int) -> Optional[tuple[str, dict]]:
        keys = list(self.goods.keys())
        if 1 <= index <= len(keys):
            return keys[index - 1], self.goods[keys[index - 1]]
        return None

    def remove(self, key: str) -> bool:
        if key in self.goods:
            del self.goods[key]
            return True
        return False

    # ---------------- 会话绑定 ----------------
    def bind_session(self, umo: str) -> bool:
        if umo and umo not in self.data["sessions"]:
            self.data["sessions"].append(umo)
            return True
        return False

    def unbind_session(self, umo: str) -> bool:
        if umo in self.data["sessions"]:
            self.data["sessions"].remove(umo)
            return True
        return False

    # ---------------- 价格记录 ----------------
    @staticmethod
    def _prev_day_price(item: dict, today: str) -> Optional[int]:
        daily = item.get("daily") or {}
        dates = sorted(d for d in daily.keys() if d < today)
        if not dates:
            return None
        return daily[dates[-1]]

    def apply_price(self, goods_id: str, price_cents: int, when: Optional[datetime] = None) -> dict:
        """写入一次取价结果，返回本次对比信息。

        is_new_low 为 True 表示刷新了历史最低价（首次记录时 old_low 为 None）。
        """
        item = self.goods[goods_id]
        when = when or datetime.now()
        today = when.strftime("%Y-%m-%d")

        old_low = item.get("lowest_cents")
        prev_day = self._prev_day_price(item, today)

        item["last_price_cents"] = price_cents
        item["last_check"] = when.strftime("%Y-%m-%d %H:%M:%S")
        item.setdefault("daily", {})[today] = price_cents

        is_new_low = old_low is None or price_cents < old_low
        if is_new_low:
            item["lowest_cents"] = price_cents
            item["lowest_at"] = when.strftime("%Y-%m-%d %H:%M")

        return {
            "price": price_cents,
            "old_low": old_low,
            "is_new_low": is_new_low,
            "prev_day_price": prev_day,
        }


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
class PDDMonitorPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config or {}
        self.store = PriceStore(os.path.join(self._resolve_data_dir(), DATA_FILE))
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._curl = None
        self._transport = ""
        self._transport_choice: Optional[bool] = None
        self._lock = asyncio.Lock()
        self._last_risk = ""
        # 风控退避：连续命中次数、下次允许巡检的时间戳。
        # 拼多多的限流是「账号级」的，被限后按原间隔继续打只会加重处罚。
        self._risk_streak = int(self.store.data.get("meta", {}).get("risk_streak") or 0)
        self._risk_until = 0.0
        # 会话 cookie：上次跑完把服务端轮换过的设备/风控 cookie 存下来，
        # 下次启动接着用。每次重启都换一套 api_uid，等于每次都是一个全新设备。
        self._jar: dict[str, str] = dict(self.store.data.get("meta", {}).get("cookie_jar") or {})
        self._warmed = False
        self._last_url = ""

    # ---------------- 基础 ----------------
    def cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _risk_cooldown(self, streak: int) -> int:
        """风控冷却时长（秒）：1h、2h、4h… 到上限封顶。

        拼多多的限流按账号算，被限后继续按原间隔打会加重处罚，
        所以这里做指数退避，宁可少查几次。
        """
        try:
            cap_hours = float(self.cfg("risk_backoff_max_hours", 8))
        except (TypeError, ValueError):
            cap_hours = 8.0
        cap = max(1, int(cap_hours * 3600))
        return min(3600 * (2 ** max(0, streak - 1)), cap)

    def _cookie(self) -> str:
        """读取并清洗 cookie；缺失时给出明确提示。"""
        raw = str(self.cfg("cookie", "") or "").strip()
        if not raw:
            raise PddFetchError(
                "未配置 cookie，请在插件配置里粘贴拼多多网页 cookie"
                "（必须包含 PDDAccessToken）"
            )
        return normalize_cookie(raw)

    def _user_agent(self) -> str:
        """UA 可配：填成你取 cookie 那台手机的同款 UA 最安全。"""
        return str(self.cfg("user_agent", "") or "").strip() or DEFAULT_UA

    def _session_cookies(self) -> dict[str, str]:
        """配置的 cookie + 上次留存的服务端 cookie，登录态以配置为准。"""
        base = parse_cookie_pairs(self._cookie())
        return merge_cookie_pairs(base, dict(self._jar) or base)

    def _remember_cookies(self, pairs: dict[str, str]) -> None:
        """把服务端轮换过的 cookie 记下来，供下次启动续用。

        只存非登录态字段：数据文件里不该出现 PDDAccessToken。
        """
        if not pairs:
            return
        changed = False
        for key, value in pairs.items():
            if key in LOGIN_COOKIE_KEYS or not value:
                continue
            if self._jar.get(key) != value:
                self._jar[key] = value
                changed = True
        if changed:
            self.store.data.setdefault("meta", {})["cookie_jar"] = dict(self._jar)

    # ---------------- 传输层 ----------------
    def _use_curl(self) -> bool:
        """配置 transport=curl_cffi 时用它模拟 TLS/HTTP2 指纹。

        结论缓存下来：不然每个关键词都会重探一次，日志也会被刷爆。
        """
        if self._transport_choice is not None:
            return self._transport_choice
        mode = str(self.cfg("transport", "auto") or "auto").strip().lower()
        ok = mode != "httpx"
        if ok:
            try:
                import curl_cffi  # noqa: F401
            except Exception:
                ok = False
                if mode == "curl_cffi":
                    logger.warning(
                        "[pdd] 配置要求 curl_cffi，但没装，回落到 httpx（TLS 指纹不会被伪装）"
                    )
                else:
                    logger.info(
                        "[pdd] 未安装 curl_cffi，使用 httpx；"
                        "pip install curl_cffi 可让 TLS 指纹也伪装成浏览器"
                    )
        self._transport_choice = ok
        return ok

    async def _get_curl(self):
        """curl_cffi 会话。impersonate 会同时改 TLS 指纹与 HTTP/2 帧序。"""
        if self._curl is not None:
            return self._curl
        try:
            from curl_cffi.requests import AsyncSession
        except Exception as e:  # 装的是老版本，没有异步接口
            logger.warning(f"[pdd] curl_cffi 版本不含异步接口，回落 httpx：{e}")
            self._transport_choice = False
            return None

        candidates = [
            str(self.cfg("impersonate", "") or "").strip(),
            "safari_ios",
            "safari18_0",
            "safari17_0",
            "safari15_5",
        ]
        timeout = float(self.cfg("request_timeout", 15))
        last_err: Optional[Exception] = None
        for name in candidates:
            if not name:
                continue
            try:
                # Cookie 统一走请求头，不用 curl 自己的 cookie 引擎：
                # 两条传输层用同一套 jar 逻辑，也不会因为 API 差异静默丢 cookie
                self._curl = AsyncSession(impersonate=name, timeout=timeout)
                self._transport = f"curl_cffi/{name}"
                logger.info(f"[pdd] 传输层：curl_cffi（模拟 {name} 的 TLS 指纹）")
                return self._curl
            except Exception as e:  # impersonate 名字不被这个版本支持
                last_err = e
                self._curl = None
        logger.warning(f"[pdd] curl_cffi 初始化失败，回落 httpx：{last_err}")
        self._transport_choice = False
        return None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            timeout = float(self.cfg("request_timeout", 15))
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
                http2=False,
            )
            if not self._transport:
                self._transport = "httpx"
        return self._client

    async def _warmup(self) -> None:
        """先访问一次首页。

        真实用户进站第一件事是打开首页，服务端在这时会下发 api_uid 等基础 cookie。
        直接凭空拿着搜索 URL 打过去、cookie 里却没有 api_uid，是很明显的自动化特征。
        失败不报错——这只是加分项，不是必需项。
        """
        if self._warmed or not bool(self.cfg("warmup_home", True)):
            return
        self._warmed = True
        try:
            await self._fetch(HOME_URL, referer=None)
            logger.info("[pdd] 已完成首页预热，基础 cookie 已就绪")
        except Exception as e:
            logger.debug(f"[pdd] 首页预热失败（不影响取价）：{e}")

    @staticmethod
    def _set_cookie_values(resp: Any) -> list[str]:
        """把响应里的 Set-Cookie 取成列表，兼容 httpx 与 curl_cffi 两种 headers。"""
        headers = getattr(resp, "headers", None)
        if headers is None:
            return []
        for getter in ("get_list", "getlist"):
            fn = getattr(headers, getter, None)
            if fn is None:
                continue
            try:
                vals = fn("set-cookie")
                if vals:
                    return [str(v) for v in vals]
            except Exception:
                continue
        try:
            one = headers.get("set-cookie")
            return [str(one)] if one else []
        except Exception:
            return []

    async def _fetch(self, url: str, referer: Optional[str] = None) -> str:
        """真正发请求的地方。curl_cffi 与 httpx 两条路，头与 cookie 逻辑完全一致。"""
        headers = build_headers(self._user_agent(), referer)
        jar = self._session_cookies()
        if jar:
            headers["Cookie"] = format_cookie(jar)

        curl = await self._get_curl() if self._use_curl() else None
        if curl is not None:
            try:
                resp = await curl.get(url, headers=headers)
            except Exception as e:
                raise PddFetchError(f"网络请求失败：{e}") from e
        else:
            client = await self._get_client()
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as e:
                raise PddFetchError(f"网络请求失败：{e}") from e

        self._last_url = url
        # 服务端轮换的设备/风控 cookie 收进 jar，下次请求带上，会话才连续
        self._remember_cookies(parse_set_cookie(self._set_cookie_values(resp)))
        if resp.status_code != 200:
            raise PddFetchError(f"页面返回 HTTP {resp.status_code}")
        return resp.text

    async def _get_html(self, url: str, cookie: Optional[str] = None) -> str:
        """抓页面。cookie 传 None 表示用配置里的，显式传 "" 表示故意不带。"""
        if cookie is not None:
            # 显式指定 cookie 的调用方（诊断）走一次性请求，不读也不改会话 jar
            headers = build_headers(self._user_agent(), None)
            if cookie:
                headers["Cookie"] = cookie
            client = await self._get_client()
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError as e:
                raise PddFetchError(f"网络请求失败：{e}") from e
            if resp.status_code != 200:
                raise PddFetchError(f"页面返回 HTTP {resp.status_code}")
            return resp.text

        await self._warmup()
        return await self._fetch(url, self._last_url or None)

    @staticmethod
    def _extract_raw_data(html: str) -> Optional[dict]:
        """从 HTML 里抠出 window.rawData 的 JSON。

        用括号配对扫描而不是正则——页面里的 JSON 很深，正则截不准。
        页面改版后这段可能不存在，返回 None 交由调用方判断原因。
        """
        m = RAW_DATA_RE.search(html)
        if not m:
            return None
        pos = m.end()
        # 跳过空白后判断是不是 null（未登录时拼多多会下发 window.rawData = null）
        while pos < len(html) and html[pos] in " \t\r\n=":
            pos += 1
        if pos >= len(html) or html[pos] != "{":
            return None
        start = pos

        depth = 0
        in_str = False
        escaped = False
        for i in range(start, len(html)):
            ch = html[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:i + 1])
                    except Exception:
                        return None
        return None

    @staticmethod
    def _find_goods_list(data: Any, depth: int = 0) -> list[dict]:
        """在 rawData 里找商品列表。

        PDD 的路径变过几次（store.data.ssrListData.list 等），所以按特征递归找：
        「元素是 dict 且带 goodsName / goodsID」的列表就是商品列表。
        """
        if depth > 8:
            return []
        if isinstance(data, dict):
            for key in ("ssrListData", "list", "items", "goods_list"):
                node = data.get(key)
                if isinstance(node, dict):
                    inner = node.get("list") or node.get("items")
                    if isinstance(inner, list) and inner and is_goods_item(inner[0]):
                        return [x for x in inner if isinstance(x, dict)]
                if isinstance(node, list) and node and is_goods_item(node[0]):
                    return [x for x in node if isinstance(x, dict)]
            for value in data.values():
                found = PDDMonitorPlugin._find_goods_list(value, depth + 1)
                if found:
                    return found
        elif isinstance(data, list):
            if data and is_goods_item(data[0]):
                return [x for x in data if isinstance(x, dict)]
            for value in data:
                found = PDDMonitorPlugin._find_goods_list(value, depth + 1)
                if found:
                    return found
        return []

    def _resolve_data_dir(self) -> str:
        candidates: list[str] = []
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            candidates.append(os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME))
        except Exception:
            pass
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
        for path in candidates:
            try:
                os.makedirs(path, exist_ok=True)
                return path
            except Exception:
                continue
        return os.getcwd()

    async def initialize(self) -> None:
        self._start_worker()

    @filter.on_astrbot_loaded()
    async def _on_loaded(self) -> None:
        # 兜底：确保定时巡检一定被启动
        self._start_worker()

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        if self._curl is not None:
            try:
                await self._curl.close()
            except Exception:
                pass
            self._curl = None
        # 退出前把会话 cookie 落盘，下次启动接着用同一套设备标识
        try:
            if self._jar:
                self.store.data.setdefault("meta", {})["cookie_jar"] = dict(self._jar)
                self.store.save()
        except Exception:
            pass

    def _start_worker(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            self._task = asyncio.create_task(self._worker())
            logger.info("[pdd] 价格巡检任务已启动")
        except RuntimeError as e:
            logger.warning(f"[pdd] 巡检任务启动失败（事件循环未就绪）：{e}")

    # ---------------- 巡检循环 ----------------
    async def _worker(self) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                # 自动巡检把请求摊到整个间隔里，避免「2 分钟打完 50 个请求，
                # 然后静默 28 分钟」这种一眼假的机器人节律
                interval = max(5, int(self.cfg("check_interval_minutes", 30))) * 60
                await self._check_all(notify=True, spread=interval * 0.7)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[pdd] 巡检异常：{e}", exc_info=True)
            try:
                await self._maybe_daily_report()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[pdd] 每日行情推送异常：{e}")

            interval = max(5, int(self.cfg("check_interval_minutes", 30))) * 60
            # 冷却期内不提前开工，否则每半小时撞一次限流只会越限越久
            remain = int(self._risk_until - time.time()) if self._risk_until else 0
            await asyncio.sleep(max(interval, remain))

    async def _maybe_daily_report(self) -> None:
        raw = str(self.cfg("daily_report_time", "") or "").strip()
        if not raw:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if self.store.data.get("meta", {}).get("last_daily_report") == today:
            return
        try:
            hh, mm = [int(x) for x in raw.replace("：", ":").split(":")]
            scheduled = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except Exception:
            return
        if now < scheduled:
            return
        chunks = self.build_today_report()
        for chunk in chunks:
            await self._broadcast(chunk)
        self.store.data.setdefault("meta", {})["last_daily_report"] = today
        self.store.save()

    def _item_gap(self, index: int, total: int, spread: float) -> float:
        """两个关键词之间该等多久（秒）。

        spread=0 时用手动巡检的固定抖动（1.5~3.5 秒）；
        否则把整轮摊到 spread 秒内，单条上限 300 秒，避免监控项很少时
        一轮拖到下一个间隔去。
        """
        base = 1.5
        if spread > 0 and total > 0:
            base = min(300.0, max(base, spread / total))
        # ±35% 抖动：真实用户不会每隔整数秒点一次
        return base * random.uniform(0.65, 1.35)

    async def _check_all(
        self, notify: bool = True, spread: float = 0.0
    ) -> Optional[tuple[int, int, int, list]]:
        """巡检全部商品，返回 (总数, 成功, 失败, 新低数)。

        spread > 0 表示把整轮的请求摊到这么多秒里（自动巡检用）。
        """
        if self._lock.locked():
            return None
        async with self._lock:
            goods_map = self.store.goods
            if not goods_map:
                return (0, 0, 0, [])
            total = len(goods_map)
            ok = fail = 0
            lows: list[tuple[str, dict]] = []
            stop_reason = ""

            for idx, (key, item) in enumerate(list(goods_map.items())):
                if idx:
                    await asyncio.sleep(self._item_gap(idx, total, spread))
                try:
                    info = await self._search_keyword(str(item.get("keyword") or ""))
                except Exception as e:
                    fail += 1
                    item["fail_count"] = int(item.get("fail_count", 0)) + 1
                    item["last_error"] = str(e)[:180]
                    logger.warning(f"[pdd] 取价失败 {key}: {e}")
                    # 风控类的失败与具体关键词无关，继续请求只会加重风控
                    if getattr(e, "risk", False):
                        stop_reason = str(e)
                        logger.warning(f"[pdd] 检测到平台风控，本轮巡检中止：{stop_reason}")
                        break
                    continue

                ok += 1
                item["fail_count"] = 0
                item["last_error"] = ""
                if info.get("title"):
                    item["title"] = info["title"]
                if info.get("samples"):
                    item["samples"] = info["samples"]
                if info.get("goods_id"):
                    item["goods_id"] = info["goods_id"]
                    # 链接始终指向当前最低价的那个商品
                    item["share_link"] = GOODS_URL.format(info["goods_id"])

                result = self.store.apply_price(key, info["price_cents"])
                # old_low 为 None 说明是首次记录，不算「新低」，避免刚添加就刷屏
                if result["is_new_low"] and result["old_low"] is not None:
                    lows.append((key, result))

            self._last_risk = stop_reason
            notice = ""
            if stop_reason:
                self._risk_streak += 1
                cooldown = self._risk_cooldown(self._risk_streak)
                self._risk_until = time.time() + cooldown
                self.store.data.setdefault("meta", {})["risk_streak"] = self._risk_streak
                logger.warning(
                    f"[pdd] 连续第 {self._risk_streak} 次触发平台限流，"
                    f"冷却 {cooldown // 60} 分钟后再试"
                )
                # 第 2 次起才打扰用户：偶尔一次可能只是瞬时抖动
                if self._risk_streak == 2:
                    notice = (
                        "【盯价暂停】\n"
                        f"拼多多搜索页连续限流，已冷却 {cooldown // 60} 分钟后再试。\n"
                        "限流按账号算，频繁重试会越限越久。换网络或换账号通常能恢复。"
                    )
            elif ok:
                if self._risk_streak >= 2:
                    notice = "【盯价恢复】\n取价已恢复正常，风控计数清零。"
                elif self._risk_streak:
                    logger.info("[pdd] 本轮取价成功，风控计数已清零")
                self._risk_streak = 0
                self._risk_until = 0.0
                self.store.data.setdefault("meta", {})["risk_streak"] = 0
            self.store.save()

        if notify and notice:
            await self._broadcast(notice)
        if notify and lows and bool(self.cfg("notify_at_new_low", True)):
            for key, result in lows:
                await self._broadcast(self._format_new_low(key, result))
        return (total, ok, fail, lows)

    # ---------------- 关键词搜索取价（H5 搜索页） ----------------
    def _search_url(self, keyword: str, page: int = 1) -> str:
        """拼搜索页地址。sort 取 price（升序）或 _price（降序）。"""
        sort = str(self.cfg("search_sort", SORT_ASC) or SORT_ASC).strip()
        if sort not in (SORT_ASC, SORT_DESC):
            sort = SORT_ASC
        query = {"search_key": keyword, "sort_type": sort, "page": str(page)}
        return SEARCH_URL + "?" + urlencode(query)

    async def _search_keyword(self, keyword: str) -> dict:
        """按关键词搜索，返回价格最低的那件商品。

        取价口径：搜索页 item 里的 price（单位分），即页面展示的拼单价。
        H5 搜索页不下发券后价，所以这里不扣券（要精确到手价只能逐件进详情页，
        那样每轮请求量会翻几十倍，风控风险不成比例）。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            raise PddFetchError("关键词为空")

        html = await self._get_html(self._search_url(keyword))
        raw = self._extract_raw_data(html)

        if raw is None:
            # 拿不到 rawData 时按页面特征分流，别把风控说成「没搜到」
            if "系统繁忙" in html or "请稍后再试" in html:
                raise PddFetchError("搜索页返回「系统繁忙」，账号被限流", risk=True)
            if "captcha" in html or "访问验证" in html or "滑块" in html:
                raise PddFetchError("搜索页要求验证码验证，账号被风控", risk=True)
            raise PddFetchError(
                "搜索页没有下发行情数据（rawData 缺失）。"
                "可能是 cookie 失效、账号被限流，或拼多多又改了页面结构",
                risk=True,
            )

        goods_list = self._find_goods_list(raw)
        if not goods_list:
            raise PddFetchError(
                "页面结构能解析，但没找到商品列表。"
                "可能是这个关键词确实没有结果，或拼多多改了字段名"
            )

        candidates: list[dict] = []
        no_price = 0
        for item in goods_list:
            name = str(item.get("goodsName") or item.get("goods_name") or "").strip()
            if not name:
                continue
            price = self._item_price_cents(item)
            if not price:
                no_price += 1
                continue
            gid = item.get("goodsID") or item.get("goods_id") or ""
            candidates.append(
                {
                    "price_cents": price,
                    "title": name,
                    "goods_id": str(gid) if gid else "",
                    "sales_tip": str(item.get("salesTip") or item.get("sales_tip") or ""),
                }
            )

        if not candidates:
            raise PddFetchError(
                f"搜到 {len(goods_list)} 件商品，但没有一件能解析出价格"
                f"（{no_price} 件缺价格字段），拼多多可能改了价格字段名"
            )

        # 先按价格升序：后面「前 N 条」「该商品往后两条」都以这个顺序为准
        candidates.sort(key=lambda c: c["price_cents"])

        anchor_idx = 0
        match_info: dict[str, Any] = {"match_mode": "off"}
        if bool(self.cfg("keyword_filter_by_title", False)):
            anchor_idx, match_info = self._pick_by_title(keyword, candidates)

        anchor = dict(candidates[anchor_idx])
        anchor["keyword"] = keyword
        # samples[0] 是选中的锚点，后面两条是它后面的报价
        anchor["samples"] = candidates[anchor_idx: anchor_idx + 3]
        anchor["count"] = len(candidates)
        anchor.update(match_info)
        return anchor

    def _title_pool_size(self) -> int:
        try:
            n = int(self.cfg("title_match_top_n", 5))
        except (TypeError, ValueError):
            n = 5
        return max(1, min(20, n))

    def _pick_by_title(self, keyword: str, candidates: list[dict]) -> tuple[int, dict[str, Any]]:
        """在前 N 条里按分词重合度挑一条作为「底价参考」，返回 (索引, 诊断信息)。

        为什么要绕这一下：搜索页对同一个关键词会混进大量「配件」「兼容」
        「替代款」，价格往往比正主低好几倍。只按价格取最低，盯出来的
        「9700X 最低价」很可能是三块钱的硅脂或散热器。

        只看前 N 条就够：候选池再往后翻，价格已经高出目标区间，
        真命中了也不是我们想记的那个价。
        """
        pool_n = self._title_pool_size()
        pool = candidates[:pool_n]
        kw_tokens = tokenize(keyword)

        if not kw_tokens:
            return 0, {"match_mode": "fallback", "match_note": "关键词分不出有效词，按最低价取"}

        if not HAS_JIEBA:
            # 没装 jieba 时退化成子串匹配：至少不会退化成「随便挑一条」
            kw_lower = keyword.lower()
            for i, cand in enumerate(pool):
                if kw_lower in cand["title"].lower():
                    return i, {
                        "match_mode": "substring",
                        "match_score": 1.0,
                        "match_index": i + 1,
                        "match_pool": len(pool),
                        "match_note": "未安装 jieba，用子串匹配",
                    }
            return 0, {
                "match_mode": "fallback",
                "match_note": f"前 {len(pool)} 条标题都没含关键词（子串匹配）",
            }

        scored: list[tuple[float, int, int, int, set[str]]] = []
        for i, cand in enumerate(pool):
            score, hit = overlap_score(kw_tokens, tokenize(cand["title"]))
            # 重合度 > 命中词数 > 价格低 > 索引靠前；
            # 索引唯一，所以比较永远不会落到最后那个 set 上
            scored.append((score, len(hit), -cand["price_cents"], i, hit))
        scored.sort(reverse=True)

        best_score, _, _, best_i, best_hit = scored[0]
        if best_score <= 0:
            return 0, {
                "match_mode": "fallback",
                "match_note": f"前 {len(pool)} 条标题都没命中关键词，按最低价取",
            }
        return best_i, {
            "match_mode": "jieba",
            "match_score": round(best_score, 2),
            "match_index": best_i + 1,
            "match_pool": len(pool),
            "match_tokens": sorted(best_hit),
        }

    @classmethod
    def _item_price_cents(cls, item: dict) -> Optional[int]:
        """从搜索页商品条目里取价格（分）。

        PRICE_KEYS 里靠前的字段优先：price / groupPrice 都是整数分，
        priceInfo / priceStr 是带 ¥ 的元字符串，走正则兜底。
        注意：不要优先用 minOnSaleGroupPrice，那个在部分场景下是 0。
        """
        for key in PRICE_KEYS:
            raw = item.get(key)
            if raw is None or isinstance(raw, bool):
                continue
            if isinstance(raw, (int, float)):
                cents = cls._to_cents(raw)
                if cents:
                    return cents
                continue
            text = str(raw).replace(",", "").replace("，", "")
            m = re.search(r"(\d+(?:\.\d+)?)", text)
            if not m:
                continue
            try:
                cents = int(round(float(m.group(1)) * 100))
            except (TypeError, ValueError):
                continue
            if cents > 0:
                return cents
        return None

    @staticmethod
    def _to_cents(value: Any) -> Optional[int]:
        """价格字段一律是「分」，转成 int 并丢掉非法值。"""
        if value is None or isinstance(value, bool):
            return None
        try:
            cents = int(value)
        except (TypeError, ValueError):
            return None
        return cents if cents > 0 else None

    # ---------------- 消息格式化 ----------------
    @staticmethod
    def _delta_text(diff: int, base: int) -> str:
        """较昨日的涨跌文案：跌 ▼、涨 ▲、平为「持平」。"""
        if diff == 0:
            return "持平"
        return f"▼{fmt_yuan(abs(diff))}" if diff < 0 else f"▲{fmt_yuan(diff)}"

    def _format_new_low(self, key: str, result: dict) -> str:
        """刷新历史最低价时的主动推送。"""
        item = self.store.goods.get(key, {})
        old_low = int(result["old_low"] or 0)
        price = int(result["price"])
        lines = [
            "【史低提醒】",
            f"关键词：{item.get('keyword') or key}",
            f"现价：{fmt_yuan(price)}",
        ]
        if old_low:
            drop = old_low - price
            pct = drop / old_low * 100
            lines.append(f"原史低：{fmt_yuan(old_low)}")
            lines.append(f"降幅：{fmt_yuan(drop)}（-{pct:.1f}%）")
        link = item.get("share_link") or (
            GOODS_URL.format(item["goods_id"]) if item.get("goods_id") else ""
        )
        if link:
            lines.append(f"链接：{link}")
        return "\n".join(lines)

    def build_today_report(self) -> list[str]:
        """「今日行情」文本：逐项列出关键词、现价、较昨日涨跌、史低。"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        items = list(self.store.goods.values())

        if not items:
            return ["【今日行情】\n监控列表还是空的，先用 /盯价 add <关键词> 添加监控。"]

        lines = ["【今日行情】", f"共监控 {len(items)} 项"]
        n_down = n_up = n_flat = n_low = n_fail = 0

        for idx, item in enumerate(items, 1):
            name = shorten(item.get("keyword") or "--", 16)
            low = item.get("lowest_cents")
            price = item.get("last_price_cents")

            if price is None:
                n_fail += 1
                lines.append(f"{idx}. 「{name}」")
                lines.append(f"　　取价失败（{shorten(item.get('last_error') or '暂无数据', 20)}）")
                continue

            prev_day = PriceStore._prev_day_price(item, today)
            if prev_day is None:
                delta = "首次记录"
            else:
                diff = int(price) - int(prev_day)
                if diff < 0:
                    n_down += 1
                elif diff > 0:
                    n_up += 1
                else:
                    n_flat += 1
                delta = self._delta_text(diff, prev_day)

            is_low = low is not None and int(price) <= int(low)
            if is_low:
                n_low += 1

            lines.append(f"{idx}. 「{name}」{'【史低】' if is_low else ''}")
            lines.append(f"　　现价 {fmt_yuan(price)} 较昨日 {delta}")
            lines.append(f"　　史低 {fmt_yuan(low)}")

        lines.append("————————————")
        lines.append(f"下跌 {n_down} · 上涨 {n_up} · 持平 {n_flat}")
        if n_low:
            lines.append(f"其中 {n_low} 项正处于史低！")
        if n_fail:
            lines.append(f"另有 {n_fail} 项取价失败")
        return self._chunk(lines)

    @staticmethod
    def _chunk(lines: list[str], limit: int = 900) -> list[str]:
        chunks: list[str] = []
        buf = ""
        for line in lines:
            piece = line if line.endswith("\n") else line + "\n"
            if buf and len(buf) + len(piece) > limit:
                chunks.append(buf.rstrip())
                buf = piece
            else:
                buf += piece
        if buf.strip():
            chunks.append(buf.rstrip())
        return chunks or [""]

    async def _broadcast(self, text: str) -> int:
        sessions = list(self.store.data.get("sessions") or [])
        if not sessions:
            logger.info("[pdd] 没有已绑定的会话，跳过推送（在群里发 /盯价 on 绑定）")
            return 0
        sent = 0
        for umo in sessions:
            try:
                chain = MessageChain().message(text)
                if await self.context.send_message(umo, chain):
                    sent += 1
                else:
                    logger.warning(f"[pdd] 推送失败（平台可能不支持主动消息）：{umo}")
            except Exception as e:
                logger.error(f"[pdd] 推送异常 {umo}: {e}")
        return sent

    # ---------------- 指令：/盯价 ----------------
    @filter.command(PRIMARY_CMD, alias=set(CMD_ALIASES))
    async def pdd(self, event: AstrMessageEvent):
        """拼多多关键词盯价：/盯价 help 查看用法"""
        raw = event.message_str or ""
        body = re.sub(r"^[\s/!！.#]*", "", raw)
        # 去掉指令词本身（各平台唤醒前缀不同，这里统一剥掉首个 token）
        parts = body.split(maxsplit=1)
        if parts and parts[0] in CMD_NAMES:
            body = parts[1].strip() if len(parts) > 1 else ""
        parts = body.split(maxsplit=1)
        sub = parts[0].lower() if parts else "help"
        arg = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("help", "帮助", "", "?"):
            yield event.plain_result(HELP_TEXT)
            return

        if sub in ("add", "添加", "加"):
            if not arg:
                yield event.plain_result("用法：/盯价 add <关键词>（例：/盯价 add 9700X）")
                return
            yield event.plain_result(await self._cmd_add(event, arg))
            return

        if sub in ("del", "delete", "remove", "删除"):
            if not arg:
                yield event.plain_result("用法：/盯价 del <序号>")
                return
            yield event.plain_result(self._cmd_del(arg))
            return

        if sub in ("list", "ls", "列表"):
            for chunk in self._chunk(self._cmd_list()):
                yield event.plain_result(chunk)
            return

        if sub in ("info", "详情"):
            yield event.plain_result(self._cmd_info(arg))
            return

        if sub in ("check", "检查", "巡检"):
            result = await self._check_all(notify=True)
            if result is None:
                yield event.plain_result("上一轮巡检还没结束，稍等一会儿。")
                return
            total, ok, fail, lows = result
            if total == 0:
                yield event.plain_result("监控列表是空的，先 /盯价 add <关键词>。")
                return
            msg = f"巡检完成：{ok}/{total} 成功"
            if lows:
                msg += f"，史低 {len(lows)} 个（已推送）"
            if fail:
                msg += f"，失败 {fail} 个"
            if self._last_risk:
                msg += f"\n本轮已中止：{shorten(self._last_risk, 40)}"
            if self._risk_streak:
                wait = max(0, int(self._risk_until - time.time()))
                msg += f"\n风控计数：连续 {self._risk_streak} 次"
                if wait:
                    msg += f"，冷却中，约 {max(1, wait // 60)} 分钟后再试"
            yield event.plain_result(msg)
            return

        if sub in ("debug", "诊断", "排查"):
            if not arg:
                yield event.plain_result("用法：/盯价 debug <关键词 或 序号>")
                return
            yield event.plain_result(await self._cmd_debug(arg))
            return

        if sub in ("on", "开启", "push"):
            changed = self.store.bind_session(event.unified_msg_origin)
            self.store.save()
            yield event.plain_result("已开启本群新低推送。" if changed else "本群已经在推送列表里了。")
            return

        if sub in ("off", "关闭", "unpush"):
            changed = self.store.unbind_session(event.unified_msg_origin)
            self.store.save()
            yield event.plain_result("已关闭本群新低推送。" if changed else "本群本来就没开推送。")
            return

        yield event.plain_result(f"没有「{sub}」这个子指令，发 /盯价 help 看用法。")

    async def _cmd_add(self, event: AstrMessageEvent, arg: str) -> str:
        """只接受关键词；贴链接的一律引导到关键词。"""
        head, _, note = arg.partition("|")
        head = head.strip()
        if not head:
            return "用法：/盯价 add <关键词>（例：/盯价 add 9700X）"
        if extract_first_url(head):
            return (
                "现在只支持关键词监控，不再盯单个链接。\n"
                "把商品名里最有辨识度的那部分当关键词就行，例：/盯价 add 9700X"
            )
        return await self._add_keyword(event, head, note.strip())

    async def _add_keyword(self, event: AstrMessageEvent, keyword: str, note: str) -> str:
        keyword = re.sub(r"\s+", " ", keyword).strip()
        if not keyword:
            return "关键词不能为空。"
        if len(keyword) > 40:
            return "关键词太长了（最多 40 字）。"

        max_items = int(self.cfg("max_goods", 50))
        if len(self.store.goods) >= max_items:
            return f"监控已达上限（{max_items} 项），先 /盯价 del 删几个。"

        existing = self.store.find_keyword(keyword)
        if existing:
            item = existing[1]
            return (
                f"这个关键词已经在监控列表里了：\n"
                f"关键词：{item.get('keyword') or keyword}\n"
                f"当前最低价：{fmt_yuan(item.get('last_price_cents'))}\n"
                f"历史最低：{fmt_yuan(item.get('lowest_cents'))}"
            )

        best: Optional[dict] = None
        err = ""
        try:
            best = await self._search_keyword(keyword)
        except Exception as e:
            err = str(e)

        item = self.store.add_keyword(
            keyword=keyword,
            note=note,
            added_by=event.get_sender_name() or event.get_sender_id(),
        )
        if best:
            item["title"] = best["title"]
            item["goods_id"] = best.get("goods_id") or ""
            item["samples"] = best.get("samples") or []
            if item.get("goods_id"):
                item["share_link"] = GOODS_URL.format(item["goods_id"])
            self.store.apply_price(keyword_key(keyword), best["price_cents"])

        self.store.bind_session(event.unified_msg_origin)
        self.store.save()

        lines = ["已加入监控：", f"关键词：{keyword}"]
        if note:
            lines.append(f"备注：{note}")
        if not best:
            tip = shorten(err or "搜索页未返回有效数据", 24)
            lines.append("当前最低价：--")
            lines.append(f"提示：本次没取到价格（{tip}），后续巡检会继续尝试。")
            return "\n".join(lines)

        lines.append(f"当前最低价：{fmt_yuan(item.get('last_price_cents'))}")
        lines.append(f"历史最低：{fmt_yuan(item.get('lowest_cents'))}（首次记录）")
        # 次低：只报价格，不报标题，避免消息过长
        others = [s for s in (best.get("samples") or [])[1:3] if s.get("price_cents")]
        if others:
            lines.append("次低：" + "、".join(fmt_yuan(s["price_cents"]) for s in others))
        if item.get("share_link"):
            lines.append(f"链接：{item['share_link']}")
        return "\n".join(lines)

    def _cmd_del(self, arg: str) -> str:
        target = arg.strip().lstrip("#")
        key: Optional[str] = None

        if target.isdigit() and len(target) <= 3:
            found = self.store.get_by_index(int(target))
            if found:
                key = found[0]
        if not key:
            found = self.store.find_keyword(target)
            if found:
                key = found[0]

        if not key:
            return f"没找到「{target}」，发 /盯价 list 看序号。"

        label = str(self.store.goods[key].get("keyword") or key)
        self.store.remove(key)
        self.store.save()
        return f"已删除「{label}」"

    def _cmd_list(self) -> list[str]:
        items = list(self.store.goods.items())
        if not items:
            return ["【监控列表】\n监控列表还是空的，先用 /盯价 add <关键词> 添加监控。"]

        lines = ["【监控列表】", f"共监控 {len(items)} 项"]
        for idx, (_key, item) in enumerate(items, 1):
            name = shorten(item.get("keyword") or "--", 16)
            price = item.get("last_price_cents")
            lines.append(f"{idx}. 「{name}」")
            if price is None:
                lines.append(f"　　现价 --　史低 {fmt_yuan(item.get('lowest_cents'))}")
            else:
                lines.append(
                    f"　　现价 {fmt_yuan(price)}　史低 {fmt_yuan(item.get('lowest_cents'))}"
                )
        lines.append("————————————")
        lines.append(f"推送会话：{len(self.store.data.get('sessions') or [])} 个")
        return lines

    def _cmd_info(self, arg: str) -> str:
        target = arg.strip()
        if not target:
            return "用法：/盯价 info <序号>"
        key: Optional[str] = None
        if target.isdigit() and len(target) <= 3:
            found = self.store.get_by_index(int(target))
            if found:
                key = found[0]
        if not key:
            found = self.store.find_keyword(target)
            if found:
                key = found[0]
        if not key:
            return f"没找到「{target}」，发 /盯价 list 看序号。"

        item = self.store.goods[key]
        daily = item.get("daily") or {}
        recent = sorted(daily.items())[-7:]
        trend = "、".join(f"{d[5:]} {fmt_yuan(c)}" for d, c in recent) or "--"

        lines = [
            "【监控详情】",
            f"关键词：{item.get('keyword') or key}",
            f"现价：{fmt_yuan(item.get('last_price_cents'))}",
            f"历史最低：{fmt_yuan(item.get('lowest_cents'))}"
            f"（{str(item.get('lowest_at') or '--')[:16]}）",
        ]
        if item.get("note"):
            lines.append(f"备注：{item['note']}")
        if item.get("title"):
            lines.append(f"最低价商品：{shorten(item['title'], 24)}")
        lines.append(f"最近记录：{trend}")
        if item.get("last_error"):
            lines.append(f"最近错误：{shorten(item['last_error'], 40)}")
        if item.get("share_link"):
            lines.append(f"链接：{item['share_link']}")
        return "\n".join(lines)

    def _cookie_line(self) -> str:
        """只报 cookie 的长度和关键字段是否齐全，绝不回显内容。"""
        raw = str(self.cfg("cookie", "") or "").strip()
        if not raw:
            return "cookie：未配置（必填，否则搜索页只下发空壳）"
        jar = normalize_cookie(raw)
        parts = [f"{len(jar)} 字符"]
        parts.append("含 PDDAccessToken" if "PDDAccessToken" in jar else "缺 PDDAccessToken")
        if "pdd_user_id" in jar:
            parts.append("含 pdd_user_id")
        line = "cookie：" + "、".join(parts)
        if self._jar:
            line += f"\n会话：已沿用 {len(self._jar)} 个服务端下发的设备 cookie"
        return line

    def _transport_line(self) -> str:
        """报当前伪装到了哪一层——这决定了被风控检测的概率。"""
        if self._transport:
            name = self._transport
        elif self._use_curl():
            name = "curl_cffi（待初始化）"
        else:
            name = "httpx"
        if name.startswith("curl_cffi"):
            layer = "TLS + HTTP2 指纹已模拟"
        else:
            layer = "仅伪造请求头，TLS 指纹仍是 Python 默认值"
        ua = self._user_agent()
        ua_kind = "默认" if ua == DEFAULT_UA else "自定义"
        return f"传输：{name}（{layer}）\nUA：{ua_kind}、{'iPhone Safari' if SAFARI_UA_RE.search(ua) else '非 Safari 系'}"

    async def _cmd_debug(self, arg: str) -> str:
        target = arg.strip()
        if target.isdigit() and len(target) <= 3:
            found = self.store.get_by_index(int(target))
            if found:
                target = str(found[1].get("keyword") or target)
        return await self._debug_keyword(target)

    def _match_line(self, best: dict) -> str:
        """把标题匹配过程讲清楚：取了几条、选了第几条、重合多少、命中了哪些词。"""
        mode = str(best.get("match_mode") or "off")
        if mode == "off":
            return "匹配：未启用标题过滤（按最低价取）"
        if mode == "fallback":
            return f"匹配：{best.get('match_note') or '未命中，按最低价取'}"

        pool = best.get("match_pool") or 0
        idx = best.get("match_index") or 1
        score = best.get("match_score")
        pct = f"{score * 100:.0f}%" if isinstance(score, (int, float)) else "--"
        line = f"匹配：前 {pool} 条里第 {idx} 条 · 重合 {pct}"
        tokens = best.get("match_tokens") or []
        if tokens:
            shown = "、".join(str(t) for t in tokens[:4])
            line += f"（命中 {shown}）"
        if mode == "substring":
            line += " · 子串匹配"
        return line

    async def _debug_keyword(self, keyword: str) -> str:
        keyword = (keyword or "").strip()
        if not keyword:
            return "用法：/盯价 debug <关键词>"
        sort = str(self.cfg("search_sort", SORT_ASC) or SORT_ASC).strip() or SORT_ASC
        try:
            best = await self._search_keyword(keyword)
        except Exception as e:
            return (
                f"「{keyword}」搜索失败\n{shorten(str(e), 60)}\n"
                f"{self._cookie_line()}\n{self._transport_line()}"
            )
        lines = [f"「{keyword}」搜到 {best.get('count', 0)} 条"]
        lines.append(f"当前最低：{fmt_yuan(best['price_cents'])}")
        lines.append(f"　{shorten(best.get('title') or '', 24)}")
        if best.get("goods_id"):
            lines.append(f"　商品 {best['goods_id']}")
        lines.append(self._match_line(best))
        lines.append(f"排序：{sort}（{ '升序' if sort == SORT_ASC else '降序'}）")
        lines.append(self._cookie_line())
        lines.append(self._transport_line())
        return "\n".join(lines)

    # ---------------- 关键词：今日行情 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_today_keyword(self, event: AstrMessageEvent):
        """被 @ 或私聊时，说「今日行情」返回全部商品今日价格。

        设计约束（避免干扰正常对话 / LLM 上下文）：
        - 不调用任何 LLM 接口，也不写会话历史；
        - 只有「短问句」才抢答，长句一律放过交给 LLM；
        - 未命中时直接 return，不调用 stop_event()，不影响后续流程；
        - 忽略本插件自己发出的文案被平台回显的事件，避免自问自答死循环。
        """
        text = re.sub(r"\s+", "", event.message_str or "")
        if not text:
            return

        # 自己的推送被回显时不能再次触发（行情标题里就含「今日行情」）
        if any(mark in text for mark in SELF_MARKS):
            return

        triggers = ("今日行情", "今天行情", "拼多多行情", "pdd行情", "查行情")
        if not any(t in text for t in triggers):
            return

        # 只处理短问句：把触发词和标点剥掉后，剩下的内容不能太多，
        # 否则「帮我把今日行情做成表格」这类请求会被劫持，LLM 就收不到了
        rest = text
        for t in triggers:
            rest = rest.replace(t, "")
        rest = re.sub(r"[，,。.!！?？~～、:：；;]+", "", rest)
        if len(rest) > 6:
            return

        # 群聊里必须 @ 机器人或使用唤醒前缀，避免抢答别人聊天
        allowed = False
        try:
            allowed = bool(event.is_private_chat())
        except Exception:
            allowed = False
        if not allowed:
            allowed = bool(getattr(event, "is_at_or_wake_command", False))
        if not allowed and Comp is not None:
            try:
                allowed = any(isinstance(c, Comp.At) for c in event.message_obj.message)
            except Exception:
                allowed = False
        if not allowed:
            return

        # 兜底：发送者就是自己时直接跳过（防平台回显导致死循环）
        try:
            self_id = event.get_self_id()
            if self_id and str(self_id) == str(event.get_sender_id()):
                return
        except Exception:
            pass

        event.stop_event()
        for chunk in self.build_today_report():
            yield event.plain_result(chunk)
