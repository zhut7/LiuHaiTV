# LiuHaiTV

Windows 桌面端的**央视 / 省级卫视直播播放器**。

- 界面 PySide6（Qt 6） · 播放内核 mpv（libmpv） · 数据 SQLite + SQLAlchemy
- 频道按「中央 / 卫视」分组，**点哪个台才测哪个台**，一眼看出哪些能播
- 一个频道可挂多条直播源，播放失败自动切下一条
- 内置**多来源同步**：从公开直播源清单批量更新地址（带 GitHub 加速），  
  上游换掉的地址自动替换、撤掉的清理掉、你手动改过的**不会被覆盖**

> 本项目不含任何音视频内容，也不内置任何直播流地址 —— 仓库里只有播放器代码，  
> 以及指向第三方公开播放列表的 URL 配置。请只使用你有合法授权的信号源，详见  
> 文末「法律与免责声明」。

---

## 快速开始

```bash
# 1) 环境（Python 3.11）
conda create -n liuhaitv python=3.11.7
conda activate liuhaitv
pip install -r requirements.txt

# 2) 准备播放内核 libmpv-2.dll（约 115 MB，不在仓库里，见下一节）
#    下载 → 用 7-Zip 解压 → 把 libmpv-2.dll 放到 bin/ 下

# 3) 自检（依赖 / 内核 / 数据库 / 网络）
python scripts/check_env.py

# 4) 启动
python scripts/run_gui.py
```

第一次打开时库是空的。拿到频道有两条路：

- **推荐**：点控制条的「🔄 同步源」，把「新增频道」选成 **央视 + 卫视都补**，开始同步 ——  
  它会把公开清单里的央视与省级卫视建出来并写入地址。
- 或者：把别人给的一份 `data/liuhaitv.db` 放进 `data/` 目录（便携版里就是随包的这一份）。

---

## 准备 libmpv-2.dll（手动下载）

播放内核 `bin/libmpv-2.dll` **不在仓库里**，需要自己下一个放进去。

### 下载

<https://sourceforge.net/projects/mpv-player-windows/files/libmpv/>

进 `libmpv/` 目录，挑**日期最新的**那个：

```
mpv-dev-x86_64-<日期>-git-<哈希>.7z      ← 要这个
```

### 注意事项（这几条都踩过）

1. **别下 `-v3` 版**（形如 `mpv-dev-x86_64-v3-*.7z`）。v3 版针对 AVX2 指令集编译，  
   老 CPU 上会直接起不来。普通 x86_64 版最保险。
2. **必须用 7-Zip 解压**。官方包用了 **BCJ2 过滤器**，实测 Windows 自带 `tar.exe`  
   报 `LZMA codec is unsupported`，Python 的 `py7zr` 报 `BCJ2 filter is not supported` ——  
   都解不开。装个 7-Zip（`winget install 7zip.7zip`）最省事。
3. **解压后只取 `libmpv-2.dll`**，把它放到项目根的 `bin/` 目录下（`bin/` 不存在就新建）。  
   包里的头文件、`.lib` 之类都不需要。
4. **必须是 64 位**，且要和你的 Python 位数一致（本机是 64 位 Python 3.11）。  
   32/64 位不匹配是最常见的失败原因。
5. 放好后跑一次 `python scripts/check_env.py` 验证 —— 它会尝试加载 dll 并打印 mpv 版本。

代码里三种文件名都认：`libmpv-2.dll` / `mpv-2.dll` / `mpv-1.dll`，  
所以旧版构建（叫 `mpv-2.dll`）也能直接用。

### 为什么不让仓库自带

`bin/libmpv-2.dll` 有 **115 MB，超过 GitHub「单文件 100 MB」硬上限**：直接提交会被  
push 拒绝；用 Git LFS 又要占**每月 1 GiB 下载带宽**（每次 clone 约 110 MiB，  
每月大概 9 次完整克隆就用满，之后别人拉不下来）。

所以 `bin/` 已在 `.gitignore` 里排除，**每个使用者各自下载一次**即可 ——  
这也是 mpv 官方社区的通行做法（mpv 本身不发布预编译包，libmpv 由社区构建提供）。

> 顺带一提：如果你有自己托管的直链，也可以把它放在 README 里给使用者，  
> 但要注意那是**再分发第三方二进制**，建议同时附上 mpv/libmpv 的许可证与来源链接。

---

## 界面说明

左侧频道列表，点频道即播放；底部控制条：

