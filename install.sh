#!/usr/bin/env bash
# NS-Mirror / NodeSeek Watcher installer
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/LYISTR2/NS-Mirror}"
REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/nodeseek-watcher}"
SERVICE_NAME="nodeseek-watcher"
SERVICE_USER="nodeseek-watcher"
SERVICE_GROUP="nodeseek-watcher"
PYTHON="${PYTHON:-python3}"
RECONFIGURE=0

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { printf '%b\n' "${GREEN}[✓]${NC} $*"; }
warn() { printf '%b\n' "${YELLOW}[!]${NC} $*"; }
die() { printf '%b\n' "${RED}[✗]${NC} $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
NS-Mirror / NodeSeek Watcher 一键安装脚本

用法：
  sudo bash install.sh [--reconfigure]
  curl -fsSL https://raw.githubusercontent.com/LYISTR2/NS-Mirror/main/install.sh | sudo bash

选项：
  --reconfigure  重新输入 Telegram Bot Token 和用户 ID
  -h, --help     显示帮助

环境变量：
  REPO_BRANCH、INSTALL_DIR、PYTHON、TG_BOT_TOKEN、TG_CHAT_ID
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --reconfigure) RECONFIGURE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（使用 --help 查看帮助）" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "请使用 root 权限运行，例如：sudo bash install.sh"
[ -d /run/systemd/system ] || die "当前系统未运行 systemd，暂不支持 OpenRC、容器或 WSL 环境"

install_dependencies() {
  local need_install=0
  command -v curl >/dev/null 2>&1 || need_install=1
  command -v tar >/dev/null 2>&1 || need_install=1
  command -v "$PYTHON" >/dev/null 2>&1 || need_install=1
  if command -v "$PYTHON" >/dev/null 2>&1; then
    "$PYTHON" -m venv --help >/dev/null 2>&1 || need_install=1
  fi
  [ "$need_install" -eq 0 ] && return

  info "安装系统依赖（curl、tar、Python、venv）……"
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl tar python3 python3-venv
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y ca-certificates curl tar python3
  elif command -v yum >/dev/null 2>&1; then
    yum install -y ca-certificates curl tar python3
  else
    die "无法识别包管理器，请先安装 curl、tar、python3 和 python3-venv"
  fi
}

install_dependencies
command -v curl >/dev/null 2>&1 || die "curl 安装失败"
command -v tar >/dev/null 2>&1 || die "tar 安装失败"
command -v "$PYTHON" >/dev/null 2>&1 || die "python3 安装失败"
"$PYTHON" -m venv --help >/dev/null 2>&1 || die "Python venv 不可用，请安装 python3-venv"
command -v systemctl >/dev/null 2>&1 || die "systemctl 不可用"
command -v useradd >/dev/null 2>&1 || die "useradd 不可用"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
TMP_SRC=""
cleanup() {
  if [ -n "$TMP_SRC" ] && [ -d "$TMP_SRC" ]; then
    rm -rf -- "$TMP_SRC"
  fi
}
trap cleanup EXIT

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/nodeseek_watcher.py" ]; then
  SRC_DIR="$SCRIPT_DIR"
  info "使用本地仓库：$SRC_DIR"
else
  TMP_SRC="$(mktemp -d /tmp/ns-mirror.XXXXXX)"
  TARBALL_URL="${REPO_URL}/archive/refs/heads/${REPO_BRANCH}.tar.gz"
  info "下载 ${REPO_URL}（${REPO_BRANCH}）……"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --connect-timeout 10 \
    "$TARBALL_URL" -o "$TMP_SRC/repository.tar.gz" \
    || die "源码下载失败，请检查网络或分支名"
  tar --no-same-owner --no-same-permissions -xzf "$TMP_SRC/repository.tar.gz" -C "$TMP_SRC" \
    || die "源码解压失败"
  MAIN_FILE="$(find "$TMP_SRC" -mindepth 2 -maxdepth 3 -type f -name nodeseek_watcher.py -print -quit)"
  [ -n "$MAIN_FILE" ] || die "下载内容中未找到 nodeseek_watcher.py"
  SRC_DIR="${MAIN_FILE%/*}"
fi

for required_file in nodeseek_watcher.py requirements.txt keywords.json install.sh uninstall.sh; do
  [ -f "$SRC_DIR/$required_file" ] || die "项目缺少必需文件：$required_file"
done

if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  NOLOGIN_SHELL="$(command -v nologin || printf '/usr/sbin/nologin')"
  useradd --system --gid "$SERVICE_GROUP" --home-dir "$INSTALL_DIR" \
    --shell "$NOLOGIN_SHELL" "$SERVICE_USER"
fi

read_from_tty() {
  local prompt="$1"
  local variable="$2"
  local value=""
  [ -r /dev/tty ] || die "当前没有交互终端；请设置 TG_BOT_TOKEN 与 TG_CHAT_ID 后重试"
  IFS= read -r -p "$prompt" value </dev/tty
  printf -v "$variable" '%s' "$value"
}

existing_value() {
  local key="$1"
  [ -f "$INSTALL_DIR/watcher.env" ] || return 0
  sed -n "s/^${key}=//p" "$INSTALL_DIR/watcher.env" | tail -n 1
}

TOKEN="${TG_BOT_TOKEN:-}"
CHAT_ID="${TG_CHAT_ID:-}"
if [ "$RECONFIGURE" -eq 0 ]; then
  [ -n "$TOKEN" ] || TOKEN="$(existing_value TG_BOT_TOKEN)"
  [ -n "$CHAT_ID" ] || CHAT_ID="$(existing_value TG_CHAT_ID)"
