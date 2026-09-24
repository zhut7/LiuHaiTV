# SPDX-License-Identifier: GPL-3.0-or-later
"""
数据层验证脚本（合并自 verify_step2.py + verify_step6.py）。

验证内容：
  A. 数据模型 / M3U 解析 / 搜刮落库（原 verify_step2）
     1. 名称清洗与去重键：CCTV-1(:3) → CCTV-1，去重键归一
     2. 关键词自动分组：中央 / 卫视 / 港澳台 / 地方 / 其他
     3. kind 按 URL 尾字自动判断（m3u 直链 vs html 网页页）
     4. 容器 kind + 网络 protocol（ipv6 / ipv4 / domain）推断
     5. 离线落库：source_id 落库、同名多条备用源合并、核心白名单过滤
  B. 核心白名单 / identity_key 归键 / 频道精简与幂等 / 备份（原 verify_step6）
     1. 白名单判定：真央视 / 省级卫视保留；假央视（CCTV-Billiards 等）、
        港澳台、省市地面频道、购物与境外频道剔除
     2. 分组归类（含英文名省级卫视，以及"含通用词被误判成中央"的旧 bug 回归）
     3. identity_key 归键：CCTV-5+ 与 CCTV-5+ 体育 同键；
        CCTV-1 与 CCTV-1 HD 不同键；北京卫视中/英别名同键
     4. 搜刮落库前过滤：_filter_group_sort 只留核心频道（且名字已规范化）
     5. 精简执行：build_plan + apply_plan 在临时库上真删，
        校验计数、别名合并后源被迁入且不丢失、无孤立源、备份文件生成
     6. 幂等：精简后再跑一次计划应为空（无删除 / 无合并 / 无重归类）

说明：
  - 全程使用隔离临时库（每段开始前重建），不碰真实 data/liuhaitv.db。
  - 不联网、不起 GUI；`--live` 才会真实联网搜刮。

运行：
  <env>/python.exe scripts/verify_data.py            # 仅离线检查（默认，无需联网）
  <env>/python.exe scripts/verify_data.py --live     # 额外做真实联网搜刮并落库

返回码：0=通过  1=存在失败项
"""
import argparse
import importlib.util
import logging
import os
import shutil
import sys
import tempfile

# 项目根入 path，保证任意工作目录可运行
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import liuhaitv  # noqa: E402  (确保 libmpv dll 路径已 prepend)

# 本脚本会真的落库，必须用隔离临时库，绝不能污染真实 data/liuhaitv.db。
# 注意：必须在导入 liuhaitv.core.database 之前改 cfg.DB_PATH（engine 在导入时绑定路径）。
_tmpdir = tempfile.mkdtemp(prefix="liuhaitv_verify_data_")
import liuhaitv.config as cfg  # noqa: E402
cfg.DB_PATH = os.path.join(_tmpdir, "liuhaitv_test.db")

import liuhaitv.logger as pylog  # noqa: E402

pylog.setup_logging()
log = logging.getLogger("verify_data")

from liuhaitv.core.channel_filter import (  # noqa: E402
    core_kind, identity_key, is_core_channel, is_hmt,
)
from liuhaitv.core.database import Base, engine, init_db, session_scope  # noqa: E402
from liuhaitv.core.m3u_parser import ParsedEntry, parse_m3u  # noqa: E402
from liuhaitv.core.models import Channel, Source  # noqa: E402
from liuhaitv.core.scraper import (  # noqa: E402
    _filter_group_sort, _persist, run_scrape,
    classify_group, clean_channel_name, detect_fetch_kind,
    normalize_key, url_container_kind, url_protocol,
)

# prune_to_core 在 scripts/ 下，不在包里 —— 用 spec 加载。
# 注意：必须先注册进 sys.modules，否则 prune_to_core 里的 @dataclass 解析
# 字符串注解时会因 `sys.modules.get(cls.__module__)` 为 None 而报 AttributeError。
_prune_spec = importlib.util.spec_from_file_location(
    "liuhaitv_prune_to_core",
    os.path.join(_PROJECT_ROOT, "scripts", "prune_to_core.py"),
)
prune = importlib.util.module_from_spec(_prune_spec)
sys.modules[_prune_spec.name] = prune
_prune_spec.loader.exec_module(prune)   # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1


