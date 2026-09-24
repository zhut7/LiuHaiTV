# SPDX-License-Identifier: GPL-3.0-or-later
"""
后台检测任务（Step 5：点击频道 → 探测 → 回主线程着色）。

为什么要单独放 QRunnable：
  - 探测是网络 I/O（可能耗时数秒），绝不能在 GUI 线程里同步跑，否则界面假死；
  - 每次点击只探一个频道（verify_channel，找到可用源即早退），任务短小，
    用 QThreadPool 的线程池比"一个频道起一个 QThread"更省资源；
  - 信号跨线程 emit，Qt 会自动排队到主线程（接收方是主线程的 QObject），
    因此回调里可以安全操作 Model / Widget。

两类任务：
  - ChannelCheckTask ：判定"频道能否播放"（供列表绿/红）。
  - SourceProbeTask  ：探测源管理弹窗里选中的单个地址（供表格刷新延迟/状态）。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from liuhaitv.core import health

log = logging.getLogger(__name__)

# 模块级保活表：channel_id -> [signal 对象]
# 为什么需要它：signals 若无父对象，只被 window/checker 引用；一旦窗口先被销毁
# （用户关窗 / 进程退出），工作线程里的 emit 就会抛 "Signal source has been deleted"。
# 放到模块级容器后，任务在跑期间 QObject 不会被回收，结果总能安全送达（或安静丢弃）。
_ALIVE_SIGNALS: dict = {}


def _retain(cid: int, obj) -> None:
    _ALIVE_SIGNALS.setdefault(cid, []).append(obj)


def _release(cid: int, obj) -> None:
    lst = _ALIVE_SIGNALS.get(cid)
    if not lst:
        return
    try:
        lst.remove(obj)
    except ValueError:
        pass
    if not lst:
        _ALIVE_SIGNALS.pop(cid, None)


def _safe_emit(signals, *args) -> None:
    """安全 emit：会话/窗口已销毁时安静丢弃（属正常退出路径，不该是 ERROR）。"""
    try:
        signals.done.emit(*args)
    except RuntimeError as exc:
        log.debug("检测结果丢弃（接收方已销毁）: %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("检测结果发送失败: %s", exc)


class _ChannelSignals(QObject):
    # channel_id, ok(能否播放), note(失败原因/空)
    done = Signal(int, bool, str)


class ChannelCheckTask(QRunnable):
    """探测单个频道是否可播放。"""

    def __init__(self, channel_id: int, signals: _ChannelSignals) -> None:
        super().__init__()
        self.channel_id = channel_id
        self.signals = signals
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            verdict = health.verify_channel(self.channel_id)
            _safe_emit(self.signals, self.channel_id, bool(verdict.ok), verdict.note)
        except Exception as exc:  # noqa: BLE001 - 线程内异常不能外泄，否则静默丢结果
            log.exception("频道 %s 检测任务异常: %s", self.channel_id, exc)
            _safe_emit(self.signals, self.channel_id, False, f"检测异常: {exc}")


class ChannelChecker(QObject):
    """
    点击判定的调度器：同一频道不会重复并发检测，结果经信号回到主线程。

    用法：
        checker = ChannelChecker()
        checker.finished.connect(self._on_check_done)
        checker.check(channel_id)          # 已在检测中则返回 False
    """

    # channel_id, ok, note
    finished = Signal(int, bool, str)

    def __init__(self, parent=None, max_threads: int = 3) -> None:
        super().__init__(parent)
        self._inflight: set = set()
        self._keepalive: dict = {}
        pool = QThreadPool.globalInstance()
        # 只影响本进程线程池上限（3 个频道并发探测足够）
        if pool.maxThreadCount() > max_threads:
            pool.setMaxThreadCount(max_threads)

    def is_checking(self, channel_id: int) -> bool:
        return channel_id in self._inflight

    def check(self, channel_id: int) -> bool:
        """提交检测任务；该频道已在检测中则忽略并返回 False。"""
        if channel_id is None or channel_id in self._inflight:
            return False
        self._inflight.add(channel_id)
        signals = _ChannelSignals()
        signals.done.connect(self._on_done)
        # 模块级保活 + 实例保活：双保险，防止任务执行期间 signals 被 GC
        _retain(channel_id, signals)
        self._keepalive[channel_id] = signals
        QThreadPool.globalInstance().start(ChannelCheckTask(channel_id, signals))
        log.info("已提交频道检测任务: id=%s", channel_id)
        return True

    @Slot(int, bool, str)
    def _on_done(self, channel_id: int, ok: bool, note: str) -> None:
        self._inflight.discard(channel_id)
        signals = self._keepalive.pop(channel_id, None)
        if signals is not None:
            _release(channel_id, signals)
        self.finished.emit(channel_id, bool(ok), note or "")


class _SourceSignals(QObject):
    # url, ok, latency_ms, note
    done = Signal(str, bool, object, str)


class SourceProbeTask(QRunnable):
    """探测源管理弹窗中选中的单个地址。"""

    def __init__(self, url: str, signals: _SourceSignals) -> None:
        super().__init__()
        self.url = url
        self.signals = signals
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            ok, latency, note = health.probe_url(self.url)
        except Exception as exc:  # noqa: BLE001
            log.exception("源探测任务异常 %s: %s", self.url[:60], exc)
            ok, latency, note = False, None, f"探测异常: {exc}"
        _safe_emit(self.signals, self.url, bool(ok), latency, note or "")


class SourceProber(QObject):
    """源管理弹窗用的单地址探测器（可并发探多个选中行）。"""

    # url, ok, latency_ms, note
    finished = Signal(str, bool, object, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._signals = []

    def probe(self, url: str) -> bool:
        if not url:
            return False
        signals = _SourceSignals()
        signals.done.connect(self.finished)
        self._signals.append(signals)
        # 限制常驻数量，避免长时间运行后列表膨胀
        if len(self._signals) > 64:
            self._signals = self._signals[-32:]
        QThreadPool.globalInstance().start(SourceProbeTask(url, signals))
        return True
