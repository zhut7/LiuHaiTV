# SPDX-License-Identifier: GPL-3.0-or-later
"""
界面层验证脚本（合并自 verify_step5_main.py + verify_step5_click.py
+ verify_step5_sources.py + verify_step9_ui.py）。

验证内容（全部 offscreen / 隔离临时库，段与段之间各自重建库）：
  A. 主窗口分组与点击判定四态（原 verify_step5_main）
     1. 分组模型：分组头行与频道行并存、行数正确
     2. 初始全灰：未点击过的频道 status=unknown，不拿源的健康标记自动染色
     3. 四态流转：set_status() 置 checking(橙) / good(绿) / bad(红)，
        且只改这一行、不重建模型（rowCount 不变）
     4. 刷新不丢色：reload() 后会话内已判定的颜色仍保留
     5. 源列表刷新：update_sources() 后该行持有的源数量随之变化
     6. 可交互性 flags：分组头不可选、频道行可选
     7. 隐藏功能已移除（回归守卫）：带 is_visible=False 的频道照样出现在列表里，
        健康检测与 run_gui.load_channels() 也不再按该列过滤
  B. 点击才判定（绿 / 红 / 灰）（原 verify_step5_click）
     1. health.verify_channel()：有可用备用源 → ok=True（绿）+ ok_url + 延迟；
        全挂 → ok=False（红）+ note；无源 / 源全禁用 → note=「无启用中的源」；
        早退（checked <= total）；结果写回数据库
     2. UI 集成：初始全灰 → 点击变 checking → 判定完 good/bad；
        无源频道点击直接 bad；没点过的仍灰；源变更后回到灰
  C. 源管理弹窗增删改与优先级（原 verify_step5_sources）
     1. 表格加载与排序、行→Source.id 映射
     2. 上移 / 下移：DB 里 default_priority 按新顺序压紧为 0,1,2…
     3. 启用开关写入 is_enabled（禁用源不参与播放与检测）
     4. 删除：记录数减少且优先级重新压紧
     5. 添加：优先级=max+1，kind/protocol 由 URL 自动推断
     6. 编辑：改地址后 kind/protocol 跟着更新、旧的健康结论作废
     7. 导入 M3U：本地文件解析 + 按频道名自动勾选 + 去重
     8. 测试选中：探测结果写回 latency_ms / checked_at
     9. sourcesChanged 信号：每次改动都会通知主窗口刷新源列表
  D. UI 细节（原 verify_step9_ui）
     1. 源状态三态与配色（未检测灰 / 可播放绿 / 未通过红），
        失败源 latency=None 但已检测必须靠 checked_at 判成「未通过」
     2. 源管理表格渲染出三态（文字 + 颜色）
     3. 「⤒ 置顶」按钮：选中行 → 优先级 0，其余按原顺序顺延
     4. 音量默认最大、可持久化、下次启动沿用
     5. 「⟳ 刷新源」按钮：用当前频道现有源重新加载画面
     6. 右键只弹菜单、不换台（ChannelListView），左键仍能正常切换

说明：
  - 用 QT_QPA_PLATFORM=offscreen 跑，不弹真实窗口、不真实播放（vo=null / stub）；
  - 用隔离临时库，不污染真实 data/liuhaitv.db；
  - 真实点击/播放/颜色目视验证用 scripts/run_gui.py。

运行：
  <env>/python.exe scripts/verify_ui.py

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
import time
from datetime import datetime
from unittest import mock

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# offscreen 必须在任何 PySide6 import 之前设置
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import liuhaitv  # noqa: E402 (确保 libmpv dll 目录 prepend)

# --- 隔离临时库：必须在 import liuhaitv.core.database 之前改写 DB_PATH ---
_tmpdir = tempfile.mkdtemp(prefix="liuhaitv_verify_ui_")
import liuhaitv.config as cfg  # noqa: E402
cfg.DB_PATH = os.path.join(_tmpdir, "liuhaitv_test.db")

import liuhaitv.logger as pylog  # noqa: E402
pylog.setup_logging()
log = logging.getLogger("verify_ui")

from PySide6.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from liuhaitv.core import health  # noqa: E402
from liuhaitv.core import settings as prefs  # noqa: E402
from liuhaitv.core.database import Base, engine, init_db, session_scope  # noqa: E402
from liuhaitv.core.models import Channel, Setting, Source  # noqa: E402
from liuhaitv.ui import source_manager as sm  # noqa: E402
from liuhaitv.ui.channel_list_model import (  # noqa: E402
    STATUS_BAD, STATUS_CHECKING, STATUS_GOOD, STATUS_UNKNOWN, ChannelListView,
    ChannelListModel, RowType,
)
from liuhaitv.ui.main_window import MainWindow  # noqa: E402
from liuhaitv.ui.source_manager import (  # noqa: E402
    M3UImportDialog, SourceManagerDialog, infer_kind, infer_protocol,
    status_color, status_text,
)

EXIT_OK = 0
EXIT_FAIL = 1
WAIT_MS = 30000

# 状态三态配色（与 ui/source_manager.py 保持一致）
GRAY = (150, 152, 158)
GREEN = (0, 150, 50)
RED = (210, 40, 40)

# 断言收集：所有分段共用同一个 check()，最后统一汇总
_FAILURES: list = []


def check(label, cond, extra=""):
    log.info("  %-52s %s %s", label, "OK" if cond else "FAIL", extra)
    if not cond:
        _FAILURES.append(label)


def reset_db():
    """清空临时库并重建，让各段互不干扰（段与段之间共享一个临时库）。"""
    Base.metadata.drop_all(engine)
    init_db()


# ===========================================================================
# 公共脚手架（本地 HTTP 服务 / 造数据 / 取行号）
# ===========================================================================
class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        if self.path.startswith("/ok"):
            body = (b"#EXTM3U\n#EXTINF:-1,test\n"
                    b"http://127.0.0.1/seg1.ts\n" * 12)
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
    """起一个本地 HTTP 服务，返回 (server, port)；/ok* 返回 200，其余 404。"""
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


def _load_channels():
    with session_scope() as s:
        return (s.query(Channel).options(selectinload(Channel.sources))
                .order_by(Channel.group_name, Channel.sort_order).all())


def _row_of(model, name):
    for i in range(model.rowCount()):
        it = model.item_at(i)
        if it.row_type == RowType.CHANNEL and it.text == name:
            return i
    return -1


def _rgb(brush):
    return tuple(brush.color().getRgb()[:3])


def _load_run_gui():
    """把 scripts/run_gui.py 当模块加载（先注册 sys.modules，否则 dataclass 解析会失败）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "liuhaitv_run_gui", os.path.join(_PROJECT_ROOT, "scripts", "run_gui.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# A. 原 verify_step5_main：主窗口分组 + 点击判定四态
# ===========================================================================
def _seed_list_model():
    reset_db()
    with session_scope() as s:
        c1 = Channel(name="CCTV-1 综合", group_name="中央")
        c2 = Channel(name="湖南卫视", group_name="卫视")
        c3 = Channel(name="某空白台", group_name="地方")
        # c4：带 is_visible=False（历史遗留的"已隐藏"标记）。
        # 隐藏功能已移除 —— 它必须和其他频道一样正常出现在列表与健康检测里。
        c4 = Channel(name="某曾隐藏台", group_name="卫视", is_visible=False)
        s.add_all([c1, c2, c3, c4])
        s.flush()
        s.add(Source(channel_id=c1.id, url="http://ok/a.m3u8", source_id="v5:ok:1",
                     origin="v5", kind="hls", protocol="domain", default_priority=0,
                     is_healthy=True))
        s.add(Source(channel_id=c2.id, url="http://bad/a.m3u8", source_id="v5:bad:1",
                     origin="v5", kind="hls", protocol="domain", default_priority=0,
                     is_healthy=False))
        s.add(Source(channel_id=c4.id, url="http://ok/hidden.m3u8", source_id="v5:hid:1",
                     origin="v5", kind="hls", protocol="domain", default_priority=0,
                     is_healthy=False))
        # c3 无任何源
        return c1.id, c2.id, c3.id, c4.id


def _check_main_list_model():
    """A：分组模型 + 点击判定四态（原 verify_step5_main 的全部断言）。"""
    log.info("===== A. 主窗口(左列表) 验证：分组 + 点击判定四态 =====")
    c1_id, c2_id, c3_id, c4_id = _seed_list_model()

    model = ChannelListModel()
    model.reload(_load_channels())

    # 1 分组头 + 频道行
    grp_rows = [i for i in range(model.rowCount())
                if model.data(model.index(i, 0), model.RowTypeRole) == RowType.GROUP.value]
    chan_rows = [i for i in range(model.rowCount())
                 if model.data(model.index(i, 0), model.RowTypeRole) == RowType.CHANNEL.value]
    log.info("  行分布: 总=%d 组头=%d 频道=%d", model.rowCount(), len(grp_rows), len(chan_rows))
    check("含分组头且频道数=4", len(grp_rows) >= 3 and len(chan_rows) == 4)

    i1 = _row_of(model, "CCTV-1 综合")
    i2 = _row_of(model, "湖南卫视")
    i3 = _row_of(model, "某空白台")

    # 2 初始全灰（不看源的 is_healthy）
    st1 = model.data(model.index(i1, 0), model.StatusRole)
    st2 = model.data(model.index(i2, 0), model.StatusRole)
    check("有健康源的频道初始也是灰(未判定)", i1 >= 0 and st1 == STATUS_UNKNOWN)
    check("不健康源的频道初始也是灰(未判定)", i2 >= 0 and st2 == STATUS_UNKNOWN)
    check("status_of() 与 data(StatusRole) 一致",
          model.status_of(c1_id) == st1 and model.status_of(c2_id) == st2)

    # 3 四态流转（就地更新、不重建）
    before = model.rowCount()
    model.set_status(c1_id, STATUS_CHECKING)
    check("点击瞬间 -> checking(橙)",
          model.data(model.index(i1, 0), model.StatusRole) == STATUS_CHECKING)
    model.set_status(c1_id, STATUS_GOOD)
    check("判定可播 -> good(绿)",
          model.data(model.index(i1, 0), model.StatusRole) == STATUS_GOOD)
    model.set_status(c2_id, STATUS_BAD)
    check("判定不可播 -> bad(红)",
          model.data(model.index(i2, 0), model.StatusRole) == STATUS_BAD)
    check("set_status 未重建模型(rowCount 不变)", model.rowCount() == before)

    # 4 reload 保留会话内颜色
    model.reload(_load_channels())
    check("reload 后保留绿(good)", model.status_of(c1_id) == STATUS_GOOD)
    check("reload 后保留红(bad)", model.status_of(c2_id) == STATUS_BAD)
    check("reload 后未判定的仍为灰", model.status_of(c3_id) == STATUS_UNKNOWN)

    # 5 源列表刷新（源管理弹窗保存后走这条路）
    with session_scope() as s:
        ch = s.get(Channel, c3_id)
        s.add(Source(channel_id=ch.id, url="http://new/x.m3u8", source_id="v5:new:1",
                     origin="user", kind="hls", protocol="domain", default_priority=0))
    with session_scope() as s:
        ch = s.get(Channel, c3_id)
        newsrc = list(ch.sources)
    item_before = len(model.item_at(_row_of(model, "某空白台")).sources)
    model.update_sources(c3_id, newsrc)
    item_after = len(model.item_at(_row_of(model, "某空白台")).sources)
    check("update_sources 生效(0 -> 1)", item_before == 0 and item_after == 1)

    # reset_statuses 回到全灰
    model.reset_statuses()
    check("reset_statuses 全部回到灰",
          model.status_of(c1_id) == STATUS_UNKNOWN and model.status_of(c2_id) == STATUS_UNKNOWN)

    # 6 flags
    check("分组头不可选", not (model.flags(model.index(grp_rows[0], 0))
                            & Qt.ItemFlag.ItemIsSelectable))
    check("频道行可选", bool(model.flags(model.index(i1, 0))
                           & Qt.ItemFlag.ItemIsSelectable))

    # 7 隐藏功能已移除（回归守卫：以后别再把 is_visible 过滤加回来）
    log.info("-- 7. 隐藏功能已移除（回归）--")
    check("is_visible=False 的频道仍出现在列表",
          _row_of(model, "某曾隐藏台") >= 0)

    from liuhaitv.core.health import _iter_plans  # noqa: E402
    with session_scope() as s:
        plans = _iter_plans(s)
    check("健康检测不再跳过 is_visible=False 的源",
          any(url == "http://ok/hidden.m3u8" for _sid, url in plans))

    run_gui = _load_run_gui()
    gui_names = {c.name for c in run_gui.load_channels()}
    check("run_gui.load_channels() 不过滤 is_visible",
          "某曾隐藏台" in gui_names, sorted(gui_names))


# ===========================================================================
# B. 原 verify_step5_click：点击才判定（绿 / 红 / 灰）
# ===========================================================================
def _seed_click(ok_url: str, dead_url: str, refuse_url: str):
    reset_db()
    with session_scope() as s:
        ch_ok = Channel(name="有备用源的频道", group_name="中央")
        ch_bad = Channel(name="全挂的频道", group_name="卫视")
        ch_none = Channel(name="没有任何源的频道", group_name="地方")
        ch_disabled = Channel(name="源被禁用的频道", group_name="其他")
        s.add_all([ch_ok, ch_bad, ch_none, ch_disabled])
        s.flush()

        def mk(cid, url, sid, prio, enabled=True):
            return Source(channel_id=cid, url=url, source_id=sid, origin="v5click",
                          kind="hls", protocol="ipv4", default_priority=prio,
                          is_enabled=enabled)

        # A: 首选源连不上，备用源可用 -> 应判"可播"
        s.add(mk(ch_ok.id, refuse_url, "c5:refuse", 0))
        s.add(mk(ch_ok.id, ok_url, "c5:ok", 1))
        # B: 404 + 拒连 -> 应判"不可播"
        s.add(mk(ch_bad.id, dead_url, "c5:dead", 0))
        s.add(mk(ch_bad.id, refuse_url, "c5:refuse2", 1))
        # C: 无源
        # D: 唯一源被禁用
        s.add(mk(ch_disabled.id, ok_url, "c5:disabled", 0, enabled=False))
        return ch_ok.id, ch_bad.id, ch_none.id, ch_disabled.id


def _wait_check(win, row, model) -> tuple:
    """点击某行并等后台判定完成；返回 (channel_id, ok, note) 或 None（未发起探测）。"""
    item = model.item_at(row)
    if not item.sources:
        win.list.setCurrentIndex(model.index(row, 0))
        return None

    loop = QEventLoop()
    got = {}

    def on_fin(cid, ok, note):
        got["r"] = (cid, ok, note)
        loop.quit()

    win.checker.finished.connect(on_fin)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(WAIT_MS)
    try:
        win.list.setCurrentIndex(model.index(row, 0))
        loop.exec()
    finally:
        timer.stop()
        try:
            win.checker.finished.disconnect(on_fin)
        except Exception:  # noqa: BLE001
            pass
    return got.get("r")


def _check_click_verdict():
    """B：后端 verify_channel + MainWindow 点击判定（原 verify_step5_click 的全部断言）。"""
    log.info("===== B. 点击才判定（绿/红/灰）=====")

    srv, port = _start_server()
    closed = _closed_port()
    ok_url = f"http://127.0.0.1:{port}/ok.m3u8"
    dead_url = f"http://127.0.0.1:{port}/dead.m3u8"
    refuse_url = f"http://127.0.0.1:{closed}/closed.m3u8"

    try:
        c_ok, c_bad, c_none, c_dis = _seed_click(ok_url, dead_url, refuse_url)

        # ---- A. 后端：verify_channel ----
        log.info("-- A. health.verify_channel() --")
        v_ok = health.verify_channel(c_ok)
        check("有可用备用源 -> 判定可播(绿)", v_ok.ok is True)
        check("给出可播源地址(优先取可用源)", v_ok.ok_url == ok_url)
        check("测到延迟(>0)", isinstance(v_ok.latency_ms, int) and v_ok.latency_ms > 0)
        check("早退(已测源数 <= 源总数)", 1 <= v_ok.checked <= v_ok.total)

        v_bad = health.verify_channel(c_bad)
        check("源全挂 -> 判定不可播(红)", v_bad.ok is False)
        check("不可播时给出原因 note", bool(v_bad.note))

        v_none = health.verify_channel(c_none)
        check("无源 -> 不可播且提示无启用源",
              v_none.ok is False and "无启用中的源" in v_none.note)

        v_dis = health.verify_channel(c_dis)
        check("唯一源被禁用 -> 不参与检测，判不可播",
              v_dis.ok is False and "无启用中的源" in v_dis.note)

        with session_scope() as s:
            ok_src = s.query(Source).filter(Source.source_id == "c5:ok").one()
            refuse_src = s.query(Source).filter(Source.source_id == "c5:refuse").one()
            check("可用源写回 is_healthy=True 且 checked_at 已更新",
                  ok_src.is_healthy is True and ok_src.checked_at is not None)
            check("长延迟/失败源写回 latency_ms",
                  refuse_src.latency_ms is None or isinstance(refuse_src.latency_ms, int))

        # ---- B. UI 集成 ----
        log.info("-- B. MainWindow 点击判定（offscreen，不真播）--")
        model = ChannelListModel()
        model.reload(_load_channels())
        win = MainWindow(model, vo="null")
        # 不真实拉流：只验证"点击 -> 判定 -> 着色"链路（播放本身由 verify_player 覆盖）
        win.player.play = lambda url: log.info("  [stub] 跳过真实播放 %s", url[:50])
        win.controller.play_channel = lambda sources: log.info(
            "  [stub] 跳过 failover 起播（%d 个源）", len(sources))

        r_ok = _row_of(model, "有备用源的频道")
        r_bad = _row_of(model, "全挂的频道")
        r_none = _row_of(model, "没有任何源的频道")

        check("初始：有源频道为灰(unknown)",
              model.status_of(c_ok) == STATUS_UNKNOWN)
        check("初始：无源频道为灰(unknown)",
              model.status_of(c_none) == STATUS_UNKNOWN)

        res_ok = _wait_check(win, r_ok, model)
        check("点击后收到后台判定结果", res_ok is not None and res_ok[0] == c_ok)
        check("点击有可用源的频道 -> 绿色(good)",
              model.status_of(c_ok) == STATUS_GOOD)
        with session_scope() as s:
            ch = s.get(Channel, c_ok)
            check("判定结果落库 last_status='good'", ch.last_status == "good")

        res_bad = _wait_check(win, r_bad, model)
        check("点击全挂频道 -> 红色(bad)",
              res_bad is not None and model.status_of(c_bad) == STATUS_BAD)

        # 无源频道：直接判红，不发起探测
        _wait_check(win, r_none, model)
        check("点击无源频道 -> 红色(bad)", model.status_of(c_none) == STATUS_BAD)
        check("没点过的频道仍为灰(unknown)",
              model.status_of(c_dis) == STATUS_UNKNOWN)

        # 源管理改动 -> 颜色回到灰（可再点一次重新判定）
        win._on_sources_changed(c_ok)
        check("源变更后该频道回到灰(需重新点击判定)",
              model.status_of(c_ok) == STATUS_UNKNOWN)

        try:
            win.player.close()
        except Exception:  # noqa: BLE001
            pass
        srv.shutdown()
        srv.server_close()
    except Exception as exc:  # noqa: BLE001 - 本段整体兜底
        log.exception("验证执行异常: %s", exc)
        _FAILURES.append(f"异常: {exc}")
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# C. 原 verify_step5_sources：直播源管理弹窗（增删改 + 优先级）
# ===========================================================================
def _prios(cid) -> list:
    with session_scope() as s:
        return [(x.url, x.default_priority, bool(x.is_enabled))
                for x in sorted(s.get(Channel, cid).sources,
                                key=lambda y: (y.default_priority, y.id))]


def _check_source_manager():
    """C：源管理弹窗增删改 + 优先级（原 verify_step5_sources 的全部断言）。"""
    log.info("===== C. 直播源管理弹窗（增删改 + 优先级）=====")

    srv, port = _start_server()
    ok_url = f"http://127.0.0.1:{port}/ok.m3u8"

    try:
        # ---- 0. 造数据 ----
        reset_db()
        with session_scope() as s:
            ch = Channel(name="CCTV-1 综合", group_name="中央")
            s.add(ch)
            s.flush()
            cid = ch.id
            s.add_all([
                Source(channel_id=cid, url="http://a/1.m3u8", source_id="s1",
                       origin="t", kind="hls", protocol="domain", default_priority=0),
                Source(channel_id=cid, url="http://b/2.flv", source_id="s2",
                       origin="t", kind="flv", protocol="domain", default_priority=1),
                Source(channel_id=cid, url="http://c/3.ts", source_id="s3",
                       origin="t", kind="ts", protocol="domain", default_priority=2),
            ])
        check("URL 类型推断(m3u8->hls / flv / ts)",
              infer_kind("http://x/a.m3u8") == "hls"
              and infer_kind("http://x/a.flv") == "flv"
              and infer_kind("http://x/a.ts") == "ts")
        check("协议推断(域名 / IPv4 / IPv6)",
              infer_protocol("http://example.com/a.m3u8") == "domain"
              and infer_protocol("http://1.2.3.4/a.m3u8") == "ipv4"
              and infer_protocol("http://[2409::1]/a.m3u8") == "ipv6")

        # ---- 1. 加载 ----
        dlg = SourceManagerDialog(cid)
        check("表格加载 3 行", dlg.tbl.rowCount() == 3 and len(dlg._rows) == 3)
        check("标题含频道名", "CCTV-1 综合" in dlg.windowTitle())
        check("按优先级排序（首个是 prio0 的 a/1.m3u8）",
              _prios(cid)[0][0] == "http://a/1.m3u8")

        # ---- 2. 优先级：下移第一行 ----
        dlg.tbl.selectRow(0)
        dlg._move(1)
        order = [u for u, _p, _e in _prios(cid)]
        check("下移：顺序变为 b,a,c", order == ["http://b/2.flv", "http://a/1.m3u8",
                                              "http://c/3.ts"])
        check("优先级压紧为 0,1,2",
              [p for _u, p, _e in _prios(cid)] == [0, 1, 2])
        dlg.tbl.selectRow(1)
        dlg._move(-1)
        check("上移：顺序复原 a,b,c",
              [u for u, _p, _e in _prios(cid)] ==
              ["http://a/1.m3u8", "http://b/2.flv", "http://c/3.ts"])

        # ---- 3. 启用开关（走真实 itemChanged 信号）----
        dlg.tbl.item(1, sm._COL_ENABLE).setCheckState(sm.Qt.CheckState.Unchecked)
        QApplication.processEvents()
        check("去勾 -> is_enabled=False",
              _prios(cid)[1][2] is False)
        dlg.tbl.item(1, sm._COL_ENABLE).setCheckState(sm.Qt.CheckState.Checked)
        QApplication.processEvents()
        check("重新勾选 -> is_enabled=True", _prios(cid)[1][2] is True)

        # ---- 4. 删除 ----
        dlg.tbl.selectRow(2)   # 删 c/3.ts
        with mock.patch.object(QMessageBox, "question",
                               staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)):
            dlg._delete()
        check("删除后剩 2 条", len(_prios(cid)) == 2)
        check("删除后优先级压紧为 0,1",
              [p for _u, p, _e in _prios(cid)] == [0, 1])

        # ---- 5. 添加 ----
        with mock.patch.object(sm.SourceEditDialog, "exec",
                               lambda self: QDialog.DialogCode.Accepted), \
             mock.patch.object(sm.SourceEditDialog, "values",
                               lambda self: ("https://cdn.example.com/new/4.m3u8",
                                             "user", True)):
            dlg._add()
        rows = _prios(cid)
        check("新增 1 条且优先级=max+1=2",
              len(rows) == 3 and rows[2][0].endswith("4.m3u8") and rows[2][1] == 2)
        with session_scope() as s:
            new_src = (s.query(Source)
                       .filter(Source.url == "https://cdn.example.com/new/4.m3u8").one())
            check("新增源的 kind/protocol 自动推断",
                  new_src.kind == "hls" and new_src.protocol == "domain")
            check("新增源默认启用", new_src.is_enabled is True)

        # ---- 6. 编辑（改地址 -> 旧结论作废）----
        with session_scope() as s:
            src = (s.query(Source)
                   .filter(Source.url == "https://cdn.example.com/new/4.m3u8").one())
            src.is_healthy = True
            src.latency_ms = 123
            sid_new = src.id
        dlg._load()
        dlg._select_source(sid_new)
        with mock.patch.object(sm.SourceEditDialog, "exec",
                               lambda self: QDialog.DialogCode.Accepted), \
             mock.patch.object(sm.SourceEditDialog, "values",
                               lambda self: ("http://other/xx.flv", "manual", True)):
            dlg._edit()
        with session_scope() as s:
            src = s.get(Source, sid_new)
            check("编辑后 url/origin 已更新",
                  src.url == "http://other/xx.flv" and src.origin == "manual")
            check("改地址后 kind 重新推断为 flv", src.kind == "flv")
            check("改地址后旧健康结论作废(is_healthy=False, latency=None)",
                  src.is_healthy is False and src.latency_ms is None)

        # ---- 7. 导入 M3U（本地文件）----
        m3u_path = os.path.join(_tmpdir, "src.m3u")
        with open(m3u_path, "w", encoding="utf-8") as fh:
            fh.write(
                "#EXTM3U\n"
                '#EXTINF:-1 tvg-logo="http://l/1.png" group-title="央视",CCTV-1 综合\n'
                "http://imp/1.m3u8\n"
                "http://imp/1b.m3u8\n"
                '#EXTINF:-1 group-title="卫视",湖南卫视 HD\n'
                "http://imp/hunan.m3u8\n"
                '#EXTINF:-1 group-title="央视",CCTV-1 综合\n'
                "http://a/1.m3u8\n"          # 与库中已有地址重复，应被跳过
            )
        imp = M3UImportDialog("CCTV-1 综合")
        imp.ed_src.setText(m3u_path)
        imp._load()
        check("M3U 解析出 3 个条目", len(imp._entries) == 3)
        check("按频道名自动勾选同名条目（2 个）",
              imp.listw.count() == 2 and len(imp.selected_urls()) == 3)
        imp.ck_only_match.setChecked(False)
        check("关掉过滤后显示全部 3 个条目", imp.listw.count() == 3)

        with session_scope() as s:
            before = s.query(Source).filter(Source.channel_id == cid).count()
        with mock.patch.object(sm.M3UImportDialog, "exec",
                               lambda self: QDialog.DialogCode.Accepted), \
             mock.patch.object(sm.M3UImportDialog, "selected_urls",
                               lambda self: ["http://imp/1.m3u8", "http://imp/1b.m3u8",
                                             "http://imp/hunan.m3u8", "http://a/1.m3u8"]):
            with mock.patch.object(QMessageBox, "information",
                                   staticmethod(lambda *a, **k: None)):
                dlg._import_m3u()
        with session_scope() as s:
            after = s.query(Source).filter(Source.channel_id == cid).count()
        check("导入新增 3 条、重复地址被跳过（共 +3）", after == before + 3)
        with session_scope() as s:
            imported = (s.query(Source)
                        .filter(Source.channel_id == cid, Source.origin == "user-import")
                        .all())
            check("导入源 origin=user-import 且默认启用",
                  len(imported) == 3 and all(x.is_enabled for x in imported))

        # ---- 8. 测试选中（真实探测本地服务）----
        with session_scope() as s:
            probe_src = Source(channel_id=cid, url=ok_url, source_id="probe1",
                               origin="t", kind="hls", protocol="ipv4",
                               default_priority=99)
            s.add(probe_src)
            s.flush()
            probe_sid = probe_src.id
        dlg._load()
        dlg._select_source(probe_sid)
        dlg._probe_selected()
        # 等探测线程把结果写回（轮询 DB，最多 20s）
        deadline = time.time() + 20
        while time.time() < deadline:
            QApplication.processEvents()
            with session_scope() as s:
                got = s.get(Source, probe_sid)
                if got.latency_ms is not None:
                    break
            time.sleep(0.2)
        with session_scope() as s:
            got = s.get(Source, probe_sid)
            check("测试选中：写回 is_healthy=True + latency + checked_at",
                  got.is_healthy is True and isinstance(got.latency_ms, int)
                  and got.checked_at is not None)

        # ---- 9. 信号 ----
        got_signal = []
        dlg.sourcesChanged.connect(lambda x: got_signal.append(x))
        dlg.tbl.selectRow(0)
        with mock.patch.object(QMessageBox, "question",
                               staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)):
            dlg._delete()
        check("删除后 emit sourcesChanged(channel_id)",
              got_signal == [cid])

        dlg.close()
        srv.shutdown()
        srv.server_close()
    except Exception as exc:  # noqa: BLE001
        log.exception("验证执行异常: %s", exc)
        _FAILURES.append(f"异常: {exc}")
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# D. 原 verify_step9_ui：UI 细节（三态配色 / 置顶 / 音量 / 刷新源 / 右键）
# ===========================================================================
def _seed_ui_details():
    """造：一个有 3 个源（三态各一）的频道 + 一个无源频道。"""
    reset_db()
    now = datetime.now()
    with session_scope() as s:
        c1 = Channel(name="测试台", group_name="中央")
        c2 = Channel(name="空源台", group_name="卫视")
        s.add_all([c1, c2])
        s.flush()
        # 可播放（绿）
        s.add(Source(channel_id=c1.id, url="http://x/good.m3u8", source_id="v9:1",
                     origin="t", kind="hls", protocol="domain", default_priority=0,
                     is_healthy=True, is_enabled=True, is_user_edited=False,
                     latency_ms=48, checked_at=now))
        # 未通过（红）：**已检测但延迟为 None** —— 旧实现会把它显示成"未检测"
        s.add(Source(channel_id=c1.id, url="http://x/dead.m3u8", source_id="v9:2",
                     origin="t", kind="hls", protocol="domain", default_priority=1,
                     is_healthy=False, is_enabled=True, is_user_edited=False,
                     latency_ms=None, checked_at=now))
        # 未检测（灰）
        s.add(Source(channel_id=c1.id, url="http://x/new.m3u8", source_id="v9:3",
                     origin="t", kind="hls", protocol="domain", default_priority=2,
                     is_healthy=False, is_enabled=True, is_user_edited=False,
                     latency_ms=None, checked_at=None))
        return c1.id, c2.id


