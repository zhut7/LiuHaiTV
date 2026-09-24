# SPDX-License-Identifier: GPL-3.0-or-later
"""
异步健康检测（Step 4 核心）。

职责：
  - 对一批直播源做连通性 + 延迟探测（httpx 并发，信号量限流）；
  - 把结果写回 Source：is_healthy / latency_ms / checked_at；
  - 提供全量后台扫描（check_all）与单频道检测（check_channel）。
  - 【Step 5 改动】verify_channel()：点击频道时判定该频道"能播/不能播"，
    供频道列表 绿/红 着色；probe_url()：源管理弹窗里测单个地址。
    不再由启动时的后台全量扫描决定颜色（未点击的频道显示灰色）。

设计要点：
  - 判定"可用"：HTTP 状态 < 400，且确实读到了媒体流字节（cfg.HEALTH_READ_BYTES），
    避免"返回 200 但是错误页/空页"的假阳性。
  - latency_ms = 读到首块字节的耗时（毫秒）；单源超时（cfg.HEALTH_TIMEOUT）判不可用，
    外层再用 asyncio.wait_for 兜一层，杜绝个别源整盘卡死。
  - protocol（ipv4/ipv6/domain）由 Step 2 在 Source 上已推断，此处仅用于打印/决策，
    不改变探测逻辑（httpx 会按解析结果自动连接）。
  - 探测在"数据库会话外"进行（不占用 DB 事务/长连接），探测完再开短会话写回
    —— 避免漫长的网络 I/O 期间锁住 SQLite。
  - 一切 try-except + logging，不裸 except；单源失败不影响其它源。
  - 与 PlayerController 解耦：健康源优先排序是 Step 3 _sorted_urls 的事，
    本模块负责给它喂数据。

并发模型：
  - asyncio.Semaphore(cfg.HEALTH_CONCURRENCY) 限流；
  - 每个 probe 用 asyncio.wait_for 强制单源超时兜底。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

import httpx

import liuhaitv.config as cfg
from liuhaitv.core.database import session_scope
from liuhaitv.core.models import Channel, Source

log = logging.getLogger(__name__)


@dataclass
class HealthReport:
    """一次健康扫描的总体结果。"""

    checked: int = 0
    healthy: int = 0
    unhealthy: int = 0
    avg_latency_ms: Optional[float] = None
    failures: List[str] = field(default_factory=list)


# 探测计划的轻量表示 (source_id, url)，避免探测期间持有长事务
SourcePlan = Tuple[int, str]


@dataclass
class _Probe:
    url: str
    ok: bool
    latency_ms: Optional[int] = None
    note: str = ""


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(cfg.HEALTH_TIMEOUT, connect=cfg.HEALTH_TIMEOUT),
        headers=cfg.HTTP_HEADERS,
        follow_redirects=True,
    )


async def _probe(url: str, client: httpx.AsyncClient) -> _Probe:
    """探测单个源：读前几 KB 确认是真实媒体流并测首块延迟。"""
    start = time.perf_counter()
    got = 0
    code = 0
    try:
        async with client.stream("GET", url) as resp:
            code = resp.status_code
            if code >= 400:
                return _Probe(url, False, None, f"HTTP {code}")
            async for chunk in resp.aiter_bytes():
                got += len(chunk)
                if got >= cfg.HEALTH_READ_BYTES:
                    break
        if got <= 0:
            return _Probe(url, False, None, f"空响应(HTTP {code})")
        latency = int((time.perf_counter() - start) * 1000)
        return _Probe(url, True, latency, f"HTTP {code}")
    except Exception as exc:  # noqa: BLE001 - 连不上/超时/非媒体均判不可用
        return _Probe(url, False, None, f"{type(exc).__name__}: {str(exc)[:80]}")


async def _scan_async(plans: List[SourcePlan]) -> Tuple[HealthReport, List[tuple]]:
    """并发探测全部 plan，返回 (报告, 待写回结果[(source_id, ok, latency)])。"""
    report = HealthReport()
    if not plans:
        return report, []

    sem = asyncio.Semaphore(cfg.HEALTH_CONCURRENCY)

    async def _bounded(plan: SourcePlan):
        async with sem:
            return await _bounded_probe(plan, client)

    async with _build_client() as client:
        tasks = [asyncio.create_task(_bounded(pn)) for pn in plans]
        results: List[tuple] = []
        lat_sum = 0
        lat_n = 0
        for fut in asyncio.as_completed(tasks):
            try:
                plan, probe = await fut
            except Exception as exc:  # task 级兜底
                report.checked += 1
                report.unhealthy += 1
                log.debug("探测任务异常(源 id=%s): %s", "?", exc)
                continue
            report.checked += 1
            if probe.ok:
                report.healthy += 1
                lat_sum += probe.latency_ms or 0
                lat_n += 1
            else:
                report.unhealthy += 1
                report.failures.append(plan[1])
            results.append((plan[0], probe.ok, probe.latency_ms))

    if lat_n:
        report.avg_latency_ms = round(lat_sum / lat_n, 1)
    return report, results


def _iter_plans(
    session,
    channel_id: Optional[int] = None,
    only_unhealthy: bool = False,
) -> List[SourcePlan]:
    """按条件选出待探测的源（只取 id+url 的轻量计划）。禁用源不参与检测。"""
    q = (
        session.query(Source)
        .join(Channel, Source.channel_id == Channel.id)
        .filter(Source.is_enabled.is_(True))
    )
    if channel_id is not None:
        q = q.filter(Channel.id == channel_id)
    if only_unhealthy:
        q = q.filter(Source.is_healthy.is_(False))
    return [(src.id, src.url) for src in q.all()]


def _write_back(session, results: List[tuple]) -> None:
    """把探测结果写回 Source 行（is_healthy / latency_ms / checked_at）。"""
    for sid, ok, latency in results:
        src = session.get(Source, sid)
        if src is None:
            continue
        src.is_healthy = bool(ok)
        src.latency_ms = latency
        src.checked_at = datetime.now()


def check_all(
    channel_id: Optional[int] = None,
    only_unhealthy: bool = False,
) -> HealthReport:
    """
    探测所选源并把结果写回。
      channel_id=None → 全量后台扫描；否则只测单频道。
      only_unhealthy=True → 只重测当前标记不可用的源（后台定时增量）。
    """
    with session_scope() as s:
        plans = _iter_plans(s, channel_id=channel_id, only_unhealthy=only_unhealthy)
    if not plans:
        log.info("健康检测: 无待检测源（空库或没有启用的源）")
        return HealthReport()

    report, results = asyncio.run(_scan_async(plans))
    if results:
        with session_scope() as s:
            _write_back(s, results)

    log.info(
        "健康检测完成: 检查=%d 可用=%d 不可用=%d 平均延迟=%s ms",
        report.checked, report.healthy, report.unhealthy, report.avg_latency_ms,
    )
    return report


def check_channel(channel_id: int, only_unhealthy: bool = False) -> HealthReport:
    """单频道健康检测（播放前预扫 / 手动一键刷新）。"""
    return check_all(channel_id=channel_id, only_unhealthy=only_unhealthy)


# ===========================================================================
# 点击式判定（Step 5 改动）：点击频道 -> 立刻测该频道的源 -> 绿/红
# ===========================================================================
@dataclass
class ChannelVerdict:
    """单个频道"能否播放"的判定结果（供频道列表绿/红着色）。"""

    channel_id: Optional[int] = None
    ok: bool = False                 # True=至少一个源可播(绿)；False=全部不可播(红)
    checked: int = 0                 # 实际探测过的源数
    total: int = 0                   # 该频道启用的源总数
    ok_url: Optional[str] = None     # 第一个判定可用的源地址
    latency_ms: Optional[int] = None  # 上述源的延迟
    note: str = ""                   # 失败原因摘要（便于日志/UI 提示）


async def _bounded_probe(plan: SourcePlan, client: httpx.AsyncClient) -> tuple:
    """带信号量与"整源超时兜底"的单源探测。"""
    try:
        probe = await asyncio.wait_for(
            _probe(plan[1], client), timeout=cfg.HEALTH_TIMEOUT * 2
        )
    except asyncio.TimeoutError:
        probe = _Probe(plan[1], False, None, "超时")
    return plan, probe


async def _verify_async(
    plans: List[SourcePlan], early_exit: bool = True
) -> Tuple[ChannelVerdict, List[tuple]]:
    """
    并发探测一个频道的全部源；early_exit=True 时一旦某源判定可播，
    立即取消其余探测（点击判定要快，不必等最慢的源超时）。

    返回 (判定结果, 本轮已探到的原始结果 [(plan, probe)])，后者供写回数据库。
    """
    verdict = ChannelVerdict(channel_id=plans[0][0] if plans else None, total=len(plans))
    if not plans:
        verdict.note = "该频道未配置任何启用中的源"
        return verdict, []

    sem = asyncio.Semaphore(min(cfg.HEALTH_CONCURRENCY, len(plans)))

    async def _run(plan: SourcePlan):
        async with sem:
            return await _bounded_probe(plan, client)

    results: List[tuple] = []
    async with _build_client() as client:
        pending = {asyncio.create_task(_run(p)) for p in plans}
        while pending:
            done, still = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                try:
                    results.append(task.result())
                except Exception as exc:  # noqa: BLE001 - 单任务异常不影响其它源
                    log.debug("频道判定任务异常: %s", exc)
            if early_exit and any(pr.ok for _, pr in results):
                for task in still:
                    task.cancel()
                break
            pending = still

    # 汇总：按优先级取"第一个可用源"作为起播推荐
    plan_index = {id(p): i for i, p in enumerate(plans)}
    ok_hits = [(plan, pr) for plan, pr in results if pr.ok]
    ok_hits.sort(key=lambda x: plan_index.get(id(x[0]), 999))
    verdict.checked = len(results)
    verdict.ok = bool(ok_hits)
    if ok_hits:
        verdict.ok_url = ok_hits[0][0][1]
        verdict.latency_ms = ok_hits[0][1].latency_ms
    else:
        notes = [pr.note for _, pr in results if pr.note]
        verdict.note = "; ".join(notes[:3]) or "全部源不可用"
    return verdict, results


def verify_channel(channel_id: int, early_exit: bool = True) -> ChannelVerdict:
    """
    判定单个频道当前是否可播（点击频道时调用），并写回源健康状况。

    与 check_all 的区别：
      - 只测这一个频道的**启用中**的源；
      - 默认 early_exit：找到第一个可用源即返回（点击判定要快）；
      - 返回 ChannelVerdict 供 UI 决定绿/红。
    """
    try:
        with session_scope() as s:
            plans = _iter_plans(s, channel_id=channel_id)
    except Exception as exc:  # noqa: BLE001 - 读计划失败按"不可用"处理，避免卡住 UI
        log.exception("读取频道 %s 的源失败: %s", channel_id, exc)
        return ChannelVerdict(channel_id=channel_id, ok=False, note=f"读库失败: {exc}")

    if not plans:
        return ChannelVerdict(channel_id=channel_id, ok=False, note="无启用中的源")

    try:
        verdict, results = asyncio.run(_verify_async(plans, early_exit=early_exit))
    except Exception as exc:  # noqa: BLE001
        log.exception("频道 %s 判定异常: %s", channel_id, exc)
        return ChannelVerdict(channel_id=channel_id, ok=False,
                              total=len(plans), note=f"判定异常: {exc}")

    verdict.channel_id = channel_id
    try:
        with session_scope() as s:
            # 只写回本轮真正探到的源（early_exit 下未探的源保持原状态）
            _write_back(s, [(plan[0], pr.ok, pr.latency_ms) for plan, pr in results])
    except Exception as exc:  # noqa: BLE001 - 写回失败不影响判定结果
        log.warning("写回频道 %s 检测结果失败: %s", channel_id, exc)

    log.info(
        "频道判定 id=%s -> %s (已测 %d/%d, 延迟=%s ms) %s",
        channel_id, "可播放" if verdict.ok else "不可播放",
        verdict.checked, verdict.total, verdict.latency_ms, verdict.note,
    )
    return verdict


def probe_url(url: str) -> tuple:
    """
    探测单个任意地址（源管理弹窗"测试选中源"用）。
    返回 (ok: bool, latency_ms: int|None, note: str)。
    """
    if not url:
        return False, None, "地址为空"

    async def _one():
        async with _build_client() as client:
            return await _bounded_probe((0, url), client)

    try:
        _, pr = asyncio.run(_one())
        return bool(pr.ok), pr.latency_ms, pr.note
    except Exception as exc:  # noqa: BLE001
        log.warning("单源探测异常 %s: %s", url[:60], exc)
        return False, None, f"{type(exc).__name__}: {str(exc)[:60]}"
