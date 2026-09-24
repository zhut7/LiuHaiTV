# SPDX-License-Identifier: GPL-3.0-or-later
"""
应用级键值偏好（音量等"记住上次设置"类数据）。

为什么用数据库而不是 JSON/ini：SQLite 库已经是本项目的持久化载体，
偏好放这里不必再引入一个配置文件，也符合最初设计里
"SQLite 用于存储频道数据、**用户偏好**和源地址"的分工。

表由 `models.Setting` 定义，`Base.metadata.create_all()` 会自动创建，
**老库无需列迁移**（create_all 只建"不存在的表"，正好）。

设计约定：所有读写都吞掉异常并记日志 —— 偏好读不出来只该退回默认值，
绝不能让主界面因此起不来或卡住。
"""
from __future__ import annotations

import logging
from typing import Optional

from liuhaitv.core.database import session_scope
from liuhaitv.core.models import Setting

log = logging.getLogger(__name__)

# 约定好的键名（别再散落魔法字符串）
KEY_VOLUME = "volume"          # 音量 0~100，整数字符串
KEY_LAST_CHANNEL = "last_channel_id"   # 预留：上次播放的频道

# 音量默认值 = 最大
DEFAULT_VOLUME = 100


def get_value(key: str, default: Optional[str] = None) -> Optional[str]:
    """读字符串偏好；不存在或出错时返回 default。"""
    try:
        with session_scope() as s:
            row = s.get(Setting, key)
            return row.value if row is not None else default
    except Exception as exc:  # noqa: BLE001 - 偏好读失败不该影响界面
        log.warning("读取偏好 %s 失败，用默认值: %s", key, exc)
        return default


def set_value(key: str, value: str) -> None:
    """写字符串偏好（不存在则新建）；失败只记日志。"""
    try:
        with session_scope() as s:
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=str(key), value=str(value)))
            else:
                row.value = str(value)
    except Exception as exc:  # noqa: BLE001
        log.warning("保存偏好 %s 失败: %s", key, exc)


def get_int(key: str, default: int) -> int:
    """读整数偏好；缺失或格式不对时返回 default。"""
    raw = get_value(key, None)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("偏好 %s 的值 %r 不是整数，用默认值 %s", key, raw, default)
        return default


def set_int(key: str, value: int) -> None:
    set_value(key, str(int(value)))


def clamp_volume(value: int) -> int:
    """把音量夹到 0~100，避免历史脏数据把滑块设成越界值。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_VOLUME
    return max(0, min(100, v))


def get_volume() -> int:
    """取上次记住的音量（默认最大 100）。"""
    return clamp_volume(get_int(KEY_VOLUME, DEFAULT_VOLUME))


def set_volume(value: int) -> None:
    set_int(KEY_VOLUME, clamp_volume(value))
