#!/usr/bin/env bash
# 把 wecom-daemon 装成 launchd 常驻服务（开机自启、异常自动重启）。
set -euo pipefail

REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.imwower.wecom-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -f "$HOME/.config/wecom-bot/config.json" ]; then
  echo "错误：还没有配置。先执行：$REPO/bin/wecom setup --bot-id <ID> --secret <SECRET>" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/wecom-bot"

# 已经用 wecom daemon start 起了的话，先停掉，避免两个实例抢长连接
"$REPO/bin/wecom" daemon stop >/dev/null 2>&1 || true

sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" -e "s|__LABEL__|$LABEL|g" \
    "$REPO/launchd/$LABEL.plist.template" > "$PLIST"
chmod 644 "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true

echo "已安装 $PLIST"
echo "查看状态： launchctl print gui/$(id -u)/$LABEL | head -20"
echo "日志：     $HOME/Library/Logs/wecom-bot/daemon.log"
sleep 2
"$REPO/bin/wecom" status || true