def reset_db():
    """清空临时库并重建，让各段互不干扰（段与段之间共享一个临时库）。"""
    Base.metadata.drop_all(engine)
    init_db()


# 内置合成 M3U（含 去重/括号/测试垃圾/港澳台/多备用源/ipv6 等用例）
SAMPLE_M3U = """#EXTM3U
#EXTINF:-1,CCTV-1
http://example.com/cctv1.m3u8
#EXTINF:-1,CCTV-1(:3)
http://example.com/cctv1b.m3u8
#EXTINF:-1,CCTV-5+ 体育
http://example.com/cctv5.m3u8
#EXTINF:-1,凤凰卫视中文台
http://[::1]:8080/phone.m3u8
#EXTINF:-1,TVB明珠台
http://example.com/tvb.m3u8
#EXTINF:-1,湖南卫视
http://192.168.1.5/hunan.ts
#EXTINF:-1,湖南经视
http://example.com/hnjs.m3u8
#EXTINF:-1,北京卫视 HD
http://live.bj.com/beijing.m3u8
#EXTINF:-1,测试频道
http://example.com/test.m3u8
#EXTINF:-1,浙江卫视 蓝
rtmp://192.168.1.6/zj
"""


# ===========================================================================
# A. 原 verify_step2：数据模型与搜刮引擎（一个检查一个函数、返回 bool）
# ===========================================================================
def _check_clean() -> bool:
    """A1：名称清洗 + 去重键归一。"""
    cases = {
        "CCTV-1 综合": "CCTV-1 综合",
        "CCTV-1(:3)": "CCTV-1",
        "CCTV-1 高清(备用)": "CCTV-1 高清",
        "湖南卫视(高清)": "湖南卫视",
        "  北京卫视   ": "北京卫视",
    }
    ok = True
    log.info("  [去重键] normalize_key('CCTV-1(:3)')=%r  normalize_key('cctv—1')=%r",
             normalize_key("CCTV-1(:3)"), normalize_key("cctv—1"))
    for raw, want in cases.items():
        got = clean_channel_name(raw)
        st = "OK" if got == want else "FAIL"
        if got != want:
            ok = False
        log.info("  %-5s %-22r -> %r (期望 %r)", st, raw, got, want)
    # CCTV-1(:3) 与 CCTV-1 应同键
    key_same = normalize_key("CCTV-1(:3)") == normalize_key("CCTV-1")
    log.info("  CCTV-1(:3) 与 CCTV-1 同键 -> %s", "OK" if key_same else "FAIL")
    ok = ok and key_same
    return ok


def _check_group() -> bool:
    """A2：关键词自动分组（含 港澳台 / 其他）。"""
    cases = {
        "CCTV-1 综合": "中央",
        "凤凰卫视中文台": "港澳台",
        "TVB明珠台": "港澳台",
        "湖南卫视": "卫视",
        "北京卫视 HD": "卫视",
        "湖南经视": "地方",
        "某城市公共": "其他",
    }
    ok = True
    for name, want in cases.items():
        got = classify_group(name)
        st = "OK" if got == want else "FAIL"
        if got != want:
            ok = False
        log.info("  %-5s %-14r -> %-4s (期望 %s)", st, name, got, want)
    return ok


def _check_fetch_kind() -> bool:
    """A3：抓取方式 kind 按 URL 尾字自动判断。"""
    cases = {
        "https://a.example/cn.m3u": "m3u",
        "https://a.example/live.m3u8": "m3u",
        "https://github.com/o/r/tree/master/live": "html",
        "https://i.example/index.html": "html",
    }
    ok = True
    for url, want in cases.items():
        got = detect_fetch_kind(url)
        st = "OK" if got == want else "FAIL"
        if got != want:
            ok = False
        log.info("  %-5s %-45r -> %s (期望 %s)", st, url, got, want)
    return ok


def _check_kind_protocol() -> bool:
    """A4：URL → 容器 kind + 网络 protocol。"""
    cases = {
        "http://example.com/a.m3u8": ("hls", "domain"),
        "http://[::1]:8080/phone.m3u8": ("hls", "ipv6"),
        "rtmp://192.168.1.6/zj": ("rtmp", "ipv4"),
        "http://192.168.1.5/hunan.ts": ("ts", "ipv4"),
    }
    ok = True
    for url, (wk, wp) in cases.items():
        kind = url_container_kind(url)
        proto = url_protocol(url)
        st = "OK" if (kind == wk and proto == wp) else "FAIL"
        if (kind == wk and proto == wp) is False:
            ok = False
        log.info("  %-5s %-38r kind=%-5s protocol=%s (期望 %s/%s)",
                 st, url, kind, proto, wk, wp)
    return ok


