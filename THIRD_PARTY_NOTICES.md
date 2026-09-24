# 致谢与第三方组件

本项目站在很多人的肩膀上。这里列出用到的开源项目、数据来源与它们各自的许可证。

> 本文件只做说明，**不改变**任何第三方组件自己的授权条款。
> 各组件的完整许可证文本请见其官方仓库。

---

## 一、运行时依赖

| 组件 | 用途 | 许可证 | 主页 |
|---|---|---|---|
| **mpv / libmpv** | 播放内核（`bin/libmpv-2.dll`） | LGPL-2.1+ 或 GPL-2.0+（**取决于构建**，见下方说明） | <https://mpv.io> |
| **PySide6（Qt for Python）** | 图形界面 | LGPL-3.0-only / GPL-2.0-only / GPL-3.0-only（三者择一） | <https://www.qt.io/qt-for-python> |
| **shiboken6** | PySide6 的绑定生成器运行时 | 同 PySide6 | 同上 |
| **python-mpv** | libmpv 的 Python 绑定 | GPL-2.0+ 或 LGPL-2.1+ | <https://github.com/jaseg/python-mpv> |
| **SQLAlchemy** | ORM / 数据库访问 | MIT | <https://www.sqlalchemy.org> |
| **httpx** | HTTP 客户端（搜刮 / 同步 / 探测） | BSD-3-Clause | <https://www.python-httpx.org> |
| **Python 3.11** | 运行时 | PSF-2.0 | <https://www.python.org> |

`httpx` 的传递依赖（`certifi` MPL-2.0，以及 `h11` / `anyio` / `idna` / `sniffio` 等 MIT / BSD 系组件）
随 pip 自动安装，未在本项目中单独使用。

### 关于 mpv 的许可证（重要）

mpv 本体是 **LGPL-2.1+**；但若构建时启用了 GPL 组件（`--enable-gpl`，例如 x264 / x265 等编码器），
产出的二进制就是 **GPL-2.0+**。

社区常提供的 Windows 构建（shinchiro、zhongfly 的默认产物）**多数启用了 GPL 组件**，
因此那个 `libmpv-2.dll` 通常是 **GPL-2.0+**。zhongfly 另有 `mpv-dev-lgpl-*.7z`，
是不含 GPL 组件的 **LGPL** 版本。

本项目采用 **GPL-3.0-or-later**，与上述两种情形都兼容；
若你想改用更宽松的协议（如 MIT），请把 `bin/libmpv-2.dll` 换成 LGPL 版本。

---

## 二、直播源清单（数据来源）

本项目的「🔄 同步源」会从下面这些**公开清单**读取频道地址。它们由各自的维护者长期维护，
本项目只是消费者 —— **不对其内容、可用性与合法性作任何担保**。
若你是权利人并认为某些地址侵权，请直接联系实际托管该流的服务方。

| 来源 | 说明 | 地址 |
|---|---|---|
| **iptv-org** | 全球公开 IPTV 频道集合（中国区 / 原始流清单） | <https://github.com/iptv-org/iptv> |
| **Kimentanm / aptv** | 央视 + 省级卫视 + 4K 汇总 | <https://github.com/Kimentanm/aptv> |
| **Guovin / iptv-api** | 自动测速后输出的可用源 | <https://github.com/Guovin/iptv-api> |
| **YueChan / Live** | 精选直播源 | <https://github.com/YueChan/Live> |
| **vbskycn / iptv** | IPv4 双栈源 | <https://github.com/vbskycn/iptv> |
| **fanmingming / live** | IPv6 精选源 | <https://github.com/fanmingming/live> |
| **hujingguang / ChinaIPTV** | 自动更新清单 | <https://github.com/hujingguang/ChinaIPTV> |

清单格式解析遵循 **M3U / M3U8** 开放格式，感谢 [RFC 8216](https://datatracker.ietf.org/doc/html/rfc8216)
（HLS）与 [m3u](https://en.wikipedia.org/wiki/M3U) 规范的制定者。

---

## 三、GitHub 加速服务

本机网络环境直连 `raw.githubusercontent.com` 不稳定，因此项目内置了 URL 改写（`liuhaitv/core/mirror.py`），
按顺序尝试下面这些**公益加速服务**。感谢它们的存在与运营：

| 服务 | 形式 | 地址 |
|---|---|---|
| **jsDelivr** | GitHub 仓库文件 CDN | <https://www.jsdelivr.com> |
| **gh-proxy.com** | 前缀式 GitHub 代理 | <https://gh-proxy.com> |
| **ghproxy.net** | 前缀式 GitHub 代理 | <https://ghproxy.net> |
| **ghfast.top** | 前缀式 GitHub 代理 | <https://ghfast.top> |

这些是第三方服务，本项目不控制其可用性，也不对其内容作任何担保。
你可以随时在同步弹窗里把加速方式改成「直连」。

---

## 四、mpv 二进制构建

`libmpv-2.dll` 的 Windows 构建来自社区维护者：

| 构建者 | 地址 |
|---|---|
| **shinchiro**（mpv-winbuild-cmake，官方论坛常用） | <https://github.com/shinchiro/mpv-winbuild-cmake> |
| **zhongfly**（提供 LGPL 变体） | <https://github.com/zhongfly/mpv-winbuild> |
| SourceForge 镜像 | <https://sourceforge.net/projects/mpv-player-windows/files/libmpv/> |

---

## 五、设计上的借鉴

- **IPTV 聚合的一般做法**（只存链接不存内容、提供移除渠道、明确免责声明）
  参考了 `iptv-org/iptv` 的法律声明写法。
- **频道名规范化**（英文台名 → 中文、`CCTV4` → `CCTV-4`）参考了各清单的实际命名习惯。
- **Python + mpv 的集成方式**参考了 `python-mpv` 的官方示例。

---

## 六、开源协议

本项目采用 **GNU General Public License v3.0 or later（GPL-3.0-or-later）**，
全文见仓库根目录的 [`LICENSE`](LICENSE)。

选择它的理由很简单：**运行时链接的 mpv（社区构建）与 PySide6 都是 GPL 兼容授权**，
用 GPL-3.0 是最省事、也最不容易出错的选择。详见上方「关于 mpv 的许可证」。

```
Copyright (C) 2026 LiuHaiTV contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
```
