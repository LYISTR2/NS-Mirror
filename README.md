# NS-Mirror

一个轻量的 NodeSeek 新帖关键词监控工具。程序直接轮询 NodeSeek 官方 RSS，标题命中关键词后立即推送到 Telegram；关键词可以在 Telegram 中随时增删，不需要登录服务器修改配置。

## 功能

- 直接读取 `https://rss.nodeseek.com/`，无需 NodeSeek 账号、Cookie 或浏览器
- 默认每 5 秒检查一次，支持中文和英文关键词，不区分大小写
- 首次启动只记录最新帖子，不会把历史帖子全部推送一遍
- Telegram 内联控制面板管理关键词，无需记忆指令
- 点击按钮添加、删除、刷新关键词，保留 `/add`、`/del`、`/list` 兼容指令
- 仅响应安装时绑定的 Telegram 用户，忽略其他用户指令
- 新帖命中通知使用 Telegram MarkdownV2 排版，清晰展示命中关键词与发布时间
- RSS 与 Telegram 请求失败自动重试，通知发送失败时不推进游标
- 关键词与监控游标原子写入，升级和重启后继续使用
- systemd 开机自启、异常自动重启，并以专用低权限用户运行

## 环境要求

- **Docker 方式（推荐）**：Debian、Ubuntu、RHEL 系 Linux；已有 Docker 可直接使用，没有时一键脚本会安装 Docker Engine 与 Compose v2
- **systemd 方式（兼容保留）**：Debian 11/12/13、Ubuntu 20.04+，或使用 systemd 的 RHEL 系发行版
- root 或 sudo 权限
- 服务器可以访问 `rss.nodeseek.com`、`api.telegram.org` 和 GitHub
- 一个 Telegram Bot Token

> Docker 版不开放任何端口，只有出站网络请求；配置和运行数据集中在 `/opt/docker/ns-mirror/`，适合整目录备份、迁移和恢复。

## Docker 一键安装（推荐）

