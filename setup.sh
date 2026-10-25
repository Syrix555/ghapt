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
for path in /opt/ghapt /etc/ghapt /srv/ghapt /srv/ghapt/repo /srv/ghapt/gnupg; do
    if [[ -L "$path" ]]; then
        echo "$path 不能是符号链接" >&2
        exit 1
    fi
    if [[ -e "$path" && ! -d "$path" ]]; then
        echo "$path 已存在但不是目录" >&2
        exit 1
    fi
done
mkdir -p /opt/ghapt /etc/ghapt /srv/ghapt/repo /srv/ghapt/gnupg
install -m 755 "$SRC/ghapt.py" /opt/ghapt/ghapt.py
if [[ -L /etc/ghapt/config.ini ]]; then
    echo "/etc/ghapt/config.ini 不能是符号链接" >&2
    exit 1
fi
if [[ -e /etc/ghapt/config.ini && ! -f /etc/ghapt/config.ini ]]; then
    echo "/etc/ghapt/config.ini 已存在但不是普通文件" >&2
    exit 1
fi
if [[ ! -e /etc/ghapt/config.ini ]]; then
    install -o ghapt -g ghapt -m 600 "$SRC/config.ini" /etc/ghapt/config.ini
    echo "    已安装默认配置 /etc/ghapt/config.ini(按需编辑)"
else
    chown ghapt:ghapt /etc/ghapt/config.ini
    chmod 600 /etc/ghapt/config.ini
fi
if [[ -L /etc/ghapt/env ]]; then
    echo "/etc/ghapt/env 不能是符号链接" >&2
    exit 1
fi
if [[ -e /etc/ghapt/env && ! -f /etc/ghapt/env ]]; then
    echo "/etc/ghapt/env 已存在但不是普通文件" >&2
    exit 1
fi
if [[ ! -e /etc/ghapt/env ]]; then
    install -o ghapt -g ghapt -m 600 /dev/null /etc/ghapt/env
else
    chown ghapt:ghapt /etc/ghapt/env
fi
chmod 600 /etc/ghapt/env
# Do not follow repository-internal symlinks into another filesystem or
# outside /srv/ghapt while fixing ownership.
find -P /srv/ghapt -xdev -exec chown --no-dereference ghapt:ghapt {} +
chmod 700 /srv/ghapt/gnupg

echo "==> 安装 systemd 定时任务(每天自动同步)"
install -m 644 "$SRC/systemd/ghapt-sync.service" /etc/systemd/system/
install -m 644 "$SRC/systemd/ghapt-sync.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ghapt-sync.timer