| 按钮     | 作用                                  |
| ------ | ----------------------------------- |
| ☰ 频道   | 收起 / 展开左侧列表                         |
| 📡 源管理 | 维护当前频道的源：增删改、⤒置顶、上下移、启用开关、测速、导入 M3U |
| 🔄 同步源 | 批量从公开清单更新地址（可选加速方式与「新增频道」档位）        |
| ⟳ 刷新源  | 画面卡住 / 黑屏时点它：断开当前流，用现有源重新加载         |
| 音量     | 默认最大，且会记住上次调的值                      |
| ⛶ 全屏   | 退出按 Esc                             |

**右键菜单**（「直播源管理…」/「重新检测该频道」）只弹菜单，**不会切换频道**。

频道颜色（点过才判定）：

| 颜色 | 含义                 |
| -- | ------------------ |
| 灰  | 本次会话还没点过           |
| 橙  | 正在检测               |
| 绿  | 可播放                |
| 红  | 播不了（试试「🔄 同步源」换地址） |

源管理里的状态列是三态：**未检测（灰）/ 可播放（绿）/ 未通过（红）**。

---

## 目录结构

```
LiuHaiTV/
├─ liuhaitv/                     ← 应用源码
│  ├─ __init__.py                   定位并加载 bin/ 下的 libmpv（打包形态也处理）
│  ├─ config.py                     集中配置：路径、网络参数、直播源清单、加速方式
│  ├─ logger.py                     日志（按天滚动，logs/ 下保留 10 天）
│  ├─ core/                         数据与逻辑层
│  │  ├─ models.py                  ORM 模型：Channel / Source / Setting
│  │  ├─ database.py                引擎、建表、幂等补列（老库免迁移）、首次播种
│  │  ├─ m3u_parser.py              M3U / M3U8 清单解析
│  │  ├─ scraper.py                 搜刮：抓取 → 清洗 → 过滤 → 合并 → 落库
│  │  ├─ channel_filter.py          白名单判定（只留央视/卫视）与身份归键
│  │  ├─ naming.py                  频道名规范化（全项目唯一来源）
│  │  ├─ health.py                  源探测：并发 + 找到可用即早退
│  │  ├─ mirror.py                  GitHub 加速器：URL 改写
│  │  ├─ sync_sources.py            手动同步引擎（按来源对账）
│  │  └─ settings.py                键值偏好（音量等）
│  ├─ player/mpv_wrapper.py         python-mpv 封装 + PlayerController（failover）
│  └─ ui/                           界面层
│     ├─ main_window.py             主窗口：控制条、列表、全屏、右键菜单
│     ├─ channel_list_model.py      列表模型 / 颜色状态 / 自定义绘制 / 列表视图
│     ├─ source_manager.py          源管理弹窗（增删改 / 置顶 / 测速 / 导入 M3U）
│     ├─ sync_dialog.py             「🔄 同步源」弹窗 + 后台任务
│     ├─ check_worker.py            后台探测任务（QThreadPool）
│     └─ video_view.py              视频渲染区域（绑定 mpv wid）
│
├─ scripts/                      ← 启动、维护与验证脚本
├─ requirements.txt              依赖清单
├─ LiuHaiTV.spec                 PyInstaller 打包配置（one-folder 便携版）
├─ 使用说明.txt                   给最终用户的说明，随便携版分发
├─ LICENSE                       GPL-3.0 全文
├─ THIRD_PARTY_NOTICES.md        致谢与第三方组件许可证
│
├─ bin/          ❌ 不上传 —— libmpv-2.dll 需自己下载，见上文「准备 libmpv-2.dll」
├─ data/         ❌ 不上传 —— SQLite 库与备份（运行时生成）
├─ logs/         ❌ 不上传 —— 运行日志
├─ protable/     ❌ 不上传 —— 打包成品
└─ build/ dist/  ❌ 不上传 —— PyInstaller 中间产物
```

---

## 脚本一览

### 启动与维护

| 脚本                                         | 作用                                  |
| ------------------------------------------ | ----------------------------------- |
| `run_gui.py`                               | 启动图形界面（也是打包 exe 的入口）                |
| `check_env.py`                             | 环境自检：依赖版本、libmpv 能否加载、数据库能否建、网络连通性  |
| `prune_to_core.py`                         | 频道精简：预演 → 打印计划 → `--apply` 真删（自动备份） |
| `normalize_names.py`                       | 频道名规范化：预演 → `--apply`（自动备份，有冲突保护）   |
| `diag_stream.py` / `query_cctv_sources.py` | 排障小工具                               |

改库的两个脚本都**默认只预演**，要加 `--apply` 才动手，且动手前自动把库快照到  
`data/backup/`。

### 验证脚本

全部离线（临时库 + offscreen），**退出码 0 即通过**；共 280+ 条断言。

