# SPDX-License-Identifier: GPL-3.0-or-later
"""
GitHub 加速器：把"清单的原始 URL"改写成可实际拉取的候选地址。

## 为什么必须有它
本机（2026-09-23 实测）**`raw.githubusercontent.com` 直连完全不通**（curl 返回
连接失败），而 `*.github.io`（GitHub Pages）可以直连。项目里绝大多数精选直播源
都挂在 `raw.githubusercontent.com` 上，所以"加速"不是可选优化，而是这类源能不能用的前提。

实测可用（均为 HTTP 200）：
  - jsDelivr：把 `raw.githubusercontent.com/<u>/<r>/<b>/<path>`
    改写成 `cdn.jsdelivr.net/gh/<u>/<r>@<b>/<path>`
  - 前缀式代理：`gh-proxy.com` / `ghproxy.net` / `ghfast.top`，形如 `<代理前缀> + 原始 URL`

实测**不可用**（已从配置里剔除，留档免得后人再试）：
  `mirror.ghproxy.com`、`hub.fastgit.xyz`、`raw.kkgithub.com`、`github.com/.../raw/...`

## URL 形态与适配关系

| 原始 URL 形态 | direct | jsdelivr | 前缀式代理 |
|---|---|---|---|
| `raw.githubusercontent.com/<u>/<r>/<b>/<path>` | ✅ 原样 | ✅ 改写成 cdn.jsdelivr.net/gh | ✅ 前缀拼接 |
| `<user>.github.io/<repo>/<path>` | ✅ 原样 | ✅ 走仓库 gh-pages 分支 | ❌ 返回 403 |
| `github.com/...` | ✅ 原样 | ❌ | ✅ 前缀拼接 |

不适配的组合会被**跳过**（返回 None），不会被当成失败。

## 用法

    from liuhaitv.core import mirror

    for u in mirror.candidates(src["url"], "auto"):
        try:
            data = fetch(u)          # 调用方自己发请求
            break                    # 第一个成功的就用它
        except Exception:
            continue
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

import liuhaitv.config as cfg

log = logging.getLogger(__name__)

__all__ = [
    "list_mirrors", "mirror_name", "apply_mirror", "candidates", "can_handle",
]

_RAW_RE = re.compile(
    r"^https?://raw\.githubusercontent\.com/"
    r"(?P<user>[^/]+)/(?P<repo>[^/]+)/(?P<branch>[^/]+)/(?P<path>.+)$",
    re.I,
)
_PAGES_RE = re.compile(
    r"^https?://(?P<user>[^./]+)\.github\.io/(?P<repo>[^/]+)/(?P<path>.+)$",
    re.I,
)
_GH_RE = re.compile(r"^https?://github\.com/", re.I)


def _defs() -> Dict[str, dict]:
    return {m["id"]: m for m in cfg.GITHUB_MIRRORS}


def list_mirrors() -> List[dict]:
    """给 UI 下拉用的加速方式列表（不含 auto 之外的过滤）。"""
    return list(cfg.GITHUB_MIRRORS)


def mirror_name(mirror_id: Optional[str]) -> str:
    """取加速方式的显示名；未知 id 原样返回。"""
    mid = mirror_id or cfg.DEFAULT_MIRROR
    d = _defs().get(mid)
    return d["name"] if d else str(mid)


def _jsdelivr(url: str) -> Optional[str]:
    """raw / GitHub Pages → cdn.jsdelivr.net 形式。"""
    m = _RAW_RE.match(url)
    if m:
        return ("https://cdn.jsdelivr.net/gh/%s/%s@%s/%s"
                % (m.group("user"), m.group("repo"), m.group("branch"), m.group("path")))
    m = _PAGES_RE.match(url)
    if m:
        # GitHub Pages 的内容来自仓库的 gh-pages 分支（如 iptv-org/iptv@gh-pages/...）
        return ("https://cdn.jsdelivr.net/gh/%s/%s@gh-pages/%s"
                % (m.group("user"), m.group("repo"), m.group("path")))
    return None


def apply_mirror(url: str, mirror_id: Optional[str]) -> Optional[str]:
    """
    用指定加速方式改写 URL；**该方式不适配这个 URL 形态时返回 None**。

    `mirror_id="auto"` 不适用本函数（auto 是"依次尝试"，请用 `candidates()`）。
    """
    mid = mirror_id or cfg.DEFAULT_MIRROR
    if mid == "auto":
        return None

    if mid == "direct":
        return url
    if mid == "jsdelivr":
        return _jsdelivr(url)

    d = _defs().get(mid)
    if d is None:
        log.warning("未知的加速方式: %s", mid)
        return None
    prefix = d.get("prefix")
    if prefix is None:            # auto 或其他特殊项
        return None
    if d.get("id") == "direct":
        return url
    # 前缀式代理：只认 github.com / raw.githubusercontent.com
    if _RAW_RE.match(url) or _GH_RE.match(url):
        return prefix + url
    return None


def can_handle(url: str, mirror_id: str) -> bool:
    """该加速方式能否处理这个 URL（供 UI 置灰不适用项 / 引擎跳过）。"""
    return apply_mirror(url, mirror_id) is not None


def candidates(url: str, mirror_id: Optional[str] = None) -> List[str]:
    """
    返回"可以依次尝试"的 URL 列表（已去重、已剔除不适配项）。

    - `mirror_id="auto"`（或未指定）→ 按 `config.MIRROR_TRY_ORDER` 依次给出所有候选。
    - 指定某个加速方式 → 只给出它改写后的地址；**但末尾一定附上原始 URL 兜底**，
      避免用户选错加速方式后整个同步直接失败（原始 Pages 地址往往是可直连的）。
    """
    mid = mirror_id or cfg.DEFAULT_MIRROR
    order = list(cfg.MIRROR_TRY_ORDER) if mid == "auto" else [mid]

    out: List[str] = []
    for m in order:
        c = apply_mirror(url, m)
        if c and c not in out:
            out.append(c)
    if url not in out:
        out.append(url)           # 兜底：原始地址
    return out
