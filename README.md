# NS-Mirror

监控 NodeSeek 新帖标题，命中关键词后推送到 Telegram。

- Linux，需 `curl`；使用 root 运行（普通用户先执行 `sudo -i`）。
- 在 [@BotFather](https://t.me/BotFather) 创建机器人，保存 Token，并给机器人发送 `/start`；安装时按提示填写。

## Docker 安装（推荐）

```bash
curl -fsSLO https://raw.githubusercontent.com/LYISTR2/NS-Mirror/main/docker-install.sh && bash docker-install.sh
```

自动安装缺失的 Docker / Compose。常用管理命令：

```bash
bash /opt/docker/ns-mirror/docker-manage.sh status       # 状态
bash /opt/docker/ns-mirror/docker-manage.sh logs         # 日志
bash /opt/docker/ns-mirror/docker-manage.sh restart      # 重启
bash /opt/docker/ns-mirror/docker-manage.sh update       # 更新
bash /opt/docker/ns-mirror/docker-manage.sh reconfigure  # 更换机器人或用户
bash /opt/docker/ns-mirror/docker-manage.sh backup       # 备份
bash /opt/docker/ns-mirror/docker-manage.sh uninstall    # 卸载，保留数据
```

配置与数据位于 `/opt/docker/ns-mirror/`。备份含 Bot Token，请妥善保管；`uninstall --purge` 会删除整个目录。

## systemd 安装（不使用 Docker）

需系统运行 systemd；与 Docker 方式二选一：

```bash
curl -fsSLO https://raw.githubusercontent.com/LYISTR2/NS-Mirror/main/install.sh && bash install.sh
```

再次执行上面的命令即可更新。常用管理命令：

```bash
systemctl status nodeseek-watcher
journalctl -u nodeseek-watcher -f
systemctl restart nodeseek-watcher
bash /opt/nodeseek-watcher/install.sh --reconfigure
bash /opt/nodeseek-watcher/uninstall.sh
```

配置与数据位于 `/opt/nodeseek-watcher/`；卸载时可选择保留。

## Telegram 使用

- `/panel`：打开控制面板，添加、删除关键词，暂停或恢复推送。
- `/add 词1 词2`、`/del 词`、`/list`：管理关键词。
- `/block 词`、`/unblock 词`：屏蔽或取消屏蔽。
- `/pause`、`/resume`：暂停或恢复；暂停期间的新帖不会补发。

只匹配标题，不区分大小写。首次启动不推送历史帖子。Bot Token 不要公开，也不要同时运行同一机器人的多个实例。

[MIT License](LICENSE)
