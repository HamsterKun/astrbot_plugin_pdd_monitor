# -*- coding: utf-8 -*-
"""astrbot_plugin_pdd_monitor

拼多多关键词价格监控插件（浏览器取数版）。

/盯价 add 9700X —— 以 "9700X" 为关键词搜索 mobile.yangkeduo.com，
按价格排序后取列表里最便宜那件商品的「页面展示价」，作为该关键词的价格。
后台定时巡检，跌破历史最低价时主动推送到已绑定的会话（群）；
群里 @机器人 说「今日行情」，汇报所有关键词的今日价格与涨跌。

为什么开浏览器取数
    搜索页已经改成客户端渲染：服务端只返回空骨架，商品由前端 XHR 拉回后解密
    再渲染。请求要 anti_content 签名（606 字符，页面 JS 现算），响应是
    rc-res-data 网关密文（十万字符，解密器在按需加载的 chunk 里）—— 纯 HTTP
    复刻要逆向两套算法。所以插件开一个无头 Chromium，等它渲染完，再用注入的
    hook 把解密后的对象取出来（见 pdd_browser.py）。
    老路线抓 HTML 抠 window.rawData 的方式保留在 fetch_mode=http 里，仅供排查。

命中原生「系统繁忙 / 滑块验证」时按 1h→2h→4h→8h 指数退避，避免越撞越死。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import statistics
import time
import unicodedata
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote, unquote, urlencode, urlsplit

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:  # astrbot 不同版本对消息组件的导出路径保持一致，这里仍做一层保护
    import astrbot.api.message_components as Comp
except Exception:  # pragma: no cover
    Comp = None  # type: ignore

try:  # 浏览器取数模块。playwright 没装也能正常 import，只是用不了 browser 模式
    from .pdd_browser import BrowserError, BrowserFetcher, BrowserUnavailable
except ImportError:  # pragma: no cover - AstrBot 按文件加载插件时没有包上下文
    try:
        from pdd_browser import BrowserError, BrowserFetcher, BrowserUnavailable
    except Exception:  # 模块缺失不该拖垮整个插件
        BrowserError = BrowserFetcher = BrowserUnavailable = None  # type: ignore

try:  # WebUI 里的可视化登录
    from .login_web import RemoteLogin
except ImportError:  # pragma: no cover
    try:
        from login_web import RemoteLogin  # type: ignore
    except Exception:
        RemoteLogin = None  # type: ignore

PLUGIN_NAME = "astrbot_plugin_pdd_monitor"
DATA_FILE = "pdd_price.json"
# 取价口径或数据源变化时递增，读到旧版本的数据文件会直接重置
# v3：取价从开放接口换回 H5 搜索页，价格口径从「券后到手价」变成「页面展示价」
DATA_VERSION = 3

GOODS_URL = "https://mobile.yangkeduo.com/goods.html?goods_id={}"
SEARCH_URL = "https://mobile.yangkeduo.com/search_result.html"
HOME_URL = "https://mobile.yangkeduo.com/"

# 扫码登录最多等多久：一张二维码大约两分钟过期，留出扫码加确认的余量。
# 期间二维码到期会自动重截一张再发，不会让用户对着失效的码干等。
LOGIN_TIMEOUT = 180.0

# 扫码后可能弹「安全验证」（选一个用过的收货人姓名），这步只能人来点。
# 把题目发出去后等这么久，没人回就放弃，浏览器一起关掉。
VERIFY_TIMEOUT = 180.0

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
# 登录态之外、决定「像不像正常客户端」的几个字段。电脑端复制常常缺，
# 手机端整条复制才全；缺了更容易被判定成异常客户端（返回空列表的降级页）。
BASE_COOKIE_KEYS = ("api_uid", "webp", "pdd_user_id")

# 判断「这个 UA 是不是 iPhone Safari」，用于决定要不要发 Safari 才有的头
SAFARI_UA_RE = re.compile(r"Version/[\d.]+ (?:Mobile/\w+ )?Safari", re.I)


def build_headers(ua: Optional[str], referer: Optional[str] = None) -> dict[str, str]:
    """拼一套自洽的浏览器请求头。

    刻意不设 Accept-Encoding：交给底层库按已装的解码器决定（httpx 有 brotli 就发 br）。
    手动写死 `gzip, deflate` 而 UA 声称是 Safari，等于自报「我不是浏览器」——
    Safari 一定会发 br。

    ua 传 None 时不写 User-Agent，交给 curl_cffi 的 impersonate：
    UA 版本与 TLS/h2 指纹是配套的，自己另写一个版本号会拼出浏览器里不存在的组合。
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
    }
    if ua:
        headers["User-Agent"] = ua
    if referer:
        headers["Referer"] = referer
    return headers


def mask_proxy(url: str) -> str:
    """代理地址脱敏：藏掉账号密码，IPv4 只留前两段。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        auth = f"{parts.username}:***@" if parts.username else ""
        nums = host.split(".")
        if len(nums) == 4 and all(n.isdigit() for n in nums):
            host = ".".join(nums[:2] + ["*", "*"])
        return f"{parts.scheme}://{auth}{host}{port}"
    except Exception:
        return "已配置"


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
# 综合排序：不带 sort_type。拼多多自己的推荐排序，相关性权重高，
# 拿它当价格基准比拿升序页靠谱得多（升序页会被最便宜的一批占满）。
SORT_DEFAULT = ""

# 基准价缓存时长：基准只看「这个型号大致多少钱」，半天内的漂移可以忽略。
# 缓存住它，稳态下巡检仍然是每个关键词一次请求，只有过期那一轮才多打一次。
BASELINE_TTL = 6 * 3600
# 基准下沿占中位数的百分比：低于这个比例视为「不是同一档商品」。
# 取 60 而不是更严：升序页本来就该比基准页便宜一截，卡到七成会把正常促销剔掉。
# 真正区分近似型号靠型号整串校验（见 model_must_match），这道只拦配件、引流 SKU。
DEFAULT_BASELINE_FLOOR_PCT = 60

# 型号词：3~5 位数字 + 可选字母后缀，如 5070ti、9700x、4060。
# 不取小写字母前缀：卖家写「RTX5070Ti」也常写「5070Ti 显卡」，带上前缀就比不中。
# 两侧不吃紧邻数字，免得「5070」匹配到「50700mAh」这种规格。
MODEL_TOKEN_RE = re.compile(r"(?<!\d)\d{3,5}[a-z]{0,3}(?!\d)", re.I)

# 价格字段候选：整数分优先，字符串价（带 ¥ 或千分位）兜底
# 搜索接口（items[].item_data）用 snake_case，商品详情页的 rawData 用 camelCase，
# 两套都收进来，靠顺序保证「拼单价」优先于「单买价」。
PRICE_KEYS = (
    "price",
    "groupPrice",
    "group_price",
    "minGroupPrice",
    "min_group_price",
    "minOnSaleGroupPrice",
    "min_on_sale_group_price",
    "priceInfo",
    "priceStr",
    "priceText",
    # 单买价（比拼单价高）只在前面的都没有时兜底，别让它盖掉真实底价
    "minNormalPrice",
    "min_normal_price",
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


def model_key(keyword: str) -> str:
    """取出关键词里的型号词，有多段取最长的。

    「5070ti」整段是型号，「5070」只是它的一半；取最长的那个才分得开。
    只认 3 位以上数字的串，避免把 16g、d5 这类规格当成型号去硬卡。
    """
    hits = [m.group(0).lower() for m in MODEL_TOKEN_RE.finditer(keyword or "")]
    if not hits:
        return ""
    return max(hits, key=len)


def squash(text: str) -> str:
    """抹平空格与分隔符、统一小写，只用于型号整串比对。

    卖家写「RTX 5070 Ti」「5070ti」「5070-TI」是同一个型号，
    保留空格就没法比；中文括号与顿号也一并去掉。
    """
    return re.sub(r"[\s\-_/.+·、,，()（）\[\]【】]+", "", (text or "").lower())


# 配件/引流词：标题里出现、而关键词里没有时，说明这条不是完整商品。
# 「选配」是拼多多常见的引流 SKU（一个链接挂多个型号，标的是最低配的价格），
# 它价格低、标题又含型号，是短关键词下最容易误选的一类。
ACCESSORY_WORDS = (
    "配件", "适用", "兼容", "专用", "转接", "扣具", "底座", "支架",
    "硅脂", "导热", "硅胶", "选配", "单拍", "定金", "补差", "邮费",
    "替换", "替代", "维修", "清洁", "工具", "仅适配",
)
# 降权系数：命中配件词的那条，重合度打对折，让完整商品优先
ACCESSORY_PENALTY = 0.5


def accessory_penalty(title: str, keyword_tokens: set[str]) -> float:
    """标题像配件就降权，但如果关键词本身就含配件词（如「手机壳」），不降。"""
    text = title or ""
    if not text:
        return 1.0
    if any(word in keyword_tokens for word in ACCESSORY_WORDS):
        return 1.0
    return ACCESSORY_PENALTY if any(word in text for word in ACCESSORY_WORDS) else 1.0


# --------------------------------------------------------------------------- #
# 价格离群剔除
# --------------------------------------------------------------------------- #
def price_band(
    items: list[dict],
    k: float = 3.0,
    min_drop_ratio: float = 0.3,
) -> dict[str, Any]:
    """算一组商品的价格带：中位数与可接受下沿（分）。

    单独抽出来是因为它有两个用处：
    - 同一批结果内部剔除配件、引流 SKU（filter_price_outliers）
    - 拿综合排序那批「推荐算法筛过的真实商品」当尺子，去校准价格升序那批
      （升序页会被最便宜的一批占满，本身没有可比样本）

    返回 mode：
    - `ok`  算出下沿，cutoff 有值
    - `thin` 样本太少或价格没有区分度，cutoff 为 0，调用方不该据此过滤
    """
    total = len(items)
    if total < 6:
        return {"mode": "thin", "total": total, "cutoff": 0, "median": 0,
                "floor": 0, "note": f"仅 {total} 条，样本太少"}

    logs = [math.log(max(1, int(i.get("price_cents") or 1))) for i in items]
    center = statistics.median(logs)
    mad = statistics.median([abs(x - center) for x in logs])
    if mad <= 1e-9:
        # 价格几乎完全一致（或绝大多数一致），给一个极小的尺度兜底。
        # 这时阈值贴着中位数，下限几乎等于中位数本身，不会误剔任何一侧。
        mad = 0.01

    floor = math.exp(center - k * mad)
    median_price = math.exp(center)
    # 光靠 MAD 不够：价格挤得很紧的时候门槛会贴到中位数身上。
    # 实测搜「9600X」的 20 条里 18 条是「CPU+主板」套装，全挤在 @1645~1899，
    # 于是 ¥1049 的裸 CPU 反倒成了统计学上的离群值 —— 而那正是我们要的最低价。
    # 所以再加一道比例闸门：只有「明显低于主体」才剔。配件那种 ¥3.99 对 ¥1000
    # 差三个数量级，能拦住；¥1049 对 ¥1800 只差 0.58 倍，放过。
    cutoff = min(floor, median_price * max(0.05, float(min_drop_ratio)))
    return {
        "mode": "ok",
        "total": total,
        "median": int(round(median_price)),
        "floor": int(round(floor)),
        "cutoff": int(round(cutoff)),
    }


def filter_price_outliers(
    items: list[dict],
    k: float = 3.0,
    min_samples: int = 6,
    min_keep_ratio: float = 0.15,
    min_drop_ratio: float = 0.3,
) -> tuple[list[dict], dict[str, Any]]:
    """把低价离群的商品剔掉（对数空间的中位数绝对偏差）。

    为什么在对数空间：商品价格右偏严重，¥3.99 到 ¥3000 差 750 倍，
    线性尺度下几个高价就能把阈值撑开，反而剔不掉低价配件。

    为什么用 MAD 而不是标准差或 IQR：
    - 标准差会被离群点本身带偏，越离谱的点反而越剔不掉；
    - IQR 在价格跨度大时窗口过宽（实测 1.5×IQR 一条都剔不掉），
      而 MAD 只看「偏离中位数多远」，对「少量配件混进大堆正品」更灵敏。

    为什么只剔下限、不剔上限：目标是取最低价，高价离群（套装、多件装）
    不影响结果，剔了反而可能误伤；低价离群（配件、引流 SKU）才会把
    「最低价」拉到完全无关的价位。而且只剔单边，能避开「配件数量远超
    正品时，把少量正品当离群剔掉」这个致命误判。

    两道保护：
    - 样本少于 min_samples 不做（统计量不可靠）
    - 保留比例低于 min_keep_ratio 视为剔除过度，放弃剔除

    参数 k 越大越宽容：2 严格、3 标准、4 宽松。
    """
    total = len(items)
    if not items:
        return items, {"outlier_mode": "off", "outlier_total": 0}

    band = price_band(items, k=k, min_drop_ratio=min_drop_ratio)
    if band["mode"] != "ok":
        return items, {
            "outlier_mode": "skip",
            "outlier_total": total,
            "outlier_note": f"{band.get('note') or '样本不足'}，不做剔除",
        }
    cutoff = band["cutoff"]
    kept = [i for i in items if i.get("price_cents", 0) >= cutoff]
    dropped = total - len(kept)

    if not dropped:
        return items, {
            "outlier_mode": "skip",
            "outlier_total": total,
            "outlier_note": "价格分布连续，没有可剔除的离群项",
        }

    if len(kept) < max(3, int(total * min_keep_ratio)):
        return items, {
            "outlier_mode": "skip",
            "outlier_total": total,
            "outlier_note": f"价格过于分散，剔除会伤到主体（只剩 {len(kept)}/{total}），已放弃",
        }

    return kept, {
        "outlier_mode": "mad",
        "outlier_total": total,
        "outlier_kept": len(kept),
        "outlier_dropped": dropped,
        "outlier_floor": band["floor"],
        "outlier_cutoff": cutoff,
    }


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

# 本插件自己发出的文案特征，用于识别平台回显、避免自问自答。
# 所有对外文案都以 TAG 开头，所以一条就够；后面两条兼容旧版本留下的消息。
SELF_MARKS = (
    "【多多盯价】",
    "【今日行情】",
    "【史低提醒】",
)

# 指令名刻意用中文：QQ 客户端会把「/pd」这类前缀转成表情，英文缩写容易被打断
PRIMARY_CMD = "盯价"
CMD_ALIASES = ("多多", "拼多多", "价格监控", "比价")
CMD_NAMES = (PRIMARY_CMD,) + CMD_ALIASES

# 所有对外文案统一以它开头，一眼能看出是哪个插件发的
TAG = "【多多盯价】"


# --------------------------------------------------------------------------- #
# 文案格式
#
# 统一体例：
#   首行   【多多盯价】动作 [· 对象]
#   正文   (标签, 值) 两列，标签按显示宽度补齐
#   短回执 【多多盯价】动作 · 对象
#
# 不用「已经帮你…」「请注意…」这类句子，只留状态、数值和可执行的下一步。
# --------------------------------------------------------------------------- #
def dwidth(text: str) -> int:
    """显示宽度：全角/宽字符占 2 列，半角占 1 列。对齐全靠它。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(text))


