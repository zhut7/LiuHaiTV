# SPDX-License-Identifier: GPL-3.0-or-later
"""
频道名规范化验证脚本（原 verify_step8_naming.py）。

验证内容（全离线，用隔离临时库）：
  A. 央视统一（core/naming.py）
     1. `CCTV4` / `cctv4` / `CCTV 4` → `CCTV-4`
     2. `CCTV4K` / `CCTV8K` → `CCTV-4K` / `CCTV-8K`
     3. `CCTV5` / `CCTV5+` → `CCTV-5` / `CCTV-5+`
     4. 中文节目名后缀被丢弃（`CCTV-1综合` → `CCTV-1`、`CCTV-5+ 体育` → `CCTV-5+`）
     5. 清晰度保留（`CCTV13 HD` → `CCTV-13 HD`）
     6. 上游元数据标签被去掉（`CCTV-6 [Geo-blocked]` → `CCTV-6`）
     7. `CCTV+ 1`（央视海外版）不与 `CCTV-1` 混淆
  B. 省级卫视英文名 → 中文
     8. `Anhui TV` → `安徽卫视`、`Beijing Satellite TV HD` → `北京卫视 HD`
     9. `Ningxia Satellite Channel` → `宁夏卫视`、`Xinjiang TV 1` → `新疆卫视`
    10. `Dragon TV` → `东方卫视`（不是"上海卫视"）
    11. `Xizang TV Tibetan` → `西藏卫视 藏语`
    12. 保守：`Dragon TV International` / `Guangzhou TV` / `QTV-1` / `香港卫视` 一律不动
  C. 通用性质
    13. 幂等：canonical(canonical(x)) == canonical(x)
    14. `is_canonical()` 判定正确
  D. 改名脚本的冲突保护（scripts/normalize_names.py）
    15. 正常改名会被列入计划
    16. 目标名已被别的频道占用 → 跳过并记为冲突
    17. 两条都改到同一个名字 → 只放行一条，另一条记为冲突（绝不制造重名）
  E. 落库/同步路径也用规范名
    18. `scraper._filter_group_sort` + `_persist` 落库时写入规范名
    19. 库里已有 `CCTV-4` 时，上游 `CCTV4` 不会重复建台
    20. `sync_sources._core_names` 会把上游写法规范掉

说明：
  - 全程使用隔离临时库（E 段开始前重建），不碰真实 data/liuhaitv.db。
  - 不联网、不起 GUI。

运行：
  <env>/python.exe scripts/verify_naming.py

返回码：0=通过  1=存在失败项
"""
import importlib.util
import logging
import os
import shutil
import sys
import tempfile
import types

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import liuhaitv  # noqa: E402

_tmpdir = tempfile.mkdtemp(prefix="liuhaitv_verify_naming_")
import liuhaitv.config as cfg  # noqa: E402
cfg.DB_PATH = os.path.join(_tmpdir, "liuhaitv_test.db")

import liuhaitv.logger as pylog  # noqa: E402
pylog.setup_logging()
log = logging.getLogger("verify_naming")

from liuhaitv.core.database import Base, engine, init_db, session_scope  # noqa: E402
from liuhaitv.core.m3u_parser import ParsedEntry  # noqa: E402
from liuhaitv.core.models import Channel  # noqa: E402
from liuhaitv.core.naming import canonical_channel_name, is_canonical  # noqa: E402
from liuhaitv.core.scraper import _filter_group_sort, _persist  # noqa: E402
from liuhaitv.core.sync_sources import _core_names  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1


def reset_db():
    """清空临时库并重建，让各段互不干扰（段与段之间共享一个临时库）。"""
    Base.metadata.drop_all(engine)
    init_db()


# 加载 scripts/normalize_names.py（不在包里）
_spec = importlib.util.spec_from_file_location(
    "liuhaitv_normalize_names",
    os.path.join(_PROJECT_ROOT, "scripts", "normalize_names.py"))
norm = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = norm
_spec.loader.exec_module(norm)

