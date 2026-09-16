#!/usr/bin/env bash
# 把本仓库的 skill/ 目录软链到 Claude Code 与 Codex CLI 的 skills 目录。
# 已存在同名目录/软链时只提示，不覆盖。
set -euo pipefail

REPO="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO/skill"
NAME="wecom-bot"

link_one() {
  local target_dir="$1" dest="$1/$NAME"
  if [ ! -d "$target_dir" ]; then
    echo "跳过 $dest（目录 $target_dir 不存在）"
    return
  fi
  if [ -L "$dest" ]; then
    local cur; cur="$(readlink "$dest")"
    if [ "$cur" = "$SRC" ]; then
      echo "已就位 $dest -> $SRC"
    else
      echo "已存在软链 $dest -> $cur（指向别处，未覆盖）" >&2
    fi
    return
  fi
  if [ -e "$dest" ]; then
    echo "已存在 $dest（不是软链，未覆盖）" >&2
    return
  fi
  ln -s "$SRC" "$dest"
  echo "已创建 $dest -> $SRC"
}

link_one "$HOME/.claude/skills"
link_one "$HOME/.codex/skills"

# 可选：把 wecom 放到 PATH 上
BIN_DIR="$HOME/.local/bin"
if [ -d "$BIN_DIR" ]; then
  if [ -L "$BIN_DIR/wecom" ] && [ "$(readlink "$BIN_DIR/wecom")" = "$REPO/bin/wecom" ]; then
    echo "已就位 $BIN_DIR/wecom -> $REPO/bin/wecom"
  elif [ -e "$BIN_DIR/wecom" ]; then
    echo "已存在 $BIN_DIR/wecom（未覆盖）" >&2
  else
    ln -s "$REPO/bin/wecom" "$BIN_DIR/wecom"
    echo "已创建 $BIN_DIR/wecom -> $REPO/bin/wecom"
  fi
fi

echo
echo "完成。Claude / Codex 里都能用 skill: $NAME"
