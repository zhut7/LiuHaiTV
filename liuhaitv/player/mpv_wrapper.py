# SPDX-License-Identifier: GPL-3.0-or-later
"""
播放器封装 + Failover（Step 3 核心）。

结构：
  - MPVPlayer：python-mpv 薄封装。持有真实 mpv 实例，提供
    play / stop / pause / resume / set_volume 等操作；用 register_event_callback
    注册事件回调，把 "end-file(原因)" 翻译成对外回调。
    视频窗口嵌入(HWND)在窗口创建后通过 set_wid() 注入（Step 5 UI 调用）。

  - PlayerController：Failover 状态机。与 mpv 解耦 —— 接收 MPVPlayer 翻译后的
    end-file 原因(字符串)，按频道绑定的一串备用源逐个尝试（自动切源），
    超过 max_failovers 后停止并回调 on_exhausted。
    顺序：is_healthy 为 True 的源在前（Step 4 健康检测后自动生效），其余按 priority。

设计要点（基于真机探测定论）：
  - python-mpv 没有 get_property/set_property/wait_event。事件用
    register_event_callback(cb)；回调收到 MpvEvent，用 ev.as_dict() 拿 dict，
    其中 event/reason 是 bytes，需 decode。
  - reason 语义（python-mpv 的 MpvEventEndFile 枚举）：
      EOF=0 RESTARTED=1 ABORTED=2 QUIT=3 ERROR=4 REDIRECT=5
    故用字符串比较更稳（'eof'/'error'/'redirect' 触发 failover；'quit' 视为用户停止）。
  - 所有操作 try-except + logging，不裸 except。
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

# 触发 failover 的 end-file 原因（字符串形式）。'quit' 属用户主动停止，不自动切。
_FAILOVER_REASONS = {"eof", "error", "redirect"}
# 用户主动停止产生的原因（mpv 主动 stop/quit/watch-later）
_MANUAL_REASONS = {"quit", "stop", "aborted"}


def _as_str(v) -> str:
    """end-file 的 event/reason 可能是 bytes，统一 decode 成 str。"""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


# ===========================================================================
# MPVPlayer：python-mpv 薄封装
# ===========================================================================
class MPVPlayer:
    """封装一个真实 mpv 实例。默认 vo=null，可在 UI 中 set_wid 后切到窗口渲染。"""

    def __init__(
        self,
        on_end_file: Optional[Callable] = None,   # 回调(reason_str)
        vo: str = "null",
        wid: Optional[int] = None,
    ) -> None:
        self._on_end_file = on_end_file

        try:
            import mpv as mpv_mod
            self._mpv = mpv_mod.MPV(
                vo=vo,
                wid=wid if wid is not None else 0,
                terminal=False,
                ytdl=False,
                input_default_bindings=False,
                input_vo_keyboard=False,
                osc=False,
                keep_open="yes",   # 断流后保持窗口，便于 failover 切源续播
                idle="yes",
            )
            self._mpv.volume = 80
        except Exception as exc:  # noqa: BLE001 - dll/实例创建失败，交高层处理
            log.exception("MPV 实例创建失败: %s", exc)
            raise

        self._mpv.register_event_callback(self._on_mpv_event)

    # -- 基础操作 -----------------------------------------------------------
    def play(self, url: str) -> None:
        """开始/切换播放一个流地址。若源不可用，mpv 会触发 end-file(error)，由 failover 接管。"""
        try:
            self._mpv.play(url)
            self._mpv.pause = False
            log.info("播放: %s", url[:80])
        except Exception as exc:  # noqa: BLE001
            log.error("启动播放失败 %s: %s", url[:80], exc)

    def stop(self) -> None:
        """主动停止（触发 end-file('quit')，不会走 failover）。"""
        try:
            self._mpv.stop()
        except Exception as exc:  # noqa: BLE001
            log.error("停止播放失败: %s", exc)

    def pause(self) -> None:
        try:
            self._mpv.pause = True
        except Exception as exc:  # noqa: BLE001
            log.error("暂停失败: %s", exc)

    def resume(self) -> None:
        try:
            self._mpv.pause = False
        except Exception as exc:  # noqa: BLE001
            log.error("恢复失败: %s", exc)

    def set_volume(self, vol: float) -> None:
        try:
            self._mpv.volume = max(0, min(100, float(vol)))
        except Exception as exc:  # noqa: BLE001
            log.error("设置音量失败: %s", exc)

    def set_wid(self, win_id: int) -> None:
        """把视频渲染绑定到指定窗口句柄(HWND)。UI 用 widget.winId() 传入。"""
        try:
            self._mpv.wid = int(win_id)
            log.info("视频窗口已绑定 wid=%d", win_id)
        except Exception as exc:  # noqa: BLE001
            log.error("绑定窗口失败: %s", exc)

    @property
    def is_idle(self) -> bool:
        """mpv 是否空闲（无在播内容）。用于判断当前源是否可用。"""
        try:
            return bool(self._mpv.core_idle)
        except Exception:  # noqa: BLE001
            return True

    @property
    def mpv_version(self) -> str:
        try:
            return str(self._mpv.mpv_version)
        except Exception:  # noqa: BLE001
            return "?"

    @property
    def volume(self) -> float:
        try:
            return float(self._mpv.volume)
        except Exception:  # noqa: BLE001
            return 0.0

    # -- 事件回调 -----------------------------------------------------------
    def _on_mpv_event(self, ev) -> None:
        """python-mpv 事件回调：把 end-file 翻译成对外回调。"""
        try:
            d = ev.as_dict()
        except Exception as exc:  # noqa: BLE001
            return
        name = _as_str(d.get("event"))
        if name != "end-file":
            return
        reason = _as_str(d.get("reason"))
        log.info("end-file 事件 reason=%s", reason)
        if self._on_end_file:
            try:
                self._on_end_file(reason)
            except Exception as exc:  # noqa: BLE001
                log.error("end-file 回调执行异常: %s", exc)

    def close(self) -> None:
        """优雅关闭：注销回调并终止 mpv 实例。"""
        try:
            self._mpv.unregister_event_callback(self._on_mpv_event)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._mpv.terminate()
        except Exception as exc:  # noqa: BLE001
            log.debug("mpv terminate 已忽略: %s", exc)


# ===========================================================================
# PlayerController：Failover 状态机（与 mpv 解耦，可纯逻辑测试）
# ===========================================================================
class PlayerController:
    """
    频道播放 + 多备用源自动切换（failover）。

    用法：
        ctrl = PlayerController(on_play=..., on_exhausted=...)
        ctrl.play_channel(sources_list)      # 从最优源开始
        # MPVPlayer 事件回调把 end-file(reason_str) 转给 ctrl.notify_end(reason)
        ctrl.notify_end("error")             # 触发 failover 切下一个源

    参数 sources_list 元素可以是 Source ORM 对象（取 .url）或 str 地址，
    兼容两步：从 DB 取 频道.sources 直接传入，或测试传字符串列表。
    """

    def __init__(
        self,
        on_play: Optional[Callable] = None,            # 回调(url, index)
        on_exhausted: Optional[Callable] = None,       # 回调 → 备用源全部失败
        max_failovers: int = 2,
    ) -> None:
        self.on_play = on_play
        self.on_exhausted = on_exhausted
        self.max_failovers = int(max_failovers)

        self._urls: List[str] = []
        self._idx = 0
        self._attempts = 0
        self._manual_stop = False
        self._playing = False

    # -- 内部工具 -----------------------------------------------------------
    @staticmethod
    def _as_url(x) -> str:
        return x.url if hasattr(x, "url") else str(x)

    @staticmethod
    def _healthy(s) -> bool:
        return bool(getattr(s, "is_healthy", False)) if not isinstance(s, str) else True

    @staticmethod
    def _prio(s) -> int:
        return getattr(s, "default_priority", 0) if not isinstance(s, str) else 0

    @staticmethod
    def _enabled(s) -> bool:
        """禁用源(is_enabled=False)不参与播放；纯字符串/无该属性的对象视为启用。"""
        return bool(getattr(s, "is_enabled", True))

    def _sorted_urls(self, sources) -> List[str]:
        """先剔除禁用源，再健康源优先，其余按 default_priority 升序。"""
        usable = [s for s in sources if self._enabled(s)]
        return [self._as_url(s) for s in sorted(
            usable, key=lambda s: (not self._healthy(s), self._prio(s)))]

    # -- 对外接口 -----------------------------------------------------------
    def play_channel(self, sources) -> None:
        """开始播放一个频道：从最优源起播，重置 failover 计数。"""
        self._urls = self._sorted_urls(sources)
        self._idx = 0
        self._attempts = 0
        self._manual_stop = False
        if not self._urls:
            if self.on_exhausted:
                self.on_exhausted()   # 该频道无任何源
            return
        self._playing = True
        if self.on_play:
            self.on_play(self._urls[0], 0)

    def stop(self) -> None:
        """用户主动停止：标记 manual_stop，后续 end-file 不触发 failover。"""
        self._manual_stop = True
        self._playing = False

    def notify_end(self, reason) -> None:
        """mpv end-file 原因入口。'quit'/'stop'/'aborted' 忽略；'eof'/'error'/'redirect' 触发 failover。"""
        r = _as_str(reason).lower()
        if r in _MANUAL_REASONS:
            self._manual_stop = True
            self._playing = False
            return
        if self._manual_stop:
            return
        if r not in _FAILOVER_REASONS:
            return
        self._handle_fail()

    def _handle_fail(self) -> None:
        """当前源失败：尝试下一个备用源；耗尽则 on_exhausted。"""
        self._attempts += 1
        if self._idx + 1 >= len(self._urls) or self._attempts > self.max_failovers:
            self._playing = False
            log.warning("频道全部备用源失败，已停止播放（已尝试 %d 源）", self._attempts)
            if self.on_exhausted:
                self.on_exhausted()
            return
        self._idx += 1
        if self.on_play:
            self.on_play(self._urls[self._idx], self._idx)
        log.info("failover: 切到备用源[%d]: %s", self._idx, self._urls[self._idx][:60])

    # -- 状态读取 -----------------------------------------------------------
    @property
    def current_url(self) -> Optional[str]:
        return self._urls[self._idx] if self._urls else None

    @property
    def is_playing(self) -> bool:
        return self._playing