| 脚本                 | 覆盖                                                      |
| ------------------ | ------------------------------------------------------- |
| `verify_data.py`   | 数据模型 / M3U 解析 / 搜刮落库 / 核心白名单 / 身份归键 / 精简计划与幂等 / 备份      |
| `verify_player.py` | mpv 封装 / failover 状态机 / 健康源优先 / 异步健康检测                  |
| `verify_ui.py`     | 主窗口分组 / 点击判定四态 / 源管理弹窗 / 三态配色 / 置顶 / 音量记忆 / 刷新源 / 右键不换台 |
| `verify_sync.py`   | 加速器改写 / 同步对账语义 / 新增频道三档 / 弹窗                            |
| `verify_naming.py` | 频道名规范化 / 改名脚本的冲突保护                                      |

```bash
python scripts/verify_data.py
python scripts/verify_player.py
python scripts/verify_ui.py
python scripts/verify_sync.py
python scripts/verify_naming.py
```

改动任何逻辑后请把这五个都跑一遍。

### 排障环境变量

| 变量                        | 作用                  |
| ------------------------- | ------------------- |
| `LIUHAITV_FAULTHANDLER=1` | 卡住 20 秒后打印各线程堆栈     |
| `LIUHAITV_CONSOLE=1`      | 构建时用：产出带控制台的调试版 exe |

---

## 打包便携版 exe

产物是一个**文件夹**（约 249 MB），整个拷到别的 Windows 机器就能用，无需装 Python。

```powershell
pip install pyinstaller

$env:LIUHAITV_DISTNAME = "protable"
python -m PyInstaller --noconfirm `
    --distpath D:\Code\LiuHaiTV --workpath D:\Code\LiuHaiTV\build LiuHaiTV.spec
