# SPDX-License-Identifier: GPL-3.0-or-later
"""
手动同步直播源验证脚本（原 verify_step7_sync.py）。

验证内容（全部离线，用线程内的本地 http.server 造上游清单，不依赖外网）：
  A. GitHub 加速器改写（core/mirror.py，纯字符串逻辑）
     1. raw 链接 → jsDelivr 的 cdn.jsdelivr.net/gh/<u>/<r>@<b>/<path>
     2. GitHub Pages 链接 → jsDelivr 的 gh-pages 分支形式
     3. 前缀式代理只认 github.com / raw.githubusercontent.com —— 给它 Pages 地址应返回 None
     4. 「自动」模式给出多个候选，且一定包含原始地址兜底
  B. 对账语义（core/sync_sources.py）
     5. 同来源的旧地址被原地更换（行 id 与优先级不变）
     6. 上游不再提供的自动源被删除
     7. 被手动改过的源（is_user_edited=True）不被覆盖，上游地址另存并置顶
     8. 库里没有的地址 → 新增
     9. 已是其它来源（user）的重复地址 → 跳过，不重复插入
    10. 上游出现库里没有的核心频道 → 新建频道
    11. 非核心条目（港澳台）→ 被白名单过滤，不参与同步
    12. 用户自己加的源（origin=user）全程不动
    13. 幂等：上游没变时第二次同步不应产生任何改动
    14. new_channel_scope 三档（none / cctv / all）的新增口径
  C. UI 冒烟
    15. SyncDialog 能列出配置来源、能取到勾选集合、默认勾选与按钮可用状态正确

说明：
  - 全程使用隔离临时库，不碰真实 data/liuhaitv.db。
  - 本地 http.server 造上游清单，零外网依赖，确定性可重跑。

运行：
  <env>/python.exe scripts/verify_sync.py

返回码：0=通过  1=存在失败项
"""
import http.server
import logging
import os
import shutil
import socketserver
import sys
import tempfile
import threading

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import liuhaitv  # noqa: E402

# 隔离临时库：必须在导入 liuhaitv.core.database 之前改 DB_PATH
_tmpdir = tempfile.mkdtemp(prefix="liuhaitv_verify_sync_")
import liuhaitv.config as cfg  # noqa: E402
cfg.DB_PATH = os.path.join(_tmpdir, "liuhaitv_test.db")

import liuhaitv.logger as pylog  # noqa: E402
pylog.setup_logging()
log = logging.getLogger("verify_sync")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from liuhaitv.core import mirror, sync_sources  # noqa: E402
from liuhaitv.core.database import Base, engine, init_db, session_scope  # noqa: E402
from liuhaitv.core.models import Channel, Source  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1

RAW_URL = "https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u"
PAGES_URL = "https://iptv-org.github.io/iptv/countries/cn.m3u"
ORIGIN = "testsrc"

UPSTREAM = """#EXTM3U
#EXTINF:-1,CCTV-1
{u}/NEW1.m3u8
#EXTINF:-1,Beijing Satellite TV
{u}/NEWBJ.m3u8
#EXTINF:-1,浙江卫视
{u}/Z1.m3u8
#EXTINF:-1,江苏卫视
{u}/J1.m3u8
#EXTINF:-1,CCTV-2
{u}/U2.m3u8
#EXTINF:-1,CCTV-2
{u}/NEWC2.m3u8
#EXTINF:-1,CCTV-16
{u}/C16A.m3u8
#EXTINF:-1,CCTV-16
{u}/C16B.m3u8
#EXTINF:-1,CCTV-16
{u}/C16C.m3u8
#EXTINF:-1,CCTV-16
{u}/C16D.m3u8
#EXTINF:-1,凤凰卫视中文台
{u}/FH.m3u8
#EXTINF:-1,香港卫视
{u}/HKS.m3u8
"""


def reset_db():
    """清空临时库并重建，让各段互不干扰（段与段之间共享一个临时库）。"""
    Base.metadata.drop_all(engine)
    init_db()