def pad(text: str, width: int) -> str:
    """按显示宽度右侧补空格。"""
    text = str(text)
    return text + " " * max(0, width - dwidth(text))


def head(action: str, *extra: Any) -> str:
    """首行：动作 + 可选的对象/状态。"""
    line = f"{TAG}{action}"
    tail = [str(x) for x in extra if x not in (None, "")]
    return line + (" · " + " · ".join(tail) if tail else "")


def rows_block(rows: list[tuple[str, str]], width: int = 8) -> str:
    """把 (标签, 值) 排成两列。值里有换行时按同样缩进续行。"""
    lines: list[str] = []
    for label, value in rows:
        value = str(value)
        parts = value.split("\n")
        lines.append(f"{pad(label, width)}  {parts[0]}")
        for extra in parts[1:]:
            lines.append(f"{' ' * width}  {extra}")
    return "\n".join(lines)


def split_error(msg: str) -> tuple[str, str, str]:
    """把「现象｜原因｜处理」拆开，缺项返回空串。"""
    parts = [p.strip() for p in str(msg or "").split("｜")]
    parts += [""] * (3 - len(parts))
    return parts[0], parts[1], "｜".join(p for p in parts[2:] if p)


def error_rows(msg: str) -> list[tuple[str, str]]:
    """错误拆成「现象 / 原因 / 处理」三行，缺哪段就不显示哪行。

    留一个空标签比不显示更糟：用户会以为信息在，只是没加载出来。
    """
    first, reason, action = split_error(msg)
    rows = [("现象", first)] if first else []
    if reason:
        rows.append(("原因", reason))
    if action:
        rows.append(("处理", action))
    return rows


HELP_TEXT = "\n".join([
    f"{TAG}指令",
    "",
    f"{pad('add <关键词>', 16)}添加监控",
    f"{pad('login', 16)}登录（在面板里操作）",
    f"{pad('login qr', 16)}聊天里扫码登录",
    f"{pad('验证 <序号>', 16)}聊天扫码时的安全验证",
    f"{pad('del <序号>', 16)}删除监控项",
    f"{pad('list', 16)}列出全部监控",
    f"{pad('info <序号>', 16)}单项详情",
    f"{pad('check', 16)}立即巡检一次",
    f"{pad('reset', 16)}清除风控冷却",
    f"{pad('debug <关键词>', 16)}取价诊断",
    f"{pad('on | off', 16)}开 / 关本群新低推送",
    f"{pad('help', 16)}本页",
    "",
    "@机器人 说「今日行情」查看全部现价。",
])



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


