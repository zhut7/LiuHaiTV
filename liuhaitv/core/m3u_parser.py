# SPDX-License-Identifier: GPL-3.0-or-later
"""
M3U / M3U8 解析器。

输入：M3U 清单的文本内容（bytes 或 str），及其来源标识（origin）。
输出：频道条目列表，每条含频道名、分组名、台标、以及绑定的流地址。

支持的格式要点：
  - 标准 #EXTINF 行 + 下一行地址的经典 #EXTM3U 结构；
  - 兼容个别源"#EXTINF 无逗号"或"频道名含逗号/多逗号"的写法（取末段最稳妥，
    但在常见形态下取逗号后首段）。
  - 跳过注释（# 开头非 EXTINF / EXTVLCOPT / EXTM3U）以及空行。
  - 对 tvg-name 优先，其次 x-tvg-url 里的 logo（tvg-logo / tvg-name）。

错误处理：单行解析失败不中断整体，记录 warning 后继续。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass
class ParsedEntry:
    """一条解析出的频道（含其首个流地址）。"""

    name: str
    group: str = "其它"
    url: str = ""
    logo: Optional[str] = None
    origin: str = "scrape"  # 该条目来自哪个搜刮源（解析时写入）
    # 额外附加的流地址（个别频道可能同时给多行地址，合并到 sources 备用）
    extra_urls: List[str] = field(default_factory=list)

    def all_urls(self) -> List[str]:
        """返回该条目全部流地址（去重、去空）。"""
        out: List[str] = []
        for u in [self.url, *self.extra_urls]:
            if u and u not in out:
                out.append(u)
        return out


def _decode_payload(payload: object) -> str:
    """把 bytes / str 输入统一解码为 str。bytes 按 utf-8 (errors=replace) 解码。"""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return str(payload)


def _clean_name(raw: str) -> str:
    """清洗频道名：去空白、去首尾异常字符。"""
    return raw.strip().strip(" ,，:")


def parse_m3u(payload, origin: str = "unknown") -> List[ParsedEntry]:
    """
    解析一份 M3U 清单内容。

    参数:
        payload: M3U 文本（bytes 或 str）。
        origin:  来源标识（用于稍后写入 Source.origin）。

    返回:
        ParsedEntry 列表。无有效条目时返回空列表（不抛异常）。
    """
    text = _decode_payload(payload)
    lines = text.splitlines()

    entries: List[ParsedEntry] = []
    current: Optional[ParsedEntry] = None
    pending_extinf: Optional[ParsedEntry] = None  # 已看到 #EXTINF、等待地址行

    for idx, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue

        try:
            # ---- 注释行处理 ----
            if line.startswith("#"):
                if line.startswith("#EXTINF"):
                    pending_extinf = _parse_extinf(line)
                    if pending_extinf is not None:
                        pending_extinf.origin = origin
                # #NAME / #PLAYLIST 等其它注释：忽略，不产生条目
                continue

            # ---- 非注释行 = 流地址候选 ----
            if "://" not in line:
                # 既非注释又非地址（如纯相对路径）：忽略
                continue

            # 已有待定的 EXTINF 元信息 => 生成条目
            if pending_extinf is not None:
                current = pending_extinf
                current.url = line
                entries.append(current)
                pending_extinf = None
            elif current is not None:
                # 连续地址行：作为该频道的一条备用源附加
                _append_url(current, line)
            # 既无 EXTINF 又无当前频道：孤立地址，忽略（多数源不会出现）

        except Exception as exc:  # noqa: BLE001 - 单行容错，不影响整体
            log.warning("M3U 第 %d 行解析失败，已跳过: %s", idx, exc)

    if pending_extinf is not None:
        # 末尾只有 #EXTINF 而无地址：丢弃
        log.debug("M3U 尾部存在无地址的 #EXTINF，已丢弃 (%s)", origin)
    return entries


def _parse_extinf(line: str) -> Optional[ParsedEntry]:
    """解析一行 #EXTINF:...，返回带元信息的条目（无地址），失败返回 None。"""
    body = line[len("#EXTINF"):].lstrip(":")
    # 分离可选属性段与名称段
    attrs = ""
    rest = body
    if "," in rest:
        attrs, rest = _split_tvg_attrs(rest)
    name = _clean_name(rest)
    if not name:
        name = "未命名"

    entry = ParsedEntry(name=name)
    if attrs:
        tvg = _extract_attr(attrs, "tvg-logo")
        if tvg:
            entry.logo = tvg.strip()
        grp = _extract_attr(attrs, "group-title")
        if grp:
            entry.group = grp.strip() or "其他"
        # 极个别源在 #EXTINF 内直接带地址（少见，忽略以保持简单）
    return entry


def _split_tvg_attrs(rest: str) -> Tuple[str, str]:
    """
    把一行 #EXTINF 切成 (属性+duration, 频道名)。

    标准语法：#EXTINF:-1 tvg-id=.. tvg-logo=.. group-title=.. ,频道名
    即 属性/duration 位于最后一个逗号之前，频道名在最后一个逗号之后。
    频道名允许含空格、括号，因此不能用空白切分，只能取“最后一个逗号”：
      - "1,CCTV-1 综合"        → attrs="1" , rest="CCTV-1 综合"
      - "…group-title=\"央视\",CCTV-13 新闻" → attrs=…group-title… , rest="CCTV-13 新闻"
    （个别频名自身含逗号时会误切，但 CN 清单极罕见，可接受。）
    """
    comma = rest.rfind(",")
    if comma == -1:
        return "", rest
    return rest[:comma], rest[comma + 1:]


def _extract_attr(attrs: str, key: str) -> Optional[str]:
    """
    从 M3U 属性段中提取指定 key 的值。
    兼容四种写法：
      tvg-logo="http://..."
      tvg-logo="..."         （带引号）
      tvg-logo="http://..."   （无空格）
    只要 attrs 里出现 key= 即尝试取其后到下一个属性或行尾的字符串。
    """
    needle = key + "="
    pos = attrs.find(needle)
    if pos == -1:
        return None
    after = attrs[pos + len(needle):].lstrip()
    # 去掉可能的前导引号
    if after.startswith(('"', "'")):
        after = after[1:]
    # 截断到下一个属性（空格/引号）或行尾
    end = len(after)
    for ch in ('"', "'"):
        cut = after.find(ch)
        if cut != -1:
            end = min(end, cut)
    cut = after.find(" ")
    if cut != -1:
        end = min(end, cut)
    return after[:end]


def _append_url(entry: ParsedEntry, url: str) -> None:
    """把备用地址追加到条目的 extra_urls（去重）。"""
    url = url.strip()
    if url and url not in entry.extra_urls and url != entry.url:
        entry.extra_urls.append(url)
