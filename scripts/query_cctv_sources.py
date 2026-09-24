# SPDX-License-Identifier: GPL-3.0-or-later
"""查询库里 CCTV-14 / CCTV-10 的所有源（区分 origin，用于换源测试）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import liuhaitv  # noqa: E402
from liuhaitv.core.database import session_scope  # noqa: E402
from liuhaitv.core.models import Channel  # noqa: E402

with session_scope() as s:
    for c in s.query(Channel).all():
        name = c.name or ""
        if "14" in name or "10" in name:
            print(f"[{c.id}] {name} (group={c.group_name})")
            for so in c.sources:
                print(f"    healthy={int(so.is_healthy)} lat={so.latency_ms} "
                      f"origin={so.origin} kind={so.kind} {so.url}")