_CCTV_CASES = {
    "CCTV4": "CCTV-4", "cctv4": "CCTV-4", "CCTV 4": "CCTV-4", "CCTV-4": "CCTV-4",
    "CCTV4K": "CCTV-4K", "CCTV-4K HD": "CCTV-4K HD",
    "CCTV8K": "CCTV-8K", "CCTV-8K HD": "CCTV-8K HD",
    "CCTV5": "CCTV-5", "CCTV5+": "CCTV-5+", "CCTV-5+": "CCTV-5+",
    "CCTV-5+ 体育": "CCTV-5+", "CCTV-5+体育赛事": "CCTV-5+",
    "CCTV-1综合": "CCTV-1", "CCTV1综合": "CCTV-1", "CCTV-1 综合": "CCTV-1",
    "CCTV13 HD": "CCTV-13 HD", "CCTV-13": "CCTV-13",
    "CCTV-6 [Geo-blocked]": "CCTV-6",
    "CCTV+ 1 [Not 24/7]": "CCTV+ 1", "CCTV+1": "CCTV+ 1", "CCTV+ 2": "CCTV+ 2",
    "cctv-1(:3)": "CCTV-1", "CCTV-16": "CCTV-16", "CCTV-16 HD": "CCTV-16 HD",
}

_SAT_CASES = {
    "Anhui TV": "安徽卫视",
    "Beijing Satellite TV HD": "北京卫视 HD",
    "Beijing Satellite TV [Not 24/7]": "北京卫视",
    "Ningxia Satellite Channel": "宁夏卫视",
    "Xinjiang TV 1": "新疆卫视",
    "Tianjin TV": "天津卫视",
    "Shandong TV": "山东卫视",
    "Shenzhen Satellite TV": "深圳卫视",
    "Nei Monggol TV": "内蒙古卫视",
    "Bingtuan Satellite TV [Not 24/7]": "兵团卫视",
    "Dragon TV": "东方卫视",
    "Xizang TV Tibetan [Not 24/7]": "西藏卫视 藏语",
}

_UNTOUCHED = [
    "Dragon TV International", "Guangzhou TV", "QTV-1", "Anshun Comprehensive News Channel",
    "湖南卫视", "北京卫视 HD", "浙江卫视 蓝", "香港卫视", "凤凰卫视中文台",
]


def _fake_channel(name, group="卫视"):
    return types.SimpleNamespace(name=name, group_name=group)


