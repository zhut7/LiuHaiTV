# SPDX-License-Identifier: GPL-3.0-or-later
"""
数据库层：SQLAlchemy 引擎 + 会话工厂 + 初始化/种子。

职责：
  - 构建指向 config.DB_PATH 的 SQLite 引擎（连接池 + 外键约束开关）。
  - 提供 session_factory —— 全项目统一的 Session 创建入口。
    （UI 线程与异步健康检测共用；并发写由 SQLite 自身串行化处理。）
  - init_db()     创建表（若不存在），并给老库补齐缺失列（幂等，可重复调用）。
  - reset_db()    仅测试/维护用：删除全部业务表（谨慎使用）。

用 SQLModel 风格但保持轻量：core 不引入第三方 ORM 之外的依赖，
仍以 SQLAlchemy 2.0 原生 API 为准。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

import liuhaitv.config as cfg
from liuhaitv.core.models import Base

log = logging.getLogger(__name__)


def _build_engine_url(db_path: str) -> str:
    """Convert a db file path to a sqlite:// URL (SQLAlchemy 2.0 needs a real URL)."""
    # Use forward slashes to avoid Windows backslash being mis-parsed
    normalized = db_path.replace("\\", "/")
    return f"sqlite:///{normalized}"


def _fk_pragma(dbapi_conn, _) -> None:
    """SQLite 默认不强制外键，此回调让每次新连接都开启外键约束。"""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


engine = create_engine(
    _build_engine_url(cfg.DB_PATH),
    connect_args={"check_same_thread": False},  # 允许跨线程使用连接
    echo=False,
    pool_pre_ping=True,
)
# SQLite 引擎是静态单例，创建后立即绑定外键 pragma
event.listen(engine, "connect", _fk_pragma)


session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


# ---------------------------------------------------------------------------
# 轻量列迁移
# ---------------------------------------------------------------------------
# create_all() 只建"不存在的表"，**不会**给已存在的表补列。
# 老库（Step 2 建的 liuhaitv.db）缺 last_status / is_enabled，
# 这里用幂等的 ALTER TABLE 补齐，避免用户手动删库。
# 约定：SQLite 的 ADD COLUMN 若带 NOT NULL 必须给 DEFAULT。
_COLUMN_MIGRATIONS = {
    "channels": {
        # 点击播放后的判定结果：'good'(绿)/'bad'(红)/NULL(灰=本次会话未点击)
        "last_status": "VARCHAR(16)",
    },
    "sources": {
        # 源是否启用（禁用源不参与播放与检测）
        "is_enabled": "BOOLEAN NOT NULL DEFAULT 1",
        # 源管理里被手动改过 URL 的标记：同步直播源时据此保护用户改动
        "is_user_edited": "BOOLEAN NOT NULL DEFAULT 0",
    },
}


def _migrate_columns() -> None:
    """给已存在的表补齐 _COLUMN_MIGRATIONS 中声明的缺失列（幂等）。"""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        existing_tables = set(insp.get_table_names())
    except Exception as exc:  # noqa: BLE001 - 探测失败则跳过迁移（新库无需迁移）
        log.warning("读取表结构失败，跳过列迁移: %s", exc)
        return

    for table, columns in _COLUMN_MIGRATIONS.items():
        if table not in existing_tables:
            continue  # 新库由 create_all 直接建全，无需补列
        try:
            have = {c["name"] for c in insp.get_columns(table)}
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 %s 列信息失败，跳过: %s", table, exc)
            continue
        for col, ddl in columns.items():
            if col in have:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {ddl}'))
                log.info("迁移: %s 表新增列 %s", table, col)
            except Exception as exc:  # noqa: BLE001 - 单列失败不阻断启动
                log.error("迁移 %s.%s 失败: %s", table, col, exc)


def init_db() -> None:
    """在建表前确保 data/ 目录存在，然后创建全部表并补齐缺列（幂等）。"""
    try:
        cfg.ensure_dirs()
        # 打包形态首次运行：把随包的"种子库"复制到 exe 旁边，
        # 这样换台电脑一打开就有完整的央视+卫视频道表
        if cfg.ensure_seed_db():
            log.info("首次运行：已从随包模板库播种数据库 -> %s", cfg.DB_PATH)
        Base.metadata.create_all(engine)
        _migrate_columns()
        log.info("数据库初始化完成: %s", cfg.DB_PATH)
    except Exception as exc:  # noqa: BLE001 - 初始化失败需对外暴露但可重试
        log.exception("数据库初始化失败: %s", exc)
        raise


@contextmanager
def session_scope() -> Iterator[Session]:
    """
    上下文管理器风格的会话。保证异常时回滚、正常时提交并关闭。

    用法：
        with session_scope() as s:
            s.add(...)
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Session:
    """创建一个独立会话（适用于需要手动控制提交时机的场景）。"""
    return session_factory()
