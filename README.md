# Antigravity Web UI (`agy-web`)

🚀 **极轻量、纯原生直驱、双向免同步的 Google Antigravity CLI 图形化 Web 工作台**。

无需配置第三方代理或中转服务，直接调用宿主机 `/usr/local/bin/agy` 原生执行引擎，实时直读并持久化沉淀于本地 `~/.gemini/antigravity-cli/` 数据结构中。

---

## 🌟 核心特性

- **🌳 树状多层级项目与会话管理**：
  - 左侧原生展示宿主机所有实际项目（如 `光储token机`、`飞书`、`腾讯云`、`ai旅居`、`ai实验室` 等）；
  - **双重时间倒序**：项目文件夹与各文件夹内的会话列表均严格按照**最新活跃时间从新到旧**智能排序，最新操作永远置顶。
- **🎯 项目感知与归属自由选择**：
  - 顶栏与新建会话界面提供直观的 **“所属项目” 下拉选择器**；
  - 发起新对话时可自由指定目标项目，对话自动绑定并沉淀到对应项目目录下。
- **🔒 多用户安全隔离与独立账户环境**：
  - 支持多用户密码登录（加盐 SHA-256 哈希加密存储于本地硬盘，严禁前端暴露）；
  - 登录态通过安全 HTTPOnly Cookie 保持；
  - 不同用户登录后自动映射到各自独立的 CLI 启动程序（如 `agy9328` / `agy93091028`）与专属数据目录，数据互不交叉、配额互不干扰。
- **⚡ 100% 纯原生 CLI 直驱（零代理架构）**：
  - 彻底摆脱 `agy-proxy` 或任何第三方 API 中转；
  - 后端直接拉起 `/usr/local/bin/agy -p ... --output-format stream-json` 管道，毫秒级流式吐字输出；
  - 原生支持 Gemini 3.8 Flash、Gemini 3.1 Pro、Claude Sonnet 4.6 (Thinking) 等高阶模型。
- **🔄 双向免同步体系（单一数据源 Single Source of Truth）**：
  - 历史会话直接读取对应用户的 `conversation_summaries.db`；
  - 完整聊天记录与思考链（Thinking Chain）直接解析 `brain/<id>/.../transcript.jsonl`；
  - 在 Web 上的对话立即生效于终端，在终端敲 `agy` 产生的记录 Web 刷新立即可见。
- **🪶 极致轻量化**：
  - 纯 Python 3 原生标准库实现，常驻内存仅 **~10MB ~ 20MB**，CPU 平时为 0.0%；
  - 即使在 1GB 内存的微型 VPS 上也游刃有余，彻底告别臃肿的 Docker 镜像。
- **🎨 现代化交互质感**：
  - 媲美 Claude / ChatGPT / Antigravity 官方 App 的暗黑科技风排版；
  - 完备的 Markdown 渲染、代码高亮与右上角一键复制；
  - 思考过程（Thinking）折叠手风琴面板；
  - 响应式设计，完美支持移动端抽屉与桌面端双栏布局。

---

## 📁 目录结构

```text
agy-web/
├── server.py                 # 原生直驱多用户轻量 Web 后端 (纯 Python 3 标准库)
├── index.html                # 现代化单页前端 (含多用户登录弹窗与账户状态栏)
├── users.example.json        # 账户配置安全模板 (真实 users.json 独立保存并加入 gitignore)
├── antigravity-web.service   # Systemd User 守护进程模板
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

## 🌐 访问与安全配置

- **本地访问**：浏览器打开 `http://localhost:8008`
- **SSH 端口转发隧道**（推荐）：
  ```bash
  ssh -L 8008:127.0.0.1:8008 <user>@<server-ip>
  ```
  在本地浏览器直接打开 `http://localhost:8008`。
- **反向代理（Caddy / Nginx）**：
  通过反代可直接配置免端口的公网访问或配置全绿锁 HTTPS 证书。

---

## 📄 开源许可

[MIT License](LICENSE)