def _check_persist_offline() -> bool:
    """A5：解析 SAMPLE_M3U → 清洗/去重/合并 → 落库；核对分组、source_id、多备用源。"""
    reset_db()
    entries = parse_m3u(SAMPLE_M3U, origin="sample")
    cleaned = _filter_group_sort(entries)
    res = _persist(cleaned)
    log.info("  离线落库: 解析原始=%d 过滤合并后=%d 新增频道=%d 新增源=%d",
             len(entries), len(cleaned), res.channels_created, res.sources_created)
    # 注：_persist 为增量入库，重复运行/上次已入库时 created 可能为 0。
    #     因此质量断言基于"库内实际内容"，不基于本次创建数。

    ok = True
    with session_scope() as s:
        names = {c.name: c for c in s.query(Channel).all()}
        cctv1 = names.get("CCTV-1")
        if cctv1 is None:
            log.error("  未找到清洗后的频道 CCTV-1（可能括号未去除）")
            return False
        urls = {so.url for so in cctv1.sources}
        log.info("  CCTV-1 已合并备用源=%d 个（期望 >=2），分组=%s",
                 len(urls), cctv1.group_name)
        if len(urls) < 2:
            ok = False
        if cctv1.group_name != "中央":
            log.error("  CCTV-1 分组=%s 期望 中央", cctv1.group_name)
            ok = False
        first = cctv1.sources[0]
        log.info("  CCTV-1 首源 source_id=%r kind=%s protocol=%s",
                 first.source_id, first.kind, first.protocol)
        if not first.source_id:
            log.error("  source_id 为空")
            ok = False
        # 落库前会过一遍"核心白名单"，只留央视 + 省级卫视。
        # 因此港澳台（凤凰 / TVB）、地方台（湖南经视）、垃圾条目（测试频道）
        # 出现在库里才是 bug —— 它们应当在这一步就被丢掉。
        stray = [n for n in ("凤凰卫视中文台", "TVB明珠台", "湖南经视", "测试频道")
                 if n in names]
        if stray:
            log.error("  非核心频道未被白名单过滤，仍入库: %s", stray)
            ok = False
        missing_core = [n for n in ("湖南卫视", "北京卫视 HD", "浙江卫视 蓝")
                        if n not in names]
        if missing_core:
            log.error("  核心频道(省级卫视)被误过滤: %s", missing_core)
            ok = False
    return ok


def _run_live() -> bool:
    """真实联网搜刮 + 落库 + 分组统计。"""
    log.info("----- 真实联网搜刮 (--live) -----")
    try:
        res = run_scrape()
    except Exception as exc:  # noqa: BLE001
        log.error("实搜异常: %s", exc)
        return False
    log.info("实搜结果: 原始=%d 新增频道=%d 新增源=%d 失败源=%s",
             res.total_fetched, res.channels_created, res.sources_created,
             res.failed_sources or "无")
    with session_scope() as s:
        from sqlalchemy import func
        rows = (s.query(Channel.group_name, func.count(Channel.id))
                 .group_by(Channel.group_name).order_by(Channel.group_name).all())
    counts = {k: v for k, v in rows}
    log.info("  当前按分组统计: %s", counts or "（库内暂无频道）")
    return res.channels_created > 0 or bool(counts)


def _run_data_model_checks() -> bool:
    """跑 A 段的 5 项检查（一个检查一个函数、返回 bool）。"""
    log.info("-- A. 数据模型 / M3U 解析 / 搜刮落库 --")
    checks = {
        "名称清洗/去重键": _check_clean,
        "关键词自动分组": _check_group,
        "抓取方式kind(URL尾字)": _check_fetch_kind,
        "容器kind+协议protocol": _check_kind_protocol,
        "离线落库(source_id/多源/分组)": _check_persist_offline,
    }
    all_ok = True
    for name, fn in checks.items():
        ok = fn()
        all_ok = all_ok and ok
        log.info("  %-28s %s", name, "OK" if ok else "FAIL")
    return all_ok


