# Antigravity Web UI (`agy-web`)

🚀 **极轻量、纯原生直驱、双向实时同步、支持 PWA 原生应用安装的 Google Antigravity CLI 图形化 Web 工作台**。

无需配置第三方商业代理或中转服务，支持直接调用宿主机语言服务（Connect-RPC）与原生 `/usr/local/bin/agy` 执行引擎，与 Google Antigravity Remote / CLI 会话实现 100% 双向实时互通。

---

## 🌟 核心特性

- **📱 PWA 原生应用与移动端/折叠屏全屏沉浸支持**：
  - 符合 W3C PWA 规范，支持在 Android（Chrome/Edge/各大手机浏览器）与 iOS（Safari）上直接「**安装为应用 / 添加到主屏幕**」；
  - 启动后拥有独立原生窗口，自动隐藏浏览器地址栏与导航条；
  - 全面支持 `env(safe-area-inset-top)` 与 `env(safe-area-inset-bottom)` 安全区域，完美适配刘海屏、挖孔屏、以及**折叠屏外屏与展开内屏**；
  - 配备高清自适应蒙版图标（Maskable Icons）与专属 Service Worker 缓存，极速启动。

- **🛡️ 系统命令执行确认卡片与「⚡ 自动执行」**：
  - 当智能体执行系统 Shell 命令（如检查内存、进程、查看磁盘等）时，Web 界面实时截获交互状态，弹出**交互式确认卡片**；
  - 支持直接在浏览器内操作：**「允许执行 (本次)」**、**「本会话始终允许」** 或 **「拒绝执行」**；
  - 顶栏配备 **「⚡ 自动执行」** 开关：开启后自动放行所有系统命令，无需每次手动确认。

- **🔄 双向实时同步体系（与 Google Antigravity Remote 完全互通）**：
  - 直连 Antigravity Language Server 守护进程（Connect-RPC 协议）；
  - **真正的双向同步**：在 Web 上的对话实时同步到 Google Antigravity Remote / CLI 终端；在 Remote 或终端发起的任务，Web 界面立即可见；
  - 守护进程未启动时，具备**自适应平滑降级**机制，自动回退到本地 CLI 管道直驱模式。

- **📊 Google 官方实时模型配额看板**：
  - 直连 Google CloudCode API 查询账户真实可用配额；
  - 实时掌握 Gemini 3.8 Flash、Gemini 3.1 Pro、Claude 3.7 / 4.6 Sonnet 等各模型的剩余百分比与配额刷新倒计时。

- **🌳 树状多层级项目与会话管理**：
  - 左侧原生展示宿主机所有实际项目（如 `光储token机`、`飞书`、`腾讯云`、`ai旅居`、`ai实验室` 等）；
  - **双重时间倒序**：项目文件夹与各文件夹内的会话列表严格按照**最新活跃时间从新到旧**智能排序，最新操作置顶显示。

- **🔒 多用户安全隔离与独立账户环境**：
  - 支持多用户密码登录（加盐 SHA-256 哈希加密存储于本地硬盘，严禁前端暴露）；
  - 登录态通过安全 HTTPOnly Cookie 保持；
  - 不同用户登录后自动映射到各自独立的 CLI 启动程序（如 `agy9328` / `agy93091028`）与专属数据目录，数据互不交叉、配额互不干扰。

- **🪶 极致轻量化**：
  - 纯 Python 3 原生标准库实现，常驻内存仅 **~15MB**，CPU 平时为 0.0%；
  - 即使在 1GB 内存的微型 VPS 上也游刃有余，彻底告别臃肿的 Docker 容器。

- **🎨 现代化交互体验**：
  - 媲美 Claude / ChatGPT / 官方 App 的暗黑科技风排版；
  - Markdown 渲染、代码高亮与右上角一键复制；
  - 思考过程（Thinking Chain）折叠手风琴面板。

---

## 📁 目录结构

```text
agy-web/
├── server.py                 # 原生双模后端 (Connect-RPC 实时同步 + CLI 降级，纯 Python 3 标准库)
├── index.html                # 现代化响应式单页前端 (支持 PWA、命令确认、配额看板)
├── manifest.json             # W3C Web App PWA 清单规范
├── sw.js                     # PWA Service Worker 离线缓存与网络穿透
├── icons/                    # 高清应用图标 (180/192/512 及 Android Maskable 自适应图标)
│   ├── icon.svg
│   ├── icon-180.png
│   ├── icon-192.png
│   ├── icon-512.png
│   ├── icon-maskable-192.png
│   └── icon-maskable-512.png
├── users.example.json        # 账户配置安全模板 (真实 users.json 独立保存并加入 gitignore)
├── antigravity-web.service   # Systemd User 守护进程配置
├── install.sh                # 一键部署与开机自启安装脚本
├── .gitignore                # 排除本地数据库、用户密码配置与缓存
└── README.md                 # 项目详细指南
```

---

## 🚀 快速启动

### 方式一：直接运行

```bash
# 默认监听 0.0.0.0:8008
python3 server.py
```

自定义端口与主机：
```bash
AGY_WEB_PORT=5000 AGY_WEB_HOST=127.0.0.1 python3 server.py
```

### 方式二：Systemd 守护进程常驻（推荐）

运行自带的一键安装脚本：
```bash
./install.sh
```

常用服务管理命令：
```bash
# 查看状态
systemctl --user status antigravity-web.service

# 查看实时日志
journalctl --user -u antigravity-web.service -f

# 重启 / 停止服务
systemctl --user restart antigravity-web.service
systemctl --user stop antigravity-web.service
```

---

## 📱 安装为手机 App (PWA)

1. **Android / 折叠屏（Chrome、Edge 等浏览器）**：
   - 打开网页，点击页面顶栏或侧边栏底部的 **「📱 安装 App」** 按钮；
   - 或点击浏览器菜单 **「⋮」 -> 选择「安装应用」/「添加到主屏幕」**；
   - 桌面生成专属独立 App 图标，全屏无边框运行。
2. **iPhone / iPad（Safari 浏览器）**：
   - 打开网页，点击底部工具栏 **「分享」** 图标（向上箭头方框）；
   - 选择 **「添加到主屏幕」** 并确认添加即可。

---

## 🌐 访问与安全配置

- **本地访问**：浏览器打开 `http://localhost:8008`
- **SSH 端口转发隧道**（推荐）：
  ```bash
  ssh -L 8008:127.0.0.1:8008 <user>@<server-ip>
  ```
  在本地浏览器直接打开 `http://localhost:8008`。
- **反向代理（Caddy / Nginx）**：
  可配置域名公网访问并自动申请 HTTPS 证书。

---

## 📄 开源许可

[MIT License](LICENSE)
