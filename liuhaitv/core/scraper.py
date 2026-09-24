# SPDX-License-Identifier: GPL-3.0-or-later
"""
搜刮引擎（Step 2 核心）。

职责：
  - 按 config.SCRAPE_SOURCES 并行拉取多个开源 IPTV 仓库的源清单；
  - 解析出频道条目，清洗频道名（去序号/括号，如 CCTV-1(:3) → CCTV-1）；
  - 按关键词自动分类：中央 / 卫视 / 港澳台 / 地方 / 其他；
  - **核心白名单过滤（Step 6）**：只保留央视与省级卫视，其余（港澳台、省市地面频道、
    购物/境外频道、iptv-org 里 CCTV-Billiards / CCTV-Storm * 之类的"假央视"）一律丢弃；
  - 跨源去重合并（同名频道 → 一条，多条地址 → 多条备用源）；
  - 每条源记录 source_id / origin / kind(容器) / protocol(ipv4|ipv6|域名)，
    供 Step 3/4 的 failover 与健康检测决策参考；
  - 落库：写 Channel + Source。

关键点：
  - 单源失败不中断整体（记入 failed_sources，继续其它源）。
  - 源抓取方式（直链 m3u 清单 或 html 网页页）由 URL 尾字自动判断 detect_fetch_kind()。
  - 去重以"清洗后去括号的归一化名"为键；对已存在频道只补录其未拥有的新地址。
  - 分类与白名单规则集中在 liuhaitv/core/channel_filter.py，本模块只调用不重复实现。
"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urljoin

import httpx

import liuhaitv.config as cfg
from liuhaitv.core.channel_filter import (
    is_cctv, is_cn_satellite, is_core_channel, is_hmt, is_province_satellite,
)
from liuhaitv.core.database import session_scope
from liuhaitv.core.m3u_parser import ParsedEntry, parse_m3u
from liuhaitv.core.models import Channel, Source
from liuhaitv.core.naming import canonical_channel_name

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 分类关键词（点2：关键词自动分类；含"其他"与"港澳台"分组）
# ---------------------------------------------------------------------------
# 央视识别的"关键词兜底"：只保留明确的央视字样。
# 注意**不能**放"综合/新闻/体育/电影/高清"这类通用词 —— 否则"北京卫视高清"、
# "广东体育频道"都会被误判成"中央"，这是 Step 5 之前的旧 bug。
_CCTV_WORD_KEYS = ("cctv", "央视", "中央电视台", "中央电视")
# 各省级/市级行政名 → 归"地方"
_LOCAL_KEYS = ("北京", "上海", "天津", "重庆", "河北", "山西", "内蒙古", "辽宁",
               "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建", "江西", "山东",
               "河南", "湖北", "湖南", "广东", "广西", "海南", "四川", "贵州",
               "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆", "兵团")
# 明显垃圾/占位条目 → 直接剔除
_JUNK_KEYS = ("测试", "演示", "广告", "宣传", "待播", "敬请期待")


# ---------------------------------------------------------------------------
# 名称清洗与归一化（点1）
# ---------------------------------------------------------------------------
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")   # 匹配 (...)/(...)，含中文括号

def clean_channel_name(raw: str) -> str:
    """
    清洗频道显示名：
      - 去掉括号及其内容：CCTV-1(:3) → CCTV-1（括号常为去重序号/分组标签）
      - 折叠多余空白
    注意：不会剥离频道本身的编号（CCTV-1 / CCTV-13 的 -1、-13 是含义保留的）。
    """
    s = raw.strip()
    if not s:
        return "未命名"
    s = _PAREN_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(" ,，、:：—-·")
    return s or "未命名"


def normalize_key(name: str) -> str:
    """去重键：NFKC 归一化 → 去括号 → 去空白/标点 → 小写。"""
    s = unicodedata.normalize("NFKC", name)
    s = _PAREN_RE.sub("", s)
    s = re.sub(r"[\s,，、:：。·\-—]+", "", s)
    return s.lower()


# ---------------------------------------------------------------------------
# 分组判定（点2）
# ---------------------------------------------------------------------------
def classify_group(name: str, entry_group: str = "") -> str:
    """
    自动分类：中央 / 卫视 / 港澳台 / 地方 / 其他。

    判定顺序（关键，Step 6 修正）：
      1. **港澳台优先** —— "凤凰卫视中文台"名字里也含"卫视"，必须先剔除；
      2. **央视** —— 用 channel_filter.is_cctv 精确判（CCTV 后必须紧跟合法编号），
         而不是"名称里含 CCTV 就算"，否则 CCTV-Billiards / CCTV-Storm * 会混进"中央"；
      3. **卫视** —— 中文含"卫视"，或英文形态 `<省份> [Satellite] TV/Channel`
         （iptv-org 的省级上星频道多为英文名，此前全被误分到"其他"）；
      4. 地方 / 其他。
    """
    text = (name or "") + " " + (entry_group or "")
    low = text.lower()

    if is_hmt(text):
        return "港澳台"
    if is_cctv(name) or any(k in low for k in _CCTV_WORD_KEYS):
        return "中央"
    if is_cn_satellite(name) or is_province_satellite(name):
        return "卫视"
    if "卫视" in text:
        # 分组标签兜底（源清单里靠 entry_group 标明卫视的情况）
        return "卫视"
    for k in _LOCAL_KEYS:
        if k in text:
            return "地方"
    return "其他"


def _should_keep(name: str, entry_group: str = "") -> bool:
    """是否保留该条目：非空 且 不命中垃圾关键词。"""
    text = (name + " " + entry_group).lower()
    for k in _JUNK_KEYS:
        if k in text:
            return False
    return bool(name.strip())


# ---------------------------------------------------------------------------
# URL 推断：容器 kind 与 网络协议（点4）
# ---------------------------------------------------------------------------
def url_container_kind(url: str) -> str:
    """按 URL 扩展名推断流容器类型：hls / ts / flv / rtmp / m3u / http。"""
    low = url.lower()
    if ".m3u8" in low:
        return "hls"
    if low.startswith("rtmp"):
        return "rtmp"
    if ".ts" in low:
        return "ts"
    if ".flv" in low:
        return "flv"
    if ".m3u" in low:
        return "m3u"
    return "http"


def url_protocol(url: str) -> str:
    """判断网络协议族：ipv6（含 [..]） / ipv4（纯 IPv4 字面量） / domain（域名）。"""
    low = url.lower()
    if "[" in low:
        return "ipv6"
    if re.search(r"//\d{1,3}(\.\d{1,3}){3}(:\d+)?", low):
        return "ipv4"
    return "domain"


# ---------------------------------------------------------------------------
# 源抓取方式（点3：kind 按 URL 尾字自动判断）
# ---------------------------------------------------------------------------
def detect_fetch_kind(url: str) -> str:
    """判断一个搜刮源 URL 的抓取方式：\"m3u\"（直链清单）或 \"html\"（网页，需提取链接）。"""
    low = url.lower()
    if ".m3u8" in low or ".m3u" in low:
        return "m3u"
    return "html"


