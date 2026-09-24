# SPDX-License-Identifier: GPL-3.0-or-later
"""
集中配置文件。

模块被 liuhaitv 包下的各功能模块引用，用于统一读取路径、网络参数、
搜刮源列表和健康检测参数，避免散落 magic number。

所有路径都基于 <项目根>/ 动态计算，可被 scripts、tests、UI 三方直接
import 而不必关心当前工作目录。

**打包形态（PyInstaller exe）下的路径策略**（重点，别改错）：
  - `RESOURCE_ROOT` = 随包分发的**只读资源**（bin/libmpv-2.dll、模板库），
    打包后是临时解包目录 `sys._MEIPASS`；
  - `BASE_DIR` = **用户数据**根（data/ 与 logs/），打包后是 **exe 所在目录**，
    源码运行则是项目根。
  两者必须分开：把用户数据写进 `_MEIPASS` 会随进程退出一起消失，
  写进程序安装目录又可能因无写权限而失败。
"""
import os
import shutil
import sys

# ---------------------------------------------------------------------------
# 基础路径
# ---------------------------------------------------------------------------
IS_FROZEN = bool(getattr(sys, "frozen", False))


def _source_root() -> str:
    """源码运行时的项目根 = 本文件 (config.py) 所在目录的上一级。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 只读资源根：打包后指向 PyInstaller 的解包目录，源码运行就是项目根
RESOURCE_ROOT = getattr(sys, "_MEIPASS", None) or _source_root()

# 用户数据根：打包后 = exe 所在目录（便携化，插到哪写到哪）；源码运行 = 项目根
BASE_DIR = (os.path.dirname(os.path.abspath(sys.executable))
            if IS_FROZEN else _source_root())

# 兼容旧名字（既有脚本/代码里引用的是 PROJECT_ROOT）
PROJECT_ROOT = BASE_DIR

# bin/ 目录，存放随程序分发的 libmpv-2.dll 等动态库（只读资源，跟着包走）
BIN_DIR = os.path.join(RESOURCE_ROOT, "bin")

# data/ 目录，运行时生成：SQLite 数据库、用户偏好等持久化数据（用户数据，跟着 exe 走）
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# 数据库文件路径
DB_PATH = os.path.join(DATA_DIR, "liuhaitv.db")

# 日志文件路径（按天滚动）
LOG_FILE = os.path.join(LOGS_DIR, "liuhaitv.log")

# 首次运行的"种子库"：打包时把当前库打到 data_seed/ 下，
# 程序启动时若 exe 旁边还没有 data/liuhaitv.db 就复制过去，
# 这样换台电脑也一打开就有完整的央视+卫视频道表。
SEED_DB = os.path.join(RESOURCE_ROOT, "data_seed", "liuhaitv.db")


# 创建目录（幂等）。数据库/日志由各自模块按需创建，这里统一预建以便首跑可用
def ensure_dirs() -> None:
    for d in (DATA_DIR, LOGS_DIR):
        os.makedirs(d, exist_ok=True)


def ensure_seed_db() -> bool:
    """
    首次运行时把随包的模板库复制到用户数据目录；已有库则不动。
    返回是否真的复制了。源码运行不参与（库本来就由程序自己建）。
    """
    if os.path.exists(DB_PATH):
        return False
    if not (IS_FROZEN and os.path.exists(SEED_DB)):
        return False
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        shutil.copy2(SEED_DB, DB_PATH)
        return True
    except Exception:  # noqa: BLE001 - 播种失败就退化为"空库"，不该阻断启动
        return False


# ---------------------------------------------------------------------------
# 网络与异步
# ---------------------------------------------------------------------------
# 单次 HTTP 请求超时（秒）
HTTP_TIMEOUT = 15.0

# 搜刮远程 M3U 时的总超时（秒）
SCRAPE_TIMEOUT = 60.0

# HTTP 客户端请求头（部分源需要伪装浏览器）
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
}

# ---------------------------------------------------------------------------
# 直播源健康检测（Step 4 完善，这里先给默认值）
# ---------------------------------------------------------------------------
# 测速并发上限（信号量）
HEALTH_CONCURRENCY = 20
# 单个源检测超时（秒）
HEALTH_TIMEOUT = 8.0
# 判定"可用"的延迟阈值（毫秒）。高于此值标记为慢但可用；超时则不可用
HEALTH_LATENCY_OK_MS = 3000
# 检测时读取流的前多少字节用于确认返回的是真实媒体流
HEALTH_READ_BYTES = 4096


# ---------------------------------------------------------------------------
# 直播源搜刮：多源合并
# ---------------------------------------------------------------------------
# 结构：每个源为一个 dict，包含
#   id           —— 内部唯一标识（同时作为 Source.origin 落库，是"这份地址来自哪个源"的依据）
#   name         —— 显示名称
#   kind         —— "m3u"（直接 M3U 清单）或 "category"（按分类过滤）
#   url          —— 远程清单地址（原始 URL；真正的请求地址由 core/mirror.py 按所选加速器改写）
#   group_key    —— 该源中对"央视 / 卫视"分类的匹配关键字（历史字段，现已由 channel_filter 接管）
#   default      —— 在"同步直播源"弹窗里是否默认勾选
#                   ⚠ 现已全部置为 False（2026-09-24 按需求改）：默认一个都不勾，
#                     由使用者自己决定同步哪些来源 —— 工具保持中立，不替用户挑内容源。
#   note         —— 弹窗里给用户看的补充说明（覆盖范围 / 网络要求等）
#
# ⚠ 实测结论（2026-09-23）：本机 **raw.githubusercontent.com 完全不通**（连接被阻断），
#   而 iptv-org.github.io（GitHub Pages）可直连。所以下面凡是以 raw.githubusercontent.com
#   开头的源，**必须配合 GitHub 加速器**才能拉到（见 GITHUB_MIRRORS）。
#   这不是可选优化项，而是这类源能否工作的前提。
SCRAPE_SOURCES = [
    # --- GitHub Pages 直连源：无需加速器 ---
    {
        "id": "iptvorg_cn",
        "name": "iptv-org 中国(央视+卫视)",
        "kind": "m3u",
        "url": "https://iptv-org.github.io/iptv/countries/cn.m3u",
        "group_key": ["CCTV", "央", "卫视", "各省", "地方"],
        "default": False,
        "note": "GitHub Pages，可直连；约 144 条，含大量英文名条目",
    },
    # --- 以下都是 raw.githubusercontent.com，需要加速器 ---
    {
        "id": "iptvorg_streams",
        "name": "iptv-org 原始流清单(中文名更全)",
        "kind": "m3u",
        "url": "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u",
        "group_key": ["CCTV", "央", "卫视"],
        "default": False,
        "note": "官方源文件（非 Pages 精简版），约 500 条，含 BRTV 北京卫视/云南卫视/四川卫视等中文名条目",
    },
    {
        "id": "kimentanm",
        "name": "aptv 汇总(央视+省级卫视+4K)",
        "kind": "m3u",
        "url": "https://raw.githubusercontent.com/Kimentanm/aptv/master/m3u/iptv.m3u",
        "group_key": ["央视", "卫视", "CCTV"],
        "default": False,
        "note": "央视 19 路 + 卫视 40 路，且带各省 4K 版本，覆盖最整齐",
    },
    {
        "id": "guovin",
        "name": "iptv-api 自动测速输出",
        "kind": "m3u",
        "url": "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
        "group_key": ["央视", "卫视", "CCTV"],
        "default": False,
        "note": "仓库自带定时测速，产出的都是当时可用的地址；条目多(1600+)，含央视 18 + 卫视 40",
    },
    {
        "id": "yuechan",
        "name": "YueChan Live 精选",
        "kind": "m3u",
        "url": "https://raw.githubusercontent.com/YueChan/Live/main/IPTV.m3u",
        "group_key": ["央视", "卫视", "CCTV"],
        "default": False,
        "note": "人工精选，条目少但质量较稳；卫视 29 路",
    },
    {
        "id": "vbskycn",
        "name": "vbskycn 双栈源(IPv4)",
        "kind": "m3u",
        "url": "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
        "group_key": ["央视", "卫视", "CCTV"],
        "default": False,
        "note": "530 条，含 4K/8K 与多路备份；另仓库有 tv/iptv6.m3u 走 IPv6",
    },
    {
        "id": "fanmingming_ipv6",
        "name": "fanmingming 精选(IPv6)",
        "kind": "m3u",
        "url": "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u",
        "group_key": ["CCTV", "中央", "卫视", "地方"],
        "default": False,
        "note": "★ 纯 IPv6 地址，**需要本机有 IPv6 出口**才能播放；卫视覆盖 34 路，中文名齐全",
    },
    {
        "id": "hujingguang",
        "name": "ChinaIPTV 自动更新",
        "kind": "m3u",
        "url": "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/cnTV_AutoUpdate.m3u8",
        "group_key": ["央视", "卫视", "CCTV"],
        "default": False,
        "note": "只覆盖卫视(14 路)，可作为额外备份口径",
    },
    # 实测无价值、暂不启用（留档说明，避免后人重复试）：
    #   Free-TV/IPTV        -> 2074 条里只有 1 个央视、0 个卫视（是国际频道大杂烩）
    #   YanG-1989/m3u       -> 123 条，按本项目的央视/卫视判定一个都不命中（命名格式不同）
    # 可继续追加：新增条目即可，引擎与弹窗都会自动识别。
]


# ---------------------------------------------------------------------------
# GitHub 加速器（"同步直播源"弹窗里的"加速方式"下拉）
# ---------------------------------------------------------------------------
# 为什么需要：本机实测 raw.githubusercontent.com 直连**完全不通**，
# 而下面这些加速方式都能拉到同一份文件（2026-09-23 实测均为 HTTP 200）：
#   jsdelivr      把 raw 链接改写成 cdn.jsdelivr.net/gh/<user>/<repo>@<branch>/<path>
#   gh-proxy.com / ghproxy.net / ghfast.top   前缀式代理：<代理前缀> + 原始 raw 链接
#
# 注意：前缀式代理**只认 github.com / raw.githubusercontent.com**，
# 给它 GitHub Pages（*.github.io）地址会返回 403；jsDelivr 也不代理 Pages。
# 所以 github.io 的源在"自动"模式下会走直连。core/mirror.py 会按 URL 形态自动取舍。
GITHUB_MIRRORS = [
    {
        "id": "auto",
        "name": "自动（推荐）",
        "prefix": None,
        "note": "依次尝试直连与所有加速器，取第一个拉取成功的",
    },
    {
        "id": "direct",
        "name": "直连（不用加速）",
        "prefix": "",
        "note": "GitHub Pages 可直连；raw.githubusercontent.com 在本机不通",
    },
    {
        "id": "jsdelivr",
        "name": "jsDelivr CDN",
        "prefix": None,
        "note": "把 raw 链接改写成 cdn.jsdelivr.net/gh/...，国内通常可用",
    },
    {
        "id": "gh_proxy_com",
        "name": "gh-proxy.com",
        "prefix": "https://gh-proxy.com/",
        "note": "前缀式代理，只支持 github.com / raw.githubusercontent.com",
    },
    {
        "id": "ghproxy_net",
        "name": "ghproxy.net",
        "prefix": "https://ghproxy.net/",
        "note": "前缀式代理，同上",
    },
    {
        "id": "ghfast_top",
        "name": "ghfast.top",
        "prefix": "https://ghfast.top/",
        "note": "前缀式代理，同上",
    },
]

# "自动"模式下的尝试顺序（只在能处理该 URL 形态的加速器会被跳过，不会报错）
MIRROR_TRY_ORDER = ["direct", "jsdelivr", "gh_proxy_com", "ghproxy_net", "ghfast_top"]

# 弹窗默认选中的加速方式
DEFAULT_MIRROR = "auto"


# ---------------------------------------------------------------------------
# 手动同步直播源（「🔄 同步源」）的行为参数
# ---------------------------------------------------------------------------
# 单个来源、单个频道最多采纳几条地址。
# 上游清单（尤其 iptv-api / iptv-org 原始流清单）常给同一个台几十条备用地址
# （不同清晰度、不同服务器），全收下来会让 failover 挨个试到天荒地老，
# 所以按上游给出的顺序只取前 N 条 —— 上游通常把较好的排在前面。
SYNC_MAX_URLS_PER_ORIGIN = 3


