# SPDX-License-Identifier: GPL-3.0-or-later
"""
核心频道白名单（Step 6：频道精简）。

LiuHaiTV 只关心两类频道：

  1. **央视（CCTV）** —— CCTV-1 ~ CCTV-17 主频道，以及 CCTV-4K / CCTV-8K / CCTV-5+ 等衍生频道；
     允许 `HD` / `SD` / `高清` 等清晰度或描述后缀；也允许 `CCTV+`（央视海外版 CCTV+ 1 / 2）。
  2. **省级/直辖市级卫视** —— 中文名含「卫视」（北京卫视、湖南卫视…），
     或英文形如 `<省份> [Satellite] TV/Channel [HD]`（Beijing Satellite TV、Hunan TV、
     Jiangsu Satellite TV…）。iptv-org 这类仓库的省级上星频道大量使用英文名，
     所以必须同时支持英文形态，否则会被误判成「其他」。

明确排除（顺序很重要）：

  - **港澳台**：凤凰卫视、TVB 明珠台… —— 名字里也带「卫视」，必须先于卫视规则判定；
  - **挂着 CCTV 名头的非真实央视条目**：iptv-org 里存在
    `CCTV-Billiards` / `CCTV-Storm *` / `CCTV-Golf & Tennis` / `CCTV-Women's Fashion SD`
    这类并不存在的"央视"频道，本模块要求 CCTV 后面必须紧跟合法编号，把它们挡掉；
  - **省市地面频道**（Anshun / Chuzhou / QTV-* / Xinjiang TV 2…）、
    **购物频道**（Fengshang Shopping）、**境外频道**（VoA / TV BRICS / ABN China）、
    **CETV 等非央视非卫视的国家级频道**。

两个使用方：

  - `liuhaitv/core/scraper.py`：落库前过滤 —— 重跑搜刮不会再把杂项灌进库；
  - `scripts/prune_to_core.py`：对**已有**库做一次性真删。
"""
from __future__ import annotations

import re
from typing import Optional

__all__ = [
    "bare_name",
    "is_hmt",
    "is_cctv",
    "is_cn_satellite",
    "province_of",
    "is_province_satellite",
    "is_satellite",
    "core_kind",
    "is_core_channel",
    "identity_key",
    "family_key",
]


# ---------------------------------------------------------------------------
# 名称清洗
# ---------------------------------------------------------------------------
# iptv-org 常用方括号标注补充信息：[Not 24/7] / [Geo-blocked]；圆括号多为主题分组。
_TAG_RE = re.compile(r"[\[(][^\])]*[\])]")
_WS_RE = re.compile(r"\s+")


def bare_name(name: str) -> str:
    """去掉 `[Not 24/7]` / `[Geo-blocked]` / `(…)` 附注并折叠空白，便于比对。"""
    s = _TAG_RE.sub(" ", name or "")
    return _WS_RE.sub(" ", s).strip()


# ---------------------------------------------------------------------------
# 规则 1：港澳台（必须先判，因为"凤凰卫视"里也含"卫视"）
# ---------------------------------------------------------------------------
_HMT_KEYS = (
    "凤凰", "香港", "澳门", "台湾", "澳亚", "明珠", "本港", "星空", "有线电视",
    "东风卫视", "中天", "东森", "三立", "民视", "华视", "台视", "无线电视",
    "tvb", "tvbs", "rthk", "港台", "濠江", "莲花", "澳广视",
)


def is_hmt(text: str) -> bool:
    """是否为港澳台频道（含电视/卫视字样，但属港澳台，需剔除）。"""
    low = (text or "").lower()
    return any(k in low for k in _HMT_KEYS)


# ---------------------------------------------------------------------------
# 规则 2：央视（CCTV）
# ---------------------------------------------------------------------------
# 合法编号：1~17 / 4K / 8K / 5+；CCTV 之后允许 `-`、空格、`+` 等分隔；
# 编号后允许一串清晰度/描述后缀（HD、SD、高清、体育、赛事…）。
# 关键：编号必须紧跟分隔符，因此 CCTV-Billiards / CCTV-Storm Football 之类不会命中。
_CCTV_RE = re.compile(
    r"^cctv\s*(?:\+\s*)?[- ]?\s*"          # 前缀 CCTV，允许 CCTV+ / CCTV- / CCTV
    r"(?:5\s*\+|4k|8k|1[0-7]|[1-9])"       # 合法编号（先长后短，避免 13 被 1 截断）
    r"(?:\s*[- ]?\s*"                       # 可选后缀
    r"(?:hd|sd|fhd|uhd|4k|8k|pluss?|高清|超清|标清|体育|赛事|综合|\+))*"
    r"\s*$",
    re.I,
)