_M3U_LINK_RE = re.compile(r"""["'\s]+([^"'\s]+\.m3u8?)[\s"']""", re.I)


def _extract_m3u_links(html: str, base_url: str) -> List[str]:
    """从网页 HTML 中提取 .m3u / .m3u8 直链（resolve 相对地址、去重）。"""
    out: List[str] = []
    for hit in _M3U_LINK_RE.findall(html):
        resolved = urljoin(base_url, hit.strip())
        if resolved and resolved not in out:
            out.append(resolved)
    return out


async def fetch_entries(client: httpx.AsyncClient, url: str,
                        origin: str) -> List[ParsedEntry]:
    """
    拉取**单个清单地址**并解析为条目（m3u 直链，或 html 页里提取出的 m3u 链接）。

    抽成公共函数是为了让 `_fetch_and_parse()`（Step 2 全量搜刮）与
    `core/sync_sources.py`（手动同步）共用同一套抓取/解析逻辑，避免两处实现漂移。
    失败时抛异常，由调用方决定是"跳过这个源"还是"换一个加速地址重试"。
    """
    kind = detect_fetch_kind(url)
    log.info("抓取清单 [%s] kind=%s %s", origin, kind, url)

    resp = await client.get(url, timeout=cfg.HTTP_TIMEOUT)
    resp.raise_for_status()

    if kind == "m3u":
        return parse_m3u(resp.content, origin=origin)

    # html 页：提取页内 m3u 链接并逐一解析（轻量实现）
    links = _extract_m3u_links(resp.text, base_url=url)
    if not links:
        log.warning("网页 [%s] 未发现 .m3u/.m3u8 链接", origin)
        return []
    entries: List[ParsedEntry] = []
    for lk in links[:8]:
        sub = await client.get(lk, timeout=cfg.HTTP_TIMEOUT)
        sub.raise_for_status()
        entries.extend(parse_m3u(sub.content, origin=origin))
    return entries


