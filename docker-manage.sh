#!/usr/bin/env bash
# NS-Mirror Docker 管理器：状态、日志、更新、备份、恢复和卸载。
set -Eeuo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/docker/ns-mirror}"
REPO_URL="${REPO_URL:-https://github.com/LYISTR2/NS-Mirror}"
REPO_BRANCH="${REPO_BRANCH:-main}"
COMPOSE_FILE="$INSTALL_DIR/compose.yaml"
ENV_FILE="$INSTALL_DIR/.env"
BACKUP_DIR="$INSTALL_DIR/backups"
CONTAINER_NAME="ns-mirror"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
info() { printf '%b\n' "${GREEN}[✓]${NC} $*"; }
warn() { printf '%b\n' "${YELLOW}[!]${NC} $*"; }
die() { printf '%b\n' "${RED}[✗]${NC} $*" >&2; exit 1; }

usage() {
  cat <<EOF
NS-Mirror Docker 管理器

用法：sudo bash $INSTALL_DIR/docker-manage.sh <命令> [参数]

命令：
  status                  查看容器状态
  logs [行数]             查看日志，默认 100 行
  restart                 重启容器
  update                  从 GitHub 更新代码、备份数据、重新构建
  backup [输出目录]       生成可迁移 tar.gz 与 sha256（含 .env/关键词/游标）
  restore <备份包>        恢复迁移包并启动
  reconfigure             重新输入 Bot Token 与用户 ID
  uninstall [--purge]     移除容器；--purge 同时删除整个项目目录
  help                    显示帮助
EOF
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "请使用 root 权限运行"
}

require_stack() {
  [ -f "$COMPOSE_FILE" ] || die "找不到 $COMPOSE_FILE；请先安装 NS-Mirror Docker 版"
  [ -f "$ENV_FILE" ] || die "找不到 $ENV_FILE；请重新配置"
  command -v docker >/dev/null 2>&1 || die "Docker 不可用"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 不可用"
}

compose() {
  (
    cd "$INSTALL_DIR"
    docker compose --env-file .env "$@"
  )
}

service_status() {
  require_stack
  printf '%s\n' "项目目录：$INSTALL_DIR"
  compose ps
  local state
  state="$(docker inspect -f '{{.State.Status}} running={{.State.Running}} restarting={{.State.Restarting}} exit={{.State.ExitCode}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$CONTAINER_NAME" 2>/dev/null || true)"
  if [ -n "$state" ]; then
    printf '容器状态：%s\n' "$state"
  else
    warn "容器尚未创建"
  fi
}

service_logs() {
  require_stack
  local lines="${1:-100}"
  [[ "$lines" =~ ^[1-9][0-9]{0,4}$ ]] || die "日志行数必须是 1–99999 的整数"
  compose logs --tail="$lines" watcher
}

service_restart() {
  require_stack
  compose restart watcher
  service_status
}

create_backup() {
  require_stack
  local output_dir="${1:-$BACKUP_DIR}"
  local stamp archive basename
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  mkdir -p "$output_dir"
  archive="$output_dir/ns-mirror-backup-${stamp}.tar.gz"
  basename="${archive##*/}"

  tar --numeric-owner -czf "$archive" -C "$INSTALL_DIR" \
    .env compose.yaml Dockerfile .dockerignore nodeseek_watcher.py requirements.txt \
    docker-install.sh docker-manage.sh data
  sha256sum "$archive" > "$archive.sha256"
  tar -tzf "$archive" >/dev/null
  info "备份完成：$archive"
  info "校验文件：$archive.sha256"
  printf '%s\n' "迁移到新服务器后运行：sudo bash docker-manage.sh restore $basename"
}

