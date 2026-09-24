# SPDX-License-Identifier: GPL-3.0-or-later
"""
手动同步直播源（主界面「🔄 同步源」按钮的后端）。

与 Step 2 的"全量搜刮"不同：这里做的是**按来源对账**，而不是无脑追加。
用户勾选若干来源 → 拉取它们的清单 → 把"该来源当前提供的地址"与
库里**该来源已有的地址**逐频道对齐。

## 对账规则（每个频道 × 每个选中来源）

| 情况 | 动作 |
|---|---|
| 同来源、未被手动改过 | **原地更换**为上游最新地址：行 id / 优先级不变，只作废旧健康结论 |
| 上游给了库里没有的地址 | **新增**一条源（默认置顶，见下） |
| 上游不再提供的自动源 | **删除** |
| 该来源的地址**被手动改过**（`Source.is_user_edited=True`） | **绝不覆盖**，另把上游地址插为新源，并**强制置顶** |
| `origin` 以 `user` 开头（用户自己加/导入的源） | 完全不参与对账，永不删改 |

「置顶」= 该源优先级设为 0，其余源优先级顺延 +1，于是它会成为 failover 的第一个候选。

## 频道怎么对齐（关键）

用 `channel_filter.identity_key()`，**不是**频道名：
上游叫 `Beijing Satellite TV`、库里叫 `北京卫视 HD`，两者 identity_key 都是 `sat:北京`，
因此能正确对到同一个频道。若改用名字匹配，上游一改名就会被当成新频道，
库里就会长出重复条目 —— 这正是 Step 6 之前踩过的坑。

## 线程

`sync_from_sources()` 是**同步阻塞**的（内部 `asyncio.run`），
必须由 UI 侧放进 QThreadPool 后台线程执行，绝不能在 GUI 线程直接调用。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import liuhaitv.config as cfg
from liuhaitv.core import mirror as mirror_mod
from liuhaitv.core.channel_filter import (
    core_kind, family_key, identity_key, is_core_channel,
)
from liuhaitv.core.database import session_scope
from liuhaitv.core.models import Channel, Source
from liuhaitv.core.naming import canonical_channel_name
from liuhaitv.core.scraper import (
    build_client, classify_group, fetch_entries,
    normalize_key, url_container_kind, url_protocol,
)

log = logging.getLogger(__name__)

# origin 以这些前缀开头 → 用户自己加的源，永不参与对账
USER_ORIGIN_PREFIXES = ("user",)

# 「新增频道」的三种口径：
#   none —— 只同步地址，绝不新建频道（默认，尊重用户自己筛过的列表）
#   cctv —— 只补缺失的央视主频道
#   all  —— 央视 + 卫视都补
NEW_CHANNEL_SCOPES = ("none", "cctv", "all")


def _scope_allows(scope: str, kind: Optional[str]) -> bool:
    if scope == "all":
        return True
    if scope == "cctv":
        return kind == "cctv"
    return False


def is_user_origin(origin: Optional[str]) -> bool:
    o = (origin or "").strip().lower()
    return any(o.startswith(p) for p in USER_ORIGIN_PREFIXES)


def list_sources() -> List[dict]:
    """给"同步直播源"弹窗用的来源清单（直接取自 config）。"""
    return list(cfg.SCRAPE_SOURCES)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
@dataclass
class OriginReport:
    """单个来源的同步结果。"""

    origin: str
    name: str = ""
    ok: bool = False
    error: str = ""
    used_url: str = ""          # 实际拉取成功的地址（可能是加速器改写后的）
    fetched: int = 0            # 上游命中的核心频道条目数
    added: int = 0              # 新增的源
    replaced: int = 0           # 原地更换的源
    removed: int = 0            # 上游已不提供、被删掉的自动源
    kept_manual: int = 0        # 因被手改而保留不动的源
    pinned: int = 0             # 置顶插入的源
    skipped_dup: int = 0        # 因地址已存在而跳过的
    channels_created: int = 0   # 因上游有新频道而新建
    channels_touched: int = 0   # 受影响的频道数
    created_names: List[str] = field(default_factory=list)   # 新建的频道名（前若干个）
    # 上游有、库里没有的核心频道数（未开启"允许新增频道"时只统计不创建）
    new_available: int = 0
    available_names: List[str] = field(default_factory=list)

    def line(self) -> str:
        title = self.name or self.origin
        if not self.ok:
            return "✗ %s —— %s" % (title, self.error or "失败")
        bits = []
        if self.replaced:
            bits.append("更换 %d" % self.replaced)
        if self.added:
            bits.append("新增 %d" % self.added)
        if self.removed:
            bits.append("删除失效 %d" % self.removed)
        if self.pinned:
            bits.append("置顶 %d" % self.pinned)
        if self.kept_manual:
            bits.append("保护手改 %d" % self.kept_manual)
        if self.skipped_dup:
            bits.append("跳过重复 %d" % self.skipped_dup)
        out = "✓ %s —— 上游 %d 个核心频道，影响 %d 个频道（%s）" % (
            title, self.fetched, self.channels_touched,
            "，".join(bits) if bits else "无需改动")
        if self.created_names:
            out += "\n    新建频道 %d 个：%s" % (
                self.channels_created, _name_list(self.created_names))
        if self.new_available:
            out += ("\n    另有 %d 个上游频道库里还没有（按当前「新增频道」设置未创建）：%s"
                    % (self.new_available, _name_list(self.available_names)))
        return out


def _name_list(names: List[str], limit: int = 12) -> str:
    head = "、".join(names[:limit])
    return head + ("…" if len(names) > limit else "")


@dataclass
class SyncReport:
    """一次同步的总体结果。"""

    reports: List[OriginReport] = field(default_factory=list)
    top_new: bool = True
    mirror_id: str = ""
    mirror_name: str = ""
    new_channel_scope: str = "none"

    @property
    def ok(self) -> bool:
        return any(r.ok for r in self.reports)

    def totals(self) -> Dict[str, int]:
        keys = ("added", "replaced", "removed", "kept_manual", "pinned",
                "skipped_dup", "channels_created", "channels_touched")
        return {k: sum(getattr(r, k) for r in self.reports) for k in keys}

    def text(self) -> str:
        """给弹窗结果区用的多行文本。"""
        scope_text = {"none": "不新增", "cctv": "只补央视", "all": "央视+卫视都补"}
        lines = ["加速方式：%s" % (self.mirror_name or self.mirror_id),
                 "新增地址置顶：%s" % ("是" if self.top_new else "否"),
                 "新增频道：%s" % scope_text.get(self.new_channel_scope,
                                                self.new_channel_scope),
                 ""]
        for r in self.reports:
            lines.append(r.line())
        t = self.totals()
        lines.append("")
        lines.append(
            "合计：更换 %d 条、新增 %d 条、删除失效 %d 条、保护手改 %d 条，"
            "涉及 %d 个频道%s"
            % (t["replaced"], t["added"], t["removed"], t["kept_manual"],
               t["channels_touched"],
               ("，新建频道 %d 个" % t["channels_created"]) if t["channels_created"] else "")
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 单频道对账
# ---------------------------------------------------------------------------
def _reset_health(src: Source, url: str) -> None:
    """地址变了 → 旧健康结论作废，容器/协议重新推断。"""
    src.url = url
    src.kind = url_container_kind(url)
    src.protocol = url_protocol(url)
    src.is_healthy = False
    src.latency_ms = None
    src.checked_at = None


def _reconcile_channel(session, chan: Channel, urls: List[str], origin: str,
                       top_new: bool, rep: OriginReport) -> int:
    """
    把 `chan` 下 `origin` 这一路的源对齐到 `urls`（上游给该频道的地址，按上游顺序）。
    返回本频道被改动的条数（0 = 无需改动）。

    地址数按 `config.SYNC_MAX_URLS_PER_ORIGIN` 截断：上游常给同一个台几十条备用地址，
    全收下来会让 failover 挨个试到天荒地老。
    """
    cap = int(getattr(cfg, "SYNC_MAX_URLS_PER_ORIGIN", 3) or 3)
    want = [u for u in dict.fromkeys(urls) if u][:cap]      # 去重保序 + 截断
    if not want:
        return 0

    same = [s for s in chan.sources if s.origin == origin]
    manual = [s for s in same if bool(getattr(s, "is_user_edited", False))]
    auto = [s for s in same if not bool(getattr(s, "is_user_edited", False))]
    rep.kept_manual += len(manual)

    # 被手改过的地址不参与替换，也不允许被重复插入
    protected = {s.url for s in manual}
    target = [u for u in want if u not in protected]
    target_set = set(target)

    keep = [s for s in auto if s.url in target_set]           # 上游仍有 → 保留（含健康信息）
    slots = [s for s in auto if s.url not in target_set]      # 上游已无 → 待替换 / 待删
    have = {s.url for s in keep}
    missing = [u for u in target if u not in have]            # 需要落地的地址

    # 频道内已有全部地址（含其它来源），防止插入重复
    present = {s.url for s in chan.sources}

    in_place = missing[:len(slots)]
    leftover = slots[len(in_place):]
    to_insert = missing[len(slots):]

    touched = 0

    # 1) 用"上游已删除的自动源"的槽位原地更换 —— 保留行与优先级
    for src, url in zip(slots, in_place):
        _reset_health(src, url)
        src.source_id = "%s:%s:%d" % (origin, normalize_key(chan.name), touched + 1)
        rep.replaced += 1
        present.add(url)
        touched += 1

    # 2) 上游不再提供的自动源 → 删除
    for src in leftover:
        session.delete(src)
        rep.removed += 1
        touched += 1

    # 3) 纯新增（上游给得比库里多）
    #    手改保护场景下需求明确要求"新增的地址直接置顶"，因此强制置顶；
    #    普通新增则按用户勾选的 top_new 决定。
    pin = bool(top_new or manual)
    if to_insert:
        fresh = [u for u in to_insert if u not in present]
        rep.skipped_dup += len(to_insert) - len(fresh)
        if fresh:
            if pin:
                for s in chan.sources:
                    s.default_priority = int(s.default_priority or 0) + len(fresh)
                base = 0
                rep.pinned += len(fresh)
            else:
                base = max((int(s.default_priority or 0) for s in chan.sources),
                           default=-1) + 1
            for i, url in enumerate(fresh):
                src = Source(
                    channel_id=chan.id, url=url,
                    source_id="%s:%s:%d" % (origin, normalize_key(chan.name), i + 1),
                    origin=origin,
                    kind=url_container_kind(url), protocol=url_protocol(url),
                    default_priority=base + i,
                    is_healthy=False, is_enabled=True, is_user_edited=False,
                )
                session.add(src)
                chan.sources.append(src)
                rep.added += 1
                present.add(url)
                touched += 1

    if touched:
        log.info("同步[%s] 频道 %s: 更换%d 删除%d 新增%d", origin, chan.name,
                 len(in_place), len(leftover), rep.added)
    return touched


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def _core_names(entries) -> List:
    """只保留"央视 + 省级卫视"的条目（与落库白名单同一套规则）。

    顺带就地套用名称规范化 —— 后面落库/报错都用规范名，省得再转一次。
    """
    out = []
    for e in entries:
        e.name = canonical_channel_name(e.name)
        if not e.name or not e.all_urls():
            continue
        if is_core_channel(e.name, e.group):
            out.append(e)
    return out


async def _fetch_for_origin(client, src: dict, mirror_id: str,
                            rep: OriginReport) -> List:
    """按加速方式依次尝试拉取；第一个成功的就用它。全部失败则抛异常。"""
    tried: List[str] = []
    for url in mirror_mod.candidates(src.get("url", ""), mirror_id):
        try:
            entries = await fetch_entries(client, url, src["id"])
        except Exception as exc:  # noqa: BLE001 - 换下一个候选地址继续
            tried.append("%s（%s）" % (url[:70], exc))
            continue
        if not entries:
            tried.append("%s（清单为空）" % url[:70])
            continue
        rep.used_url = url
        return entries
    raise RuntimeError("所有加速地址都失败：" + "；".join(tried[:4]))


def _build_index(session):
    """
    建立两级频道索引：
      - `index`     : identity_key -> Channel（精确匹配）
      - `fam_index` : family_key   -> [Channel, ...]（忽略清晰度的兜底匹配）

    为什么要两级：上游普遍用不带清晰度的名字（`CCTV-16`），而库里可能叫
    `CCTV-16 HD`。只按精确键匹配的话，同步会以为"上游多了个台"而重复建频道。
    """
    from sqlalchemy.orm import selectinload

    index: Dict[str, Channel] = {}
    fam_index: Dict[str, List[Channel]] = {}
    chans = (session.query(Channel)
             .options(selectinload(Channel.sources)).all())
    for c in chans:
        k = identity_key(c.name)
        if k and k not in index:
            index[k] = c
        elif k:
            log.warning("身份键冲突（%s）：%s 与 %s 同键，同步只会命中前者",
                        k, index[k].name, c.name)
        f = family_key(c.name)
        if f:
            fam_index.setdefault(f, []).append(c)
    return index, fam_index


def _lookup(chan_key: str, name: str, index: Dict[str, Channel],
            fam_index: Dict[str, List[Channel]]):
    """按 精确身份键 → 兜底族键 的顺序找频道，返回 (Channel 或 None, 命中方式)。"""
    chan = index.get(chan_key)
    if chan is not None:
        return chan, "exact"
    cands = fam_index.get(family_key(name), [])
    if len(cands) == 1:
        return cands[0], "family"
    if len(cands) > 1:
        # 同族有多条（如库里有 CCTV-1 与 CCTV-1 HD 并列）→ 选源最多的那条
        return max(cands, key=lambda c: len(c.sources or [])), "family-multi"
    return None, ""


def _apply_origin(session, index: Dict[str, Channel],
                  fam_index: Dict[str, List[Channel]], src: dict, entries,
                  top_new: bool, new_channel_scope: str,
                  rep: OriginReport) -> None:
    """把一个来源的核心条目对账写入数据库。"""
    origin = src["id"]
    by_key: Dict[str, List[str]] = {}
    label: Dict[str, str] = {}
    for e in entries:
        k = identity_key(e.name)
        if not k:
            continue
        bucket = by_key.setdefault(k, [])
        for u in e.all_urls():
            if u and u not in bucket:
                bucket.append(u)
        label.setdefault(k, e.name)

    for k, urls in by_key.items():
        raw_name = label.get(k, k)
        chan, how = _lookup(k, raw_name, index, fam_index)
        if chan is not None and how != "exact":
            log.debug("同步[%s] 兜底匹配：上游 %s -> 库里 %s", origin, raw_name, chan.name)
        if chan is None:
            if not _scope_allows(new_channel_scope, core_kind(raw_name)):
                # 只统计并告知，不建频道 —— 尊重用户自己筛选过的频道列表
                rep.new_available += 1
                rep.available_names.append(canonical_channel_name(raw_name))
                continue
            canon = canonical_channel_name(raw_name)
            chan = Channel(name=canon,
                           group_name=classify_group(raw_name),
                           sort_order=0)
            session.add(chan)
            session.flush()                  # 取 chan.id
            index[k] = chan
            fam_index.setdefault(family_key(raw_name), []).append(chan)
            rep.channels_created += 1
            rep.created_names.append(chan.name)
            log.info("同步[%s] 新建频道: %s", origin, chan.name)
        if _reconcile_channel(session, chan, urls, origin, top_new, rep):
            rep.channels_touched += 1
    session.flush()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
async def _sync_async(src_list: List[dict], mirror_id: str, top_new: bool,
                      new_channel_scope: str,
                      progress: Optional[Callable[[str], None]]) -> SyncReport:
    def say(msg: str) -> None:
        log.info("同步: %s", msg)
        if progress is not None:
            try:
                progress(msg)
            except Exception as exc:  # noqa: BLE001 - 进度回调失败不能影响同步
                log.debug("进度回调异常: %s", exc)

    report = SyncReport(top_new=top_new, mirror_id=mirror_id,
                        mirror_name=mirror_mod.mirror_name(mirror_id),
                        new_channel_scope=new_channel_scope)

    # ---- 阶段一：网络抓取（async）----
    fetched: List[Tuple[dict, list, OriginReport]] = []
    async with build_client() as client:
        for src in src_list:
            rep = OriginReport(origin=src.get("id", "?"), name=src.get("name", ""))
            report.reports.append(rep)
            say("拉取 %s …" % (rep.name or rep.origin))
            try:
                entries = await _fetch_for_origin(client, src, mirror_id, rep)
            except Exception as exc:  # noqa: BLE001 - 单源失败不影响其它源
                rep.error = str(exc)
                say("  ✗ %s 拉取失败" % (rep.name or rep.origin))
                continue
            core = _core_names(entries)
            rep.ok = True
            # 记"上游覆盖的核心频道数"（按身份键去重），而不是原始条目数 ——
            # 同一个台在上游常有多条条目（不同清晰度 / 备用地址），对用户来说只算一个。
            rep.fetched = len({identity_key(e.name) for e in core})
            fetched.append((src, core, rep))
            used = rep.used_url
            tag = "（经加速）" if used != src.get("url") else ""
            say("  ✓ %s：上游 %d 条核心频道%s" % (rep.name or rep.origin, len(core), tag))

    # ---- 阶段二：数据库对账（单个 session 内完成，避免跨会话使用 ORM 对象）----
    if fetched:
        say("写入数据库 …")
        with session_scope() as s:
            index, fam_index = _build_index(s)
            for src, core, rep in fetched:
                _apply_origin(s, index, fam_index, src, core, top_new,
                              new_channel_scope, rep)
    else:
        say("没有任何来源拉取成功，未改动数据库。")

    return report


def sync_from_sources(
    origin_ids: Optional[Sequence[str]] = None,
    *,
    mirror_id: Optional[str] = None,
    top_new: bool = True,
    new_channel_scope: str = "all",
    sources: Optional[List[dict]] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> SyncReport:
    """
    按来源同步直播源（同步阻塞，请放到后台线程调用）。

    参数：
        origin_ids          —— 要同步的来源 id 列表；None/空 = 全部配置来源
        mirror_id           —— 加速方式（见 config.GITHUB_MIRRORS）；默认 config.DEFAULT_MIRROR
        top_new             —— 新增的源是否置顶（优先级 0）
        new_channel_scope   —— 上游有、库里没有的核心频道怎么办：
                               "none" 只统计不建 / "cctv" 只补央视 / "all" 央视+卫视都补
        sources             —— 覆盖来源列表（测试用）
        progress            —— 进度回调（在工作线程中被调用，UI 需经信号转主线程）

    返回：SyncReport（逐来源明细 + 合计）
    """
    scope = new_channel_scope if new_channel_scope in NEW_CHANNEL_SCOPES else "all"
    if new_channel_scope not in NEW_CHANNEL_SCOPES:
        log.warning("未知的 new_channel_scope=%r，按 all 处理", new_channel_scope)

    all_src = sources if sources is not None else cfg.SCRAPE_SOURCES
    if origin_ids:
        want = set(origin_ids)
        src_list = [s for s in all_src if s.get("id") in want]
    else:
        src_list = list(all_src)

    if not src_list:
        log.warning("同步：没有匹配的来源（origin_ids=%s）", origin_ids)
        return SyncReport(mirror_id=mirror_id or cfg.DEFAULT_MIRROR,
                          mirror_name=mirror_mod.mirror_name(mirror_id),
                          top_new=top_new, new_channel_scope=scope)

    mid = mirror_id or cfg.DEFAULT_MIRROR
    log.info("开始同步：来源=%s 加速方式=%s 置顶=%s 新增频道=%s",
             [s.get("id") for s in src_list], mid, top_new, scope)
    return asyncio.run(_sync_async(src_list, mid, top_new, scope, progress))
