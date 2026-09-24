# SPDX-License-Identifier: GPL-3.0-or-later
"""
播放链路验证脚本（合并自 verify_step3.py + verify_step4.py）。

验证内容：
  A. mpv 封装与 Failover 状态机（原 verify_step3，纯逻辑 + 真实 mpv 实例）
     1. 真实 mpv 实例封装：构造 MPVPlayer(vo=null, 无 GUI) 能加载 libmpv、
        设置音量、优雅 close —— 证明 python-mpv 封装可用，且不依赖 GUI/网络
     2. Failover 状态机（纯逻辑，不依赖 mpv）：
        play_channel(['a','b','c']) → 先播 a；notify_end(ERROR) → 切 b → 切 c；
        再 error → 备用全耗尽 → on_exhausted（播放停止）
     3. 主动停止不触发 failover：notify_end(STOP) 后不再切源
     4. 健康源优先排序：is_healthy=True 的源排在最前
  B. 异步健康检测（原 verify_step4，本地 HTTP 服务，零外网依赖）
     1. 隔离临时库（运行前把 cfg.DB_PATH 指到 tempdir）
     2. 本地 HTTP 服务器：/ok.m3u8 → 200+媒体字节；/dead.m3u8 → 404；
        已关闭端口 → 连接被拒
     3. 三个源 is_healthy 初始值故意错置，check_all 后双向正确写回，
        可用源测到 latency_ms 并更新 checked_at
     4. 断言报告：checked=3 healthy=1 unhealthy=2，avg_latency_ms 有值

说明：
  - 全程使用隔离临时库（每段开始前重建），不碰真实 data/liuhaitv.db。
  - 不联网、不起 GUI（mpv 用 vo=null）。

运行：
  <env>/python.exe scripts/verify_player.py

返回码：0=通过  1=存在失败项
"""
import http.server
import logging
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import liuhaitv  # noqa: E402 (确保 libmpv dll 已在 PATH)

# --- 隔离临时库：必须在 import liuhaitv.core.database 之前改写 DB_PATH ---
_tmpdir = tempfile.mkdtemp(prefix="liuhaitv_verify_player_")
import liuhaitv.config as cfg  # noqa: E402
cfg.DB_PATH = os.path.join(_tmpdir, "liuhaitv_test.db")

import liuhaitv.logger as pylog  # noqa: E402
pylog.setup_logging()
log = logging.getLogger("verify_player")

# 此 import 同时触发 database 按已改写的 cfg.DB_PATH 构建引擎
from liuhaitv.core import health  # noqa: E402
from liuhaitv.core.database import Base, engine, init_db, session_scope  # noqa: E402
from liuhaitv.core.models import Channel, Source  # noqa: E402
from liuhaitv.player.mpv_wrapper import MPVPlayer, PlayerController  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1


def reset_db():
    """清空临时库并重建，让各段互不干扰（段与段之间共享一个临时库）。"""
    Base.metadata.drop_all(engine)
    init_db()


# ===========================================================================
# A. 原 verify_step3：mpv 封装与 Failover
# ===========================================================================
def _check_real_mpv() -> bool:
    """A1：真实创建/操作/关闭一个 vo=null 的 mpv 实例（无 GUI）。"""
    log.info("----- 真实 mpv 封装（vo=null，无 GUI）-----")
    ok = True
    try:
        p = MPVPlayer(vo="null")
    except Exception as exc:  # noqa: BLE001
        log.error("  MPVPlayer 构造失败（libmpv 未加载？）: %s", exc)
        return False
    try:
        p.set_volume(55)
        # wid=0 + vo=null 下不真实播放，仅验证实例可操作
        ver = p.mpv_version
        idle = p.is_idle
        vol = p.volume
        log.info("  构造 OK | 内核=%s | is_idle=%s | volume=%s",
                 ver, idle, vol)
    except Exception as exc:  # noqa: BLE001
        log.error("  MPVPlayer 操作异常: %s", exc)
        ok = False
    finally:
        p.close()
    log.info("  真实 mpv 封装 -> %s", "PASS" if ok else "FAIL")
    return ok


def _check_failover() -> bool:
    """A2：Failover 状态机 —— 错误事件逐级切备用源，耗尽则停止。"""
    log.info("----- Failover 状态机 -----")
    played: list = []
    exhausted: list = []
    ctrl = PlayerController(
        on_play=lambda url, idx: played.append((url, idx)),
        on_exhausted=lambda: exhausted.append(1),
    )
    ctrl.play_channel(["a", "b", "c"])
    ok = played and played[0] == ("a", 0)
    log.info("  首播 a[0] -> %s", "OK" if ok else "FAIL")

    ctrl.notify_end("error")
    seq_ok = played == [("a", 0), ("b", 1)]
    log.info("  切到 b[1] -> %s (seq=%r)", "OK" if seq_ok else "FAIL", played)
    ok = ok and seq_ok

    ctrl.notify_end("error")
    seq_ok = played == [("a", 0), ("b", 1), ("c", 2)]
    log.info("  切到 c[2] -> %s (seq=%r)", "OK" if seq_ok else "FAIL", played)
    ok = ok and seq_ok

    ctrl.notify_end("error")
    exh_ok = len(exhausted) == 1 and ctrl.is_playing is False
    log.info("  备用全耗尽 -> on_exhausted、停止播放 -> %s",
             "OK" if exh_ok else "FAIL")
    ok = ok and exh_ok
    return ok