class _Handler(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/up.m3u"):
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl")
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
        else:
            self.send_error(404)

    def log_message(self, *args):  # 静音
        pass


def _serve():
    """起一个只服务 /up.m3u 的本地 HTTP 服务，返回 (httpd, 清单URL)。"""
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    _Handler.body = UPSTREAM.format(u="http://127.0.0.1:%d" % port).encode("utf-8")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/up.m3u" % port


def _seed(root: str) -> dict:
    """造初始数据。注意：凡是要与"上游同地址"的地方都必须用 root 拼，
    否则夹具里两条 URL 看着一样其实不同（这一步踩过坑：动态端口）。"""
    reset_db()
    with session_scope() as s:
        def chan(name, group, srcs):
            c = Channel(name=name, group_name=group)
            s.add(c)
            s.flush()
            for i, (url, origin, edited) in enumerate(srcs):
                s.add(Source(channel_id=c.id, url=url, source_id="%s:%d" % (origin, i),
                             origin=origin, kind="hls", protocol="domain",
                             default_priority=i, is_healthy=False,
                             is_enabled=True, is_user_edited=edited))
            return c.id

        return {
            # 自动源、上游换地址 → 应被原地更换
            "cctv1": chan("CCTV-1", "中央", [("%s/OLD1.m3u8" % root, ORIGIN, False)]),
            # 被手动改过的源 → 不覆盖，上游地址另存并置顶
            "bj": chan("北京卫视 HD", "卫视", [("%s/USERBJ.m3u8" % root, ORIGIN, True)]),
            # 两个自动源，上游只给一个 → 多的应被删除（Z1 同地址应被保留、不算更换）
            "zj": chan("浙江卫视 蓝", "卫视",
                       [("%s/Z1.m3u8" % root, ORIGIN, False),
                        ("%s/Z2.m3u8" % root, ORIGIN, False)]),
            # 用户源，其地址与上游给的 U2 完全相同 → 应跳过重复、只新增 NEWC2
            "cctv2": chan("CCTV-2", "中央", [("%s/U2.m3u8" % root, "user", False)]),
            # 库里叫 CCTV-16 HD，上游叫 CCTV-16（不带清晰度）→
            # 必须靠 family_key 兜底命中，**不能**新建一个"CCTV-16"；
            # 且上游给了 4 条地址，应被 SYNC_MAX_URLS_PER_ORIGIN 截断为 3 条。
            "cctv16": chan("CCTV-16 HD", "中央", [("%s/U16.m3u8" % root, "user", False)]),
        }


def _all_channel_names():
    with session_scope() as s:
        return {c.name for c in s.query(Channel).all()}


def _snapshot(names):
    """取若干频道当前的 (url, origin, priority, is_user_edited)，按优先级排序。"""
    out = {}
    with session_scope() as s:
        for c in (s.query(Channel).options(selectinload(Channel.sources))
                  .filter(Channel.name.in_(names)).all()):
            out[c.name] = [(x.url, x.origin, x.default_priority, bool(x.is_user_edited))
                           for x in sorted(c.sources, key=lambda y: (y.default_priority, y.id))]
    return out


def main() -> int:
    log.info("===== verify_sync：手动同步直播源 =====")
    failures: list = []

    def check(label, cond, extra=""):
        log.info("  %-52s %s %s", label, "OK" if cond else "FAIL", extra)
        if not cond:
            failures.append(label)

    # ---- A. 加速器改写 ----
    log.info("-- A. GitHub 加速器改写 --")
    jsd = mirror.apply_mirror(RAW_URL, "jsdelivr")
    check("raw → jsDelivr gh 形式",
          jsd == "https://cdn.jsdelivr.net/gh/Kimentanm/aptv@master/m3u/iptv.m3u", jsd)

    jsd_pages = mirror.apply_mirror(PAGES_URL, "jsdelivr")
    check("Pages → jsDelivr gh-pages 分支",
          jsd_pages == "https://cdn.jsdelivr.net/gh/iptv-org/iptv@gh-pages/countries/cn.m3u",
          jsd_pages)

    check("前缀代理只认 github 域名（Pages → None）",
          mirror.apply_mirror(PAGES_URL, "gh_proxy_com") is None)
    check("前缀代理拼 raw 链接",
          mirror.apply_mirror(RAW_URL, "gh_proxy_com") == "https://gh-proxy.com/" + RAW_URL)
    check("direct 原样返回", mirror.apply_mirror(RAW_URL, "direct") == RAW_URL)

    cands = mirror.candidates(RAW_URL, "auto")
    check("自动模式给出多个候选", len(cands) >= 4, len(cands))
    check("自动模式含 jsDelivr 与原始地址兜底",
          any("cdn.jsdelivr.net" in c for c in cands) and RAW_URL in cands)
    check("自动模式对 Pages 地址也保留原始兜底",
          PAGES_URL in mirror.candidates(PAGES_URL, "auto"))
    check("指定加速方式时仍附原始地址兜底",
          RAW_URL in mirror.candidates(RAW_URL, "jsdelivr"))

    # ---- B. 对账语义 ----
    log.info("-- B. 对账语义（本地 http.server 造上游清单）--")
    httpd, up_url = _serve()
    root = up_url.rsplit("/", 1)[0]
    ids = _seed(root)

    before = _snapshot(["CCTV-1", "北京卫视 HD", "浙江卫视 蓝", "CCTV-2"])

    srcs = [{"id": ORIGIN, "name": "测试清单", "kind": "m3u", "url": up_url}]
    rep = sync_sources.sync_from_sources([ORIGIN], mirror_id="direct",
                                         top_new=True, sources=srcs)
    r = rep.reports[0]
    check("来源拉取成功", r.ok, r.error)
    check("上游覆盖 6 个核心频道（凤凰被白名单过滤）", r.fetched == 6, r.fetched)

    t = rep.totals()
    check("更换 1 条（CCTV-1 旧地址）", t["replaced"] == 1, t["replaced"])
    check("删除 1 条（浙江卫视 蓝 上游已撤的源）", t["removed"] == 1, t["removed"])
    check("新增 6 条（含 CCTV-16 截断后的 3 条）", t["added"] == 6, t["added"])
    check("置顶 6 条", t["pinned"] == 6, t["pinned"])
    check("保护手改 1 条", t["kept_manual"] == 1, t["kept_manual"])
    check("跳过重复 1 条（U2 已被 user 源占用）", t["skipped_dup"] == 1, t["skipped_dup"])
    check("只新建 1 个频道（江苏卫视；CCTV-16 应兜底命中而非新建）",
          t["channels_created"] == 1, t["channels_created"])
    check("涉及频道 6 个", t["channels_touched"] == 6, t["channels_touched"])

    after = _snapshot(["CCTV-1", "北京卫视 HD", "浙江卫视 蓝", "江苏卫视", "CCTV-2",
                       "CCTV-16 HD"])

    # 5. 原地更换：行不变（同一 url 位置被替换），优先级不变
    cctv1 = after["CCTV-1"]
    check("CCTV-1 旧地址被换成上游新地址",
          len(cctv1) == 1 and cctv1[0][0].endswith("/NEW1.m3u8"), cctv1)
    check("CCTV-1 优先级保持 0（原地更换）", cctv1[0][2] == 0, cctv1)

    # 6. 上游撤掉的源被删除
    zj = after["浙江卫视 蓝"]
    check("浙江卫视 蓝 只剩上游仍在发布的那条",
          len(zj) == 1 and zj[0][0].endswith("/Z1.m3u8"), zj)

    # 7. 手改保护 + 置顶
    bj = after["北京卫视 HD"]
    bj_urls = [x[0] for x in bj]
    manual = [x for x in bj if x[3]]
    check("被手改的地址未被覆盖",
          any(u.endswith("/USERBJ.m3u8") for u in bj_urls), bj_urls)
    check("手改标记仍为 True", len(manual) == 1 and manual[0][0].endswith("/USERBJ.m3u8"))
    check("上游新地址已置顶（优先级 0）",
          bj[0][0].endswith("/NEWBJ.m3u8") and bj[0][2] == 0, bj)
    check("手改的那条被顺延到优先级 1",
          manual[0][2] == 1, manual[0][2])

    # 8/10. 新增 + 新建频道
    js = after.get("江苏卫视")
    check("上游新频道已入库", js is not None and len(js) == 1
          and js[0][0].endswith("/J1.m3u8"), js)

    # 9. 跳过重复：CCTV-2 里 U2 只有一条（user 那份），另加一条 NEWC2
    cctv2urls = [x[0] for x in after["CCTV-2"]]
    check("CCTV-2 未插入重复的 U2", sum(1 for u in cctv2urls if u.endswith("/U2.m3u8")) == 1,
          cctv2urls)
    check("CCTV-2 新增了 NEWC2", any(u.endswith("/NEWC2.m3u8") for u in cctv2urls))

    # 12. 用户源全程不动
    user_src = [x for x in after["CCTV-2"] if x[1] == "user"]
    check("用户源未被改动", len(user_src) == 1 and user_src[0][0].endswith("/U2.m3u8"), user_src)

    # 11. 非核心频道没被建出来
    check("港澳台条目未参与同步",
          _snapshot(["凤凰卫视中文台"]).get("凤凰卫视中文台") is None)
    check("名字含港澳台的「香港卫视」也没被建出来",
          "香港卫视" not in _all_channel_names())

    # 11b. 清晰度变体兜底匹配 + 每来源地址数截断
    check("未重复新建 CCTV-16（应兜底命中 CCTV-16 HD）",
          "CCTV-16" not in _all_channel_names(), sorted(_all_channel_names()))
    c16 = [x for x in after["CCTV-16 HD"] if x[1] == ORIGIN]
    check("CCTV-16 HD 采纳的地址被截断为 %d 条"
          % cfg.SYNC_MAX_URLS_PER_ORIGIN,
          len(c16) == cfg.SYNC_MAX_URLS_PER_ORIGIN, c16)
    check("截断时按上游顺序取前 N 条（含 C16A ~ C16C，不含 C16D）",
          [x[0].rsplit("/", 1)[-1] for x in c16] == ["C16A.m3u8", "C16B.m3u8", "C16C.m3u8"],
          [x[0].rsplit("/", 1)[-1] for x in c16])
    check("原 user 源仍在 CCTV-16 HD 下",
          any(x[1] == "user" and x[0].endswith("/U16.m3u8") for x in after["CCTV-16 HD"]))

    # 13. 幂等
    log.info("-- 幂等：上游未变时再同步一次 --")
    rep2 = sync_sources.sync_from_sources([ORIGIN], mirror_id="direct",
                                          top_new=True, sources=srcs)
    t2 = rep2.totals()
    check("二次同步无任何改动",
          t2["replaced"] == 0 and t2["added"] == 0 and t2["removed"] == 0
          and t2["channels_created"] == 0, t2)

    # 13b. 「不新增频道」口径：只统计告知，不往库里塞新台
    log.info("-- new_channel_scope=none：只统计不创建 --")
    with session_scope() as s:
        gone = s.query(Channel).filter(Channel.name == "江苏卫视").one()
        s.delete(gone)
    rep3 = sync_sources.sync_from_sources([ORIGIN], mirror_id="direct",
                                          top_new=True, new_channel_scope="none",
                                          sources=srcs)
    t3 = rep3.totals()
    r3 = rep3.reports[0]
    check("scope=none 时不创建频道", t3["channels_created"] == 0, t3["channels_created"])
    check("scope=none 时仍报告『上游有但库里没有』的频道数",
          r3.new_available >= 1, r3.new_available)
    check("报告里列出了这些频道名",
          any("江苏" in n for n in r3.available_names), r3.available_names)
    check("该频道确实没被建出来", "江苏卫视" not in _all_channel_names())

    # 13c. scope=cctv：只补央视
    log.info("-- new_channel_scope=cctv：只补央视 --")
    rep4 = sync_sources.sync_from_sources([ORIGIN], mirror_id="direct",
                                          top_new=True, new_channel_scope="cctv",
                                          sources=srcs)
    r4 = rep4.reports[0]
    check("scope=cctv 时卫视仍不创建", "江苏卫视" not in _all_channel_names())
    check("scope=cctv 时把上游缺的卫视计入待创建提示",
          any("江苏" in n for n in r4.available_names), r4.available_names)

    # ---- C. UI 冒烟 ----
    log.info("-- C. 同步弹窗冒烟 --")
    from liuhaitv.ui.sync_dialog import SyncDialog  # noqa: E402
    app = QApplication.instance() or QApplication(sys.argv)
    dlg = SyncDialog()
    check("弹窗列出全部配置来源",
          dlg.lst.count() == len(sync_sources.list_sources()), dlg.lst.count())
    ids_checked = dlg._selected_ids()
    # 按需求：config 里所有来源的 default 一律 False，
    # 默认一个都不勾 —— 由使用者自己决定同步哪些来源（工具保持中立）。
    check("默认一个来源都不勾选", ids_checked == [], ids_checked)
    check("没勾任何来源时「开始同步」被禁用（防误操作）", not dlg.btn_run.isEnabled())
    check("默认加速方式为「自动」", dlg.cmb_mirror.currentData() == cfg.DEFAULT_MIRROR,
          dlg.cmb_mirror.currentData())
    check("默认不新增频道（只同步地址）", dlg.cmb_new.currentData() == "none",
          dlg.cmb_new.currentData())
    check("「新增频道」有三种口径",
          [dlg.cmb_new.itemData(i) for i in range(dlg.cmb_new.count())]
          == ["none", "cctv", "all"])
    # 手动勾一个之后：按钮应恢复可用，且 _selected_ids 能取到
    try:
        first = dlg.lst.item(0)
        first.setCheckState(Qt.CheckState.Checked)
        check("勾选一个来源后按钮恢复可用", dlg.btn_run.isEnabled())
        check("能取到刚勾选的来源", len(dlg._selected_ids()) == 1, dlg._selected_ids())
    except Exception as exc:  # noqa: BLE001
        check("勾选后按钮恢复可用", False, exc)
    dlg._check_all(False)
    check("全部取消勾选后按钮置灰", not dlg.btn_run.isEnabled())
    dlg.deleteLater()

    httpd.shutdown()

    try:
        engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(_tmpdir, ignore_errors=True)

    log.info("----- 汇总 -----")
    if failures:
        log.info("存在失败项(%d): %s", len(failures), failures)
    else:
        log.info("verify_sync 全部通过 ✓")
    return EXIT_OK if not failures else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