安装脚本会**先让你输入并验证 Bot Token、识别 Telegram 用户 ID，然后才安装 Docker**。Token 输入时不会回显，也不会作为命令参数写入 Shell 历史。

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/LYISTR2/NS-Mirror/main/docker-install.sh)
```

脚本会自动完成：

1. 验证 Bot Token，并自动识别或读取 Telegram 用户 ID；
2. 检测 Docker 与 Compose v2，缺失时使用当前系统包管理器安装；
3. 将项目部署到 `/opt/docker/ns-mirror/`；
4. 构建非 root、只读文件系统容器；
5. 将关键词与帖子游标持久化到 `/opt/docker/ns-mirror/data/`；
6. 启动容器并发送 Telegram 测试消息。

### Docker 管理

```bash
sudo bash /opt/docker/ns-mirror/docker-manage.sh status
sudo bash /opt/docker/ns-mirror/docker-manage.sh logs
sudo bash /opt/docker/ns-mirror/docker-manage.sh restart
sudo bash /opt/docker/ns-mirror/docker-manage.sh update
sudo bash /opt/docker/ns-mirror/docker-manage.sh reconfigure
```

### 一键备份与迁移

在旧服务器生成迁移包：

```bash
sudo bash /opt/docker/ns-mirror/docker-manage.sh backup
```

会在 `/opt/docker/ns-mirror/backups/` 生成：

```text
ns-mirror-backup-YYYYMMDD-HHMMSS.tar.gz
ns-mirror-backup-YYYYMMDD-HHMMSS.tar.gz.sha256
```

迁移包包含 Docker 配置、Bot Token、Telegram 用户 ID、关键词和帖子游标。它等同于敏感凭据备份，传输后应限制权限，迁移完成后及时删除不需要的副本。

在新服务器放好迁移包与校验文件后执行；如果尚未安装 Docker/Compose，恢复脚本会自动安装：

```bash
sudo env INSTALL_DIR=/opt/docker/ns-mirror bash docker-manage.sh restore ./ns-mirror-backup-YYYYMMDD-HHMMSS.tar.gz
```

如果新服务器还没有管理脚本，可先下载：

```bash
curl -fsSL https://raw.githubusercontent.com/LYISTR2/NS-Mirror/main/docker-manage.sh -o docker-manage.sh
chmod +x docker-manage.sh
```

恢复程序会校验 SHA256（存在校验文件时）、检查归档路径安全、备份当前目录、恢复数据、重新构建并启动容器。

## 准备 Telegram Bot

1. 在 Telegram 打开 [@BotFather](https://t.me/BotFather)。
2. 发送 `/newbot`，按提示创建机器人并保存 Bot Token。
3. 给刚创建的机器人发送一条 `/start`，安装程序便可自动识别你的用户 ID。

Bot Token 属于敏感凭据，请勿发布到聊天记录、截图或 GitHub。

## systemd 一键安装（兼容方式）

```bash
curl -fsSL https://raw.githubusercontent.com/LYISTR2/NS-Mirror/main/install.sh | sudo bash
```

脚本会自动完成：

1. 安装 curl、Python 3、venv 等系统依赖；
2. 下载当前仓库源码；
3. 校验 Telegram Bot Token，并识别或读取用户 ID；
4. 安装到 `/opt/nodeseek-watcher`；
5. 创建专用低权限用户和经过加固的 systemd 服务；
6. 启动服务并发送一条测试消息。

安装过程中需要在终端输入 Token。脚本从 `/dev/tty` 读取输入，因此上面的管道命令仍可正常交互，Token 不会作为命令参数写入 Shell 历史。

### 先审查再安装

```bash
curl -fsSL https://raw.githubusercontent.com/LYISTR2/NS-Mirror/main/install.sh -o install.sh
less install.sh
sudo bash install.sh
```

### 手动克隆安装

```bash
git clone https://github.com/LYISTR2/NS-Mirror.git
cd NS-Mirror
sudo bash install.sh
```

## 升级与重新配置

再次执行一键安装命令即可升级。安装程序默认保留：

- `keywords.json`：关键词列表
- `state.json`：已处理帖子的游标
- `watcher.env`：Telegram Token 与用户 ID

需要更换机器人或绑定用户时运行：

```bash
sudo bash /opt/nodeseek-watcher/install.sh --reconfigure
```

如果安装目录里没有 `install.sh`，直接重新执行顶部的一键安装命令并添加环境变量 `TG_BOT_TOKEN`、`TG_CHAT_ID`，或从仓库重新克隆后执行 `sudo bash install.sh --reconfigure`。

## Telegram 控制面板

服务启动时会通过 Telegram Bot API 自动注册命令菜单。打开机器人聊天后，点击输入框旁的 **“/”菜单按钮**，即可选择：

- `/panel`：打开关键词控制面板
- `/add`：添加关键词
- `/del`：删除关键词
- `/list`：查看关键词
- `/help`：查看帮助

也可以直接发送 `/start` 或 `/panel` 打开内联控制面板：

- **添加关键词**：点击“➕ 添加关键词”，然后直接发送文字；多个词用空格分隔
- **取消输入**：添加过程中可点击“取消”，也可直接发送任意 `/` 指令退出输入状态
- **删除关键词**：关键词按钮每行最多显示 4 个，点击对应的“❌ 关键词”即可删除
- **刷新**：重新读取当前关键词并更新同一条面板消息
- **帮助**：显示简短操作说明

所有按钮和文字输入仅接受安装时绑定的 Telegram 用户，其他用户无法操作。

### 兼容指令

| 指令 | 说明 |
|---|---|
| `/start`、`/panel` | 打开内联控制面板 |
| `/add 关键词1 关键词2` | 添加一个或多个关键词 |
| `/del 关键词` | 删除关键词 |
| `/list` | 查看当前关键词 |
| `/help` | 显示帮助 |

默认关键词为 `dmit`、`vmiss`、`rfc`。关键词只匹配帖子标题，支持中文，匹配时不区分大小写。

## 新帖通知格式

标题命中关键词的新帖推送使用 **Telegram MarkdownV2** 格式，例如：

```text
*NS / NEW POST　№ 852452*

