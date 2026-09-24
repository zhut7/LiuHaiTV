# SPDX-License-Identifier: GPL-3.0-or-later
"""
ORM 数据模型（SQLAlchemy 2.0 声明式）。

Step 2 提供两张核心表：
  - Channel  频道：名称、分类(中央/卫视/港澳台/地方/其他)、排序、可见性、上次观看、播放次数
  - Source   源：一个频道绑定多条备用源；记录 origin/source_id/kind(容器)/protocol(ipv4|ipv6|域名)
              —— 后两项供 Step 3/4 的 failover 健康检测决策参考；健康结果也落库

设计要点：
  - 源与频道是一对多（channel_id 外键）。
  - 健康相关字段（is_healthy / latency_ms / checked_at）由 Step 4 的健康检测
    异步更新，这里先占位，类型可直接用于写回。
  - 用 SQLAlchemy 2.0 原生样式（Mapped + mapped_column），不使用旧式 Column 风格。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """所有 ORM 模型的共同基类。"""


class Channel(Base):
    """直播间/频道主表。"""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 频道名称（显示名，如 "CCTV-1 综合"）
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 分类/分组：中央 / 卫视 / 港澳台 / 地方 / 其他
    group_name: Mapped[str] = mapped_column(String(64), nullable=False, default="其他")

    # 排序权重（越小越靠前）。同一分类内按此排序
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 【已废弃】原「用户隐藏频道」标记 —— 隐藏功能已按需求整体移除
    # （右键菜单的隐藏项、控制条的显示开关、以及各处 is_visible 过滤都不在了），
    # 现在没有任何业务代码读写它。
    # 保留该字段与默认值只为兼容老库，**请勿在新代码中使用**：
    # 老库的 channels.is_visible 是 `NOT NULL` 且没有服务端默认值
    # （SQLAlchemy 的 default 是 Python 侧默认），一旦把它从模型里删掉，
    # INSERT 就会触发 NOT NULL 约束失败；而 SQLite 也无法简单地 DROP/ALTER COLUMN。
    is_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # 最后一次"点击播放"的判定结果（三态）：
    #   'good' = 能播放(绿) / 'bad' = 不能播放(红) / None = 未判定(灰)
    last_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # tvg-logo（频道台标 URL），可选
    logo_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # --- 播放统计 / 恢复记忆 ---
    # 上次观看的时刻（用于"启动自动恢复上次频道"，由播放器封装写入）
    last_watched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 累计播放次数
    played_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    # 一对多：该频道绑定的一系列直播源。按 default_priority 排序
    sources: Mapped[list["Source"]] = relationship(
        "Source",
        back_populates="channel",
        cascade="all, delete-orphan",
        order_by="Source.default_priority",
    )

    def __repr__(self) -> str:
        return f"<Channel #{self.id} {self.name} [{self.group_name}]>"


class Source(Base):
    """单个直播源地址（绑定到一个频道）。"""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )

    # 直播流地址（http/https 直链，或 rstp 等）
    url: Mapped[str] = mapped_column(String(1024), nullable=False)

    # 稳定标识：<来源>:<频道归一化名>:<序号>，供外部引用/去重追踪（区别于自增 id）
    source_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # 来源标识：搜刮自哪个源/仓库（如 iptvorg_cn / fanqiang_cn），或 手动导入 user
    origin: Mapped[str] = mapped_column(String(64), nullable=False, default="user")

    # 流的容器/格式种类，由 URL 扩展名推断（供 failover 决策优先级参考）
    #   例: "hls"(.m3u8) "ts"(.ts) "flv"(.flv) "rtmp" "m3u" "http"(其它直链)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="http")

    # 网络协议族，由 URL 判断: "ipv6" / "ipv4" / "domain"(域名)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False, default="domain")

    # 默认优先级：数字越小越优先播放（备用源排序依据）
    default_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- 健康检测结果（Step 4 写回）---
    # 该源是否当前可用（最近一次检测通过）
    is_healthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 是否启用（源管理里可启用/禁用；禁用源不参与播放）
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 该地址是否被用户**手动改过**（源管理里编辑过 URL 才会置位）。
    # 「同步直播源」据此判断：被手改的源绝不覆盖，改为把上游来的新地址另存一条并置顶。
    is_user_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 最近一次检测的延迟（毫秒）；None = 尚未检测
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 最近一次被检测 / 使用的时刻
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # 创建时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    # 多对一反向
    channel: Mapped["Channel"] = relationship("Channel", back_populates="sources")

    def __repr__(self) -> str:
        return (
            f"<Source #{self.id} pri={self.default_priority} "
            f"healthy={self.is_healthy} {self.url[:48]}>"
        )


class Setting(Base):
    """
    应用级键值偏好（音量等）。

    极简 K/V 表：`Base.metadata.create_all()` 会自动建出"不存在的表"，
    所以老库**不需要**列迁移就能用上它（与"给已有表补列"那套机制互不干扰）。

    读写统一走 `liuhaitv/core/settings.py`，不要在业务代码里直接操作本表。
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Setting {self.key}={self.value!r}>"
