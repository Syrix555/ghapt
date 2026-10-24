#!/usr/bin/env bash
# ghapt 服务器端部署脚本(Debian/Ubuntu,以 root 运行)
# 用法: sudo bash setup.sh [--with-nginx]
#   --with-nginx  顺便安装 nginx 并启用 ghapt 站点(会停用 nginx 自带的默认欢迎页站点)
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "请用 root 运行: sudo bash setup.sh" >&2
    exit 1
fi

WITH_NGINX=0
[[ "${1:-}" == "--with-nginx" ]] && WITH_NGINX=1

SRC="$(cd "$(dirname "$0")" && pwd)"
export DEBIAN_FRONTEND=noninteractive

echo "==> 安装依赖 (gnupg, dpkg, python3)"
apt-get update -qq
# dpkg 提供 `dpkg --compare-versions`(版本排序)和 dpkg-deb;最小化镜像上
# 不能假定它们已经存在。zstd 是可选的:只有跟踪 .tar.zst 资产的项目才需要。
apt-get install -y --no-install-recommends gnupg dpkg python3 >/dev/null

echo "==> 检查必需的外部命令"
MISSING=0
for cmd in python3 dpkg dpkg-deb gpg; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "    缺少必需命令: $cmd" >&2
        MISSING=1
    fi
done
if ! command -v zstd >/dev/null 2>&1; then
    echo "    提示: 未安装 zstd —— 只有跟踪 .tar.zst 资产的项目才会用到它"
fi
if [[ $MISSING -eq 1 ]]; then
    echo "依赖不完整,请先安装后重试。" >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
    echo "    python3 版本过低(需要 3.8+): $(python3 -V 2>&1)" >&2
    exit 1
fi

echo "==> 创建 ghapt 用户和目录"
id -u ghapt >/dev/null 2>&1 || useradd --system --home-dir /srv/ghapt --shell /usr/sbin/nologin ghapt
mkdir -p /opt/ghapt /etc/ghapt /srv/ghapt/repo /srv/ghapt/gnupg
install -m 755 "$SRC/ghapt.py" /opt/ghapt/ghapt.py
if [[ ! -f /etc/ghapt/config.ini ]]; then
    install -m 644 "$SRC/config.ini" /etc/ghapt/config.ini
    echo "    已安装默认配置 /etc/ghapt/config.ini(按需编辑)"
fi
touch /etc/ghapt/env
chmod 600 /etc/ghapt/env
chown -R ghapt:ghapt /srv/ghapt
chmod 700 /srv/ghapt/gnupg

echo "==> 安装 systemd 定时任务(每天自动同步)"
install -m 644 "$SRC/systemd/ghapt-sync.service" /etc/systemd/system/
install -m 644 "$SRC/systemd/ghapt-sync.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ghapt-sync.timer

if [[ $WITH_NGINX -eq 1 ]]; then
    echo "==> 安装并配置 nginx"
    apt-get install -y --no-install-recommends nginx >/dev/null
    # 检测是否已有其他启用站点占用 default_server(自带的 default 欢迎页和 ghapt 自己除外)
    CONFLICT=$(grep -lsE 'listen[^;]*default_server' /etc/nginx/sites-enabled/* 2>/dev/null \
               | grep -vE '/(default|ghapt\.conf)$' || true)
    if [[ -n "$CONFLICT" ]]; then
        # 有冲突:去掉 default_server 标记,以非默认站点安装,不影响现有站点
        sed 's/ default_server//' "$SRC/nginx/ghapt.conf" > /etc/nginx/sites-available/ghapt.conf
        chmod 644 /etc/nginx/sites-available/ghapt.conf
        echo "    检测到已有站点占用 default_server:"
        echo "$CONFLICT" | sed 's/^/      /'
        echo "    ghapt 已按非默认站点安装(不影响现有站点)。请编辑 /etc/nginx/sites-available/ghapt.conf,"
        echo "    把 server_name 改成专用域名(或把 listen 改成独立端口),否则访问不到。"
    else
        install -m 644 "$SRC/nginx/ghapt.conf" /etc/nginx/sites-available/ghapt.conf
        if [[ -L /etc/nginx/sites-enabled/default ]]; then
            rm -f /etc/nginx/sites-enabled/default
            echo "    已停用 nginx 自带的默认站点(恢复: ln -s ../sites-available/default /etc/nginx/sites-enabled/)"
        fi
    fi
    ln -sf /etc/nginx/sites-available/ghapt.conf /etc/nginx/sites-enabled/ghapt.conf
    nginx -t && systemctl reload nginx
fi

echo "==> 首次同步(耗时取决于服务器到 GitHub 的下载速度)"
if ! runuser -u ghapt -- python3 /opt/ghapt/ghapt.py sync -c /etc/ghapt/config.ini; then
    echo "首次同步失败(常见原因:服务器访问 GitHub 不畅)。稍后手动重试:" >&2
    echo "  sudo systemctl start ghapt-sync.service && journalctl -u ghapt-sync -f" >&2
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<EOF

════════════════════════════════════════════════════════════════
部署完成。客户端接入(把 ${IP:-<服务器地址>} 换成你的域名或公网 IP):

  curl -fsSL http://${IP:-<服务器地址>}/ghapt.gpg | sudo tee /usr/share/keyrings/ghapt.gpg >/dev/null
  echo "deb [signed-by=/usr/share/keyrings/ghapt.gpg] http://${IP:-<服务器地址>}/ stable main" | sudo tee /etc/apt/sources.list.d/ghapt.list
  sudo apt update

未加 --with-nginx 的话,请自行把 /srv/ghapt/repo 暴露为静态站点(参考 nginx/ghapt.conf)。
════════════════════════════════════════════════════════════════
EOF