def _ago_text(ts: float) -> str:
    """「多久以前」，只到分钟/小时，诊断里不需要更细。"""
    if not ts:
        return "未知"
    secs = max(0, int(time.time() - float(ts)))
    if secs < 60:
        return "刚刚"
    if secs < 3600:
        return f"{secs // 60} 分钟前"
    return f"{secs // 3600} 小时前"


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

    def __init__(self, message: str = "", risk: bool = False, html: str = ""):
        super().__init__(message)
        self.risk = risk
        # 出错时的页面原文，只给 /盯价 debug 落盘快照用，巡检路径不读它
        self.html = html


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
        self._migrate_flat_config()
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
        # 浏览器取数的实例，第一次用到时才启动（Chromium 起一次一两秒）
        self._fetcher = None
        self._fetcher_lock = asyncio.Lock()
        self._warm_task: Optional[asyncio.Task] = None
        self._browser_hint = ""
        # 扫码登录期间的状态：
        #   _login_busy  同一时间只跑一轮，重复发指令会互相顶掉浏览器
        #   _verify_*    扫码后的「安全验证」把题目发出去，等 /盯价 验证 <序号> 回来
        self._login_busy = False
        self._verify_event: Optional[asyncio.Event] = None
        self._verify_answer: Optional[int] = None
        self._verify_count = 0
        # WebUI 里的可视化登录会话；_web_ok 表示当前 AstrBot 支持插件 Web API
        self._remote = None
        self._web_ok = False
        self._register_web()

    # ---------------- WebUI：可视化登录 ----------------
    def _register_web(self) -> None:
        """把可视化登录的接口挂到 AstrBot 的 Dashboard 路由上。

        只有 4.x 才有 `register_web_api`；老版本没有这个能力，这里安静跳过，
        `/盯价 login qr` 那条聊天扫码的路仍然可用。
        """
        if RemoteLogin is None:
            logger.warning("[pdd] login_web 模块缺失，可视化登录不可用")
            return
        try:
            from astrbot.api.web import json_response  # noqa: F401
        except Exception as e:
            logger.info(f"[pdd] 当前 AstrBot 没有插件 Web API（{e}），可视化登录跳过")
            return

        routes = [
            (f"/{PLUGIN_NAME}/login/state", self._web_login_state,
             ["GET"], "登录会话状态"),
            (f"/{PLUGIN_NAME}/login/start", self._web_login_start,
             ["POST"], "开始登录"),
            (f"/{PLUGIN_NAME}/login/frame", self._web_login_frame,
             ["GET"], "登录页画面"),
            (f"/{PLUGIN_NAME}/login/input", self._web_login_input,
             ["POST"], "转发鼠标键盘"),
            (f"/{PLUGIN_NAME}/login/close", self._web_login_close,
             ["POST"], "结束登录会话"),
        ]
        ok = 0
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(path, handler, methods, desc)
                ok += 1
            except Exception as e:
                logger.warning(f"[pdd] 注册 Web 路由失败 {path}：{e}")
        self._web_ok = ok == len(routes)
        if self._web_ok:
            logger.info("[pdd] 可视化登录已就绪：面板 → 插件 → 多多盯价 → 多多盯价登录")

    async def _web_login_state(self):
        from astrbot.api.web import json_response

        remote = self._remote
        if remote is None:
            return json_response({"status": "idle", "message": "还没有会话",
                                  "alive": False, "left": 0})
        return json_response(remote.snapshot())

    async def _web_login_start(self):
        from astrbot.api.web import error_response, json_response

        old = self._remote
        if old is not None:
            if old.alive():
                return json_response(old.snapshot())
            # 上一轮已经出结果但还在善后期（成功后 5 秒才退进程），
            # 先把它收干净，免得同时挂着两台 Chromium
            self._remote = None
            try:
                await old.close()
            except Exception as e:
                logger.debug(f"[pdd] 收上一轮登录会话出错（忽略）：{e}")
        if self._fetch_mode() != "browser":
            return error_response("当前取数方式是 http，没有浏览器可用", status_code=409)
        try:
            fetcher = self._build_fetcher(cookie="")
        except Exception as e:
            return error_response(f"浏览器参数有误：{e}", status_code=500)

        remote = RemoteLogin(fetcher, self._remote_success)
        self._remote = remote
        try:
            await remote.start()
        except Exception as e:
            self._remote = None
            try:
                await remote.close()
            except Exception:
                pass
            return error_response(f"打开登录页失败：{e}", status_code=500)
        return json_response(remote.snapshot())

    async def _web_login_frame(self):
        import base64

        from astrbot.api.web import error_response, json_response

        remote = self._remote
        if remote is None:
            return error_response("没有进行中的登录会话", status_code=409)
        snap = remote.snapshot()
        if not remote.alive():
            # 已经出结果（成功/超时/被关）。这里照样回快照、只是不带画面，
            # 前端拿到 status 就能切到「登录成功」那张卡，而不是突然断流。
            return json_response(snap)
        shot = await remote.frame()
        if not shot:
            return error_response("这一帧没截到", status_code=503)
        snap["img"] = base64.b64encode(shot).decode()
        return json_response(snap)

    async def _web_login_input(self):
        from astrbot.api.web import error_response, json_response, request

        remote = self._remote
        if remote is None or not remote.alive():
            return error_response("没有进行中的登录会话", status_code=409)
        payload = await request.json(default={})
        ok = await remote.send(payload if isinstance(payload, dict) else {})
        return json_response({"ok": bool(ok)})

    async def _web_login_close(self):
        from astrbot.api.web import json_response

        remote, self._remote = self._remote, None
        if remote is not None:
            try:
                await remote.close()
            except Exception as e:
                logger.debug(f"[pdd] 关登录会话出错（忽略）：{e}")
        return json_response({"status": "closed"})

    def _remote_success(self, cookie: str) -> None:
        """可视化登录拿到 cookie：和聊天扫码那条路写的是同一份配置。"""
        self.set_cfg("cookie", cookie)
        # 旧会话里还留着上一个账号的 PDDAccessToken，jar 的优先级高于配置
        self._jar.clear()
        self.store.data.setdefault("meta", {})["cookie_jar"] = {}
        self.store.save()
        self._last_url = ""
        self._browser_hint = ""
        logger.info("[pdd] 可视化登录成功，cookie 已写入配置")

    # ---------------- 基础 ----------------
    def cfg(self, key: str, default: Any = None) -> Any:
        """读配置。顶层没有就翻到分区里找。

        配置项按用途收进了几组 type=object 的嵌套对象（见 _conf_schema.json），
        读取方不该关心某项被放进了哪一组，所以这里统一兜住，调用处一律 cfg("名字")。
        """
        try:
            value = self._dig(self.config, key)
        except Exception:
            return default
        return default if value is None else value

    def _dig(self, node: Any, key: str) -> Any:
        """在配置里找 key：先看本层，再逐层往下。

        先本层再下钻，是为了让「顶层还留着旧键」时以顶层为准——迁移没落盘
        （AstrBot 只补默认值、不删旧键）也不至于读到另一份值。
        """
        if not isinstance(node, dict):
            return None
        if key in node:
            return node[key]
        for value in node.values():
            if isinstance(value, dict):
                got = self._dig(value, key)
                if got is not None:
                    return got
        return None

    def _cfg_groups(self) -> list[dict]:
        """配置里的分区：schema 中用 type=object 包起来的那几组。"""
        try:
            return [v for v in list(self.config.values()) if isinstance(v, dict)]
        except Exception:
            return []

    def _cfg_parent(self, key: str, node: Any = None) -> Optional[dict]:
        """找到配置项所在的那一层。分区化之后，值可能躺在某个 object 组里。"""
        if node is None:
            node = self.config
        if not isinstance(node, dict):
            return None
        if key in node:
            return node
        for value in node.values():
            if isinstance(value, dict):
                found = self._cfg_parent(key, value)
                if found is not None:
                    return found
        return None

    def set_cfg(self, key: str, value: Any) -> bool:
        """写配置并落盘。写回它原本待的那一层，不另造一个顶层副本。

        另造副本的后果是：cfg() 读顶层拿到新值，面板上显示的还是分组里那份旧的，
        两边长期不一致，下次改配置还会被旧值盖回去。
        """
        parent = self._cfg_parent(key)
        try:
            (parent if parent is not None else self.config)[key] = value
        except Exception as e:
            logger.warning(f"[pdd] 写配置失败：{e}")
            return False
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as e:
                logger.warning(f"[pdd] 配置保存失败：{e}")
                return False
        return True

    def _migrate_flat_config(self) -> None:
        """把旧的平铺配置搬进分区。

        AstrBot 升级插件时只按 schema 递归补全缺的默认值，不会删掉配置文件里
        多余的旧键。若不搬：页面上改的是分组里的值，程序读到的却是顶层那份旧的，
        表现为「改了没反应」。启动时搬一次并落盘，之后就只剩分组这一份。
        """
        moved = 0
        for group in self._cfg_groups():
            for key in [k for k in group if k in self.config]:
                group[key] = self.config.pop(key)
                moved += 1
        if not moved:
            return
        save = getattr(self.config, "save_config", None)
        if callable(save):
            try:
                save()
            except Exception as e:  # 存不下不影响本次运行，下次启动会再搬一次
                logger.warning(f"[pdd] 配置迁移后保存失败：{e}")

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

    def _cookie_or_empty(self) -> str:
        """读 cookie，没有就返回空串，不报错。

        扫码登录就是用来产生 cookie 的，那一刻还没有是正常的——在这里拦下来
        等于「要先有 cookie 才能登录」，死循环。取数仍然走 `_cookie()`，那个才该拦。
        """
        raw = str(self.cfg("cookie", "") or "").strip()
        return normalize_cookie(raw) if raw else ""

    def _user_agent(self) -> str:
        """UA 可配：填成你取 cookie 那台手机的同款 UA 最安全。"""
        return str(self.cfg("user_agent", "") or "").strip() or DEFAULT_UA

    def _ua_header(self, curl_ok: bool) -> Optional[str]:
        """真正写进请求头的 UA。用 curl_cffi 且用户没填时返回 None。

        curl_cffi 的 impersonate 会把 UA、TLS、HTTP/2 设置作为一整套改掉；
        我们再手写一个版本号，就成了「Safari 的指纹 + 另一个版本号的 UA」，
        这种组合在真实浏览器里不存在，反而是破绽。
        """
        custom = str(self.cfg("user_agent", "") or "").strip()
        if custom:
            return custom
        return None if curl_ok else DEFAULT_UA

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
                    logger.warning(
                        "[pdd] curl_cffi 没装上（它是默认依赖），已回落 httpx，"
                        "TLS 指纹不会被伪装；在 AstrBot 的运行环境执行 "
                        "pip install curl_cffi 后重载插件即可"
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
        proxy = self._proxy()
        last_err: Optional[Exception] = None
        for name in candidates:
            if not name:
                continue
            try:
                # Cookie 统一走请求头，不用 curl 自己的 cookie 引擎：
                # 两条传输层用同一套 jar 逻辑，也不会因为 API 差异静默丢 cookie
                kwargs = {"impersonate": name, "timeout": timeout}
                if proxy:
                    try:
                        self._curl = AsyncSession(proxy=proxy, **kwargs)
                    except TypeError:  # 老版本只认 proxies 字典
                        self._curl = AsyncSession(
                            proxies={"http": proxy, "https": proxy}, **kwargs
                        )
                else:
                    self._curl = AsyncSession(**kwargs)
                self._transport = f"curl_cffi/{name}"
                logger.info(f"[pdd] 传输层：curl_cffi（模拟 {name} 的 TLS 指纹）")
                return self._curl
            except Exception as e:  # impersonate 名字不被这个版本支持
                last_err = e
                self._curl = None
        logger.warning(f"[pdd] curl_cffi 初始化失败，回落 httpx：{last_err}")
        self._transport_choice = False
        return None

    def _proxy(self) -> str:
        """出口代理。留空直连。

        跑在阿里云/腾讯云这类机房时，出口 IP 属于云厂商网段，平台对这类 IP
        的信任度明显低于家宽——典型症状就是浏览器能搜、同一份 cookie 换到
        服务器上就返回「系统繁忙」的降级页。
        """
        raw = str(self.cfg("proxy", "") or "").strip()
        if not raw:
            return ""
        return raw if "://" in raw else "http://" + raw

    def _proxy_line(self) -> str:
        """代理行的「值」部分（调用方自己带「代理」标签）。"""
        proxy = self._proxy()
        if not proxy:
            return "未配置 · 直连，机房 IP 容易被按网段限流"
        return mask_proxy(proxy)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            timeout = float(self.cfg("request_timeout", 15))
            proxy = self._proxy()
            kwargs = {
                "timeout": httpx.Timeout(timeout),
                "follow_redirects": True,
                "http2": False,
            }
            try:
                # httpx >= 0.26 用 proxy，旧版用 proxies 字典
                self._client = httpx.AsyncClient(proxy=proxy or None, **kwargs)
            except TypeError:
                self._client = httpx.AsyncClient(
                    proxies={"http://": proxy, "https://": proxy} if proxy else None,
                    **kwargs,
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
        # 先定传输层，再拼头：走 curl_cffi 时 UA 交给 impersonate 自己带
        curl = await self._get_curl() if self._use_curl() else None
        headers = build_headers(self._ua_header(curl is not None), referer)
        jar = self._session_cookies()
        if jar:
            headers["Cookie"] = format_cookie(jar)

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
            headers = build_headers(self._ua_header(False), None)
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

    # ---------------- 浏览器取数 ----------------
    def _fetch_mode(self) -> str:
        mode = str(self.cfg("fetch_mode", "browser") or "browser").strip().lower()
        return mode if mode in ("browser", "http", "auto") else "browser"

    def _idle_seconds(self) -> float:
        """空闲回收阈值，秒。0 表示不按空闲回收。"""
        try:
            minutes = float(self.cfg("browser_idle_minutes", 10) or 0)
        except (TypeError, ValueError):
            minutes = 10.0
        return max(0.0, minutes) * 60.0

    def _max_searches(self) -> int:
        """取数次数回收阈值。0 表示不按次数回收。"""
        try:
            return max(0, int(self.cfg("browser_max_searches", 12) or 0))
        except (TypeError, ValueError):
            return 12

    def _apply_recycle(self, fetcher: Any) -> None:
        """把当前配置里的阈值同步给已有实例。

        实例是复用的，阈值在构造时定死。不同步的话改完配置要重载插件才生效，
        而这类值多半是「内存涨了就先调小试试」——当场生效才有意义。

        cookie 与 UA 也在这里同步：扫码登录成功时实例早就建好了（当时还没有
        cookie），不同步就会一直拿旧凭证取数，表现为「登录了但没生效」。
        """
        try:
            fetcher.idle_timeout = self._idle_seconds()
            fetcher.max_searches = self._max_searches()
            fetcher.warm_home = bool(self.cfg("warmup_home", True))
            fetcher.cookie = self._cookie_or_empty()
            fetcher.user_agent = self._user_agent()
        except Exception:
            pass

    async def _get_fetcher(self, require_cookie: bool = True):
        """惰性启动浏览器实例，之后每轮复用。

        Chromium 起一次要一两秒，每轮都开关会把巡检拖成分钟级；
        但上下文（cookie + UA）每次新建，避免页面之间互相沾染。

        require_cookie=False 供扫码登录用：那时候还没有 cookie，不能在这里拦。
        """
        if BrowserFetcher is None:
            raise PddFetchError(
                "找不到 pdd_browser 模块，插件文件不完整。请整目录重新上传安装。"
            )
        cookie = self._cookie() if require_cookie else self._cookie_or_empty()
        if self._fetcher is not None:
            self._apply_recycle(self._fetcher)
            return self._fetcher

        # 巡检和 /盯价 debug 可能撞在一起：不加锁会各起一个 Chromium，
        # 后一个把前一个的引用顶掉，前一个的进程就再也没人关了。
        # 单独一把锁，不能复用 self._lock —— 巡检本身就是持着它进来的。
        async with self._fetcher_lock:
            if self._fetcher is not None:
                self._apply_recycle(self._fetcher)
                return self._fetcher

            fetcher = self._build_fetcher(cookie)
            try:
                await fetcher.start()
            except BrowserUnavailable as e:
                self._browser_hint = "没装 playwright"
                raise PddFetchError(str(e)) from e
            except Exception as e:
                self._browser_hint = str(e)[:80]
                raise PddFetchError(f"浏览器启动失败：{e}") from e
            self._fetcher = fetcher
            return fetcher

    def _build_fetcher(self, cookie: str):
        """按当前配置造一个浏览器实例（不启动）。

        取数和可视化登录都从这里拿参数，省得两边各写一份、改一边忘一边。
        """
        if BrowserFetcher is None:
            raise PddFetchError(
                "找不到 pdd_browser 模块，插件文件不完整。请整目录重新上传安装。"
            )
        return BrowserFetcher(
            proxy=self._proxy(),
            cookie=cookie,
            channel=str(self.cfg("browser_channel", "") or ""),
            headless=bool(self.cfg("browser_headless", True)),
            timeout=float(self.cfg("browser_timeout", 45) or 45),
            auto_install=bool(self.cfg("browser_auto_install", True)),
            idle_timeout=self._idle_seconds(),
            max_searches=self._max_searches(),
            warm_home=bool(self.cfg("warmup_home", True)),
            user_agent=self._user_agent(),
            on_log=lambda m: logger.debug("[pdd] %s", m),
        )

    async def _browser_raw(self, keyword: str, sort: Optional[str] = None) -> dict:
        """走浏览器取一份解密后的搜索数据。

        sort 显式传空串表示综合排序（不带 sort_type），不能和「没传」混为一谈，
        所以这里按 None 判而不是按真假判。
        """
        fetcher = await self._get_fetcher()
        if sort is None:
            sort_type = str(self.cfg("search_sort", SORT_ASC) or SORT_ASC).strip()
        else:
            sort_type = str(sort).strip()
        try:
            return await fetcher.search(keyword, sort=sort_type)
        except BrowserError as e:
            msg = str(e)
            # 「系统繁忙」才是真的限流，要退避；跳登录页属于配置问题，退避没用
            risk = ("系统繁忙" in msg) or ("限流" in msg)
            raise PddFetchError(msg, risk=risk) from e

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
            # 列表元素是包装层时拆开。搜索接口返回的是
            #   [{item_type: "0", item_data: {goods_model: {goods_name, price, ...}}}]
            # 商品埋得比一层还深，所以要往下钻；只拆一层会一无所获。
            wrapped = []
            for x in data:
                if not isinstance(x, dict):
                    continue
                hit = PDDMonitorPlugin._dig_goods(x, 3)
                if hit is not None:
                    wrapped.append(hit)
            if wrapped and (len(wrapped) >= 2 or len(data) <= 2):
                return wrapped
            for value in data:
                found = PDDMonitorPlugin._find_goods_list(value, depth + 1)
                if found:
                    return found
        return []

    @staticmethod
    def _dig_goods(node: Any, depth: int) -> Optional[dict]:
        """在包装层里往下找那个真正带商品字段的 dict。

        包装层的层数不固定（item_data → goods_model 已经两层），所以按特征找，
        不按固定路径取。找到最浅的那个就返回。
        """
        if depth <= 0 or not isinstance(node, dict):
            return None
        if is_goods_item(node):
            return node
        for value in node.values():
            if isinstance(value, dict):
                hit = PDDMonitorPlugin._dig_goods(value, depth - 1)
                if hit is not None:
                    return hit
        return None

    @staticmethod
    def _unwrap_goods(data: Any, depth: int = 0) -> list[dict]:
        """兜底解析：商品被包了一层（{goods:{...}} / {item:{...}}）时拆出来。

        页面改版时最省事的做法是把商品塞进一个包装字段，列表元素本身就不再
        带 goodsName 了，这时按特征找列表会一无所获，得往里再认一层。
        """
        if depth > 6:
            return []
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    for inner_key in ("goods", "item", "product", "goodsInfo", "goods_info"):
                        if not is_goods_item(value[0].get(inner_key)):
                            continue
                        out = [
                            x[inner_key]
                            for x in value
                            if isinstance(x, dict) and is_goods_item(x.get(inner_key))
                        ]
                        if out:
                            return out
                elif isinstance(value, dict):
                    found = PDDMonitorPlugin._unwrap_goods(value, depth + 1)
                    if found:
                        return found
        return []

    @staticmethod
    def _list_report(
        data: Any, path: str = "", depth: int = 0, out: Optional[list[dict]] = None
    ) -> list[dict]:
        """把 rawData 里所有列表的路径、长度、首条字段名收集出来，只用于诊断。

        「没找到商品列表」有两种完全不同的原因：一是这个词真的搜不到东西，
        二是页面改版、字段改名。光看错误信息分不出来，得看 rawData 里到底
        有没有列表、列表是空的还是元素不认识。
        """
        if out is None:
            out = []
        if depth > 6 or len(out) >= 12:
            return out
        if isinstance(data, dict):
            for key, value in data.items():
                child = f"{path}.{key}" if path else str(key)
                if value is None and str(key) in ("list", "items", "goods_list"):
                    # null 和 [] 一样是「这个位置没数据」，诊断时要当成空列表
                    out.append({"path": child, "size": 0, "keys": []})
                    if len(out) >= 12:
                        break
                    continue
                if isinstance(value, list):
                    first = value[0] if value else None
                    out.append(
                        {
                            "path": child,
                            "size": len(value),
                            "keys": sorted(map(str, first.keys()))[:8]
                            if isinstance(first, dict)
                            else [],
                        }
                    )
                    if len(out) >= 12:
                        break
                elif isinstance(value, dict):
                    PDDMonitorPlugin._list_report(value, child, depth + 1, out)
        elif isinstance(data, list):
            for i, value in enumerate(data[:3]):
                if isinstance(value, dict):
                    PDDMonitorPlugin._list_report(value, f"{path}[{i}]", depth + 1, out)
        return out

    @staticmethod
    def _raw_hint(data: Any, depth: int = 0) -> str:
        """把 rawData 压成一行结构摘要，例如 store{data{ssrListData{list[0]}}}。"""
        if depth > 3 or not isinstance(data, dict):
            return ""
        parts = []
        for key, value in list(data.items())[:6]:
            if isinstance(value, dict):
                parts.append(f"{key}{{{PDDMonitorPlugin._raw_hint(value, depth + 1)}}}")
            elif isinstance(value, list):
                parts.append(f"{key}[{len(value)}]")
        return ",".join(parts)[:160]

    @staticmethod
    def _keyword_hint(keyword: str) -> str:
        """给搜不到的长词一个能立刻试的短版本（取第一个词，通常是型号）。"""
        parts = [p for p in re.split(r"\s+", (keyword or "").strip()) if p]
        if len(parts) <= 1:
            return "换个更常见的写法或同义词重试"
        return f"只留型号重试：/盯价 debug {parts[0]}"

    @staticmethod
    def _page_title(html: str) -> str:
        m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
        if not m:
            return ""
        return shorten(re.sub(r"\s+", " ", m.group(1)).strip(), 20)

    @staticmethod
    def _raw_state(html: str) -> str:
        """rawData 取不到时，说清是哪一种取不到。

        三种情况长得一样，处理办法却完全不同：
          根本没有 rawData 字段 → 服务端给的不是搜索页（登录页/验证页）
          rawData = null        → 页面是搜索页，但服务端把你当未登录
          有对象但没列表         → 页面结构变了，字段路径要重新对
        混成一句「rawData 缺失」等于什么都没说。
        """
        html = html or ""
        m = RAW_DATA_RE.search(html)
        if not m:
            return "页面里根本没有 rawData 字段（返回的不是搜索页）"
        pos = m.end()
        while pos < len(html) and html[pos] in " \t\r\n=":
            pos += 1
        if html[pos:pos + 4].lower().startswith("null"):
            return "rawData = null（服务端把你当未登录处理）"
        if pos < len(html) and html[pos] == "{":
            return "rawData 是对象但没能解析出来（页面可能被截断，或格式有变）"
        return "rawData 的赋值不是对象（页面可能被截断）"

    def _raw_missing_error(self, keyword: str, html: str) -> PddFetchError:
        """把 rawData 缺失的三种情况，各给一条能立刻动手的建议。"""
        state = self._raw_state(html)
        m = RAW_DATA_RE.search(html or "")
        if not m:
            action = ("返回的不是搜索页，多半被跳到登录页或验证页｜"
                      "重新复制整条 Cookie 头（11 个字段那个）再试")
        else:
            pos = m.end()
            while pos < len(html) and html[pos] in " \t\r\n=":
                pos += 1
            if html[pos:pos + 4].lower().startswith("null"):
                action = ("服务端没认登录态，cookie 大概已过期｜重新复制整条 Cookie 头，别只挑字段")
            else:
                action = ("rawData 的 JSON 没解析出来，页面可能被截断｜重试一次；"
                          "仍如此则存 debug 快照核对结构")
        title = self._page_title(html)
        extra = f"（标题「{title}」）" if title else ""
        return PddFetchError(
            f"搜索页没有下发行情数据：{state}{extra}｜{action}",
            risk=True,
            html=html,
        )

    def _page_line(self, raw: Any, html: str) -> str:
        """一行说清这页到底长什么样：几个列表、最大那个几条、HTML 多大。

        「搜不到」和「页面改版」在聊天窗口里长得一样，有这个才能隔空判断。
        """
        size = f"{max(1, len(html) // 1024)} KB"
        if raw is None:
            parts = [f"rawData {self._raw_state(html)}", f"HTML {size}"]
            title = self._page_title(html)
            if title:
                parts.append(f"标题「{title}」")
            return " · ".join(parts)
        report = self._list_report(raw)
        if not report:
            return f"rawData 无任何列表 · HTML {size} · {self._raw_hint(raw) or '空对象'}"
        big = max(report, key=lambda r: r["size"])
        return (
            f"{len(report)} 个列表，最大 {big['size']} 条"
            f"（{shorten(big['path'], 30)}）· HTML {size}"
        )

    def _save_snapshot(self, keyword: str, html: str) -> str:
        """把出问题的页面存成文件，方便事后核对结构。只在 /盯价 debug 时调用。

        巡检路径不调用：一天几百轮，失败几次就会堆一堆几百 KB 的文件。
        """
        if not html:
            return ""
        safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", (keyword or "")).strip("_")[:40] or "page"
        try:
            folder = os.path.join(self._resolve_data_dir(), "debug")
            os.makedirs(folder, exist_ok=True)
            name = f"{safe}_{datetime.now().strftime('%m%d_%H%M%S')}.html"
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                f.write(html)
            return name
        except Exception as e:
            logger.warning(f"[pdd] 页面快照写入失败：{e}")
            return ""

    def _is_curl_transport(self) -> bool:
        """当前是否真的在模拟浏览器 TLS 指纹。"""
        name = self._transport or ("curl_cffi" if self._use_curl() else "httpx")
        return str(name).startswith("curl_cffi")

    def _no_goods_error(self, keyword: str, raw: Any, html: str = "") -> PddFetchError:
        """解释「为什么这页没有商品」，把 0 结果和页面改版分开说。"""
        # 限流页有时也带 rawData，只是列表空着；先按页面文字认出来，
        # 否则会被说成「这个词没货」，白折腾关键词
        if html and any(w in html for w in ("系统繁忙", "访问过于频繁", "稍后再试", "异常流量")):
            if self._is_curl_transport():
                action = "请求过密或该分类被重点限流｜等几小时后重试"
            else:
                action = ("当前跑在 httpx，TLS 指纹是 Python 默认值｜"
                          "pip install curl_cffi 后重载插件，transport 保持 auto")
            return PddFetchError(
                f"平台拒绝了这次搜索｜页面有「系统繁忙」但列表为空，不是关键词没货｜{action}",
                risk=True,
            )

        report = self._list_report(raw)
        empty = [r for r in report if r["size"] == 0]
        filled = [r for r in report if r["size"] > 0]
        # 搜索位（ssrListData 之类）是空的，基本就等于「这个词没货」
        search_empty = [
            r for r in empty if re.search(r"ssrListData|searchResult|goods", r["path"], re.I)
        ]

        if search_empty or (empty and not filled):
            target = (search_empty or empty)[0]
            note = ""
            if filled:
                note = (
                    f"，另有 {len(filled)} 个非空列表"
                    f"（{shorten(filled[0]['path'], 40)}）但条目不是商品"
                )
            return PddFetchError(
                f"搜到 0 条商品｜{shorten(target['path'], 40)} 为空{note}｜"
                f"{shorten(self._keyword_hint(keyword), 60)}"
            )

        if filled:
            total = sum(r["size"] for r in filled)
            keys = "、".join(filled[0]["keys"][:6]) or "空字段"
            return PddFetchError(
                f"有 {len(filled)} 个列表共 {total} 条数据，但没有一条带商品名｜"
                f"字段名可能改了（{shorten(filled[0]['path'], 40)} 首条：{keys}）｜"
                f"上报此条等待适配"
            )

        return PddFetchError(
            f"页面能解析但没有任何列表｜rawData：{self._raw_hint(raw) or '空对象'}｜"
            f"多半是 cookie 失效拿到空壳页，重新取一条 cookie"
        )


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
        self._warm_browser()

    @filter.on_astrbot_loaded()
    async def _on_loaded(self) -> None:
        # 兜底：确保定时巡检一定被启动
        self._start_worker()

    def _warm_browser(self) -> None:
        """加载插件时后台把浏览器拉起来，缺内核顺带下载。

        不做这个的话，首次取数要现场等 Chromium 启动 + 下载内核（一百多 MB），
        用户那条 /盯价 add 会卡上几分钟。放到后台，加载完就能慢慢准备好。
        失败不打断流程：真正的错误留到取数时再报，那时能带上上下文。
        """
        if self._fetch_mode() != "browser":
            return
        if not str(self.cfg("cookie", "") or "").strip():
            # 还没填 cookie 时预热必然失败，别在日志里刷一条没用的警告
            return
        if self._warm_task and not self._warm_task.done():
            return

        async def run() -> None:
            # 让插件加载先跑完，别和 AstrBot 自己的初始化抢事件循环
            await asyncio.sleep(3)
            try:
                await self._get_fetcher()
                logger.info("[pdd] 浏览器预热完成")
            except Exception as e:
                logger.warning(f"[pdd] 浏览器预热失败，首次取数时会再试一次：{e}")

        try:
            self._warm_task = asyncio.create_task(run())
        except RuntimeError:
            # 没有运行中的事件循环（测试里直接同步调用）就跳过预热
            self._warm_task = None

    async def terminate(self) -> None:
        if self._warm_task and not self._warm_task.done():
            self._warm_task.cancel()
            self._warm_task = None
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
        # 浏览器和本地中继都要收掉，否则重新加载插件时会攒一堆进程
        remote, self._remote = self._remote, None
        if remote is not None:
            try:
                await remote.close()
            except Exception as e:
                logger.debug(f"[pdd] 关登录会话出错（忽略）：{e}")
        if self._fetcher is not None:
            try:
                await self._fetcher.close()
            except Exception:
                pass
            self._fetcher = None
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
            # 一轮跑完就把浏览器退掉。两轮之间隔着半小时，让一个空转的 Chromium
            # 常驻占几百兆没有任何意义——下次要用时再拉起，也就一两秒。
            await self._idle_release_browser()
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

    async def _idle_release_browser(self) -> None:
        """巡检跑完把浏览器收掉，别让它空转占内存。

        Chromium 只要活着就占着几百兆（渲染进程 + V8 堆 + 缓存），关页面、
        关上下文都不还，只有进程退出才还。巡检间隔按半小时算，中间这段时间
        等于白占——不如退了，下次取数再拉起来，代价一两秒。
        """
        fetcher = self._fetcher
        if fetcher is None:
            return
        # 巡检或登录还在跑的时候别动它：手动 /盯价 check 可能正跟自动巡检叠着
        if self._lock.locked() or self._login_busy:
            return
        try:
            await asyncio.wait_for(fetcher.close(), timeout=30)
            logger.info("[pdd] 巡检结束，已关闭浏览器释放内存")
        except BaseException as e:      # noqa: BLE001 - 收尾不能抛
            logger.debug(f"[pdd] 关闭浏览器时出岔子（忽略）：{e}")

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
                    notice = head("巡检暂停") + "\n" + rows_block([
                        ("原因", "搜索页连续拒绝请求"),
                        ("冷却", f"{cooldown // 60} 分钟"),
                        ("恢复", "/盯价 reset 立即重试"),
                    ])
            elif ok:
                if self._risk_streak >= 2:
                    notice = head("巡检恢复", "风控计数清零")
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
    def _search_url(self, keyword: str, page: int = 1, sort: Optional[str] = None) -> str:
        """拼搜索页地址。sort 取 price（升序）或 _price（降序）。

        空格必须编成 %20，不能编成 +：urlencode 默认走 quote_plus，
        而 + 代表空格只是 application/x-www-form-urlencoded 的约定，
        拼多多前端解 query 时按字面量处理，搜索词会变成「5070ti+ultra+w+oc」，
        整页一条都搜不出来。浏览器地址栏发的就是 %20，这里跟浏览器对齐。

        sort 显式传入时不读配置，供 /盯价 debug 做「换种排序再试一次」的对照。
        传空串（SORT_DEFAULT）表示综合排序：不带 sort_type 参数。
        """
        if sort is None:
            sort = str(self.cfg("search_sort", SORT_ASC) or SORT_ASC).strip()
        sort = str(sort).strip()
        if sort not in (SORT_DEFAULT, SORT_ASC, SORT_DESC):
            sort = SORT_ASC
        # 连续空白压成一个，首尾去掉；中间的空格是「同时包含」的分词边界，要保留
        key = re.sub(r"\s+", " ", keyword or "").strip()
        # 参数保持最小集：实测浏览器只带 search_key 也能出商品，
        # 多带 search_type/source 之类反而可能与真实入口不同，不画蛇添足。
        query = {"search_key": key}
        if sort:
            query["sort_type"] = sort
        query["page"] = str(page)
        return SEARCH_URL + "?" + urlencode(query, quote_via=quote)

    async def _first_price(self, keyword: str, attempts: int = 2,
                           refresh_baseline: bool = False) -> dict:
        """手动取一次价：失败且不像风控时再试一次。

        add 是用户当场等结果的场景，一次空壳就把关键词存进去、显示「未取到价」，
        观感和插件坏了没区别；而空壳多半是客户端水化的时序问题，重试一次常常就出价。
        风控类失败不能重试——越试限得越久，所以按 risk 标记区分。
        最终失败时抛出最后一次的异常，调用方拿到的仍是原文与 html 快照。
        """
        last: Optional[Exception] = None
        for i in range(max(1, attempts)):
            try:
                return await self._search_keyword(keyword, refresh_baseline=refresh_baseline)
            except Exception as e:
                last = e
                if getattr(e, "risk", False) or i >= attempts - 1:
                    break
                await asyncio.sleep(1.0)
        if last is not None:
            raise last
        raise PddFetchError("取价失败")

    async def _fetch_candidates(self, keyword: str, sort: Optional[str]) -> tuple[list[dict], str]:
        """取一页搜索结果，只留带价格的商品。返回 (候选, 页面原文)。"""
        mode = self._fetch_mode()
        html = ""
        raw: Optional[dict] = None

        if mode == "browser":
            raw = await self._browser_raw(keyword, sort)
        else:
            html = await self._get_html(self._search_url(keyword, sort=sort))
            raw = self._extract_raw_data(html)
            if raw is None:
                if mode == "auto":
                    # HTML 里没有 rawData（搜索页改成客户端渲染后就是这样），
                    # 说明这条路已经走不通，直接改走浏览器，别拿老文案误报风控。
                    raw = await self._browser_raw(keyword, sort)
                    html = ""
                elif "系统繁忙" in html or "请稍后再试" in html:
                    raise PddFetchError("搜索页返回「系统繁忙」｜账号被限流｜等恢复或换账号",
                                        risk=True, html=html)
                elif "captcha" in html or "访问验证" in html or "滑块" in html:
                    raise PddFetchError("要求验证码验证｜账号被风控｜用手机打开拼多多手动过一次验证",
                                        risk=True, html=html)
                else:
                    raise self._raw_missing_error(keyword, html)

        goods_list = self._find_goods_list(raw)
        if not goods_list:
            # 兜底：改版时商品常被包一层（{goods:{...}}），先拆再判「搜不到」
            goods_list = self._unwrap_goods(raw)
        if not goods_list:
            err = self._no_goods_error(keyword, raw, html)
            err.html = html
            raise err

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
                f"搜到 {len(goods_list)} 件商品，没有一件能解析出价格｜"
                f"{no_price} 件缺价格字段，字段名可能改了｜上报此条等待适配",
                html=html,
            )
        return candidates, html

    async def _search_keyword(self, keyword: str, sort: Optional[str] = None,
                              refresh_baseline: bool = False) -> dict:
        """按关键词搜索，返回价格最低的那件商品。

        取价口径：搜索页 item 里的 price（单位分），即页面展示的拼单价。
        H5 搜索页不下发券后价，所以这里不扣券（要精确到手价只能逐件进详情页，
        那样每轮请求量会翻几十倍，风控风险不成比例）。

        两段式取价（use_price_baseline 开启时）：
        1. 综合排序那一页当「尺子」——推荐排序按相关性出结果，混进来的杂型号少，
           价格落在真实档位；
        2. 价格升序那一页当「候选」——最便宜的一批在这里，但它会被更便宜的
           近似型号占满（搜 5070ti 首页全是 5070），单看这一页没有可比样本。
        用 1 的下沿和型号去筛 2，再从筛剩的里取最小，才是这个型号的真实低价。
        """
        keyword = (keyword or "").strip()
        if not keyword:
            raise PddFetchError("关键词为空")

        # 基准先取：它是「尺子」，拿不到就先按单页逻辑走，别让一次基准失败把整轮拖成取价失败。
        # 风控类失败例外——那种情况继续请求只会更糟，必须抛上去中止本轮。
        # filter_price_outliers 是价格剔除的总开关：关掉它就不取基准了，
        # use_price_baseline 只决定「用基准筛」还是「在单页内按 MAD 筛」。
        want_band = (self._use_baseline() and self._is_price_sort(sort)
                     and bool(self.cfg("filter_price_outliers", True)))
        band: dict[str, Any] = {}
        if want_band:
            try:
                band = await self._price_baseline(keyword, refresh=refresh_baseline)
            except PddFetchError as e:
                if getattr(e, "risk", False):
                    raise
                band = {"mode": "error", "note": str(e)}

        candidates, html = await self._fetch_candidates(keyword, sort)
        # 先按价格升序：后面「前 N 条」「该商品往后三条」都以这个顺序为准
        candidates.sort(key=lambda c: c["price_cents"])

        pool = candidates
        outlier_info: dict[str, Any] = {"outlier_mode": "off", "outlier_total": len(candidates)}
        model_info: dict[str, Any] = {"model_mode": "off"}
        baseline_info: dict[str, Any] = {"baseline_mode": "off"}

        if band:
            pool, outlier_info, model_info, baseline_info = await self._narrow_by_baseline(
                keyword, candidates, band)
        else:
            # 单页模式：没有外部尺子，只能在这一页内部按 MAD 剔
            if bool(self.cfg("filter_price_outliers", True)):
                pool, outlier_info = filter_price_outliers(
                    candidates,
                    k=self._outlier_k(),
                    min_drop_ratio=self._outlier_drop_ratio(),
                )
            pool, model_info = self._apply_model_gate(keyword, pool)

        # 兜底：任何一道筛子都不该把池子清空，真清空了就按原样取，
        # 让这一轮先拿到价——比抛 IndexError 有用
        if not pool:
            pool = candidates

        anchor_idx = 0
        match_info: dict[str, Any] = {"match_mode": "off"}
        if bool(self.cfg("keyword_filter_by_title", True)):
            anchor_idx, match_info = self._pick_by_title(keyword, pool)

        anchor = dict(pool[anchor_idx])
        anchor["keyword"] = keyword
        # samples[0] 是选中的锚点，后面两条是它后面的报价
        anchor["samples"] = pool[anchor_idx: anchor_idx + 3]
        anchor["count"] = len(pool)
        anchor["scanned"] = len(candidates)
        anchor.update(match_info)
        anchor.update(outlier_info)
        anchor.update(model_info)
        anchor.update(baseline_info)
        return anchor

    # ---------------- 基准价与型号校验 ----------------
    def _use_baseline(self) -> bool:
        return bool(self.cfg("use_price_baseline", True))

    def _baseline_ratio(self) -> float:
        """基准下沿占中位数的比例。填 70 表示低于七折就当作不同档商品。"""
        try:
            pct = float(self.cfg("baseline_floor_percent", DEFAULT_BASELINE_FLOOR_PCT))
        except (TypeError, ValueError):
            pct = float(DEFAULT_BASELINE_FLOOR_PCT)
        return max(0.3, min(1.0, pct / 100.0))

    def _is_price_sort(self, sort: Optional[str]) -> bool:
        """按价格排的那两路才需要基准。

        综合排序本身就是尺子，再给它套一个基准没有意义。
        """
        s = str(sort if sort is not None else self.cfg("search_sort", SORT_ASC)
                or SORT_ASC).strip()
        return s in (SORT_ASC, SORT_DESC)

    async def _price_baseline(self, keyword: str, refresh: bool = False) -> dict[str, Any]:
        """综合排序那一页的价格带，带缓存。

        基准只回答「这个型号大致多少钱」，半天内的漂移可以忽略，所以存进监控项里。
        缓存住它，稳态下巡检仍是每个关键词一次请求，只有过期那一轮多打一次。
        """
        key = keyword_key(keyword)
        item = self.store.goods.get(key) or {}
        cached = item.get("price_band") or {}
        now = time.time()
        fresh = bool(cached) and (now - float(cached.get("at") or 0) < BASELINE_TTL)
        if fresh and not refresh:
            return {**cached, "baseline_mode": "cache"}

        items, _html = await self._fetch_candidates(keyword, SORT_DEFAULT)
        band = price_band(items, k=self._outlier_k(),
                          min_drop_ratio=self._baseline_ratio())
        band["at"] = int(now)
        band["n"] = len(items)
        # 综合排序的整页留着当退路：升序页全被近似型号占满时用它出价
        band["items"] = sorted(items, key=lambda c: c["price_cents"])
        if item:
            item["price_band"] = {k2: band.get(k2) for k2 in ("mode", "median", "floor",
                                                              "cutoff", "at", "n")}
        return {**band, "baseline_mode": "fresh" if cached else "new"}

    def _apply_model_gate(self, keyword: str, items: list[dict]) -> tuple[list[dict], dict[str, Any]]:
        """型号整串必须在标题里。

        搜「5070ti」会混进大量「5070」：分词能命中「5070」，重合度就有 50%，
        而价格便宜三千，按价格升序取必然取到 5070——这就是盯 5070ti 却记成
        八千块的原因。这类混淆只能靠整串比对区分，分词打分会主动帮倒忙。
        """
        if not bool(self.cfg("model_must_match", True)):
            return items, {"model_mode": "off"}
        model = model_key(keyword)
        if not model:
            return items, {"model_mode": "skip"}
        kept = [c for c in items if model in squash(c.get("title"))]
        if not kept:
            return items, {"model_mode": "miss", "model_key": model,
                           "model_note": f"没有一条标题含「{model}」"}
        return kept, {
            "model_mode": "on",
            "model_key": model,
            "model_total": len(items),
            "model_dropped": len(items) - len(kept),
        }

    @staticmethod
    def _baseline_info(band: dict[str, Any]) -> dict[str, Any]:
        return {
            "baseline_mode": band.get("baseline_mode") or band.get("mode") or "off",
            "baseline_median": band.get("median") or 0,
            "baseline_floor": band.get("floor") or 0,
            "baseline_cutoff": band.get("cutoff") or 0,
            "baseline_n": band.get("n") or 0,
            "baseline_total": band.get("total") or 0,
            "baseline_at": band.get("at") or 0,
        }

    @staticmethod
    def _band_record(info: dict[str, Any]) -> dict[str, Any]:
        """把诊断里的基准字段收成可缓存的一条。

        add 时已经算过一次基准，顺手存下来，别让下一轮巡检为了同一个值再打一次。
        """
        return {
            "mode": "ok",
            "median": info.get("baseline_median") or 0,
            "floor": info.get("baseline_floor") or 0,
            "cutoff": info.get("baseline_cutoff") or 0,
            "at": int(info.get("baseline_at") or time.time()),
            "n": info.get("baseline_n") or 0,
        }

    async def _narrow_by_baseline(
        self, keyword: str, candidates: list[dict], band: dict[str, Any]
    ) -> tuple[list[dict], dict[str, Any], dict[str, Any], dict[str, Any]]:
        """用综合排序的价格带和型号去筛价格升序那一页。

        三道，任何一道把候选筛空就退回上一道的结果并记下原因——宁可给一个
        带注释的价格，也不要因为一次取数就判成「取价失败」。
        """
        baseline_info = self._baseline_info(band)
        cutoff = int(band.get("cutoff") or 0)
        if band.get("mode") != "ok" or not cutoff:
            # 基准样本不够，退回单页 MAD
            pool, info = filter_price_outliers(
                candidates, k=self._outlier_k(), min_drop_ratio=self._outlier_drop_ratio())
            pool, model_info = self._apply_model_gate(keyword, pool)
            baseline_info["baseline_mode"] = "thin"
            return pool, info, model_info, baseline_info

        # 第一道：价格不低于基准下沿（配件、引流 SKU 在这里出局）
        above = [c for c in candidates if c["price_cents"] >= cutoff]
        # 第二道：型号整串命中（5070 在这里出局）
        gated, model_info = self._apply_model_gate(keyword, above)
        # miss 表示一条都没命中，此时 _apply_model_gate 原样返回，不能当成功
        if gated and model_info.get("model_mode") != "miss":
            pool = gated
            outlier_info = {
                "outlier_mode": "baseline",
                "outlier_total": len(candidates),
                "outlier_kept": len(pool),
                "outlier_dropped": len(candidates) - len(pool),
                "outlier_cutoff": cutoff,
            }
            return pool, outlier_info, model_info, baseline_info

        # 升序页一条能用的都没有：改用综合排序那一页，那里至少是相关商品。
        # 这种情况说明升序页被更便宜的近似型号占满了，价格仍要给出，但要说清来源。
        fallback = band.get("items") or []
        if not fallback and band.get("baseline_mode") == "cache":
            # 缓存里只存了价格带、没存整页。要退路就得再取一次，
            # 这条只在「升序页全军覆没」时走到，不是常规开销。
            band = await self._price_baseline(keyword, refresh=True)
            baseline_info = self._baseline_info(band)
            fallback = band.get("items") or []
        if fallback:
            pool, model_info = self._apply_model_gate(keyword, fallback)
            model_info["model_fallback"] = True
            outlier_info = {
                "outlier_mode": "baseline_page",
                "outlier_total": len(candidates),
                "outlier_kept": len(pool),
                "outlier_dropped": 0,
                "outlier_cutoff": cutoff,
            }
            baseline_info["baseline_fallback"] = True
            return pool, outlier_info, model_info, baseline_info

        # 连综合排序也没东西：退回原始候选，别让这一轮直接失败
        pool, outlier_info = filter_price_outliers(
            candidates, k=self._outlier_k(), min_drop_ratio=self._outlier_drop_ratio())
        baseline_info["baseline_mode"] = "empty"
        return pool, outlier_info, model_info, baseline_info


    def _outlier_k(self) -> float:
        """MAD 的倍数：越大越宽容。2 严格、3 标准、4 宽松。"""
        try:
            k = float(self.cfg("outlier_k", 3.0))
        except (TypeError, ValueError):
            k = 3.0
        return max(1.0, min(6.0, k))

    def _outlier_drop_ratio(self) -> float:
        """价格低于中位数的这个比例才允许被剔。

        防止「主体全是套装、裸机反而显小」时把真·最低价剔掉。
        配置里填的是百分比（30 = 0.3 倍）：比中位数便宜 70% 以上才算噪声。
        """
        try:
            pct = float(self.cfg("outlier_drop_percent", 30))
        except (TypeError, ValueError):
            pct = 30.0
        return max(0.05, min(1.0, pct / 100.0))

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
        if not pool:
            return 0, {"match_mode": "fallback", "match_note": "没有可比的商品"}
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

        scored: list[tuple] = []
        for i, cand in enumerate(pool):
            raw, hit = overlap_score(kw_tokens, tokenize(cand["title"]))
            # 标题像配件/引流 SKU 的降权，让完整商品排到前面。
            # 排序用降权后的分，展示仍用原始重合度 + 标注，免得「明明全命中却显示 50%」。
            penalty = accessory_penalty(cand["title"], kw_tokens) if raw > 0 else 1.0
            # 重合度 > 命中词数 > 价格低 > 索引靠前；
            # 索引唯一，所以比较永远不会落到后面那个 set 上
            scored.append((raw * penalty, len(hit), -cand["price_cents"], i, hit, raw, penalty))
        scored.sort(reverse=True)

        best_final, _, _, best_i, best_hit, best_raw, best_pen = scored[0]
        if best_final <= 0:
            return 0, {
                "match_mode": "fallback",
                "match_note": f"前 {len(pool)} 条标题都没命中关键词，按最低价取",
            }
        return best_i, {
            "match_mode": "jieba",
            "match_score": round(best_raw, 2),
            "match_penalized": best_pen < 1.0,
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
        rows: list[tuple[str, str]] = [
            ("关键词", item.get("keyword") or key),
            ("现价", fmt_yuan(price)),
        ]
        if old_low:
            drop = old_low - price
            pct = drop / old_low * 100
            rows.append(("原史低", fmt_yuan(old_low)))
            rows.append(("降幅", f"{fmt_yuan(drop)}（-{pct:.1f}%）"))
        link = item.get("share_link") or (
            GOODS_URL.format(item["goods_id"]) if item.get("goods_id") else ""
        )
        if link:
            rows.append(("链接", link))
        return head("史低") + "\n" + rows_block(rows)

    def build_today_report(self) -> list[str]:
        """「今日行情」文本：开头汇总 + 每项一行 + 图例。

        刻意不做列对齐。手机 QQ 一屏只放得下二十来个汉字，补空格对齐的表格
        一折行就整列错位，越对齐越难读。改成一项一行、空格和 · 分隔，
        折行只影响该项自己，不影响其它项。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        items = list(self.store.goods.values())

        if not items:
            return [head("今日行情", "监控 0 项") + "\n\n用 /盯价 add <关键词> 添加"]

        n_down = n_up = n_flat = n_low = n_fail = 0
        rows: list[str] = []

        for idx, item in enumerate(items, 1):
            name = shorten(item.get("keyword") or "--", 16)
            low = item.get("lowest_cents")
            price = item.get("last_price_cents")

            if price is None:
                n_fail += 1
                # 取价失败也把上次的史低带上：行情页看不到数，用户只能靠它判断值不值得进详情
                tail = f" · 史低 {fmt_yuan(low)}" if low else ""
                rows.append(f"{idx} {name} -- 取价失败{tail}")
                continue

            prev_day = PriceStore._prev_day_price(item, today)
            if prev_day is None:
                delta = "首次"
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
            # 现价已经是史低时不必再报一遍史低值，标记即可；否则补上差值参照
            if is_low:
                tail = " ★"
            elif low:
                tail = f" · 史低 {fmt_yuan(low)}"
            else:
                tail = ""
            rows.append(f"{idx} {name} {fmt_yuan(price)} {delta}{tail}")

        # 只列有变化的项：报「上涨 0」没有信息量，还占掉手机一行
        parts = []
        if n_down:
            parts.append(f"下跌 {n_down}")
        if n_up:
            parts.append(f"上涨 {n_up}")
        if n_flat:
            parts.append(f"持平 {n_flat}")
        if not parts:
            parts.append("首次记录，暂无涨跌对比")

        lines = [head("今日行情", f"{len(items)} 项"), "", " · ".join(parts), ""]
        lines += rows
        # 脚注只列需要解释的项，且每条一行 —— 堆在一行会超过手机一屏的宽度
        foot: list[str] = []
        if n_low:
            foot.append(f"★ {n_low} 项处于史低")
        if n_fail:
            foot.append(f"取价失败 {n_fail} 项 · /盯价 list 看原因")
        if foot:
            lines.append("")
            lines += foot
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

        if sub in ("login", "登录", "扫码"):
            # 默认引导到 WebUI：滑块、验证码这些只能在真页面上操作
            if arg.strip().lower() in ("qr", "码", "chat", "聊天"):
                yield event.plain_result(await self._cmd_login(event))
            else:
                yield event.plain_result(self._cmd_login_hint(event))
            return

        if sub in ("验证", "verify", "选"):
            yield event.plain_result(self._cmd_verify(arg))
            return

        if sub in ("add", "添加", "加"):
            if not arg:
                yield event.plain_result(head("用法", "/盯价 add <关键词>", "例 /盯价 add 9700X"))
                return
            yield event.plain_result(await self._cmd_add(event, arg))
            return

        if sub in ("del", "delete", "remove", "删除"):
            if not arg:
                yield event.plain_result(head("用法", "/盯价 del <序号>"))
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
                yield event.plain_result(head("巡检", "上一轮未结束，跳过"))
                return
            total, ok, fail, lows = result
            if total == 0:
                yield event.plain_result(head("巡检", "监控列表为空", "先 /盯价 add <关键词>"))
                return
            rows = [("结果", f"{ok}/{total} 成功")]
            if lows:
                rows.append(("史低", f"{len(lows)} 个 · 已推送"))
            if fail:
                rows.append(("失败", f"{fail} 个 · /盯价 list 看原因"))
            if self._last_risk:
                first, _, _ = split_error(self._last_risk)
                rows.append(("中止", shorten(first, 40)))
            if self._risk_streak:
                wait = max(0, int(self._risk_until - time.time()))
                rows.append((
                    "风控",
                    f"连续 {self._risk_streak} 次"
                    + (f" · 冷却中，约 {max(1, wait // 60)} 分钟后重试" if wait else ""),
                ))
            yield event.plain_result(head("巡检完成") + "\n" + rows_block(rows))
            return

        if sub in ("reset", "解除", "恢复"):
            yield event.plain_result(self._cmd_reset())
            return

        if sub in ("debug", "诊断", "排查"):
            if not arg:
                yield event.plain_result(head("用法", "/盯价 debug <关键词 或 序号>"))
                return
            yield event.plain_result(await self._cmd_debug(arg))
            return

        if sub in ("on", "开启", "push"):
            changed = self.store.bind_session(event.unified_msg_origin)
            self.store.save()
            yield event.plain_result(
                head("推送", "本群已开启" if changed else "本群已在列表")
            )
            return

        if sub in ("off", "关闭", "unpush"):
            changed = self.store.unbind_session(event.unified_msg_origin)
            self.store.save()
            yield event.plain_result(
                head("推送", "本群已关闭" if changed else "本群未开启")
            )
            return

        yield event.plain_result(head("未知子指令", sub, "/盯价 help 查用法"))


    async def _cmd_add(self, event: AstrMessageEvent, arg: str) -> str:
        """只接受关键词；贴链接的一律引导到关键词。"""
        head_part, _, note = arg.partition("|")
        head_part = head_part.strip()
        if not head_part:
            return head("用法", "/盯价 add <关键词>", "例 /盯价 add 9700X")
        if extract_first_url(head_part):
            return "\n".join([
                head("只支持关键词", "输入是链接"),
                rows_block([
                    ("说明", "不再支持盯单个商品链接"),
                    ("改法", "/盯价 add <商品名里最有辨识度的词>"),
                    ("示例", "/盯价 add 9700X"),
                ]),
            ])
        return await self._add_keyword(event, head_part, note.strip())

    async def _add_keyword(self, event: AstrMessageEvent, keyword: str, note: str) -> str:
        keyword = re.sub(r"\s+", " ", keyword).strip()
        if not keyword:
            return head("关键词为空")
        if len(keyword) > 40:
            return head("关键词超长", "上限 40 字")

        max_items = int(self.cfg("max_goods", 50))
        if len(self.store.goods) >= max_items:
            return head("已达上限", f"{max_items} 项", "先 /盯价 del 删除")

        existing = self.store.find_keyword(keyword)
        if existing:
            item = existing[1]
            return "\n".join([
                head("已在监控"),
                rows_block([
                    ("关键词", item.get("keyword") or keyword),
                    ("现价", fmt_yuan(item.get("last_price_cents"))),
                    ("史低", fmt_yuan(item.get("lowest_cents"))),
                ]),
            ])

        # 首次取数比巡检多给一次机会：这一步失败会直接写在「已添加」的回执里
        best: Optional[dict] = None
        err = ""
        try:
            best = await self._first_price(keyword)
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
            # 这一步已经取过基准，存下来，下一轮巡检不用为同一个值再打一次
            if best.get("baseline_cutoff"):
                item["price_band"] = self._band_record(best)
            self.store.apply_price(keyword_key(keyword), best["price_cents"])

        self.store.bind_session(event.unified_msg_origin)
        self.store.save()

        rows: list[tuple[str, str]] = [("关键词", keyword)]
        if note:
            rows.append(("备注", note))

        if not best:
            first, reason, action = split_error(err or "取价失败")
            rows.append(("现价", "--"))
            rows.append(("原因", first or "取价失败"))
            if reason:
                rows.append(("说明", reason))
            if action:
                rows.append(("处理", action))
            rows.append(("后续", "巡检会继续尝试"))
            return head("已添加", "本次未取到价") + "\n" + rows_block(rows)

        rows.append(("现价", fmt_yuan(item.get("last_price_cents"))))
        rows.append(("史低", fmt_yuan(item.get("lowest_cents")) + " · 首次记录"))
        # 次低：只报价格，不报标题，避免消息过长
        others = [s for s in (best.get("samples") or [])[1:3] if s.get("price_cents")]
        if others:
            rows.append(("次低", "、".join(fmt_yuan(s["price_cents"]) for s in others)))
        if item.get("goods_id"):
            rows.append(("商品", item["goods_id"]))
        if item.get("share_link"):
            rows.append(("链接", item["share_link"]))
        return head("已添加") + "\n" + rows_block(rows)

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
            return head("未找到", target, "/盯价 list 查序号")

        label = str(self.store.goods[key].get("keyword") or key)
        self.store.remove(key)
        self.store.save()
        return head("已删除", label)

    def _cmd_list(self) -> list[str]:
        items = list(self.store.goods.items())
        if not items:
            return [head("列表", "监控 0 项") + "\n\n用 /盯价 add <关键词> 添加"]

        # 与行情同一套排版：一项一行、不补列宽。手机端折行只影响该项自己，不会整列错位。
        rows = [head("列表", f"{len(items)} 项"), ""]
        for idx, (_key, item) in enumerate(items, 1):
            name = shorten(item.get("keyword") or "--", 16)
            price = fmt_yuan(item.get("last_price_cents"))
            low = fmt_yuan(item.get("lowest_cents"))
            rows.append(f"{idx} {name} 现价 {price} · 史低 {low}")
        rows.append("")
        rows.append("/盯价 info <序号> 看单项详情")
        sessions = len(self.store.data.get("sessions") or [])
        if sessions:
            # 单独一行：跟 info 提示并排放会超过手机一屏的宽度
            rows.append(f"推送至 {sessions} 个会话")
        return rows

    def _cmd_info(self, arg: str) -> str:
        target = arg.strip()
        if not target:
            return head("用法", "/盯价 info <序号>")
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
            return head("未找到", target, "/盯价 list 查序号")

        item = self.store.goods[key]
        daily = item.get("daily") or {}
        recent = sorted(daily.items())[-7:]
        trend = "、".join(f"{d[5:]} {fmt_yuan(c)}" for d, c in recent) or "--"

        low_at = str(item.get("lowest_at") or "").strip()[:16]
        rows: list[tuple[str, str]] = [
            ("关键词", item.get("keyword") or key),
            ("现价", fmt_yuan(item.get("last_price_cents"))),
            ("史低", fmt_yuan(item.get("lowest_cents")) + (f" · {low_at}" if low_at else "")),
        ]
        if item.get("note"):
            rows.append(("备注", item["note"]))
        if item.get("title"):
            rows.append(("最低商品", shorten(item["title"], 24)))
        if item.get("goods_id"):
            rows.append(("商品", str(item["goods_id"])))
        rows.append(("近 7 天", trend))
        if item.get("last_error"):
            # 错误统一成「现象｜原因｜处理」，这里只留现象，避免详情页过长
            rows.append(("上次失败", shorten(split_error(item["last_error"])[0], 40)))
        if item.get("share_link"):
            rows.append(("链接", item["share_link"]))
        return head("详情") + "\n" + rows_block(rows)


    def _cookie_rows(self) -> list[tuple[str, str]]:
        """只报 cookie 的长度、字段数和关键字段是否齐全，绝不回显内容。"""
        raw = str(self.cfg("cookie", "") or "").strip()
        if not raw:
            return [("cookie", "未配置 · 必填，否则搜索页只下发空壳")]
        jar = normalize_cookie(raw)
        pairs = parse_cookie_pairs(jar)
        has_token = "PDDAccessToken" in pairs
        rows = [(
            "cookie",
            f"{len(jar)} 字符 · {len(pairs)} 个字段 · "
            + ("含 PDDAccessToken" if has_token else "缺 PDDAccessToken"),
        )]
        # 这几个字段从电脑端复制常常没有，手机端整条复制才全；缺了容易被判成异常客户端
        missing = [k for k in BASE_COOKIE_KEYS if k not in pairs and k not in self._jar]
        if missing:
            rows.append(("缺失字段", f"{'、'.join(missing)} · 从手机浏览器整条重取更全"))
        if self._jar:
            rows.append(("会话", f"沿用 {len(self._jar)} 个服务端下发的设备 cookie"))
        return rows

    def _fetcher_alive(self) -> bool:
        """浏览器实例的进程还活着吗（实例存在不等于进程在）。"""
        if self._fetcher is None:
            return False
        browser = getattr(self._fetcher, "_browser", None)
        if browser is None:
            return False
        try:
            return bool(browser.is_connected())
        except Exception:
            return False

    def _kernel_note(self) -> str:
        """内核状态：自动下载过的话要说一声，否则用户不知道那一百多 MB 哪来的。"""
        state = str(getattr(self._fetcher, "kernel_state", "") or "")
        if state == "installed":
            return " · 已自动补装内核"
        if state == "installing":
            return " · 内核下载中"
        return ""

    def _memory_note(self) -> str:
        """报回收状态。

        Chromium 关页面、关上下文都不还内存，只有进程退出才行，所以「回收过几次」
        比「启动了没有」更能说明内存问题。不列出来用户只能靠 ps 猜。
        """
        if self._fetcher is None:
            return "浏览器未启动"
        used = int(getattr(self._fetcher, "_searches", 0) or 0)
        recycled = int(getattr(self._fetcher, "recycled", 0) or 0)
        cap = self._max_searches()
        idle = self._idle_seconds()

        bits = [f"已取数 {used}/{cap} 次" if cap else f"已取数 {used} 次"]
        bits.append(f"回收 {recycled} 次")
        bits.append(f"空闲 {int(idle // 60)} 分钟退出" if idle else "空闲回收已关")
        return " · ".join(bits)

    def _transport_rows(self) -> list[tuple[str, str]]:
        """报当前取数与伪装方式——这决定了被风控检测的概率。"""
        mode = self._fetch_mode()
        custom = str(self.cfg("user_agent", "") or "").strip()

        if mode == "browser":
            kernel = str(self.cfg("browser_channel", "") or "").strip() or "chrome-headless-shell"
            if self._fetcher is None:
                state = "未启动，首次取数时拉起"
            elif self._fetcher_alive():
                state = "已启动"
            else:
                # 实例在、进程死了也是有的（被系统杀掉），报「已启动」会误导人
                state = "进程已退出，下次取数时自动重启"
            return [
                ("取数", f"浏览器 · {kernel}（{state}）{self._kernel_note()}"),
                ("UA", "自定义" if custom else "内置 iPhone Safari"),
                ("指纹", "TLS/HTTP2 由 Chromium 生成 · curl_cffi 不参与"),
                ("代理", self._proxy_line()),
                ("内存", self._memory_note()),
            ]

        if self._transport:
            name = self._transport
        elif self._use_curl():
            name = "curl_cffi（待初始化）"
        else:
            name = "httpx"
        if name.startswith("curl_cffi"):
            layer = "TLS + HTTP2 指纹已模拟"
        else:
            layer = "仅伪造请求头，TLS 指纹仍是 Python 默认值 · curl_cffi 未生效"
        if custom:
            ua = f"自定义 · {'iPhone Safari' if SAFARI_UA_RE.search(custom) else '非 Safari 系'}"
        elif name.startswith("curl_cffi"):
            ua = "跟随 curl_cffi 指纹（与 TLS 版本配套）"
        else:
            ua = f"默认 · {'iPhone Safari' if SAFARI_UA_RE.search(DEFAULT_UA) else '非 Safari 系'}"
        return [
            ("取数", f"{name}（{layer}）"),
            ("UA", ua),
            ("代理", self._proxy_line()),
        ]

    # 兼容旧调用：测试和日志里还在按整段字符串取
    def _cookie_line(self) -> str:
        return rows_block(self._cookie_rows())

    def _transport_line(self) -> str:
        return rows_block(self._transport_rows())


    def _cmd_reset(self) -> str:
        """清掉风控冷却与连续失败计数。

        换完 cookie、装完 curl_cffi 之后要能立刻重试，否则得干等退避时间，
        会让人以为插件坏了。
        """
        had_streak = self._risk_streak
        self._risk_streak = 0
        self._risk_until = 0.0
        self._last_risk = ""
        meta = self.store.data.setdefault("meta", {})
        meta["risk_streak"] = 0
        cleared = 0
        for item in self.store.goods.values():
            if item.get("fail_count") or item.get("last_error"):
                item["fail_count"] = 0
                item["last_error"] = ""
                cleared += 1
        self.store.save()

        rows: list[tuple[str, str]] = []
        if had_streak:
            rows.append(("风控计数", f"{had_streak} → 0"))
        if cleared:
            rows.append(("失败计数", f"清掉 {cleared} 项"))
        rows.append(("下一步", "/盯价 debug <关键词> 验证，再 /盯价 check"))
        return head("冷却已清除") + "\n" + rows_block(rows)


    # ---------------- 扫码登录 ----------------
    def _login_allowed(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """群里默认不给二维码：谁扫了，谁就把自己的号绑进这台机器的浏览器。"""
        try:
            if event.is_private_chat():
                return True, ""
        except Exception:
            pass
        if bool(self.cfg("login_allow_group", False)):
            return True, ""
        return False, ("群里不生成二维码｜别人扫了就等于把自己的号绑进机器人｜"
                       "到私聊里发 /盯价 login；确要群里用请打开配置项 login_allow_group")

    def _save_qr(self, png: bytes, name: str = "qr.png") -> str:
        """把二维码落到数据目录，供发图用。固定文件名，只留最新一张。"""
        try:
            folder = os.path.join(self._resolve_data_dir(), "login")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, name)
            with open(path, "wb") as f:
                f.write(png)
            return path
        except Exception as e:
            logger.warning(f"[pdd] 二维码落盘失败：{e}")
            return ""

    def _save_login_page(self, html: str) -> str:
        """存一份登录页源码。

        选择器没命中时才有，用来把二维码的位置对准——不然只能靠猜 DOM 结构。
        """
        try:
            folder = os.path.join(self._resolve_data_dir(), "login")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, "page.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html or "")
            return path
        except Exception as e:
            logger.warning(f"[pdd] 登录页快照落盘失败：{e}")
            return ""

    async def _send_once(self, umo: str, chain: Any) -> bool:
        try:
            return bool(await self.context.send_message(umo, chain))
        except Exception as e:
            logger.warning(f"[pdd] 发送失败 {umo}: {e}")
            return False

    async def _cmd_login(self, event: AstrMessageEvent) -> str:
        """扫码登录：二维码发回会话，扫完自动把 cookie 写进配置。

        为什么要绕这一圈：cookie 只能由一次真实登录产生，让用户开 F12 手抄整条
        是最容易劝退的一步。这里借插件自己那台 Chromium 登录，cookie 全程只存在
        服务器上，用户只负责扫一下——顺带还保证了 cookie 与这台机器的 UA、
        api_uid 是配套的，比从手机上抄一条过来更不容易被判异常。
        """
        ok, why = self._login_allowed(event)
        if not ok:
            return head("登录未开始") + "\n" + rows_block(error_rows(why))

        if self._fetch_mode() != "browser":
            rows = [
                ("现象", "当前取数方式是 http，没有浏览器可用"),
                ("原因", "扫码要靠无头 Chromium 打开登录页"),
                ("处理", "把 fetch_mode 改成 browser 后重载插件"),
            ]
            return head("登录未开始") + "\n" + rows_block(rows)

        if self._login_busy:
            return head("登录进行中", "上一轮还没结束") + "\n" + rows_block([
                ("处理", "等它出结果，或等它自己超时（180 秒）"),
                ("提示", "并发开两个登录会各起一个 Chromium，最费内存"),
            ])

        try:
            # 这时候还没有 cookie 是正常的：登录就是为了拿它
            fetcher = await self._get_fetcher(require_cookie=False)
        except PddFetchError as e:
            return head("登录未开始") + "\n" + rows_block(error_rows(str(e)))

        self._login_busy = True

        umo = event.unified_msg_origin
        shots = {"n": 0}

        async def deliver(png: bytes, how: str) -> None:
            shots["n"] += 1
            first = shots["n"] == 1
            title = "二维码已生成" if first else "二维码已刷新，旧码作废"
            tip = ("用拼多多 App 扫这张，手机上确认登录" if first
                   else "前面那张过期了，扫这张")
            chain = MessageChain().message(head(title, tip))
            path = self._save_qr(png)
            if path:
                try:
                    chain = chain.file_image(path)
                except Exception:
                    pass
            else:
                logger.warning("[pdd] 二维码没落盘，只发得出文字")
            await self._send_once(umo, chain)

        async def dump_page(html: str) -> None:
            path = self._save_login_page(html)
            if path:
                logger.info(f"[pdd] 登录页快照：{path}")

        async def ask(png: bytes, options: list[str]) -> int:
            """扫码后的「安全验证」：把题目发出去，等用户回一个序号。

            这步没法自动过——题目是「你用过哪个收货人姓名」，只有账号主人知道。
            所以把整页截图和读到的选项一起发出去，让用户回 /盯价 验证 2。
            """
            self._verify_answer = None
            self._verify_count = len(options)
            self._verify_event = asyncio.Event()

            tips = [head("需要你确认一步", "拼多多要做安全验证，选一个就行")]
            if options:
                rows = [(str(i + 1), text) for i, text in enumerate(options)]
                tips.append(rows_block(rows))
            else:
                tips.append("选项没读出来，看图里那一列，按从上到下数")
            tips.append("回复：/盯价 验证 序号（例 /盯价 验证 1，0 表示取消）")

            chain = MessageChain().message("\n".join(tips))
            path = self._save_qr(png, "verify.png")
            if path:
                try:
                    chain = chain.file_image(path)
                except Exception:
                    pass
            await self._send_once(umo, chain)

            try:
                await asyncio.wait_for(self._verify_event.wait(), timeout=VERIFY_TIMEOUT)
            except asyncio.TimeoutError:
                raise BrowserError(
                    f"安全验证超时｜{int(VERIFY_TIMEOUT)} 秒内没选｜重发 /盯价 login"
                ) from None
            finally:
                self._verify_event = None

            answer = self._verify_answer
            self._verify_answer = None
            return -1 if answer is None else int(answer)

        try:
            # 外面再套一层超时：登录内部有自己的 180 秒计时，但收尾（关页面、
            # 退进程）也可能卡住，一卡住浏览器就永远留在内存里了。验证环节
            # 要等人回话，所以总上限再加上这段。
            result = await asyncio.wait_for(
                fetcher.login_qr(deliver, timeout=LOGIN_TIMEOUT,
                                 on_miss=dump_page, ask=ask),
                timeout=LOGIN_TIMEOUT + VERIFY_TIMEOUT + 45,
            )
        except asyncio.TimeoutError:
            return head("登录未完成", "这轮超出预期时长，已强制收尾") + "\n" + rows_block([
                ("现象", "扫码流程没能在这轮里走完"),
                ("原因", "登录页或浏览器收尾卡住了"),
                ("处理", "重发 /盯价 login；反复出现请把日志发我"),
            ])
        except BrowserError as e:
            return head("登录未完成") + "\n" + rows_block(error_rows(str(e)))
        except Exception as e:  # pragma: no cover - 兜底，别把异常甩到聊天里
            logger.error(f"[pdd] 扫码登录异常：{e}")
            return head("登录未完成", shorten(str(e), 60))
        finally:
            self._login_busy = False
            self._verify_event = None
            self._verify_answer = None
            # 双保险：不管上面走哪条路，浏览器都得退掉。登录是一次性的，
            # 里面的 login_qr 自己会关，这里再关一次是幂等的（句柄已是 None）。
            try:
                await asyncio.wait_for(fetcher.close(), timeout=30)
            except BaseException:       # noqa: BLE001 - 收尾不能盖掉上面的结果
                pass

        written = self.set_cfg("cookie", result["cookie"])
        # 旧会话里还留着上一个账号的 PDDAccessToken，而 jar 的优先级高于配置，
        # 不清掉的话下一次取数用的还是旧身份，表现为「登录了但没生效」。
        self._jar.clear()
        self.store.data.setdefault("meta", {})["cookie_jar"] = {}
        self.store.save()
        self._last_url = ""
        self._browser_hint = ""

        rows = [
            ("字段", f"{result['count']} 个 · 含 PDDAccessToken"),
            ("保存", "已写入配置" if written else "写不进去，检查配置目录权限"),
            ("验证", "/盯价 debug 纸巾"),
        ]
        return head("已登录") + "\n" + rows_block(rows)

    def _cmd_login_hint(self, event: AstrMessageEvent) -> str:
        """登录指引：默认把用户带到 WebUI 里那台「远程登录页」。"""
        ok, why = self._login_allowed(event)
        if not ok:
            return head("登录未开始") + "\n" + rows_block(error_rows(why))

        if self._fetch_mode() != "browser":
            return head("登录未开始") + "\n" + rows_block([
                ("现象", "当前取数方式是 http，没有浏览器可用"),
                ("处理", "把 fetch_mode 改成 browser 后重载插件"),
            ])

        if not self._web_ok:
            return head("面板登录不可用", "当前 AstrBot 不支持插件页面") + "\n" + rows_block([
                ("原因", "没有 register_web_api（需要 AstrBot 4.24.2 以上）"),
                ("备选", "/盯价 login qr 走聊天扫码，或按 README 手工复制 Cookie"),
            ])

        return head("登录去面板里点", "浏览器跑在服务器上，画面搬到面板") + "\n" + rows_block([
            ("入口", "管理面板 → 插件 → 多多盯价 → 多多盯价登录"),
            ("操作", "页面里就是拼多多登录页，扫码、滑块、验证都能直接点"),
            ("结果", "登录成功后自动写入配置，页面会提示「登录成功」"),
            ("备选", "/盯价 login qr 走聊天扫码（简单验证能用，滑块不行）"),
        ])

    def _cmd_verify(self, arg: str) -> str:
        """安全验证：把用户选的序号交给正在等待的登录流程。

        这个指令跟 /盯价 login 是两条并行的消息，不共享协程，所以只用
        一个 Event + 一个序号来传话，不需要把浏览器页面传来传去。
        """
        if self._verify_event is None:
            return head("没有待确认的验证") + "\n" + rows_block([
                ("现象", "现在没有登录流程在等这一步"),
                ("处理", "先发 /盯价 login，等它把题目发过来"),
            ])
        digits = re.sub(r"[^0-9]", "", arg or "")
        if not digits:
            return head("用法", "/盯价 验证 <序号>", "例 /盯价 验证 1，0 表示取消")
        n = int(digits)
        if n == 0:
            self._verify_answer = -1
            self._verify_event.set()
            return head("已取消", "这轮登录作废，浏览器会一起关掉")
        if self._verify_count and n > self._verify_count:
            return head("序号超范围", f"这次一共 {self._verify_count} 项")
        self._verify_answer = n - 1
        self._verify_event.set()
        return head("已选择", f"第 {n} 项，正在提交")

    async def _cmd_debug(self, arg: str) -> str:
        target = arg.strip()
        if target.isdigit() and len(target) <= 3:
            found = self.store.get_by_index(int(target))
            if found:
                target = str(found[1].get("keyword") or target)
        return await self._debug_keyword(target)

    def _match_row(self, best: dict) -> tuple[str, str]:
        """把标题匹配过程讲清楚：取了几条、选了第几条、重合多少、命中了哪些词。"""
        mode = str(best.get("match_mode") or "off")
        if mode == "off":
            return ("匹配", "未启用标题过滤 · 按最低价取")
        if mode == "fallback":
            return ("匹配", best.get("match_note") or "未命中，按最低价取")

        pool = best.get("match_pool") or 0
        idx = best.get("match_index") or 1
        score = best.get("match_score")
        pct = f"{score * 100:.0f}%" if isinstance(score, (int, float)) else "--"
        text = f"前 {pool} 条里第 {idx} 条 · 重合 {pct}"
        tokens = best.get("match_tokens") or []
        if tokens:
            shown = "、".join(str(t) for t in tokens[:4])
            text += f"（命中 {shown}）"
        if best.get("match_penalized"):
            text += " · 含配件词已降权"
        if mode == "substring":
            text += " · 子串匹配"
        return ("匹配", text)

    def _outlier_row(self, best: dict) -> tuple[str, str]:
        """说明价格筛掉了哪些、留下了多少。

        两种口径都要能读懂：
        - baseline：拿综合排序那页的基准下沿去筛升序页
        - mad：单页内部按中位数绝对偏差剔（基准关掉时走这条）
        """
        mode = str(best.get("outlier_mode") or "off")
        total = best.get("outlier_total") or 0
        if mode == "off":
            return ("过滤", "未启用价格离群剔除")
        if mode == "skip":
            return ("过滤", f"{best.get('outlier_note') or '未做剔除'} · 共 {total} 条")

        kept = best.get("outlier_kept") or 0
        dropped = best.get("outlier_dropped") or 0
        cutoff = best.get("outlier_cutoff") or best.get("outlier_floor")
        if mode == "baseline_page":
            return ("过滤", f"升序页没有可用商品，改用综合排序那一页 · {kept} 条")
        if mode == "baseline":
            return ("过滤", f"剔除 {dropped} 条低于基准 · 保留 {kept}/{total}"
                            + (f" · 下沿 {fmt_yuan(cutoff)}" if cutoff else ""))

        text = (f"剔除 {dropped} 条低价离群 · 保留 {kept}/{total}"
                f" · 下限 {fmt_yuan(cutoff)}")
        floor = best.get("outlier_floor")
        if cutoff and floor and int(cutoff) < int(floor):
            text += f" · 比例闸门 {self._outlier_drop_ratio():.0%} 生效"
        return ("过滤", text)

    def _baseline_row(self, best: dict) -> tuple[str, str]:
        """基准从哪来、多少钱、多久前取的。价格不对时第一个要看的就是这行。"""
        mode = str(best.get("baseline_mode") or "off")
        if mode == "off":
            if not bool(self.cfg("filter_price_outliers", True)):
                return ("基准", "价格剔除已关闭")
            return ("基准", "未启用 · 只在单页内部剔离群")
        if mode == "thin":
            return ("基准", "综合排序样本不足，本次只在单页内剔离群")

        median = best.get("baseline_median") or 0
        n = best.get("baseline_n") or 0
        if mode == "cache":
            at = int(best.get("baseline_at") or 0)
            text = f"综合排序 {n} 条 · 中位 {fmt_yuan(median)} · 缓存 {_ago_text(at)}"
        else:
            text = f"综合排序 {n} 条 · 中位 {fmt_yuan(median)} · 刚取"
        if best.get("baseline_fallback"):
            text += " · 按此页出价"
        return ("基准", text)

    def _model_row(self, best: dict) -> tuple[str, str] | None:
        """型号整串校验：这一步负责把 5070 从 5070ti 的搜索结果里分出去。"""
        mode = str(best.get("model_mode") or "off")
        if mode == "off":
            return None
        if mode == "skip":
            return None
        if mode == "miss":
            return ("型号", f"没有一条标题含「{best.get('model_key')}」· 已退回按价格取")
        key = best.get("model_key") or ""
        dropped = best.get("model_dropped") or 0
        if not dropped:
            return ("型号", f"{key} · 全部命中")
        return ("型号", f"{key} · 剔除 {dropped} 条不含该型号的")

    def _readable_url(self, keyword: str) -> str:
        """给人看的请求地址：中文保持可读，空格写成 %20。

        直接 unquote 会让空格以原样出现，看起来像是没编码就发出去了——实际发的是
        %20。这里只把空格还原成编码形态，其余保持可读，免得对着它排查错方向。
        """
        return unquote(self._search_url(keyword)).replace(" ", "%20")

    async def _debug_keyword(self, keyword: str) -> str:
        keyword = (keyword or "").strip()
        if not keyword:
            return head("用法", "/盯价 debug <关键词 或 序号>")
        sort = str(self.cfg("search_sort", SORT_ASC) or SORT_ASC).strip() or SORT_ASC
        if sort not in (SORT_ASC, SORT_DESC):
            sort = SORT_ASC
        try:
            # 与 add 走同一条路径：排查用的那一次也该重试，否则同一个词会出现
            # 「add 说没价、debug 有价」这种自相矛盾的结果。
            # 基准强制重取：诊断是给人核对「基准准不准」用的，拿几小时前的缓存没有意义。
            best = await self._first_price(keyword, refresh_baseline=True)
        except Exception as e:
            page_html = getattr(e, "html", "") or ""
            first, reason, action = split_error(str(e))
            rows: list[tuple[str, str]] = [("现象", shorten(first, 120))]
            if reason:
                rows.append(("原因", shorten(reason, 120)))
            if action:
                rows.append(("处理", shorten(action, 120)))
            rows.append(("请求", shorten(self._readable_url(keyword), 100)))
            if page_html:
                rows.append(("页面", self._page_line(self._extract_raw_data(page_html), page_html)))
            rows += self._cookie_rows()
            # 再换一种排序试一次：能区分「排序参数不灵」和「这个词真没货」，
            # 限流时不做，避免火上浇油
            if not getattr(e, "risk", False):
                alt = SORT_DESC if sort == SORT_ASC else SORT_ASC
                alt_name = "降序" if alt == SORT_DESC else "升序"
                try:
                    alt_best = await self._search_keyword(keyword, sort=alt)
                except Exception:
                    rows.append(("对照", f"换{alt_name}也是 0 条 → 不是排序问题，是这个词没货"))
                else:
                    rows.append((
                        "对照",
                        f"换{alt_name}能搜到 {alt_best.get('scanned') or 0} 条、"
                        f"最低 {fmt_yuan(alt_best.get('price_cents'))} → 问题在"
                        f"{'升序' if sort == SORT_ASC else '降序'}排序，改 search_sort 即可",
                    ))
            snapshot = self._save_snapshot(keyword, page_html)
            if snapshot:
                rows.append(("快照", f"debug/{snapshot}"))
            rows += self._transport_rows()
            return head("诊断失败", keyword) + "\n" + rows_block(rows)

        rows = [
            ("命中", f"{best.get('scanned') or best.get('count', 0)} 条"),
            self._baseline_row(best),
            self._outlier_row(best),
        ]
        model_row = self._model_row(best)
        if model_row:
            rows.append(model_row)
        rows += [
            ("最低", fmt_yuan(best["price_cents"])),
            ("商品", shorten(best.get("title") or "", 24)),
        ]
        if best.get("goods_id"):
            rows.append(("编号", str(best["goods_id"])))
        rows.append(self._match_row(best))
        sort_text = f"{sort}（{'升序' if sort == SORT_ASC else '降序'}）"
        if self._use_baseline():
            sort_text += " · 基准用综合排序"
        rows.append(("排序", sort_text))
        rows += self._cookie_rows()
        rows += self._transport_rows()
        return head("诊断", keyword) + "\n" + rows_block(rows)


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