# ===========================================================================
# B. 原 verify_step6：频道精简（线性 check(label, cond) 风格）
# ===========================================================================
_CORE_CASES = [
    # --- 央视（保留）---
    "CCTV-1", "CCTV-1 HD", "CCTV-2", "CCTV-13 HD", "CCTV-17 HD",
    "CCTV-4K HD", "CCTV-8K HD", "CCTV-5+", "CCTV-5+ 体育",
    "CCTV-6 [Geo-blocked]", "CCTV+ 1 [Not 24/7]",
    # --- 卫视（保留）---
    "北京卫视 HD", "浙江卫视 蓝", "湖南卫视", "东方卫视",
    "Beijing Satellite TV HD", "Beijing Satellite TV [Not 24/7]",
    "Hunan TV", "Jiangsu Satellite TV", "Guangdong Satellite TV",
    "Ningxia Satellite Channel", "Xinjiang TV 1", "Xizang TV Tibetan [Not 24/7]",
    "Yanbian Satellite TV", "Shenzhen Satellite TV",
]

_DROP_CASES = [
    # --- 挂 CCTV 名头的"假央视"---
    "CCTV-Billiards", "CCTV-Golf & Tennis", "CCTV-Storm Football",
    "CCTV-Storm Music", "CCTV-Storm Theater", "CCTV-Health",
    "CCTV-Women's Fashion SD", "CCTV-World Geography SD",
    "CCTV-The First Theater", "CCTV-Culture of Quality SD",
    # --- 港澳台 ---
    "凤凰卫视中文台", "TVB明珠台", "香港卫视", "澳门卫视",
    # --- 省市地面频道 / 非上星 ---
    "湖南经视", "Anshun Comprehensive News Channel", "QTV-1", "Xinjiang TV 2",
    "Jiangsu Movie Channel", "Shandong TV Life Channel [Geo-blocked]",
    "Guangzhou TV", "Hunan News Channel [Geo-blocked]", "Siping TV",
    "Nei Monggol TV 2 Mongolian Culture Channel", "Zhejiang International Channel",
    # --- 购物 / 境外 / 其它国家级但非央视非卫视 ---
    "Fengshang Shopping Channel", "VoA TV China", "TV BRICS Chinese",
    "ABN China", "CETV-1", "China Weather Channel [Not 24/7]",
]

_GROUP_CASES = {
    "CCTV-1 综合": "中央",
    "CCTV-1": "中央",
    "凤凰卫视中文台": "港澳台",
    "TVB明珠台": "港澳台",
    "湖南卫视": "卫视",
    "北京卫视 HD": "卫视",
    "Beijing Satellite TV HD": "卫视",     # 关键：以前全落到"其他"
    "Hunan TV": "卫视",
    "Xinjiang TV 1": "卫视",
    "湖南经视": "地方",
    "Guangzhou TV": "其他",
    # --- 旧 bug 回归：含通用词不应被判成"中央" ---
    "北京卫视高清": "卫视",
    "广东体育频道": "地方",
}


def _seed() -> dict:
    """造一批频道：核心 3 条（含 1 组中/英别名）+ 非核心 3 条。"""
    reset_db()
    with session_scope() as s:
        def add(name, group, urls):
            ch = Channel(name=name, group_name=group)
            s.add(ch)
            s.flush()
            for i, u in enumerate(urls):
                s.add(Source(channel_id=ch.id, url=u, source_id=f"v6:{name}:{i}",
                             origin="v6", kind="hls", protocol="domain",
                             default_priority=i, is_healthy=False))
            return ch.id

        ids = {
            "cctv1": add("CCTV-1", "中央", ["http://x/cctv1.m3u8"]),
            "bj_cn": add("北京卫视 HD", "卫视", ["http://x/bj.m3u8"]),
            # 同一频道的英文别名（应与"北京卫视 HD"合并，源迁入）
            "bj_en": add("Beijing Satellite TV HD", "其他",
                         ["http://x/bj-en.m3u8", "http://x/bj.m3u8"]),  # 后一条是重复 url
            "fake_cctv": add("CCTV-Billiards", "中央", ["http://x/bill.m3u8"]),
            "hmt": add("凤凰卫视中文台", "港澳台", ["http://x/fh.m3u8"]),
            "local": add("湖南经视", "地方", ["http://x/jingshi.m3u8"]),
        }
        return ids