async def _fetch_and_parse(source: dict, client: httpx.AsyncClient) -> List[ParsedEntry]:
    """拉取单个搜刮源并解析。失败抛异常（由外层捕获）。kind 由 URL 自动判断。"""
    return await fetch_entries(client, source.get("url", ""), source.get("id", "?"))


# ---------------------------------------------------------------------------
# 合并：同名频道聚合
# ---------------------------------------------------------------------------
def _merge_entries(entries: Dict[str, ParsedEntry], entry: ParsedEntry) -> None:
    key = normalize_key(entry.name)
    if key in entries:
        existing = entries[key]
        for u in entry.all_urls():
            if u not in existing.all_urls():
                existing.extra_urls.append(u)
        if existing.group == "其他" and entry.group != "其他":
            existing.group = entry.group
        if not existing.logo and entry.logo:
            existing.logo = entry.logo
        # 记录第一个出现的来源标识（合并后仍可追踪）
        if existing.origin == "scrape":
            existing.origin = entry.origin
    else:
        entries[key] = entry


def _filter_group_sort(entries: List[ParsedEntry]) -> List[ParsedEntry]:
    """
    清洗名称 → **规范名** → 过滤垃圾 → 核心白名单过滤（只留央视/卫视）
    → 同名合并 → 按(分类,名)排序。

    名称在这里就规范化（而不是等到落库），有个额外好处：
    合并键用的是规范名，于是上游同一台的中/英两种写法
    （`Beijing Satellite TV HD` 与 `北京卫视 HD`）**在合并阶段就能并成一条**，
    而不是落库后才靠别名合并去补。

    Step 6 新增白名单这一步，保证重跑搜刮不会再把这些灌进库：
    港澳台、省市地面频道、购物频道、境外频道，以及 iptv-org 里
    CCTV-Billiards / CCTV-Storm * 之类挂 CCTV 名头的"假央视"条目。
    """
    keep: Dict[str, ParsedEntry] = {}
    dropped = 0
    for e in entries:
        e.name = canonical_channel_name(clean_channel_name(e.name))
        if not _should_keep(e.name, e.group):
            continue
        if not is_core_channel(e.name, e.group):
            dropped += 1
            continue
        _merge_entries(keep, e)
    if dropped:
        log.info("核心白名单过滤：丢弃非央视/卫视条目 %d 条", dropped)
    result = list(keep.values())
    result.sort(key=lambda e: (classify_group(e.name, e.group), normalize_key(e.name)))
    return result