```

- 产物：`protable\LiuHaiTV.exe`（+ `_internal\`）。  
  **分发时把 `使用说明.txt`、`LICENSE`、`THIRD_PARTY_NOTICES.md` 一起放进该目录** ——  
  GPL 要求随程序一并向接收者提供许可证文本与来源说明
- **调试版**（能看见启动报错）：`set LIUHAITV_CONSOLE=1` 再构建，产出 `LiuHaiTV_debug`
- 打包前记得先跑一次 `run_gui.py`，让 `data/liuhaitv.db` 里有一份像样的频道库 ——  
  spec 会把它打成**种子库**，别人首次运行时自动复制到 exe 旁边

### 三个坑（spec 里已注释，别改回去）

1. **conda 的运行时 DLL 必须显式带上**  
   `_sqlite3.pyd` / `_ctypes.pyd` / `_ssl.pyd` 依赖 `Library\bin` 下的 DLL，PyInstaller  
   默认搜不到。症状是启动即炸：`ImportError: DLL load failed while importing _sqlite3`。  
   spec 里把该目录全部 `*.dll`（72 个 / 22 MB）加进 `binaries`，并把它塞进 PATH  
   以帮助解析传递依赖。
2. **无控制台的 exe 出错是静默的**  
   所以 `scripts/run_gui.py` 把重量级导入（`sqlalchemy` / `PySide6` / `liuhaitv.ui.*`）  
   放在**函数内**，这样顶层 `try/except` 才兜得住 —— 启动失败会写 `logs/crash.log`  
   并弹一个原生提示框（用 ctypes，Qt 起不来时也能显示）。
3. **路径要分两套**  
   `RESOURCE_ROOT = sys._MEIPASS`（只读资源：`bin/libmpv-2.dll`、种子库）  
   `BASE_DIR = exe 所在目录`（用户数据：`data/`、`logs/`）。  
   若把用户数据写进 `_MEIPASS`，退出就丢了。

### 打包建议

- **体积**：约 249 MB ⊃ libmpv 115 MB + Qt ~90 MB + conda DLL 22 MB。再瘦身收益有限。
- **单文件夹 vs 单文件**：默认单文件夹（启动快、不往临时目录解压）。想要"只有一个 exe"，  
  把 spec 改成 onefile —— 但每次启动都要把 250 MB 解到 `%TEMP%`，首屏会明显变慢。
- **杀软误报**：PyInstaller 打包的常见现象，加白名单即可。别开 UPX（更易误报且拖慢启动）。
- **分发**：别把 249 MB 塞进 git，用 GitHub **Release** 挂压缩包。
- **VC++ 运行库**：极少精简版系统会缺 `vc_redist.x64`，让用户装微软官方运行库即可。

---

## 已知限制

- **播放只试前 3 条源**：`PlayerController` 的 `max_failovers` 默认 2，即首发 1 条 + 最多再切  
  2 条就判定"备用源全失败"。同步之后一个频道可能有十几条源，第 4 条往后不会被用到。
- **"健康"权重高于"优先级"**：源按 `(是否健康, 优先级)` 排序。你置顶的源如果没测过、  
  或测过是红的，仍会排在其他绿色源后面。想让它真正先播，先在源管理里「测试选中」把它测绿。
- **没有 CLI 搜刮入口**：只有引擎 `liuhaitv/core/scraper.py`，初次灌数据请用界面的  
  「🔄 同步源」，或直接放一份现成的 `data/liuhaitv.db`。
- **上游命名不统一**：`naming.py` 已尽量规范化，但上游偶有错别字（如"黑龙卫视"），改不了。
- **单点故障**：公开清单里不少真央视共用同一台裸 IP 服务器，那台挂了会一起挂。

---

## 常见问题

**双击 exe 没反应** → 看 exe 旁边的 `logs/crash.log`（同时会弹错误框）。

**某个台点开是红的** → 该台地址都失效了。点「🔄 同步源」换一批，或在该台右键 →  
直播源管理 → 「测试选中」看哪条还能用。

**画面卡住** → 点「⟳ 刷新源」；或在源管理里把可用的源「⤒ 置顶」（并确认它是绿色的）。

**`check_env.py` 报找不到 dll** → 确认 `bin/libmpv-2.dll` 存在，且是 **64 位**  
（32/64 位不匹配是最常见的失败原因）。

**同步时提示"没有匹配的来源"** → 弹窗里默认**一个都不勾**，需要你自己勾选要同步的来源。

**想恢复出厂频道表** → 关掉程序，删掉 `data/`，重新启动。

---

## 开源协议

本项目采用 **GPL-3.0-or-later**（GNU General Public License v3.0 或更新版本），  
全文见 [`LICENSE`](LICENSE)。

选它是因为**运行时链接的组件决定了这个选择**：

| 组件                          | 授权                                                    |
| --------------------------- | ----------------------------------------------------- |
| mpv / libmpv（社区 Windows 构建） | LGPL-2.1+ 或 **GPL-2.0+**（构建若启用 GPL 组件即为后者）            |
| PySide6（Qt for Python）      | LGPL-3.0-only / GPL-2.0-only / **GPL-3.0-only**（三者择一） |
| python-mpv                  | GPL-2.0+ 或 LGPL-2.1+                                  |

这些都和 GPL-3.0 兼容，所以选 GPL-3.0 最省事、也最不容易出错。  
（如果你想改用 MIT 这类宽松协议，需要先把 `libmpv-2.dll` 换成 LGPL 版本 ——  
zhongfly 的 `mpv-dev-lgpl-*.7z` 就是。详见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。）

Copyright (C) 2026 LiuHaiTV contributors

---

## 致谢

本项目站在很多人的肩膀上，完整清单（含各自的许可证）见  
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。特别感谢：

- **mpv / libmpv** —— 播放内核，能通吃各种野路子直播流靠的就是它内建的 ffmpeg
- **Qt for Python (PySide6)** —— 界面框架
- **SQLAlchemy / httpx / python-mpv** —— 数据层、网络层与 mpv 绑定
- **直播源清单的维护者**：iptv-org、Kimentanm/aptv、Guovin/iptv-api、YueChan/Live、  
  vbskycn/iptv、fanmingming/live、hujingguang/ChinaIPTV —— 没有他们持续整理这些公开清单，这个播放器就没有内容可播
- **公益 GitHub 加速服务**：jsDelivr、gh-proxy.com、ghproxy.net、ghfast.top
- **mpv 二进制构建者**：shinchiro、zhongfly

---

## 法律与免责声明

- 本项目**不包含、不分发任何音视频内容**。仓库里只有播放器代码，  
  以及指向第三方公开播放列表（M3U 清单）的 URL 配置。
- 播放列表与流地址由第三方维护，本项目无法控制其内容与可用性，  
  也不对其合法性作任何担保；链接失效、内容变动均与本仓库无关。
- 本项目仅供**学习与技术研究**（流媒体协议、Qt / mpv 集成、数据工程）。  
  请勿用于商业用途；请仅使用你**拥有合法授权**的信号源  
  （例如运营商提供的 IPTV 组播地址、频道官方公开的免费信号）。
- 各地法律对通过互联网转播电视节目的要求不同；在中华人民共和国境内，  
  通过互联网向公众提供视听节目服务属于需要许可的业务。  
  是否合规由使用者自行判断并承担责任。
- 若你是权利人并认为某些地址侵犯你的权益，请直接联系**实际托管该流的服务方**；  
  本仓库不存储这些地址，也无法将其从网络上移除。
- 本说明不构成法律意见。
