# NS-Mirror

一个轻量的 NodeSeek 新帖关键词监控工具。程序直接轮询 NodeSeek 官方 RSS，标题命中关键词后立即推送到 Telegram；关键词可以在 Telegram 中随时增删，不需要登录服务器修改配置。

## 功能

- 直接读取 `https://rss.nodeseek.com/`，无需 NodeSeek 账号、Cookie 或浏览器
- 默认每 5 秒检查一次，支持中文和英文关键词，不区分大小写
- 首次启动只记录最新帖子，不会把历史帖子全部推送一遍
- Telegram 内使用 `/add`、`/del`、`/list` 管理关键词
- 仅响应安装时绑定的 Telegram 用户，忽略其他用户指令
- RSS 与 Telegram 请求失败自动重试，通知发送失败时不推进游标
- 关键词与监控游标原子写入，升级和重启后继续使用
- systemd 开机自启、异常自动重启，并以专用低权限用户运行

## 环境要求

- Debian 11/12/13、Ubuntu 20.04+，或使用 systemd 的 RHEL 系发行版
- root 或 sudo 权限
- 服务器可以访问 `rss.nodeseek.com`、`api.telegram.org` 和 GitHub
- 一个 Telegram Bot Token

> 当前安装脚本依赖 systemd，不支持 Alpine/OpenRC、Docker 容器和 WSL。

## 准备 Telegram Bot

1. 在 Telegram 打开 [@BotFather](https://t.me/BotFather)。
2. 发送 `/newbot`，按提示创建机器人并保存 Bot Token。
3. 给刚创建的机器人发送一条 `/start`，安装程序便可自动识别你的用户 ID。

Bot Token 属于敏感凭据，请勿发布到聊天记录、截图或 GitHub。

## 一键安装

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

## Telegram 指令

| 指令 | 说明 |
|---|---|
| `/start` | 显示欢迎信息与常用指令 |
| `/add 关键词1 关键词2` | 添加一个或多个关键词 |
| `/del 关键词` | 删除关键词 |
| `/list` | 查看当前关键词 |
| `/help` | 显示帮助 |

默认关键词为 `dmit`、`vmiss`、`rfc`。关键词只匹配帖子标题，支持中文，匹配时不区分大小写。

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

| 路径 | 用途 | 权限/维护方式 |
|---|---|---|
| `/opt/nodeseek-watcher/nodeseek_watcher.py` | 主程序 | 安装脚本更新 |
| `/opt/nodeseek-watcher/keywords.json` | 关键词列表 | Telegram 指令更新 |
| `/opt/nodeseek-watcher/state.json` | 最新帖子游标 | 程序自动维护 |
| `/opt/nodeseek-watcher/watcher.env` | Bot Token 与用户 ID | root 专属，权限 `600` |
| `/etc/systemd/system/nodeseek-watcher.service` | systemd 服务 | 安装脚本生成 |

## 测试与排错

发送 Telegram 测试消息：

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

```bash
sudo bash /opt/nodeseek-watcher/uninstall.sh
```

卸载程序会先停止并移除 systemd 服务，然后询问是否删除 Token、关键词和游标。默认选择 `N` 会保留数据，方便以后重新安装。

## 安全说明

- Bot Token 保存在权限为 `600` 的 `watcher.env` 中，不写入代码和 Git 仓库。
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
