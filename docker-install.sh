#!/usr/bin/env bash
# NS-Mirror Docker 一键安装器：先采集 Telegram 配置，再安装 Docker 并启动。
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/LYISTR2/NS-Mirror}"
REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/docker/ns-mirror}"
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
NS-Mirror Docker 一键安装器

用法：
  bash <(curl -fsSL https://raw.githubusercontent.com/LYISTR2/NS-Mirror/main/docker-install.sh)
  sudo bash docker-install.sh [--reconfigure]

选项：
  --reconfigure  重新输入 Bot Token 与 Telegram 用户 ID
  -h, --help     显示帮助

环境变量：
  REPO_URL、REPO_BRANCH、INSTALL_DIR、TG_BOT_TOKEN、TG_CHAT_ID
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

[ "$(id -u)" -eq 0 ] || die "请使用 root 权限运行，例如：sudo bash docker-install.sh"

TMP_SRC=""
cleanup() {
  if [ -n "$TMP_SRC" ] && [ -d "$TMP_SRC" ]; then
    rm -rf -- "$TMP_SRC"
  fi
}
trap cleanup EXIT

read_from_tty() {
  local prompt="$1"
  local variable="$2"
  local secret="${3:-0}"
  local value=""
  [ -r /dev/tty ] || die "当前没有交互终端；请设置 TG_BOT_TOKEN 与 TG_CHAT_ID 后重试"
  if [ "$secret" -eq 1 ]; then
    IFS= read -r -s -p "$prompt" value </dev/tty
    printf '\n' >/dev/tty
  else
    IFS= read -r -p "$prompt" value </dev/tty
  fi
  printf -v "$variable" '%s' "$value"
}

existing_value() {
  local key="$1"
  [ -f "$INSTALL_DIR/.env" ] || return 0
  sed -n "s/^${key}=//p" "$INSTALL_DIR/.env" | tail -n 1
}

TOKEN="${TG_BOT_TOKEN:-}"
CHAT_ID="${TG_CHAT_ID:-}"
if [ "$RECONFIGURE" -eq 0 ]; then
  [ -n "$TOKEN" ] || TOKEN="$(existing_value TG_BOT_TOKEN)"
  [ -n "$CHAT_ID" ] || CHAT_ID="$(existing_value TG_CHAT_ID)"
fi
BOT_NAME=""

validate_token() {
  local response bot_name
  response="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${TOKEN}/getMe" 2>/dev/null || true)"
  bot_name="$(printf '%s' "$response" | python3 -c 'import json,sys
try:
 data=json.load(sys.stdin)
 print(data.get("result", {}).get("username", "") if data.get("ok") else "")
except (ValueError, TypeError):
 pass' 2>/dev/null || true)"
  [ -n "$bot_name" ] || return 1
  BOT_NAME="$bot_name"
}

install_input_helpers() {
  if command -v curl >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
    return
  fi

  info "安装 Bot 配置校验所需的 curl 与 python3（尚未安装 Docker）……"
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl python3
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y ca-certificates curl python3
  elif command -v yum >/dev/null 2>&1; then
    yum install -y ca-certificates curl python3
  else
    die "无法识别包管理器，请先安装 curl 与 python3"
  fi
}

collect_credentials() {
  install_input_helpers
  command -v curl >/dev/null 2>&1 || die "curl 安装失败"
  command -v python3 >/dev/null 2>&1 || die "python3 安装失败"

  while :; do
    TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
    if [ -n "$TOKEN" ] && validate_token; then
      info "Telegram Bot 验证成功：@${BOT_NAME}"
      break
    fi
    [ -z "$TOKEN" ] || warn "现有 Bot Token 无效或 Telegram API 暂时不可达"
    read_from_tty "请输入 Telegram Bot Token（输入内容不会显示）: " TOKEN 1
    [ -n "$TOKEN" ] || warn "Token 不能为空"
  done

  CHAT_ID="$(printf '%s' "$CHAT_ID" | tr -d '[:space:]')"
  if [ -z "$CHAT_ID" ]; then
    info "请先在 Telegram 中给 @${BOT_NAME} 发送 /start"
    read_from_tty "请输入 Telegram 用户 ID（直接回车自动识别）: " CHAT_ID
    CHAT_ID="$(printf '%s' "$CHAT_ID" | tr -d '[:space:]')"
  fi

  if [ -z "$CHAT_ID" ]; then
    info "正在从最近消息中识别 Telegram 用户 ID……"
    local updates="" attempt
    for attempt in 1 2 3; do
      updates="$(curl -fsS --max-time 10 "https://api.telegram.org/bot${TOKEN}/getUpdates" 2>/dev/null || true)"
      CHAT_ID="$(printf '%s' "$updates" | python3 -c 'import json,sys
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

  [[ "$CHAT_ID" =~ ^-?[0-9]+$ ]] || die "Telegram 用户 ID 识别失败；请重新运行并手动输入纯数字 ID"
  info "绑定 Telegram 用户 ID：$CHAT_ID"
}

compose_available() {
  docker compose version >/dev/null 2>&1
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && compose_available; then
    info "Docker 与 Compose 已安装"
    return
  fi

  info "安装 Docker Engine 与 Compose 插件……"
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl docker.io docker-compose-v2 \
      || { apt-get install -y -qq ca-certificates curl docker.io docker-compose-plugin \
        || die "无法通过 APT 安装 Docker Compose 插件"; }
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y docker docker-compose-plugin \
      || die "无法通过 DNF 安装 Docker/Compose"
  elif command -v yum >/dev/null 2>&1; then
    yum install -y docker docker-compose-plugin \
      || die "无法通过 YUM 安装 Docker/Compose"
  else
    die "无法识别包管理器，请按 Docker 官方文档安装 Docker Engine 与 Compose v2"
  fi

  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now docker >/dev/null 2>&1 || true
  fi
  command -v docker >/dev/null 2>&1 || die "Docker 安装失败"
  compose_available || die "Docker Compose v2 不可用，请安装 compose 插件后重试"
}