fi

validate_token() {
  local response bot_name
  response="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${TOKEN}/getMe" 2>/dev/null || true)"
  bot_name="$(printf '%s' "$response" | "$PYTHON" -c 'import json,sys
try:
 data=json.load(sys.stdin)
 print(data.get("result", {}).get("username", "") if data.get("ok") else "")
except (ValueError, TypeError):
 pass' 2>/dev/null || true)"
  [ -n "$bot_name" ] || return 1
  BOT_NAME="$bot_name"
}

while :; do
  TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
  if [ -n "$TOKEN" ] && validate_token; then
    info "Telegram Bot 验证成功：@${BOT_NAME}"
    break
  fi
  [ -z "$TOKEN" ] || warn "现有 Bot Token 无效或 Telegram API 暂时不可达"
  read_from_tty "请输入 Telegram Bot Token（从 @BotFather 获取）: " TOKEN
  [ -n "$TOKEN" ] || warn "Token 不能为空"
done

CHAT_ID="$(printf '%s' "$CHAT_ID" | tr -d '[:space:]')"
if [ -z "$CHAT_ID" ]; then
  info "请先在 Telegram 中给 @${BOT_NAME} 发送 /start"
  read_from_tty "请输入 Telegram 用户 ID（直接回车自动识别）: " CHAT_ID
  CHAT_ID="$(printf '%s' "$CHAT_ID" | tr -d '[:space:]')"
fi

if [ -z "$CHAT_ID" ]; then
  info "正在从最近消息中识别你的 Telegram 用户 ID……"
  for attempt in 1 2 3; do
    UPDATES="$(curl -fsS --max-time 10 "https://api.telegram.org/bot${TOKEN}/getUpdates" 2>/dev/null || true)"
    CHAT_ID="$(printf '%s' "$UPDATES" | "$PYTHON" -c 'import json,sys
try:
 data=json.load(sys.stdin)
 for update in reversed(data.get("result", [])):
  message=update.get("message") or update.get("edited_message") or {}
  sender=message.get("from", {})
  if sender.get("id") is not None and not sender.get("is_bot", False):
   print(sender["id"]); break
except (ValueError, TypeError):
 pass' 2>/dev/null || true)"
    [ -n "$CHAT_ID" ] && break
    [ "$attempt" -eq 3 ] || { warn "尚未收到消息，3 秒后重试……"; sleep 3; }
  done
fi
[[ "$CHAT_ID" =~ ^-?[0-9]+$ ]] || die "Telegram 用户 ID 识别失败；请使用 --reconfigure 手动输入纯数字 ID"
info "绑定 Telegram 用户 ID：$CHAT_ID"

install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$INSTALL_DIR"
install -m 644 -o root -g "$SERVICE_GROUP" "$SRC_DIR/nodeseek_watcher.py" "$INSTALL_DIR/"
install -m 644 -o root -g "$SERVICE_GROUP" "$SRC_DIR/requirements.txt" "$INSTALL_DIR/"
install -m 755 -o root -g root "$SRC_DIR/install.sh" "$INSTALL_DIR/"
install -m 755 -o root -g root "$SRC_DIR/uninstall.sh" "$INSTALL_DIR/"

if [ ! -f "$INSTALL_DIR/keywords.json" ]; then
  install -m 600 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$SRC_DIR/keywords.json" "$INSTALL_DIR/"
  info "已初始化默认关键词"
else
  chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/keywords.json"
  chmod 600 "$INSTALL_DIR/keywords.json"
  info "已保留现有关键词"
fi
if [ -f "$INSTALL_DIR/state.json" ]; then
  chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/state.json"
  chmod 600 "$INSTALL_DIR/state.json"
fi

if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
  info "创建 Python 虚拟环境……"
  "$PYTHON" -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --disable-pip-version-check \
  -r "$INSTALL_DIR/requirements.txt" || die "Python 依赖安装失败"
info "Python 依赖安装完成"

umask 077
{
  printf 'TG_BOT_TOKEN=%s\n' "$TOKEN"
  printf 'TG_CHAT_ID=%s\n' "$CHAT_ID"
} > "$INSTALL_DIR/watcher.env"
chown root:root "$INSTALL_DIR/watcher.env"
chmod 600 "$INSTALL_DIR/watcher.env"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=NS-Mirror NodeSeek keyword watcher
Documentation=https://github.com/LYISTR2/NS-Mirror
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/watcher.env
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/nodeseek_watcher.py
Restart=on-failure
RestartSec=10s
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
ReadWritePaths=$INSTALL_DIR

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl is-active --quiet "$SERVICE_NAME" \
  || die "服务启动失败，请运行：journalctl -u $SERVICE_NAME -n 80 --no-pager"
info "systemd 服务已启动"

if TG_BOT_TOKEN="$TOKEN" TG_CHAT_ID="$CHAT_ID" \
  "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/nodeseek_watcher.py" --test; then
  info "Telegram 测试消息已发送"
else
  warn "测试消息发送失败；服务已运行，请查看 journalctl 日志"
fi

printf '\n%b\n' "${GREEN}安装完成${NC}"
printf '%s\n' \
  "服务状态：systemctl status $SERVICE_NAME" \
  "实时日志：journalctl -u $SERVICE_NAME -f" \
  "修改关键词：在 Telegram 中使用 /add、/del、/list" \
  "重新配置：sudo bash $INSTALL_DIR/install.sh --reconfigure" \
  "卸载程序：sudo bash $INSTALL_DIR/uninstall.sh"