def _src_state(cid):
    """取某频道源的状态，按优先级排序：[(url, priority), ...]"""
    with session_scope() as s:
        ch = s.get(Channel, cid)
        return [(x.url, x.default_priority)
                for x in sorted(ch.sources, key=lambda y: (y.default_priority, y.id))]


def _check_ui_details():
    """D：状态三态 / 置顶 / 音量 / 刷新源 / 右键不换台（原 verify_step9_ui 的全部断言）。"""
    log.info("===== D. UI 细节（三态配色 / 置顶 / 音量 / 刷新源 / 右键）=====")

    # ---- A. 状态三态 ----
    log.info("-- D1. 源状态三态与配色 --")
    now = datetime.now()
    cases = [
        (None, False, "未检测", GRAY),
        (None, True, "未检测", GRAY),
        (now, True, "可播放", GREEN),
        (now, False, "未通过", RED),
    ]
    for checked, healthy, want_text, want_color in cases:
        got_t = status_text(checked, healthy)
        got_c = _rgb(status_color(checked, healthy))
        check("checked=%-5s healthy=%-5s -> %s" % (
            "有" if checked else "无", healthy, want_text),
            got_t == want_text and got_c == want_color,
            "实得 %r %s" % (got_t, got_c))
    check("失败源(latency=None 但已检测)显示「未通过」而不是「未检测」",
          status_text(now, False) == "未通过")

    # ---- B. 表格渲染 ----
    log.info("-- D2. 源管理表格渲染三态 --")
    cid, empty_cid = _seed_ui_details()
    dlg = SourceManagerDialog(cid)
    check("表格 3 行", dlg.tbl.rowCount() == 3, dlg.tbl.rowCount())
    texts = [dlg.tbl.item(r, 7).text() for r in range(dlg.tbl.rowCount())]
    colors = [_rgb(dlg.tbl.item(r, 7).foreground()) for r in range(dlg.tbl.rowCount())]
    check("状态列文字 = [可播放, 未通过, 未检测]",
          texts == ["可播放", "未通过", "未检测"], texts)
    check("状态列颜色 = [绿, 红, 灰]",
          colors == [GREEN, RED, GRAY], colors)
    check("存在「置顶」按钮",
          any("置顶" in b.text() for b in dlg.findChildren(QPushButton)),
          [b.text() for b in dlg.findChildren(QPushButton)])

    # ---- C. 置顶 ----
    log.info("-- D3. 置顶按钮 --")
    emitted = []
    dlg.sourcesChanged.connect(lambda cid_: emitted.append(cid_))
    before = _src_state(cid)
    dlg.tbl.selectRow(2)                      # 选中最下面那条（优先级 2 的 new.m3u8）
    dlg._move_top()
    after = _src_state(cid)
    check("被选中的源置顶为优先级 0",
          after[0][0].endswith("/new.m3u8") and after[0][1] == 0, after)
    check("其余源按原顺序顺延（0->1, 1->2）",
          [u for u, _ in after] == [before[2][0], before[0][0], before[1][0]], after)
    check("置顶后优先级压紧为 0,1,2",
          [p for _, p in after] == [0, 1, 2], [p for _, p in after])
    check("置顶后表格首行就是它",
          dlg.tbl.item(0, 2).text().endswith("/new.m3u8"), dlg.tbl.item(0, 2).text())
    check("置顶后 emit sourcesChanged", emitted == [cid], emitted)
    dlg.deleteLater()

    # ---- D. 音量 ----
    log.info("-- D4. 音量默认最大 + 记忆 --")
    with session_scope() as s:                # 清掉历史值，验证"默认最大"
        row = s.get(Setting, prefs.KEY_VOLUME)
        if row is not None:
            s.delete(row)
    check("无历史值时默认音量 = 最大(100)", prefs.get_volume() == 100, prefs.get_volume())
    prefs.set_volume(42)
    check("写入后可读回(42)", prefs.get_volume() == 42, prefs.get_volume())
    prefs.set_volume(300)
    check("超范围夹到 100", prefs.get_volume() == 100, prefs.get_volume())
    prefs.set_volume(-7)
    check("负值夹到 0", prefs.get_volume() == 0, prefs.get_volume())

    prefs.set_volume(55)                      # 模拟"上次退出时是 55"
    model = ChannelListModel()
    with session_scope() as s:
        chans = (s.query(Channel).options(selectinload(Channel.sources)).all())
    model.reload(chans)
    win = MainWindow(model, vo="null")
    check("启动时沿用上次音量(55)", win.volume_slider.value() == 55, win.volume_slider.value())

    win.volume_slider.setValue(23)            # 用户拖动
    win._persist_volume()                     # 定时器到期后做的事，这里直接触发
    check("拖动后音量写回设置(23)", prefs.get_volume() == 23, prefs.get_volume())

    # ---- E. 刷新源 ----
    log.info("-- D5.「⟳ 刷新源」按当前源重新加载画面 --")
    btn_texts = [b.text() for b in win.control_bar.findChildren(QPushButton)]
    check("控制条含「⟳ 刷新源」", any("刷新源" in t for t in btn_texts), btn_texts)

    played, stopped, checked = [], [], []
    win.controller.play_channel = lambda srcs: played.append([x.url for x in srcs])
    win.controller.stop = lambda: stopped.append(True)
    win.player.play = lambda url: None
    win.checker.check = lambda cid_: checked.append(cid_) or True

    row = model.row_of_channel(cid)
    win.list.setCurrentIndex(model.index(row, 0))     # 模拟点击（先走一次正常播放）
    played.clear(); stopped.clear(); checked.clear()

    win._reload_current()                             # 点「⟳ 刷新源」
    check("先停掉旧流", len(stopped) == 1, len(stopped))
    check("用该频道现有 3 个源重新起播",
          len(played) == 1 and len(played[0]) == 3, played)
    check("重新发起可用性判定", checked == [cid], checked)
    check("按钮给出反馈文案", "已重新加载" in win.lbl_channel.text(), win.lbl_channel.text())

    # 无源频道
    played.clear(); stopped.clear()
    row2 = model.row_of_channel(empty_cid)
    win.list.setCurrentIndex(model.index(row2, 0))
    played.clear(); stopped.clear(); checked.clear()
    win._reload_current()
    check("无源频道刷新时不发起播放", not played, played)
    check("无源频道提示去添加源", "无源" in win.lbl_channel.text(), win.lbl_channel.text())

    # ---- F. 右键不换台 ----
    log.info("-- D6. 右键只弹菜单、不切换频道 --")

    check("列表用的是 ChannelListView", isinstance(win.list, ChannelListView),
          type(win.list).__name__)
    check("右键菜单策略为 CustomContextMenu",
          win.list.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu)

    played.clear()
    r_a = model.row_of_channel(cid)          # 测试台（有 3 个源）
    r_b = model.row_of_channel(empty_cid)    # 空源台
    win.list.setCurrentIndex(model.index(r_a, 0))   # 先"正在播放"测试台
    cur_after_left = win.list.currentIndex().row()
    played.clear()

    def _mouse(kind, row, button):
        rect = win.list.visualRect(model.index(row, 0))
        pt = QPointF(rect.center())
        ev = QMouseEvent(kind, pt, win.list.viewport().mapToGlobal(rect.center()),
                         button, button, Qt.KeyboardModifier.NoModifier)
        return ev

    # 右键按在另一行上：currentIndex 不应变，也不该起播
    win.list.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, r_b,
                                    Qt.MouseButton.RightButton))
    win.list.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, r_b,
                                      Qt.MouseButton.RightButton))
    check("右键后 currentIndex 仍是原来那个频道",
          win.list.currentIndex().row() == cur_after_left,
          "now=%s want=%s" % (win.list.currentIndex().row(), cur_after_left))
    check("右键没有触发切台播放", not played, played)
    check("右键没有改 _current_channel_id",
          win._current_channel_id == cid, win._current_channel_id)

    # 左键仍应正常切台（别把正常点击也吞了）
    win.list.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, r_b,
                                    Qt.MouseButton.LeftButton))
    win.list.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, r_b,
                                      Qt.MouseButton.LeftButton))
    check("左键仍能正常切换频道",
          win.list.currentIndex().row() == r_b, win.list.currentIndex().row())

    win.deleteLater()


def main() -> int:
    log.info("===== verify_ui：主窗口 / 点击判定 / 源管理弹窗 / UI 细节 =====")
    # PySide6 进程内只能有一个 QApplication：四个分段共用这一个
    _app = QApplication.instance() or QApplication(sys.argv)   # noqa: F841

    sections = {
        "左列表分组与点击判定四态": _check_main_list_model,
        "点击才判定(绿/红/灰)": _check_click_verdict,
        "源管理弹窗(增删改/优先级)": _check_source_manager,
        "UI 细节(三态/置顶/音量/刷新源/右键)": _check_ui_details,
    }
    for name, fn in sections.items():
        log.info("========== 分段：%s ==========", name)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 单段异常不阻断其余分段
            log.exception("%s 执行异常: %s", name, exc)
            _FAILURES.append("异常(%s): %s" % (name, exc))

    try:
        engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(_tmpdir, ignore_errors=True)

    log.info("----- 汇总 -----")
    if _FAILURES:
        log.info("存在失败项(%d): %s", len(_FAILURES), _FAILURES)
    else:
        log.info("verify_ui 全部通过 ✓")
    return EXIT_OK if not _FAILURES else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
