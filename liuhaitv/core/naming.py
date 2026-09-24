# SPDX-License-Identifier: GPL-3.0-or-later
"""
频道名规范化（Step 8）—— 全项目**唯一**的"规范显示名"来源。

## 为什么需要
各上游清单对同一个台的写法五花八门：
  - aptv 写 `CCTV4`、iptv-org 写 `CCTV-4`、fanmingming 写 `CCTV-1综合`
  - 省级卫视还有 `Beijing Satellite TV HD` / `Ningxia Satellite Channel` 这类英文形态
混在一起列表看着乱，开启"新增频道"时还会因为写法不同反复建出重复台。

## 规则
1. `NFKC` 归一（全角→半角）→ 去掉 `[Not 24/7]` / `[Geo-blocked]` 等上游元数据标签
   → 折叠空白。
2. **央视**：统一成 `CCTV-<编号>`（1~17 / 4K / 8K / 5+）；
   丢弃"综合/体育/赛事"等中文节目名后缀（与库里既有惯例一致：`CCTV-1`、`CCTV-5+`），
   只保留清晰度后缀 `HD/SD/FHD/UHD`。
   `CCTV+ 1`（央视海外版）保持 `CCTV+ <编号>` 形态，**不与** `CCTV-1` 混。
3. **省级卫视英文名 → 中文规范名**，且只在形态**完全匹配**时才改写：
   `Beijing Satellite TV HD` → `北京卫视 HD`、`Ningxia Satellite Channel` → `宁夏卫视`、
   `Xinjiang TV 1` → `新疆卫视`、`Xizang TV Tibetan` → `西藏卫视 藏语`。
   带额外语义的（`Dragon TV International`、`Dragon TV` 之外的 `... International`）不动，
   避免误伤。
4. **其它**：只做第 1 步清洗，保持原名。

## 边界（重要）
本模块只管**显示名**。"是不是同一个台"依旧由
`liuhaitv/core/channel_filter.py` 的 `identity_key()` / `family_key()` 决定 ——
两者不要互相替代，改名不该影响频道对齐。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

from liuhaitv.core.channel_filter import (
    _CCTV_TOKEN_RE, _PROVINCE_ALIASES, bare_name,
)

log = logging.getLogger(__name__)

__all__ = ["canonical_channel_name", "is_canonical", "canonical_key"]

_WS_RE = re.compile(r"\s+")

# CCTV-<编号><+?> / CCTV+ <编号>；编号含 4K/8K/5+
_CCTV_PREFIX_RE = re.compile(r"^cctv(\+)?(\d{1,2}|4k|8k)(\+)?$", re.I)

# 清晰度标记（4K/8K 属编号，不在此列）
_QUALITY_RE = re.compile(r"(?<![a-z0-9])(fhd|uhd|hd|sd)(?![a-z0-9])", re.I)

# 英文省级卫视：<省份> [Satellite] TV/Television/Channel [1] [清晰度] [语言]
# 用最严格的形态：多一个词就不改写，宁可保留原文也不误伤
_EN_SAT_RE = re.compile(
    r"^(?P<prov>[a-z .]+?)\s+"
    r"(?:satellite\s+)?"
    r"(?:tv|television|channel)"
    r"(?:\s+1)?"
    r"(?:\s+(?P<q>fhd|uhd|hd|sd|4k|8k))?"
    r"(?:\s+(?P<lang>tibetan|mongolian|uyghur|kazakh))?"
    r"$",
    re.I,
)

# 少数不能按"<省份>卫视"套的特殊形态
_EN_OVERRIDES = {
    "dragon": "东方卫视",           # Dragon TV = 东方卫视（不是"上海卫视"）
}

# 语言后缀的中文写法
_LANG_CN = {"tibetan": "藏语", "mongolian": "蒙古语", "uyghur": "维吾尔语", "kazakh": "哈萨克语"}


def _canon_cctv(s: str) -> Optional[str]:
    """央视条目 → 规范名；不是央视返回 None。"""
    m = _CCTV_TOKEN_RE.match(s)
    if not m:
        return None
    raw = re.sub(r"[\s\-_]+", "", m.group(1)).lower()      # "CCTV-5+" -> "cctv5+"
    pm = _CCTV_PREFIX_RE.match(raw)
    if not pm:
        return None

    plus_prefix, num, plus_suffix = pm.group(1), pm.group(2).upper(), pm.group(3)
    if plus_prefix:
        head = "CCTV+ %s" % num                            # 央视海外版 CCTV+ 1
    else:
        head = "CCTV-%s%s" % (num, "+" if plus_suffix else "")

    rest = s[m.end():]
    qm = _QUALITY_RE.search(rest)
    return head + (" " + qm.group(1).upper() if qm else "")


def _canon_satellite(s: str) -> Optional[str]:
    """英文省级卫视 → 中文规范名；形态不匹配返回 None（保持原样）。"""
    m = _EN_SAT_RE.match(s)
    if not m:
        return None
    prov = _WS_RE.sub(" ", m.group("prov").strip().lower())

    cn = _EN_OVERRIDES.get(prov)
    if cn is None:
        region = _PROVINCE_ALIASES.get(prov)
        if region is None:
            return None                                    # 非省级（市台/境外），不动
        cn = region + "卫视"

    parts = [cn]
    if m.group("q"):
        parts.append(m.group("q").upper())
    lang = (m.group("lang") or "").lower()
    if lang:
        parts.append(_LANG_CN.get(lang, lang))
    return " ".join(parts)


def canonical_channel_name(name: str) -> str:
    """
    返回频道的规范显示名。无法识别时至少做"去标签 + 折叠空白"的清洗。
    """
    if name is None:
        return ""
    raw = str(name)
    s = unicodedata.normalize("NFKC", raw)
    s = bare_name(s)                       # 去 [..] / (..) 标签 + 折叠空白
    if not s:
        return raw.strip()

    for fn in (_canon_cctv, _canon_satellite):
        out = fn(s)
        if out:
            return out
    return s


def is_canonical(name: str) -> bool:
    """该名字是否已是规范形态。"""
    return canonical_channel_name(name) == str(name or "")


def canonical_key(name: str) -> str:
    """
    规范名对应的去重键（复用 scraper.normalize_key，避免两套规则）。
    延迟导入是为了不把 scraper 的重依赖（httpx）拖进纯命名场景。
    """
    from liuhaitv.core.scraper import normalize_key
    return normalize_key(canonical_channel_name(name))
