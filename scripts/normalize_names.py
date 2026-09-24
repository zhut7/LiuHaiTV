# SPDX-License-Identifier: GPL-3.0-or-later
"""
把库里已存在的频道名批量规范化为 `naming.canonical_channel_name()` 的形态。

背景：各上游清单对同一个台的写法不一致（`CCTV4` / `CCTV-4`、`Beijing Satellite TV HD` /
`北京卫视 HD`），历史落库的数据会混着这些形态。本脚本做一次性清洗，把显示名统一。

只改 `Channel.name`，**不动**任何源地址；改名后同步/搜刮的匹配依然靠
`channel_filter.identity_key()`，所以不影响频道对齐。

安全措施：
  - 默认 **dry-run**（只打印将改名的清单）；真改必须显式 `--apply`
  - `--apply` 先备份数据库到 `data/backup/`
  - **冲突保护**：若改名后会和另一个频道重名，则跳过并报告（绝不制造重复频道）

用法：
  <env>/python.exe scripts/normalize_names.py
  <env>/python.exe scripts/normalize_names.py --apply
  <env>/python.exe scripts/normalize_names.py --out report.txt

返回码：0=成功  1=失败
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional, Sequence, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import liuhaitv  # noqa: E402

# naming 只依赖 channel_filter（不碰 database），所以可以安全地在设置 DB_PATH 之前导入
from liuhaitv.core.naming import canonical_channel_name  # noqa: E402

log = logging.getLogger("normalize_names")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="频道名规范化（默认预演，--apply 才真改）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="真的改名（会先备份数据库）")
    p.add_argument("--db", default=None, help="数据库路径，默认取 config.DB_PATH")
    p.add_argument("--out", default=None, help="把报告写到文件")
    return p.parse_args(argv)


class Report:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)

    def dump(self, out_path: Optional[str] = None) -> None:
        blob = "\n".join(self.lines)
        try:
            print(blob)
        except UnicodeEncodeError:
            print(blob.encode("utf-8", "replace").decode("utf-8", "replace"))
        if out_path:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(blob + "\n")


def plan_renames(channels) -> Tuple[List[Tuple], List[Tuple]]:
    """
    返回 (将改名清单, 冲突清单)。

    冲突 = 改名后会与另一个频道同名（含"两条都改到同一个名字"的情况），
    一律跳过，避免制造重复频道。
    """
    todo: List[Tuple] = []
    for c in channels:
        new = canonical_channel_name(c.name)
        if new and new != c.name:
            todo.append((c, new))

    # 统计每个目标名会被几个频道占用（含本来就叫这个名字的频道）
    taken = {}
    for c in channels:
        taken.setdefault(c.name, []).append(c)

    renames: List[Tuple] = []
    conflicts: List[Tuple] = []
    for c, new in todo:
        occupiers = [x for x in taken.get(new, []) if x is not c]
        if occupiers:
            conflicts.append((c, new, [x.name for x in occupiers]))
        else:
            renames.append((c, new))
            # 同步更新占用表，避免两条都改到同一个名字却互相看不见
            taken.setdefault(new, []).append(c)
            taken[c.name] = [x for x in taken.get(c.name, []) if x is not c]
    return renames, conflicts


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    import liuhaitv.config as cfg
    if args.db:
        cfg.DB_PATH = os.path.abspath(args.db)

    import liuhaitv.logger as pylog
    pylog.setup_logging()

    from liuhaitv.core.database import init_db, session_scope
    from liuhaitv.core.models import Channel

    db_path = cfg.DB_PATH
    if not os.path.exists(db_path):
        log.error("数据库不存在: %s", db_path)
        return 1
    init_db()

    rp = Report()
    rp("=" * 78)
    rp("频道名规范化")
    rp("数据库: %s" % db_path)
    rp("模式  : %s" % ("真改 (--apply)" if args.apply else "预演 (dry-run，不写库)"))
    rp("=" * 78)

    backup_path = None
    if args.apply:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "liuhaitv_prune", os.path.join(_PROJECT_ROOT, "scripts", "prune_to_core.py"))
        prune = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = prune
        spec.loader.exec_module(prune)
        try:
            backup_path = prune.backup_db(db_path)
            rp("")
            rp("[备份] 已快照数据库 -> %s" % backup_path)
        except Exception as exc:  # noqa: BLE001
            log.exception("备份失败，中止以免误改: %s", exc)
            rp("[备份] 失败: %s —— 已中止，未做任何改动。" % exc)
            rp.dump(args.out)
            return 1

    with session_scope() as s:
        channels = s.query(Channel).all()
        total = len(channels)
        renames, conflicts = plan_renames(channels)

        rp("")
        rp("库内频道共 %d 个；需要改名的 %d 个，冲突跳过 %d 个。"
           % (total, len(renames), len(conflicts)))
        rp("")
        if renames:
            rp("-" * 78)
            rp("【将改名】%d 个" % len(renames))
            rp("-" * 78)
            for c, new in sorted(renames, key=lambda x: x[0].group_name):
                rp("  [%s] %-34s -> %s" % (c.group_name, c.name, new))
        if conflicts:
            rp("")
            rp("-" * 78)
            rp("【冲突跳过】%d 个（改名后会与已有频道重名，请手工处理）" % len(conflicts))
            rp("-" * 78)
            for c, new, occ in conflicts:
                rp("  %-34s -/-> %-20s 已被 %s 占用" % (c.name, new, "、".join(occ)))

        if args.apply and renames:
            for c, new in renames:
                c.name = new
            s.flush()
            rp("")
            rp("[结果] 已改名 %d 个频道（源地址未改动）" % len(renames))

        after_all_canonical = all(
            canonical_channel_name(c.name) == c.name for c in channels)

    rp("")
    rp("=" * 78)
    if args.apply:
        rp("复核：库内所有频道名是否都已是规范形态 -> %s"
           % ("是" if after_all_canonical else "否（存在冲突跳过项）"))
        rp("备份：%s" % backup_path)
    else:
        rp("加 --apply 才会真正改名（会先自动备份）。")
    rp("=" * 78)

    rp.dump(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