def _run_core_checks() -> bool:
    """跑 B 段的线性检查，返回整体是否通过。"""
    log.info("-- B. 核心白名单 / 归键 / 频道精简与幂等 / 备份 --")
    failures: list = []

    def check(label, cond, extra=""):
        log.info("  %-46s %s %s", label, "OK" if cond else "FAIL", extra)
        if not cond:
            failures.append(label)

    # ---- 1. 白名单判定 ----
    log.info("-- 1. 核心白名单判定 --")
    bad_keep = [n for n in _CORE_CASES if not is_core_channel(n)]
    check("核心频道全部保留(%d 例)" % len(_CORE_CASES), not bad_keep, bad_keep)
    bad_drop = [n for n in _DROP_CASES if is_core_channel(n)]
    check("非核心频道全部剔除(%d 例)" % len(_DROP_CASES), not bad_drop, bad_drop)
    check("core_kind 区分 cctv/satellite",
          core_kind("CCTV-1") == "cctv" and core_kind("Hunan TV") == "satellite")
    check("is_hmt 认得凤凰/TVB", is_hmt("凤凰卫视中文台") and is_hmt("TVB明珠台"))
    # 回归：曾经的分组兜底 bug —— 名字是港澳台但被上游标成 group-title="卫视" 时，
    #       旧实现会因"分组含卫视"而放行（同步时把「香港卫视」建了进来）。
    for nm in ("香港卫视", "澳门卫视"):
        check("港澳台名 + 分组标卫视 也不放行（%s）" % nm,
              not is_core_channel(nm, "卫视"))

    # ---- 2. 分组归类 ----
    log.info("-- 2. 分组归类 --")
    for name, want in _GROUP_CASES.items():
        got = classify_group(name)
        check("分组 %-26r -> %s" % (name, want), got == want,
              "" if got == want else "实得 %s" % got)

    # ---- 3. 归键 ----
    log.info("-- 3. identity_key 归键 --")
    check("CCTV-5+ 与 CCTV-5+ 体育 同键",
          identity_key("CCTV-5+") == identity_key("CCTV-5+ 体育"))
    check("CCTV-1 与 CCTV-1 HD 不同键(清晰度独立)",
          identity_key("CCTV-1") != identity_key("CCTV-1 HD"))
    check("CCTV-13 与 CCTV-13 HD 不同键",
          identity_key("CCTV-13") != identity_key("CCTV-13 HD"))
    check("北京卫视 三条中/英别名同键",
          identity_key("北京卫视 HD") == identity_key("Beijing Satellite TV HD")
          == identity_key("Beijing Satellite TV [Not 24/7]"))
    check("不同省份不互串",
          identity_key("湖南卫视") != identity_key("北京卫视 HD"))

    # ---- 4. 搜刮落库前过滤 ----
    log.info("-- 4. 搜刮 _filter_group_sort 落库前过滤 --")
    entries = [
        ParsedEntry(name="CCTV-1(:3)", group="央视", url="http://a/1.m3u8", origin="t1"),
        ParsedEntry(name="CCTV-1", group="央视", url="http://b/1.m3u8", origin="t2"),
        ParsedEntry(name="CCTV-Billiards", group="央视", url="http://c/x.m3u8", origin="t1"),
        ParsedEntry(name="凤凰卫视中文台", group="港澳", url="http://d/y.m3u8", origin="t1"),
        ParsedEntry(name="湖南卫视", group="卫视", url="http://e/hn.m3u8", origin="t1"),
        ParsedEntry(name="Beijing Satellite TV HD", group="", url="http://f/bj.m3u8",
                    origin="t1"),
        ParsedEntry(name="QTV-1", group="", url="http://g/q.m3u8", origin="t1"),
        ParsedEntry(name="测试频道", group="", url="http://h/z.m3u8", origin="t1"),
    ]
    kept = _filter_group_sort(entries)
    kept_names = sorted(e.name for e in kept)
    # 落库前会先做名称规范化：`Beijing Satellite TV HD` → `北京卫视 HD`，
    # 而库里既有频道就叫 `北京卫视 HD`，所以两者在合并阶段就能并成一条。
    check("落库前只剩核心频道（且名称已规范化）",
          kept_names == ["CCTV-1", "北京卫视 HD", "湖南卫视"], kept_names)
    cctv1 = next((e for e in kept if e.name == "CCTV-1"), None)
    check("CCTV-1(:3) 与 CCTV-1 合并为多条源",
          cctv1 is not None and len(cctv1.all_urls()) == 2)

    # ---- 5. 精简执行 ----
    log.info("-- 5. build_plan + apply_plan（临时库真删）--")
    ids = _seed()
    with session_scope() as s:
        plan = prune.build_plan(s, merge_alias=True)
        n_ch0 = s.query(Channel).count()
        n_src0 = s.query(Source).count()

    kept_total = len(plan.keep) + sum(len(o) for _p, o in plan.merges)
    check("计划保留 3 条(合并前) / 删除 3 条",
          kept_total == 3 and len(plan.drop) == 3,
          "keep=%d(+合并%d) drop=%d" % (len(plan.keep), kept_total - len(plan.keep),
                                        len(plan.drop)))
    check("计划合并 1 组别名", len(plan.merges) == 1)

    with session_scope() as s:
        stats = prune.apply_plan(s, plan)

    with session_scope() as s:
        n_ch1 = s.query(Channel).count()
        n_src1 = s.query(Source).count()
        orphan = s.query(Source).filter(~Source.channel_id.in_(s.query(Channel.id))).count()
        bj = s.query(Channel).filter(Channel.name == "北京卫视 HD").one()
        bj_urls = sorted(x.url for x in bj.sources)
        names_left = sorted(c.name for c in s.query(Channel).all())

    check("频道 6 -> 2 (删 3 + 合并 1)", n_ch1 == 2, "%d -> %d" % (n_ch0, n_ch1))
    check("删除计数=3", stats["channels_deleted"] == 3)
    check("合并计数=1", stats["channels_merged"] == 1)
    check("合并后无孤立源", orphan == 0)
    check("无残留非核心频道",
          names_left == ["CCTV-1", "北京卫视 HD"], names_left)
    check("别名源的 URL 已迁入保留者",
          bj_urls == ["http://x/bj-en.m3u8", "http://x/bj.m3u8"], bj_urls)
    # 7 = 1(CCTV-1) + 1(北京卫视 HD) + 2(英文别名,其中1条重复) + 3(三个待删频道各1源)
    expected_src = n_src0 - 3 - 1        # 被删频道带走的 3 条 + 重复丢弃的 1 条
    check("源计数自洽(%d)" % expected_src, n_src1 == expected_src,
          "src %d -> %d" % (n_src0, n_src1))
    check("统计口径自洽(moved=1, deduped=1)",
          stats["sources_moved"] == 1 and stats["sources_deduped"] == 1)

    # ---- 6. 幂等 ----
    log.info("-- 6. 幂等：再跑一次计划应为空 --")
    with session_scope() as s:
        plan2 = prune.build_plan(s, merge_alias=True)
    check("二次计划：无删除/无合并/无重归类",
          not plan2.drop and not plan2.merges and not plan2.regroup,
          "drop=%d merges=%d regroup=%d"
          % (len(plan2.drop), len(plan2.merges), len(plan2.regroup)))

    # ---- 7. 备份 ----
    log.info("-- 7. 备份 --")
    bpath = prune.backup_db(cfg.DB_PATH)
    check("备份文件已生成且非空",
          os.path.exists(bpath) and os.path.getsize(bpath) > 0, bpath)

    return not failures


def main(args) -> int:
    log.info("===== verify_data：数据模型 / 搜刮落库 / 核心白名单与频道精简 =====")
    all_ok = _run_data_model_checks()
    if not _run_core_checks():
        all_ok = False
    if all_ok and args.live:
        all_ok = _run_live() and all_ok

    log.info("----- 汇总 -----")
    log.info("%s", "verify_data 全部通过 ✓" if all_ok else "存在失败项，请查看上方日志")

    try:
        engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(_tmpdir, ignore_errors=True)
    return EXIT_OK if all_ok else EXIT_FAIL


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="数据层验证（verify_data）")
    p.add_argument("--live", action="store_true", help="额外做真实联网搜刮并落库")
    sys.exit(main(p.parse_args()))
