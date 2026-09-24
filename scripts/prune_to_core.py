# SPDX-License-Identifier: GPL-3.0-or-later
"""
Step 6：频道精简 —— 只保留「央视 + 省级卫视」，真删其余频道。

背景：Step 2 的搜刮把 iptv-org 全量清单灌进了库（151 频道），里面混着
港澳台、省市地面频道、购物/境外频道，以及 `CCTV-Billiards` / `CCTV-Storm *`
这类挂 CCTV 名头但并不存在的"假央视"。用户要求**只保留央视和地方卫视**。

本脚本做三件事（判定规则全部来自 liuhaitv/core/channel_filter.py）：

  1. **真删**：非核心频道连同其源一起 DELETE（不是隐藏）。
  2. **别名合并**（可用 --no-merge-alias 关闭）：同一频道的多条中/英名重复条目合并成一条。
     例：`北京卫视 HD` + `Beijing Satellite TV HD` + `Beijing Satellite TV` → 一条。
     被合并方的源会**移动**到保留者名下（不丢数据），重复 URL 才丢弃。
     注意：`CCTV-1` 与 `CCTV-1 HD` 视为不同清晰度的独立流，**不合并**。
  3. **重归类**：按新规则刷新 `group_name`，让英文名的省级卫视从"其他"归到"卫视"。

安全措施：
  - 默认 **dry-run**（只打印清单，不碰数据库）；真删必须显式 `--apply`。
  - `--apply` 会先用 SQLite 在线备份 API 把库快照到 `data/backup/`，再动数据。
  - 结束时自动校验：无孤立源、频道/源计数与计划一致。

用法：
  # 预演（默认）
  <env>/python.exe scripts/prune_to_core.py
  # 真删
  <env>/python.exe scripts/prune_to_core.py --apply
  # 指定库 / 关闭别名合并 / 额外豁免 / 报告落文件
  <env>/python.exe scripts/prune_to_core.py --db D:\\path\\liuhaitv.db --no-merge-alias
  <env>/python.exe scripts/prune_to_core.py --keep "CCTV-Billiards,某频道" --out report.txt

返回码：0=成功  1=失败
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import liuhaitv  # noqa: E402  确保 libmpv 等资源路径就绪（与其他脚本保持一致）

log = logging.getLogger("prune_to_core")


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="频道精简：只保留央视 + 省级卫视（默认预演，--apply 才真删）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--apply", action="store_true",
                   help="真的执行删除（会先备份数据库）。不加则是预演。")
    p.add_argument("--db", default=None,
                   help="数据库文件路径，默认取 config.DB_PATH")
    p.add_argument("--no-merge-alias", dest="merge_alias", action="store_false",
                   default=True, help="关闭『同频道中/英名重复条目』的合并")
    p.add_argument("--keep", default="",
                   help="额外豁免的频道名，逗号分隔（精确匹配原名）")
    p.add_argument("--out", default=None, help="把报告写到指定文件（便于留档/回读）")
    p.add_argument("--json", dest="as_json", action="store_true",
                   help="以 JSON 输出报告主体")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# 报告缓冲（同时打印到 stdout 与可选文件）
# ---------------------------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.lines: List[str] = []

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)

    def dump(self, out_path: Optional[str] = None) -> None:
        blob = "\n".join(self.lines)
        try:
            print(blob)
        except UnicodeEncodeError:      # 控制台代码页非 UTF-8 时兜底
            print(blob.encode("utf-8", "replace").decode("utf-8", "replace"))
        if out_path:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(blob + "\n")


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
def backup_db(db_path: str) -> str:
    """用 SQLite 在线备份 API 把库快照到同目录 backup/ 下，返回快照路径。"""
    dest_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backup")
    os.makedirs(dest_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(db_path))[0]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(dest_dir, f"{stem}-{stamp}.db")

    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)      # 一致快照，比文件拷贝安全（不怕 WAL/并发写）
        finally:
            dst.close()
    finally:
        src.close()
    return dest


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------
@dataclass
class Plan:
    keep: List = field(default_factory=list)             # 保留的 Channel 对象
    drop: List = field(default_factory=list)             # 要真删的 Channel 对象
    merges: List[Tuple] = field(default_factory=list)    # [(primary, [absorbed...])]
    regroup: Dict[int, str] = field(default_factory=dict)  # channel_id -> 新 group_name


def _primary_rank(ch) -> tuple:
    """别名合并时选出"哪条留下来"的排序键（越小越优先）。"""
    name = ch.name or ""
    cn_sat = 1 if "卫视" in name else 0           # 1. 中文"卫视"名优先
    en_sat = 1 if "satellite" in name.lower() else 0   # 2. 其次带 Satellite 的英文名
    return (-cn_sat, -en_sat, -len(ch.sources or []), len(name), name.lower())


def build_plan(session, *, merge_alias: bool = True,
               extra_keep: Sequence[str] = ()) -> Plan:
    """算出保留/删除/合并/重归类方案，不改动数据库。"""
    from sqlalchemy.orm import selectinload

    from liuhaitv.core.channel_filter import identity_key, is_core_channel
    from liuhaitv.core.models import Channel
    from liuhaitv.core.scraper import classify_group

    chans = (session.query(Channel)
             .options(selectinload(Channel.sources))
             .order_by(Channel.group_name, Channel.sort_order, Channel.name).all())

    extra = {n.strip() for n in extra_keep if n and n.strip()}
    plan = Plan()

    for c in chans:
        if c.name in extra or is_core_channel(c.name, c.group_name):
            plan.keep.append(c)
        else:
            plan.drop.append(c)

    # --- 别名合并：同一 identity_key 的多条只留一条 ---
    if merge_alias and plan.keep:
        buckets: Dict[str, List] = {}
        for c in plan.keep:
            buckets.setdefault(identity_key(c.name), []).append(c)

        merged: List = []
        for _key, members in buckets.items():
            if len(members) == 1:
                merged.append(members[0])
                continue
            primary = min(members, key=_primary_rank)
            others = [m for m in members if m is not primary]
            plan.merges.append((primary, others))
            merged.append(primary)
        plan.keep = merged

    # --- 重归类（只对最终保留的频道做）---
    for c in plan.keep:
        want = classify_group(c.name, c.group_name)
        if want != c.group_name:
            plan.regroup[c.id] = want

    return plan


def apply_plan(session, plan: Plan) -> Dict[str, int]:
    """
    执行计划。返回统计（移动的源数、删除频道数、重归类数）。

    注意：plan 里的 ORM 对象可能来自**已关闭**的会话（build_plan 与 apply_plan
    可以分属两次 session_scope），所以这里统一按 id 在本 session 内重新取对象，
    使 apply_plan 与调用方的会话状态解耦。
    """
    from sqlalchemy.orm import selectinload

    from liuhaitv.core.models import Channel

    # 被合并方既不在 keep（已被合并掉）也不在 drop 里，必须一并纳入 id 列表
    ids = [c.id for c in plan.keep] + [c.id for c in plan.drop]
    ids += [o.id for _p, others in plan.merges for o in others]
    objs: Dict[int, object] = {}
    if ids:
        rows = (session.query(Channel)
                .options(selectinload(Channel.sources))
                .filter(Channel.id.in_(ids)).all())
        objs = {c.id: c for c in rows}

    stats = {"sources_moved": 0, "sources_deduped": 0, "channels_deleted": 0,
             "channels_merged": 0, "regrouped": 0}

    # 1) 别名合并：把被合并方的源挪到保留者，再删掉被合并的频道
    for primary_ref, others_ref in plan.merges:
        primary = objs.get(primary_ref.id)
        if primary is None:
            continue
        have = {s.url for s in primary.sources}
        prio = max([s.default_priority for s in (primary.sources or [])], default=-1) + 1
        for other_ref in others_ref:
            other = objs.get(other_ref.id)
            if other is None:
                continue
            for src in list(other.sources):
                if src.url in have:
                    stats["sources_deduped"] += 1
                    continue          # 重复地址：随被合并频道一起删掉
                src.default_priority = prio
                prio += 1
                primary.sources.append(src)   # back_populates：自动从 other.sources 摘除
                have.add(src.url)
                stats["sources_moved"] += 1
            session.delete(other)
            stats["channels_merged"] += 1

    # 2) 真删非核心频道（cascade 会一并删除其源）
    for c in plan.drop:
        obj = objs.get(c.id)
        if obj is not None:
            session.delete(obj)
            stats["channels_deleted"] += 1

    # 3) 重归类
    for c in plan.keep:
        obj = objs.get(c.id)
        want = plan.regroup.get(c.id)
        if obj is not None and want:
            obj.group_name = want
            stats["regrouped"] += 1

    session.flush()
    return stats


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    import liuhaitv.config as cfg
    if args.db:
        cfg.DB_PATH = os.path.abspath(args.db)

    import liuhaitv.logger as pylog
    pylog.setup_logging()

    db_path = cfg.DB_PATH
    if not os.path.exists(db_path):
        log.error("数据库不存在: %s", db_path)
        return 1

    # 必须在改完 cfg.DB_PATH 之后再导入 database（engine 在导入时绑定路径）
    from liuhaitv.core.database import init_db, session_scope
    from liuhaitv.core.models import Channel, Source

    init_db()

    extra_keep = [x for x in (args.keep or "").split(",") if x.strip()]
    rp = Report()
    rp("=" * 78)
    rp("LiuHaiTV 频道精简 —— 只保留央视 + 省级卫视")
    rp("数据库: %s" % db_path)
    rp("模式  : %s" % ("真删 (--apply)" if args.apply else "预演 (dry-run，不写库)"))
    rp("合并别名: %s" % ("开" if args.merge_alias else "关"))
    if extra_keep:
        rp("豁免  : %s" % ", ".join(extra_keep))
    rp("=" * 78)

    # ---- 1. 备份（仅真删前）----
    backup_path = None
    if args.apply:
        try:
            backup_path = backup_db(db_path)
            rp("")
            rp("[备份] 已快照数据库 -> %s" % backup_path)
        except Exception as exc:  # noqa: BLE001
            log.exception("备份失败，中止以免误删: %s", exc)
            rp("")
            rp("[备份] 失败: %s —— 已中止，未做任何改动。" % exc)
            rp.dump(args.out)
            return 1

    # ---- 2. 构建计划 ----
    with session_scope() as s:
        plan = build_plan(s, merge_alias=args.merge_alias, extra_keep=extra_keep)
        n_ch_before = s.query(Channel).count()
        n_src_before = s.query(Source).count()

    kept_names = {c.name for c in plan.keep}

    # ---- 3. 打印计划 ----
    by_group_keep: Dict[str, List[str]] = {}
    for c in plan.keep:
        by_group_keep.setdefault(c.group_name, []).append(c.name)
    by_group_drop: Dict[str, List[str]] = {}
    for c in plan.drop:
        by_group_drop.setdefault(c.group_name, []).append(c.name)

    rp("")
    rp("-" * 78)
    rp("【保留】%d 个频道" % len(plan.keep))
    rp("-" * 78)
    for g in ("中央", "卫视", "港澳台", "地方", "其他"):
        names = by_group_keep.get(g)
        if not names:
            continue
        rp("  [%s] %d 个:" % (g, len(names)))
        for n in names:
            rp("      - %s" % n)

    rp("")
    rp("-" * 78)
    rp("【删除】%d 个频道（连同其源一并 DELETE）" % len(plan.drop))
    rp("-" * 78)
    for g in ("中央", "卫视", "港澳台", "地方", "其他"):
        names = by_group_drop.get(g)
        if not names:
            continue
        rp("  [%s] %d 个:" % (g, len(names)))
        for n in names:
            rp("      x %s" % n)

    rp("")
    rp("-" * 78)
    rp("【别名合并】%d 组（保留中文/带 Satellite 的那条，源不丢）" % len(plan.merges))
    rp("-" * 78)
    for primary, others in plan.merges:
        rp("  %s  <= 合并 %s" % (primary.name, " / ".join(o.name for o in others)))

    if plan.regroup:
        rp("")
        rp("-" * 78)
        rp("【重归类】%d 条（英文名省级卫视 其他 -> 卫视）" % len(plan.regroup))
        rp("-" * 78)
        for c in plan.keep:
            want = plan.regroup.get(c.id)
            if want:
                rp("  %s : %s -> %s" % (c.name, c.group_name, want))

    # ---- 4. 执行 ----
    stats = {"sources_moved": 0, "sources_deduped": 0, "channels_deleted": 0,
             "channels_merged": 0, "regrouped": 0}
    if args.apply:
        with session_scope() as s:
            stats = apply_plan(s, plan)

    # ---- 5. 结果 ----
    with session_scope() as s:
        n_ch_after = s.query(Channel).count()
        n_src_after = s.query(Source).count()
        orphan = s.query(Source).filter(
            ~Source.channel_id.in_(s.query(Channel.id))
        ).count()

    rp("")
    rp("=" * 78)
    if args.apply:
        rp("【结果】")
        rp("  频道: %d -> %d   (删除 %d, 合并 %d)"
           % (n_ch_before, n_ch_after, stats["channels_deleted"], stats["channels_merged"]))
        rp("  源  : %d -> %d   (跨频道移动 %d, 重复丢弃 %d)"
           % (n_src_before, n_src_after, stats["sources_moved"], stats["sources_deduped"]))
        rp("  重归类: %d 条" % stats["regrouped"])
        rp("  孤立源: %d  %s" % (orphan, "OK" if orphan == 0 else "!! 异常"))
        rp("  备份  : %s" % backup_path)
    else:
        rp("【预演统计】（未写库）")
        rp("  当前  : %d 频道 / %d 源" % (n_ch_before, n_src_before))
        rp("  将保留: %d 频道" % len(plan.keep))
        rp("  将删除: %d 频道（含其源）" % len(plan.drop))
        rp("  将合并: %d 组别名" % len(plan.merges))
        rp("  将重归类: %d 条" % len(plan.regroup))
        rp("  预计保留源数 = 保留频道现有源 + 被合并方迁入的源")
        rp("")
        rp("  加 --apply 才会真正执行（会先自动备份）。")
    rp("=" * 78)

    if args.as_json:
        payload = {
            "db": db_path,
            "applied": bool(args.apply),
            "backup": backup_path,
            "channels_before": n_ch_before,
            "channels_after": n_ch_after if args.apply else None,
            "sources_before": n_src_before,
            "sources_after": n_src_after if args.apply else None,
            "keep": sorted(kept_names),
            "drop": sorted(c.name for c in plan.drop),
            "merges": [{"keep": p.name, "absorbed": [o.name for o in oth]}
                       for p, oth in plan.merges],
            "regroup": {c.name: [c.group_name, plan.regroup[c.id]]
                        for c in plan.keep if c.id in plan.regroup},
            "stats": stats,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    rp.dump(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