def is_cctv(name: str) -> bool:
    """是否为真实央视频道（CCTV-1~17 / 4K / 8K / 5+ / CCTV+）。"""
    return bool(_CCTV_RE.match(bare_name(name)))


# ---------------------------------------------------------------------------
# 规则 3：卫视（中文名 / 英文省份上星频道）
# ---------------------------------------------------------------------------
# 省份英文（含常见别名）→ 规范中文地区名
_PROVINCE_ALIASES = {
    "beijing": "北京", "tianjin": "天津", "hebei": "河北", "shanxi": "山西",
    "nei monggol": "内蒙古", "neimonggol": "内蒙古", "inner mongolia": "内蒙古",
    "liaoning": "辽宁", "jilin": "吉林", "heilongjiang": "黑龙江",
    "shanghai": "上海", "dragon": "上海",           # Dragon TV = 东方卫视
    "jiangsu": "江苏", "zhejiang": "浙江", "anhui": "安徽", "fujian": "福建",
    "jiangxi": "江西", "shandong": "山东", "henan": "河南", "hubei": "湖北",
    "hunan": "湖南", "guangdong": "广东", "guangxi": "广西", "hainan": "海南",
    "chongqing": "重庆", "sichuan": "四川", "guizhou": "贵州", "yunnan": "云南",
    "tibet": "西藏", "xizang": "西藏", "shaanxi": "陕西", "gansu": "甘肃",
    "qinghai": "青海", "ningxia": "宁夏", "xinjiang": "新疆", "bingtuan": "兵团",
    # 计划单列市 / 自治州上星频道（深圳卫视、厦门卫视、延边卫视）
    "shenzhen": "深圳", "xiamen": "厦门", "yanbian": "延边",
}

# 中文地区名 → 规范中文地区名（用于把"东方卫视"归到"上海"等）
_CN_REGIONS = {
    "北京": "北京", "天津": "天津", "河北": "河北", "山西": "山西", "内蒙古": "内蒙古",
    "辽宁": "辽宁", "吉林": "吉林", "黑龙江": "黑龙江", "上海": "上海", "东方": "上海",
    "江苏": "江苏", "浙江": "浙江", "安徽": "安徽", "福建": "福建", "东南": "福建",
    "海峡": "福建", "江西": "江西", "山东": "山东", "泰山": "山东", "河南": "河南",
    "湖北": "湖北", "湖南": "湖南", "广东": "广东", "南方": "广东", "广西": "广西",
    "海南": "海南", "重庆": "重庆", "四川": "四川", "贵州": "贵州", "云南": "云南",
    "西藏": "西藏", "陕西": "陕西", "甘肃": "甘肃", "青海": "青海", "宁夏": "宁夏",
    "新疆": "新疆", "兵团": "兵团", "深圳": "深圳", "厦门": "厦门", "延边": "延边",
}

# 英文形态：<省份> [Satellite] TV/Television/Channel，尾部允许清晰度/序号修饰。
# 省份段用惰性匹配，靠"整串必须匹配完"来排除 Anshun Comprehensive News Channel 之类。
_SAT_EN_RE = re.compile(
    r"^(?P<prov>[a-z ]+?)\s+"
    r"(?:satellite\s+)?"
    r"(?:tv|television|channel)"
    r"(?:\s+(?:hd|sd|fhd|uhd|4k|8k|1|blue|international|tibetan))*"
    r"\s*$",
    re.I,
)


def is_cn_satellite(name: str) -> bool:
    """中文名里含「卫视」即视为上星频道（需先排除港澳台）。"""
    return "卫视" in bare_name(name)


def province_of(name: str) -> Optional[str]:
    """
    解析频道所属省级地区（规范中文名，如 "北京"/"湖南"）；无法识别返回 None。

    支持中文名（北京卫视 / 东方卫视）与英文名（Beijing Satellite TV / Hunan TV）两种形态。
    """
    s = bare_name(name)
    if not s:
        return None

    # 中文：只要出现已知地区名即归属（"北京卫视 HD"、"浙江卫视 蓝"）
    for cn, canon in _CN_REGIONS.items():
        if cn in s:
            return canon

    m = _SAT_EN_RE.match(s)
    if m:
        prov = _WS_RE.sub(" ", m.group("prov").strip().lower())
        return _PROVINCE_ALIASES.get(prov)
    return None


