#!/usr/bin/env bash
# 卸载 launchd 服务（不删配置、不删收件箱）。
set -euo pipefail

LABEL="com.imwower.wecom-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo "已卸载 $LABEL（配置与收件箱保留）"