# ---------------------------------------------------------------------------
# 落库（点5：source_id 落库）
# ---------------------------------------------------------------------------
@dataclass
class ScrapeResult:
    total_fetched: int = 0
    channels_created: int = 0
    sources_created: int = 0
    failed_sources: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _persist(entries: List[ParsedEntry]) -> ScrapeResult:
    """
    把清洗/过滤/合并后的条目写入数据库。
    已有频道只补录尚未拥有的新地址，不覆盖已有手动源，保持既有优先级稳定。

    落库前统一过一遍 `naming.canonical_channel_name()` —— 上游对同一个台的写法
    五花八门（`CCTV4` / `CCTV-4` / `CCTV-1综合` / `Beijing Satellite TV HD`），
    规范化后既好读，也避免"写法不同就重复建台"。
    """
    result = ScrapeResult()
    with session_scope() as s:
        existing = s.query(Channel).all()
        # 用**规范化后**的键索引已有频道，这样库里若残留 `CCTV4`，
        # 上游来的 `CCTV-4` 也能对上、不会重复建台。
        by_key = {normalize_key(canonical_channel_name(c.name)): c for c in existing}

        for entry in entries:
            canon = canonical_channel_name(entry.name)
            key = normalize_key(canon)
            chan = by_key.get(key)

            # 全新频道 → 创建
            if chan is None:
                chan = Channel(
                    name=canon,
                    group_name=classify_group(entry.name, entry.group),
                    sort_order=0,
                    # 不传 is_visible：该列已废弃，由模型 default=True 兜底
                    logo_url=entry.logo,
                )
                s.add(chan)
                s.flush()          # 取 chan.id
                by_key[key] = chan
                result.channels_created += 1

            # 录源：只补录频道尚未拥有的地址
            existing_urls = {so.url for so in chan.sources}
            origin = entry.origin or "scrape"
            prio = len(chan.sources)   # 顺延追加，保持既有优先级稳定
            for i, url in enumerate(entry.all_urls(), start=1):
                if url in existing_urls:
                    continue
                s.add(Source(
                    channel_id=chan.id,
                    url=url,
                    source_id=f"{origin}:{key}:{i}",
                    origin=origin,
                    kind=url_container_kind(url),
                    protocol=url_protocol(url),
                    default_priority=prio,
                    is_healthy=False,
                ))
                result.sources_created += 1
                prio += 1

    return result


# ---------------------------------------------------------------------------
# 对外统一入口
# ---------------------------------------------------------------------------
def build_client() -> httpx.AsyncClient:
    """构建搜刮/同步共用的 HTTP 客户端（伪装浏览器 UA、跟随跳转、宽松超时）。"""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(cfg.SCRAPE_TIMEOUT, connect=cfg.HTTP_TIMEOUT),
        headers=cfg.HTTP_HEADERS,
        follow_redirects=True,
    )


# 旧名保留（内部曾用 _build_client），新代码请用 build_client()
_build_client = build_client


async def scrape_and_store(
    sources: Optional[List[dict]] = None,
    limit_total: Optional[int] = None,
) -> ScrapeResult:
    """拉取全部搜刮源 → 解析 → 清洗过滤合并 → 落库 → 返回统计。"""
    src_list = sources if sources is not None else cfg.SCRAPE_SOURCES
    if limit_total is not None:
        src_list = src_list[:limit_total]

    result = ScrapeResult()
    if not src_list:
        result.warnings.append("没有可配置的搜刮源")
        return result

    async with _build_client() as client:
        sem = asyncio.Semaphore(4)   # 限制并发，避免打爆网络
        tasks: List[asyncio.Task] = []

        async def _bounded(src: dict):
            async with sem:
                try:
                    return await _fetch_and_parse(src, client)
                except Exception as exc:  # single-source tolerance
                    log.warning("抓取源失败 [%s]: %s", src.get("id", "?"), exc)
                    result.failed_sources.append(src.get("id", "?"))
                    return []

        for src in src_list:
            tasks.append(asyncio.create_task(_bounded(src)))

        all_entries: List[ParsedEntry] = []
        for task in asyncio.as_completed(tasks):
            try:
                entries = await task
            except Exception as exc:  # task-level tolerance
                log.warning("搜刮任务异常: %s", exc)
                continue
            all_entries.extend(entries)
            result.total_fetched += len(entries)

    log.info("原始条目合计: %d", result.total_fetched)
    filtered = _filter_group_sort(all_entries)
    log.info("清洗过滤合并后待入库: %d", len(filtered))
    merged = _persist(filtered)

    result.channels_created = merged.channels_created
    result.sources_created = merged.sources_created
    log.info(
        "搜刮完成: 新增频道=%d 新增源=%d 失败源=%s",
        result.channels_created, result.sources_created,
        result.failed_sources or "无",
    )
    return result


def run_scrape(
    sources: Optional[List[dict]] = None,
    limit_total: Optional[int] = None,
) -> ScrapeResult:
    """同步便捷入口：在事件循环中执行 async 版本（供脚本/测试调用）。"""
    async def _run():
        return await scrape_and_store(sources, limit_total)
    return asyncio.run(_run())
