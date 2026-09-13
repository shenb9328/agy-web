#!/usr/bin/env bash
set -e

echo "🚀 [Agy-Web] 正在安装与配置 Antigravity Web UI 服务..."

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

# 复制服务单元文件
sed "s|%h/agy-web|$DIR|g" "$DIR/antigravity-web.service" > "$SYSTEMD_DIR/antigravity-web.service"

systemctl --user daemon-reload
systemctl --user enable --now antigravity-web.service

echo "✅ [Agy-Web] 安装成功！服务已在后台常驻运行。"
echo "🌐 默认访问地址: http://127.0.0.1:8008"
systemctl --user status antigravity-web.service --no-pager