safe_extract_archive() {
  local archive="$1" destination="$2"
  python3 - "$archive" "$destination" <<'PY'
import os
import sys
import tarfile
from pathlib import Path

archive = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
with tarfile.open(archive, "r:gz") as bundle:
    for member in bundle.getmembers():
        name = Path(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise SystemExit(f"unsafe archive member: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            raise SystemExit(f"unsupported archive member: {member.name}")
    bundle.extractall(destination)
PY
}

install_docker_for_restore() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return
  fi

  info "新服务器缺少 Docker/Compose，开始安装……"
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl docker.io docker-compose-v2 \
      || { apt-get install -y -qq ca-certificates curl docker.io docker-compose-plugin \
        || die "无法通过 APT 安装 Docker Compose 插件"; }
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y docker docker-compose-plugin || die "无法通过 DNF 安装 Docker/Compose"
  elif command -v yum >/dev/null 2>&1; then
    yum install -y docker docker-compose-plugin || die "无法通过 YUM 安装 Docker/Compose"
  else
    die "无法识别包管理器，请先安装 Docker Engine 与 Compose v2"
  fi

  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now docker >/dev/null 2>&1 || true
  fi
  command -v docker >/dev/null 2>&1 || die "Docker 安装失败"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 不可用"
}

restore_backup() {
  local archive="${1:-}"
  [ -n "$archive" ] || die "用法：docker-manage.sh restore <备份包.tar.gz>"
  [ -f "$archive" ] || die "备份包不存在：$archive"
  command -v python3 >/dev/null 2>&1 || die "恢复需要 python3"
  install_docker_for_restore

  if [ -f "$archive.sha256" ]; then
    (cd "$(dirname "$archive")" && sha256sum -c "$(basename "$archive").sha256") \
      || die "备份包 SHA256 校验失败"
  else
    warn "未找到 $archive.sha256，仅执行 tar 完整性检查"
  fi
  tar -tzf "$archive" >/dev/null || die "备份包损坏"

  local temp current_backup=""
  temp="$(mktemp -d /tmp/ns-mirror-restore.XXXXXX)"
  safe_extract_archive "$archive" "$temp"
  for file in .env compose.yaml Dockerfile nodeseek_watcher.py requirements.txt docker-install.sh docker-manage.sh; do
    [ -f "$temp/$file" ] || die "备份包缺少：$file"
  done
  [ -d "$temp/data" ] || die "备份包缺少 data/"

  if [ -f "$COMPOSE_FILE" ] && [ -f "$ENV_FILE" ]; then
    compose down >/dev/null 2>&1 || true
    current_backup="$INSTALL_DIR.pre-restore.$(date -u +%Y%m%d-%H%M%S)"
    cp -a "$INSTALL_DIR" "$current_backup"
    info "已保存恢复前快照：$current_backup"
  fi

  mkdir -p "$INSTALL_DIR"
  cp -a "$temp"/. "$INSTALL_DIR"/
  chmod 600 "$ENV_FILE"
  chown -R 10001:10001 "$INSTALL_DIR/data"
  chmod 750 "$INSTALL_DIR/data"
  chmod 755 "$INSTALL_DIR/docker-install.sh" "$INSTALL_DIR/docker-manage.sh"

  compose config -q
  compose up -d --build --remove-orphans
  rm -rf -- "$temp"
  service_status
  info "恢复完成"
}

update_stack() {
  require_stack
  local token chat_id temp src_dir snapshot
  token="$(sed -n 's/^TG_BOT_TOKEN=//p' "$ENV_FILE" | tail -n 1)"
  chat_id="$(sed -n 's/^TG_CHAT_ID=//p' "$ENV_FILE" | tail -n 1)"
  if [ -z "$token" ] || [ -z "$chat_id" ]; then
    die "现有 .env 不完整，请先 reconfigure"
  fi

  create_backup "$BACKUP_DIR"
  snapshot="$INSTALL_DIR.pre-update.$(date -u +%Y%m%d-%H%M%S)"
  cp -a "$INSTALL_DIR" "$snapshot"
  info "已创建更新回滚快照：$snapshot"

  temp="$(mktemp -d /tmp/ns-mirror-update.XXXXXX)"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 3 --connect-timeout 10 \
    "${REPO_URL}/archive/refs/heads/${REPO_BRANCH}.tar.gz" -o "$temp/repository.tar.gz" \
    || die "下载更新失败"
  tar --no-same-owner --no-same-permissions -xzf "$temp/repository.tar.gz" -C "$temp"
  src_dir="$(find "$temp" -mindepth 2 -maxdepth 3 -type f -name compose.yaml -printf '%h\n' -quit)"
  [ -n "$src_dir" ] || die "更新包中找不到 compose.yaml"

  local file
  for file in Dockerfile compose.yaml .dockerignore nodeseek_watcher.py requirements.txt docker-install.sh docker-manage.sh; do
    [ -f "$src_dir/$file" ] || die "更新包缺少：$file"
  done
  install -m 644 "$src_dir/Dockerfile" "$INSTALL_DIR/Dockerfile"
  install -m 644 "$src_dir/compose.yaml" "$INSTALL_DIR/compose.yaml"
  install -m 644 "$src_dir/.dockerignore" "$INSTALL_DIR/.dockerignore"
  install -m 644 "$src_dir/nodeseek_watcher.py" "$INSTALL_DIR/nodeseek_watcher.py"
  install -m 644 "$src_dir/requirements.txt" "$INSTALL_DIR/requirements.txt"
  install -m 755 "$src_dir/docker-install.sh" "$INSTALL_DIR/docker-install.sh"
  install -m 755 "$src_dir/docker-manage.sh" "$INSTALL_DIR/docker-manage.sh"

  if ! compose config -q || ! compose up -d --build --remove-orphans; then
    warn "更新失败，开始自动回滚"
    compose down >/dev/null 2>&1 || true
    rm -rf -- "$INSTALL_DIR"
    mv "$snapshot" "$INSTALL_DIR"
    compose up -d --build --remove-orphans || true
    die "更新失败，已尝试恢复旧版本"
  fi

  rm -rf -- "$temp"
  rm -rf -- "$snapshot"
  service_status
  info "更新完成；关键词、游标和 Token 均已保留"
}

reconfigure_stack() {
  [ -x "$INSTALL_DIR/docker-install.sh" ] || die "找不到 docker-install.sh"
  exec bash "$INSTALL_DIR/docker-install.sh" --reconfigure
}

uninstall_stack() {
  require_stack
  local purge="${1:-}"
  compose down --remove-orphans
  if [ "$purge" = "--purge" ]; then
    local answer=""
    if [ -r /dev/tty ]; then
      IFS= read -r -p "确认删除 $INSTALL_DIR（含 Token、关键词、游标和备份）？请输入 DELETE: " answer </dev/tty || true
    fi
    [ "$answer" = "DELETE" ] || die "未输入 DELETE，已取消清理数据"
    [ "$INSTALL_DIR" = "/opt/docker/ns-mirror" ] || die "安装目录不是默认路径，拒绝自动 rm -rf：$INSTALL_DIR"
    cd /
    rm -rf -- "$INSTALL_DIR"
    info "容器与项目数据已删除"
  else
    warn "容器已移除，但保留 $INSTALL_DIR；重新安装可继续使用"
  fi
}

require_root
command="${1:-help}"
shift || true
case "$command" in
  status) service_status ;;
  logs) service_logs "${1:-100}" ;;
  restart) service_restart ;;
  update) update_stack ;;
  backup) create_backup "${1:-$BACKUP_DIR}" ;;
  restore) restore_backup "${1:-}" ;;
  reconfigure) reconfigure_stack ;;
  uninstall) uninstall_stack "${1:-}" ;;
  help|-h|--help) usage ;;
  *) usage; die "未知命令：$command" ;;
esac