fetch_sources() {
  local script_dir=""
  local resolved_source="${BASH_SOURCE[0]}"
  if [[ "$resolved_source" == /dev/fd/* || "$resolved_source" == /proc/self/fd/* ]]; then
    script_dir=""
  else
    script_dir="$(cd "$(dirname "$resolved_source")" 2>/dev/null && pwd)" || script_dir=""
  fi
  if [ -n "$script_dir" ] && [ -f "$script_dir/compose.yaml" ] && [ -f "$script_dir/Dockerfile" ]; then
    SRC_DIR="$script_dir"
    info "使用本地仓库：$SRC_DIR"
    return
  fi

  TMP_SRC="$(mktemp -d /tmp/ns-mirror-docker.XXXXXX)"
  info "下载 ${REPO_URL}（${REPO_BRANCH}）……"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --connect-timeout 10 \
    "${REPO_URL}/archive/refs/heads/${REPO_BRANCH}.tar.gz" -o "$TMP_SRC/repository.tar.gz" \
    || die "源码下载失败，请检查网络或分支名"
  tar --no-same-owner --no-same-permissions -xzf "$TMP_SRC/repository.tar.gz" -C "$TMP_SRC" \
    || die "源码解压失败"
  local compose_file=""
  compose_file="$(find "$TMP_SRC" -mindepth 2 -maxdepth 3 -type f -name compose.yaml -print -quit)"
  [ -n "$compose_file" ] || die "下载内容中未找到 compose.yaml"
  SRC_DIR="${compose_file%/*}"
}

install_sources() {
  local file
  install -d -m 755 "$INSTALL_DIR" "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
  for file in Dockerfile compose.yaml .dockerignore nodeseek_watcher.py requirements.txt keywords.json docker-install.sh docker-manage.sh; do
    [ -f "$SRC_DIR/$file" ] || die "项目缺少必需文件：$file"
  done

  install -m 644 "$SRC_DIR/Dockerfile" "$INSTALL_DIR/Dockerfile"
  install -m 644 "$SRC_DIR/compose.yaml" "$INSTALL_DIR/compose.yaml"
  install -m 644 "$SRC_DIR/.dockerignore" "$INSTALL_DIR/.dockerignore"
  install -m 644 "$SRC_DIR/nodeseek_watcher.py" "$INSTALL_DIR/nodeseek_watcher.py"
  install -m 644 "$SRC_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
  install -m 755 "$SRC_DIR/docker-install.sh" "$INSTALL_DIR/docker-install.sh"
  install -m 755 "$SRC_DIR/docker-manage.sh" "$INSTALL_DIR/docker-manage.sh"
  if [ ! -f "$INSTALL_DIR/data/keywords.json" ]; then
    install -m 644 "$SRC_DIR/keywords.json" "$INSTALL_DIR/data/keywords.json"
    info "已初始化默认关键词"
  else
    info "已保留现有关键词和帖子游标"
  fi
  chown -R 10001:10001 "$INSTALL_DIR/data"
  chmod 750 "$INSTALL_DIR/data" "$INSTALL_DIR/backups"
}

write_env() {
  umask 077
  {
    printf 'TG_BOT_TOKEN=%s\n' "$TOKEN"
    printf 'TG_CHAT_ID=%s\n' "$CHAT_ID"
  } > "$INSTALL_DIR/.env"
  chmod 600 "$INSTALL_DIR/.env"
}

start_stack() {
  info "构建并启动 NS-Mirror 容器……"
  (
    cd "$INSTALL_DIR"
    docker compose config -q
    docker compose up -d --build --remove-orphans
  )

  local status=""
  for _ in 1 2 3 4 5; do
    status="$(docker inspect -f '{{.State.Status}} {{.State.Running}} {{.State.Restarting}} {{.State.ExitCode}}' ns-mirror 2>/dev/null || true)"
    case "$status" in
      "running true false 0") break ;;
    esac
    sleep 2
  done
  [ "$status" = "running true false 0" ] || {
    (
      cd "$INSTALL_DIR"
      docker compose --env-file .env logs --tail=80 watcher || true
    )
    die "容器启动失败"
  }
  info "容器已启动"

  if (cd "$INSTALL_DIR" && docker compose --env-file .env exec -T watcher \
    python /app/nodeseek_watcher.py --test); then
    info "Telegram 测试消息已发送"
  else
    warn "测试消息发送失败；请查看容器日志"
  fi
}

collect_credentials
install_docker
fetch_sources
install_sources
write_env
start_stack

printf '\n%b\n' "${GREEN}Docker 安装完成${NC}"
printf '%s\n' \
  "项目目录：$INSTALL_DIR" \
  "管理入口：sudo bash $INSTALL_DIR/docker-manage.sh" \
  "查看状态：sudo bash $INSTALL_DIR/docker-manage.sh status" \
  "查看日志：sudo bash $INSTALL_DIR/docker-manage.sh logs" \
  "更新程序：sudo bash $INSTALL_DIR/docker-manage.sh update" \
  "迁移备份：sudo bash $INSTALL_DIR/docker-manage.sh backup" \
  "重新配置：sudo bash $INSTALL_DIR/docker-install.sh --reconfigure"