def main() -> int:
    log.info("===== verify_naming：频道名规范化 =====")
    failures: list = []

    def check(label, cond, extra=""):
        log.info("  %-50s %s %s", label, "OK" if cond else "FAIL", extra)
        if not cond:
            failures.append(label)

    # ---- A. 央视 ----
    log.info("-- A. 央视统一 --")
    for raw, want in _CCTV_CASES.items():
        got = canonical_channel_name(raw)
        check("央视 %-24r -> %s" % (raw, want), got == want, "" if got == want else "实得 %r" % got)
    check("CCTV+ 1 不与 CCTV-1 混淆",
          canonical_channel_name("CCTV+1") != canonical_channel_name("CCTV-1"))

    # ---- B. 省级卫视 ----
    log.info("-- B. 省级卫视英文名 -> 中文 --")
    for raw, want in _SAT_CASES.items():
        got = canonical_channel_name(raw)
        check("卫视 %-32r -> %s" % (raw, want), got == want, "" if got == want else "实得 %r" % got)
    for raw in _UNTOUCHED:
        got = canonical_channel_name(raw)
        check("保守不改 %-34r" % raw, got == raw, "" if got == raw else "实得 %r" % got)

    # ---- C. 通用性质 ----
    log.info("-- C. 幂等 / is_canonical --")
    all_samples = list(_CCTV_CASES) + list(_SAT_CASES) + _UNTOUCHED
    bad = [s for s in all_samples
           if canonical_channel_name(canonical_channel_name(s)) != canonical_channel_name(s)]
    check("规范化幂等(%d 例)" % len(all_samples), not bad, bad)
    bad2 = [s for s in all_samples
            if is_canonical(s) != (canonical_channel_name(s) == s)]
    check("is_canonical 与 canonical 一致", not bad2, bad2)
    check("已是规范名则 is_canonical 为真", is_canonical("CCTV-4") and is_canonical("安徽卫视"))
    check("非规范名则 is_canonical 为假", not is_canonical("CCTV4"))

    # ---- D. 改名脚本的冲突保护 ----
    log.info("-- D. 改名脚本冲突保护 --")
    chans = [
        _fake_channel("CCTV4", "中央"),          # 应改为 CCTV-4
        _fake_channel("安徽卫视", "卫视"),         # 已是规范名
        _fake_channel("Anhui TV", "卫视"),        # 目标是"安徽卫视" → 被占用 → 冲突
        _fake_channel("Guangzhou TV", "其他"),     # 不变
    ]
    renames, conflicts = norm.plan_renames(chans)
    rn = {c.name: new for c, new in renames}
    check("正常改名进入计划", rn.get("CCTV4") == "CCTV-4", rn)
    check("已是规范名的不进计划", "安徽卫视" not in [c.name for c, _ in renames])
    check("目标名被占用 -> 记为冲突",
          any(c.name == "Anhui TV" for c, _, _ in conflicts), [c.name for c, _, _ in conflicts])
    check("不再变化的名字不进计划",
          not any(c.name == "Guangzhou TV" for c, _ in renames))

    # 两条都改到同一个名字 → 只放行一条
    dup = [_fake_channel("CCTV4", "中央"), _fake_channel("cctv-4", "中央")]
    renames2, conflicts2 = norm.plan_renames(dup)
    check("两条撞同一个目标名时只放行一条",
          len(renames2) == 1 and len(conflicts2) == 1,
          "renames=%d conflicts=%d" % (len(renames2), len(conflicts2)))

    # ---- E. 落库/同步路径 ----
    log.info("-- E. 落库与同步路径也用规范名 --")
    reset_db()
    entries = [
        ParsedEntry(name="CCTV4", group="央视", url="http://a/4.m3u8", origin="t"),
        ParsedEntry(name="CCTV4 HD", group="央视", url="http://a/4hd.m3u8", origin="t"),
        ParsedEntry(name="Anhui TV", group="卫视", url="http://a/ah.m3u8", origin="t"),
        ParsedEntry(name="凤凰卫视中文台", group="港澳", url="http://a/fh.m3u8", origin="t"),
    ]
    kept = _filter_group_sort(entries)
    kept_names = sorted(e.name for e in kept)
    check("过滤后名字已规范化",
          kept_names == ["CCTV-4", "CCTV-4 HD", "安徽卫视"], kept_names)

    _persist(kept)
    with session_scope() as s:
        db_names = sorted(c.name for c in s.query(Channel).all())
    check("落库写入的是规范名",
          db_names == ["CCTV-4", "CCTV-4 HD", "安徽卫视"], db_names)

    # 再落一次（上游仍写 CCTV4）→ 不应重复建台
    res2 = _persist(_filter_group_sort([
        ParsedEntry(name="CCTV4", group="央视", url="http://b/4b.m3u8", origin="t2"),
    ]))
    with session_scope() as s:
        names_after = sorted(c.name for c in s.query(Channel).all())
    check("上游写 CCTV4 不会再建一个台",
          names_after == db_names and res2.channels_created == 0,
          "created=%d names=%s" % (res2.channels_created, names_after))

    # 同步路径
    core = _core_names([
        ParsedEntry(name="CCTV4", group="", url="http://c/4.m3u8"),
        ParsedEntry(name="Beijing Satellite TV HD", group="", url="http://c/bj.m3u8"),
    ])
    check("sync_sources._core_names 也规范化",
          sorted(e.name for e in core) == ["CCTV-4", "北京卫视 HD"],
          sorted(e.name for e in core))

    try:
        engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(_tmpdir, ignore_errors=True)

    log.info("----- 汇总 -----")
    if failures:
        log.info("存在失败项(%d): %s", len(failures), failures)
    else:
        log.info("verify_naming 全部通过 ✓")
    return EXIT_OK if not failures else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