def is_province_satellite(name: str) -> bool:
    """是否为省级上星频道（英文形态，如 Beijing Satellite TV / Hunan TV）。"""
    return province_of(name) is not None and bool(_SAT_EN_RE.match(bare_name(name)))


def is_satellite(name: str) -> bool:
    """是否为省级卫视（中文含"卫视" 或 英文省份上星频道）；港澳台需另判。"""
    if is_hmt(name):
        return False
    return is_cn_satellite(name) or is_province_satellite(name)


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def core_kind(name: str) -> Optional[str]:
    """返回核心类型：`"cctv"` / `"satellite"` / `None`（非核心）。"""
    if is_hmt(name):
        return None
    if is_cctv(name):
        return "cctv"
    if is_satellite(name):
        return "satellite"
    return None


def is_core_channel(name: str, group_name: str = "") -> bool:
    """
    是否为需要保留的核心频道（央视 / 省级卫视）。

    `group_name` 参与判定：源清单里少数条目只在分组标签里写了"卫视"，
    此时按分组兜底（例如 name="XX台" + group="卫视"）。

    注意兜底也必须先排除港澳台：像 `香港卫视` 这种名字含"香港"、
    却被上游标成 `group-title="卫视"` 的条目，绝不能因为分组标签就放行。
    """
    if core_kind(name) is not None:
        return True
    if is_hmt(name):                       # 名字本身是港澳台 → 绝不保留
        return False
    if not is_hmt(group_name) and "卫视" in (group_name or ""):
        return is_cn_satellite(name)
    return False


# 只截取"CCTV + 分隔 + 合法编号"这一段作为频道身份
_CCTV_TOKEN_RE = re.compile(
    r"^(cctv\s*(?:\+\s*)?[- ]?\s*(?:5\s*\+|4k|8k|1[0-7]|[1-9]))", re.I
)
# 清晰度标记：决定 CCTV-1 与 CCTV-1 HD 是否算同一频道
_QUALITY_RE = re.compile(r"(fhd|uhd|hd|sd|4k|8k)", re.I)


def identity_key(name: str) -> str:
    """
    同一频道的归一键，用于合并"同一频道的多个别名条目"。

    - 央视：`CCTV + 编号` + 可选清晰度。于是
      `CCTV-5+`（体育赛事频道）与 `CCTV-5+ 体育` 归并成一条；
      而 `CCTV-1` / `CCTV-1 HD`、`CCTV-6` / `CCTV-6 HD` **故意不合并**
      —— 它们是不同清晰度的独立流，合并属于"多源补全"范畴，不在精简职责内。
    - 卫视：按省级地区归并 —— `北京卫视 HD`、`Beijing Satellite TV HD`、
      `Beijing Satellite TV` 得到同一个 key，可合并为一条。
    """
    s = bare_name(name)
    if core_kind(s) == "satellite":
        prov = province_of(s)
        return "sat:" + (prov or s.lower())

    m = _CCTV_TOKEN_RE.match(s)
    if not m:
        return "cctv:" + re.sub(r"[\s\-]+", "", s).lower()
    token = re.sub(r"[\s\-]+", "", m.group(1)).lower()   # "CCTV-5+" -> "cctv5+"
    rest = s[m.end():]
    q = _QUALITY_RE.search(rest)
    return "cctv:" + token + ("|" + q.group(1).lower() if q else "")


def family_key(name: str) -> str:
    """
    「同属一个台」的粗粒度键 —— 与 identity_key 的**唯一区别是忽略清晰度**。

    用途：`core/sync_sources.py` 做**兜底匹配**。
    上游清单普遍用不带清晰度的名字（`CCTV-16`），而库里可能是 `CCTV-16 HD`
    （Step 6 保留了两者并列）。严格键不同，若不兜底，同步就会以为"上游多了一个台"
    而重复建频道。先用 identity_key 精确匹配，匹配不上再按 family_key 找唯一候选。

    例：identity_key("CCTV-16 HD") = "cctv:cctv16|hd"，而
        family_key("CCTV-16 HD") = family_key("CCTV-16") = "cctv:cctv16"。
    卫视两者相同（地区本身就是身份）。
    """
    s = bare_name(name)
    if core_kind(s) == "satellite":
        prov = province_of(s)
        return "sat:" + (prov or s.lower())
    m = _CCTV_TOKEN_RE.match(s)
    if not m:
        return "cctv:" + re.sub(r"[\s\-]+", "", s).lower()
    return "cctv:" + re.sub(r"[\s\-]+", "", m.group(1)).lower()