[DeepSeek V4 Flash 额度与价格调整讨论](https://www.nodeseek.com/post-852452-1)

> 命中关键词：`deepseek`
> 发布时间：`2026-08-07 15:30`
```

- 第一行为固定标题，`№` 后是帖子 ID；
- 第二行为帖子标题链接，点击直达原帖；
- 引用块中展示命中的关键词与 RSS 发布时间（格式 `YYYY-MM-DD HH:MM`，统一为**北京时间 UTC+8**；解析失败时保留原文）。

帖子标题、关键词、时间、链接中的动态内容会按 MarkdownV2 规则转义（标题中的 `_ * [ ] ( )` 等特殊字符不会破坏格式，链接中的反斜杠与右括号也会被正确处理；关键词与时间置于 code 代码块内，其中的反斜杠与反引号会额外转义，避免破坏 code 实体）。控制面板、帮助和指令回复等管理消息仍使用 HTML 格式发送，两者互不影响。

## 服务管理

```bash
sudo systemctl status nodeseek-watcher
sudo journalctl -u nodeseek-watcher -f
sudo systemctl restart nodeseek-watcher
sudo systemctl stop nodeseek-watcher
```

查看最近 100 行日志：

```bash
sudo journalctl -u nodeseek-watcher -n 100 --no-pager
```

## 文件位置

### Docker 版

| 路径 | 用途 |
|---|---|
| `/opt/docker/ns-mirror/compose.yaml` | Compose 配置 |
| `/opt/docker/ns-mirror/.env` | Bot Token 与用户 ID，权限 `600` |
| `/opt/docker/ns-mirror/data/keywords.json` | 关键词列表 |
| `/opt/docker/ns-mirror/data/state.json` | 最新帖子游标 |
| `/opt/docker/ns-mirror/backups/` | 一键迁移备份 |
| `/opt/docker/ns-mirror/docker-manage.sh` | 状态、日志、更新、备份、恢复、卸载 |

### systemd 版

| 路径 | 用途 | 权限/维护方式 |
|---|---|---|
| `/opt/nodeseek-watcher/nodeseek_watcher.py` | 主程序 | 安装脚本更新 |
| `/opt/nodeseek-watcher/keywords.json` | 关键词列表 | Telegram 指令更新 |
| `/opt/nodeseek-watcher/state.json` | 最新帖子游标 | 程序自动维护 |
| `/opt/nodeseek-watcher/watcher.env` | Bot Token 与用户 ID | root 专属，权限 `600` |
| `/etc/systemd/system/nodeseek-watcher.service` | systemd 服务 | 安装脚本生成 |

## 测试与排错

Docker 版查看日志和发送测试消息：

```bash
sudo bash /opt/docker/ns-mirror/docker-manage.sh logs
sudo bash -c 'cd /opt/docker/ns-mirror && docker compose --env-file .env exec -T watcher python /app/nodeseek_watcher.py --test'
```

systemd 版发送测试消息：

```bash
sudo bash -c 'set -a; source /opt/nodeseek-watcher/watcher.env; set +a; /opt/nodeseek-watcher/venv/bin/python /opt/nodeseek-watcher/nodeseek_watcher.py --test'
```

手动执行一次 RSS 检查：

```bash
sudo systemctl stop nodeseek-watcher
sudo bash -c 'set -a; source /opt/nodeseek-watcher/watcher.env; set +a; exec runuser -u nodeseek-watcher --preserve-environment -- /opt/nodeseek-watcher/venv/bin/python /opt/nodeseek-watcher/nodeseek_watcher.py --once'
sudo systemctl start nodeseek-watcher
```

常见问题：

- `401 Unauthorized`：Bot Token 无效，请使用 `--reconfigure` 重新配置。
- 自动识别用户 ID 失败：先给机器人发送 `/start`，然后重新安装；也可以手动填写纯数字 ID。
- 服务启动失败：运行 `journalctl -u nodeseek-watcher -n 100 --no-pager` 查看原因。
- RSS 暂时不可用：程序会自动重试，不会因抓取失败推进帖子游标。
- 完全没有推送：使用 `/list` 检查关键词，并确认目标词出现在帖子标题中。

## 卸载

Docker 版（默认保留整个项目目录）：

```bash
sudo bash /opt/docker/ns-mirror/docker-manage.sh uninstall
```

同时删除 Token、关键词、游标和备份：

```bash
sudo bash /opt/docker/ns-mirror/docker-manage.sh uninstall --purge
```

systemd 版：

```bash
sudo bash /opt/nodeseek-watcher/uninstall.sh
```

卸载程序会先停止并移除 systemd 服务，然后询问是否删除 Token、关键词和游标。默认选择 `N` 会保留数据，方便以后重新安装。

## 安全说明

- Bot Token 保存在权限为 `600` 的 `watcher.env` 中，不写入代码和 Git 仓库。
- Docker 版将 Token 保存为权限 `600` 的 `.env`，容器不映射端口、使用非 root 用户、只读根文件系统、移除全部 Linux capabilities。
- 服务以独立的 `nodeseek-watcher` 系统用户运行，不使用 root 常驻。
- systemd 启用了 `NoNewPrivileges`、文件系统保护、空 Capability 集等限制。
- Telegram 管理指令仅接受绑定用户 ID；其他聊天发送的消息会被确认游标后忽略。
- RSS 请求强制直连，不继承服务器的 `HTTP_PROXY` 或 `HTTPS_PROXY`。

## 工作原理

```text
NodeSeek RSS → 解析帖子 ID/标题 → 关键词匹配 → Telegram 推送
                                      ↕
                              Telegram 指令管理关键词
```

## 许可证

[MIT License](LICENSE)