if [[ $WITH_NGINX -eq 1 ]]; then
    echo "==> 安装并配置 nginx"
    apt-get install -y --no-install-recommends nginx >/dev/null
    NGINX_AVAILABLE=/etc/nginx/sites-available
    NGINX_ENABLED=/etc/nginx/sites-enabled
    NGINX_CONFIG="$NGINX_AVAILABLE/ghapt.conf"
    NGINX_LINK="$NGINX_ENABLED/ghapt.conf"
    NGINX_DEFAULT="$NGINX_ENABLED/default"

    for dir in "$NGINX_AVAILABLE" "$NGINX_ENABLED"; do
        if [[ -L "$dir" || ! -d "$dir" ]]; then
            echo "$dir 必须是普通目录,不能是符号链接或其他文件" >&2
            exit 1
        fi
    done
    if [[ -L "$NGINX_CONFIG" || ( -e "$NGINX_CONFIG" && ! -f "$NGINX_CONFIG" ) ]]; then
        echo "$NGINX_CONFIG 必须是普通文件" >&2
        exit 1
    fi
    if [[ -L "$NGINX_LINK" ]]; then
        NGINX_LINK_TARGET=$(readlink -- "$NGINX_LINK")
        if [[ "$NGINX_LINK_TARGET" != "../sites-available/ghapt.conf" \
              && "$NGINX_LINK_TARGET" != "$NGINX_CONFIG" ]]; then
            echo "$NGINX_LINK 是未知符号链接,拒绝覆盖" >&2
            exit 1
        fi
    elif [[ -e "$NGINX_LINK" ]]; then
        echo "$NGINX_LINK 已存在但不是允许的符号链接,拒绝覆盖" >&2
        exit 1
    fi

    DEFAULT_IS_STANDARD=0
    if [[ -L "$NGINX_DEFAULT" ]]; then
        DEFAULT_TARGET=$(readlink -- "$NGINX_DEFAULT")
        if [[ "$DEFAULT_TARGET" == "../sites-available/default" \
              || "$DEFAULT_TARGET" == "/etc/nginx/sites-available/default" ]]; then
            DEFAULT_IS_STANDARD=1
        fi
    fi

    # 检测已有站点的 default_server。标准 nginx default 软链稍后会安全停用；
    # 普通文件和未知软链都视为冲突，避免未经确认覆盖现有入口。
    CONFLICT=""
    for site in "$NGINX_ENABLED"/*; do
        [[ -e "$site" || -L "$site" ]] || continue
        [[ "$site" == "$NGINX_LINK" ]] && continue
        if [[ "$site" == "$NGINX_DEFAULT" && $DEFAULT_IS_STANDARD -eq 1 ]]; then
            continue
        fi
        if grep -qE 'listen[^;]*default_server' "$site" 2>/dev/null; then
            CONFLICT+="$site"$'\n'
        elif [[ "$site" == "$NGINX_DEFAULT" ]]; then
            CONFLICT+="$site"$'\n'
        fi
    done

    if ! ROLLBACK_DIR=$(mktemp -d /tmp/ghapt-nginx-rollback.XXXXXX); then
        echo "无法创建 nginx 回滚目录" >&2
        exit 1
    fi
    OLD_CONFIG=0
    if [[ -e "$NGINX_CONFIG" ]]; then
        if ! cp -a -- "$NGINX_CONFIG" "$ROLLBACK_DIR/ghapt.conf"; then
            echo "无法备份现有 nginx 配置,停止安装" >&2
            rm -rf -- "$ROLLBACK_DIR"
            exit 1
        fi
        OLD_CONFIG=1
    fi
    OLD_LINK=0
    OLD_LINK_TARGET=""
    if [[ -L "$NGINX_LINK" ]]; then
        OLD_LINK=1
        OLD_LINK_TARGET=$(readlink -- "$NGINX_LINK")
    fi
    OLD_DEFAULT=0
    OLD_DEFAULT_TARGET=""
    if [[ -L "$NGINX_DEFAULT" && $DEFAULT_IS_STANDARD -eq 1 ]]; then
        OLD_DEFAULT=1
        OLD_DEFAULT_TARGET=$(readlink -- "$NGINX_DEFAULT")
    fi

    cleanup_nginx_temps() {
        local status=0
        if [[ -n "${NGINX_TMP:-}" ]] \
           && [[ -e "$NGINX_TMP" || -L "$NGINX_TMP" ]]; then
            if ! rm -f -- "$NGINX_TMP"; then
                echo "无法清理 nginx 临时配置: $NGINX_TMP" >&2
                status=1
            fi
        fi
        if [[ -n "${LINK_TMP_DIR:-}" ]] \
           && [[ -e "$LINK_TMP_DIR" || -L "$LINK_TMP_DIR" ]]; then
            if ! rm -rf -- "$LINK_TMP_DIR"; then
                echo "无法清理 nginx 临时软链目录: $LINK_TMP_DIR" >&2
                status=1
            fi
        fi
        if [[ -n "${ROLLBACK_DIR:-}" ]] \
           && [[ -e "$ROLLBACK_DIR" || -L "$ROLLBACK_DIR" ]]; then
            if ! rm -rf -- "$ROLLBACK_DIR"; then
                echo "无法清理 nginx 回滚目录: $ROLLBACK_DIR" >&2
                status=1
            fi
        fi
        return "$status"
    }

    restore_nginx_files() {
        local status=0 restore_tmp=""
        if [[ $OLD_CONFIG -eq 1 ]]; then
            if restore_tmp=$(mktemp "$NGINX_AVAILABLE/.ghapt-restore.XXXXXX") \
               && cp -a -- "$ROLLBACK_DIR/ghapt.conf" "$restore_tmp" \
               && chown root:root "$restore_tmp" \
               && chmod 644 "$restore_tmp" \
               && mv -fT -- "$restore_tmp" "$NGINX_CONFIG"; then
                :
            else
                echo "无法恢复 $NGINX_CONFIG" >&2
                status=1
                [[ -z "$restore_tmp" ]] || rm -f -- "$restore_tmp" || true
            fi
        else
            if ! rm -f -- "$NGINX_CONFIG"; then
                echo "无法删除新安装的 $NGINX_CONFIG" >&2
                status=1
            fi
        fi
        if [[ $OLD_LINK -eq 1 ]]; then
            if ! rm -f -- "$NGINX_LINK" \
                 || ! ln -s -- "$OLD_LINK_TARGET" "$NGINX_LINK"; then
                echo "无法恢复 $NGINX_LINK" >&2
                status=1
            fi
        else
            if ! rm -f -- "$NGINX_LINK"; then
                echo "无法删除新安装的 $NGINX_LINK" >&2
                status=1
            fi
        fi
        if [[ $OLD_DEFAULT -eq 1 ]]; then
            if ! rm -f -- "$NGINX_DEFAULT" \
                 || ! ln -s -- "$OLD_DEFAULT_TARGET" "$NGINX_DEFAULT"; then
                echo "无法恢复 $NGINX_DEFAULT" >&2
                status=1
            fi
        fi
        return "$status"
    }

    nginx_failure() {
        local reason=$1 rollback_status=0
        echo "$reason" >&2
        if ! restore_nginx_files; then
            rollback_status=1
        fi
        if ! cleanup_nginx_temps; then
            rollback_status=1
        fi
        if [[ $rollback_status -ne 0 ]]; then
            echo "nginx 回滚未完全成功,请检查 /etc/nginx/sites-available 和 sites-enabled" >&2
        elif ! nginx -t; then
            echo "nginx 回滚后配置测试仍失败,请手工修复 nginx 配置" >&2
        else
            echo "nginx 已恢复原配置" >&2
        fi
        exit 1
    }

    NGINX_TMP=""
    LINK_TMP_DIR=""
    if ! NGINX_TMP=$(mktemp "$NGINX_AVAILABLE/.ghapt-config.XXXXXX"); then
        rm -rf -- "$ROLLBACK_DIR"
        echo "无法创建 nginx 临时配置" >&2
        exit 1
    fi
    if [[ -n "$CONFLICT" ]]; then
        # 有冲突:去掉 default_server 标记,以非默认站点安装,不影响现有站点。
        if ! sed 's/ default_server//g' "$SRC/nginx/ghapt.conf" > "$NGINX_TMP"; then
            nginx_failure "生成 nginx 配置失败"
        fi
        echo "    检测到已有站点占用 default_server:"
        echo "$CONFLICT" | sed 's/^/      /'
        echo "    ghapt 已按非默认站点安装(不影响现有站点)。请编辑 $NGINX_CONFIG,"
        echo "    把 server_name 改成专用域名(或把 listen 改成独立端口),否则访问不到。"
    else
        if ! cat "$SRC/nginx/ghapt.conf" > "$NGINX_TMP"; then
            nginx_failure "读取 nginx 配置模板失败"
        fi
        if [[ $DEFAULT_IS_STANDARD -eq 1 ]]; then
            if ! rm -f -- "$NGINX_DEFAULT"; then
                nginx_failure "停用 nginx 默认站点失败"
            fi
            echo "    已停用 nginx 自带的默认站点(失败时会自动恢复)"
        fi
    fi
    if ! chown root:root "$NGINX_TMP" \
       || ! chmod 644 "$NGINX_TMP" \
       || ! mv -fT -- "$NGINX_TMP" "$NGINX_CONFIG"; then
        nginx_failure "安装 nginx 配置失败"
    fi

    if [[ $OLD_LINK -eq 0 ]]; then
        if ! LINK_TMP_DIR=$(mktemp -d "$NGINX_ENABLED/.ghapt-link.XXXXXX") \
           || ! ln -s -- "../sites-available/ghapt.conf" \
                  "$LINK_TMP_DIR/ghapt.conf" \
           || ! mv -fT -- "$LINK_TMP_DIR/ghapt.conf" "$NGINX_LINK" \
           || ! rmdir -- "$LINK_TMP_DIR"; then
            nginx_failure "安装 nginx 站点软链失败"
        fi
    fi

    if ! nginx -t; then
        nginx_failure "nginx 配置测试失败,恢复原配置和站点链接"
    fi
    if ! systemctl reload nginx; then
        nginx_failure "nginx reload 失败,恢复原配置和站点链接"
    fi
    if ! cleanup_nginx_temps; then
        echo "nginx 已 reload,但临时文件清理失败,请手工检查" >&2
        exit 1
    fi
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
