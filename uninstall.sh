#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="nodeseek-watcher"
SERVICE_USER="nodeseek-watcher"
SERVICE_GROUP="nodeseek-watcher"
INSTALL_DIR="${INSTALL_DIR:-/opt/nodeseek-watcher}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
info() { printf '%b\n' "${GREEN}[✓]${NC} $*"; }
warn() { printf '%b\n' "${YELLOW}[!]${NC} $*"; }
die() { printf '%b\n' "${RED}[✗]${NC} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请使用 root 权限运行：sudo bash uninstall.sh"

systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
rm -f -- "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
info "服务已停止并移除"

answer=""
if [ -r /dev/tty ]; then
  IFS= read -r -p "是否删除 $INSTALL_DIR 内的配置、关键词和游标？[y/N] " answer </dev/tty || true
else
  warn "当前无交互终端，默认保留数据目录 $INSTALL_DIR"
fi

case "$answer" in
  y|Y)
    if [ "$INSTALL_DIR" = "/opt/nodeseek-watcher" ] && [ -d "$INSTALL_DIR" ]; then
      rm -rf -- "$INSTALL_DIR"
      info "已删除数据目录 $INSTALL_DIR"
      userdel "$SERVICE_USER" >/dev/null 2>&1 || true
      groupdel "$SERVICE_GROUP" >/dev/null 2>&1 || true
      info "已删除专用系统用户"
    elif [ "$INSTALL_DIR" = "/opt/nodeseek-watcher" ]; then
      userdel "$SERVICE_USER" >/dev/null 2>&1 || true
      groupdel "$SERVICE_GROUP" >/dev/null 2>&1 || true
      info "数据目录不存在，已清理专用系统用户"
    else
      warn "安装目录不是默认路径，为安全起见未删除目录或专用用户：$INSTALL_DIR"
    fi
    ;;
  *) warn "已保留数据目录和专用用户，重新安装时可继续使用" ;;
esac

info "卸载完成"