def _check_no_failover_on_stop() -> bool:
    """A3：主动 STOP 不触发 failover。"""
    log.info("----- 主动停止不切源 -----")
    played: list = []
    ctrl = PlayerController(on_play=lambda url, idx: played.append((url, idx)))
    ctrl.play_channel(["a", "b"])
    ctrl.stop()
    ctrl.notify_end("stop")
    ctrl.notify_end("error")   # 即便再来 error 也不应切（manual_stop）
    ok = played == [("a", 0)]
    log.info("  stop 后不切源 b -> %s (seq=%r)", "OK" if ok else "FAIL", played)
    return ok


def _check_healthy_first() -> bool:
    """A4：健康源优先排序。"""
    log.info("----- 健康源优先 -----")
    class FakeSource:
        def __init__(self, url, healthy, prio):
            self.url, self.is_healthy, self.default_priority = url, healthy, prio
    sources = [
        FakeSource("sick", False, 0),
        FakeSource("ok", True, 5),
        FakeSource("sick2", False, 1),
    ]
    order = PlayerController()._sorted_urls(sources)
    ok = order == ["ok", "sick", "sick2"]
    log.info("  健康源排最前 -> %s (order=%r)", "OK" if ok else "FAIL", order)
    return ok


# ===========================================================================
# B. 原 verify_step4：异步健康检测
# ===========================================================================
class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        if self.path.startswith("/ok"):
            body = (
                b"#EXTM3U\n#EXTINF:-1,\xe6\xb5\x8b\xe8\xaf\x95\n"
                b"http://127.0.0.1/ok.m3u8\n" * 8
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):  # 静音访问日志
        pass


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _start_server():
    srv = _TCPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def _closed_port() -> int:
    """拿一个确定没在监听的端口（bind 后立刻 close）。"""
    so = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    so.bind(("127.0.0.1", 0))
    port = so.getsockname()[1]
    so.close()
    return port


def _check_async_health() -> bool:
    """B：本地 HTTP 服务探测 3 个源（1 可用 + 2 不可用），核对写回与报告。"""
    reset_db()
    srv, port = _start_server()
    closed_port = _closed_port()

    ok_url = f"http://127.0.0.1:{port}/ok.m3u8"
    dead_url = f"http://127.0.0.1:{port}/dead.m3u8"
    refuse_url = f"http://127.0.0.1:{closed_port}/closed.m3u8"

    # 错置健康标记以验证双向写回
    with session_scope() as s:
        ch = Channel(name="异步检测频道", group_name="其他")
        s.add(ch)
        s.flush()
        cid = ch.id

        def mk(url, sid, prio, healthy):
            return Source(
                channel_id=cid, url=url, source_id=sid, origin="v4test",
                kind="hls", protocol="ipv4", default_priority=prio,
                is_healthy=healthy,
            )

        s.add(mk(ok_url, "v4:ok", 0, False))        # 实为可用，当前错置 False
        s.add(mk(dead_url, "v4:dead", 1, True))     # 实为404，当前错置 True
        s.add(mk(refuse_url, "v4:refuse", 2, True)) # 实为拒连，当前错置 True

    log.info("[verify_player] 探测 3 个源：1 可用 + 2 不可用（404 / 连接拒绝）")

    report = health.check_all(channel_id=cid)
    log.info("  报告: checked=%d healthy=%d unhealthy=%d avg_latency=%s failures=%d",
             report.checked, report.healthy, report.unhealthy,
             report.avg_latency_ms, len(report.failures))

    ok = True
    if (report.checked, report.healthy, report.unhealthy) != (3, 1, 2):
        log.error("  报告计数不符（期望 checked=3 healthy=1 unhealthy=2）")
        ok = False
    if report.avg_latency_ms is None or report.avg_latency_ms <= 0:
        log.error("  可用源未测得平均延迟 avg_latency_ms=%r", report.avg_latency_ms)
        ok = False

    with session_scope() as s:
        srcs = {so.url: so for so in s.query(Source).all()}

    labels = [
        ("可用源 ok 应置 True", srcs[ok_url].is_healthy is True),
        ("可用源应测到 latency(int>0)",
         isinstance(srcs[ok_url].latency_ms, int) and srcs[ok_url].latency_ms > 0),
        ("可用源 checked_at 应更新", srcs[ok_url].checked_at is not None),
        ("404 源应置 False", srcs[dead_url].is_healthy is False),
        ("连接拒绝源应置 False", srcs[refuse_url].is_healthy is False),
    ]
    for label, cond in labels:
        log.info("  %-28s %s", label, "OK" if cond else "FAIL")
        ok = ok and cond

    srv.shutdown()
    srv.server_close()
    return ok


def main() -> int:
    log.info("===== verify_player：播放器封装 / Failover / 异步健康检测 =====")
    checks = {
        "真实mpv封装(vo=null)": _check_real_mpv,
        "Failover状态机(逐级切源/耗尽停)": _check_failover,
        "主动停止不触发failover": _check_no_failover_on_stop,
        "健康源优先排序": _check_healthy_first,
        "异步健康检测(本地3源写回)": _check_async_health,
    }
    all_ok = True
    for name, fn in checks.items():
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001 - 单段异常不阻断其余检查
            log.exception("%s 执行异常: %s", name, exc)
            ok = False
        all_ok = all_ok and ok
        log.info("  %-28s %s", name, "OK" if ok else "FAIL")
    log.info("----- 汇总 -----")
    log.info("%s", "verify_player 全部通过 ✓" if all_ok else "存在失败项，请查看上方日志")

    try:
        engine.dispose()  # 释放 SQLite 连接句柄，便于删除临时目录
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(_tmpdir, ignore_errors=True)
    return EXIT_OK if all_ok else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
