#!/bin/sh
# 별채 봇을 LaunchAgent로 등록한다 (SPEC 9.3). Zero가 직접 실행한다.
#   설치: scripts/install_launchd.sh
#   해제: scripts/install_launchd.sh --uninstall
# 터미널에서 띄운 봇이 있으면 먼저 끈다 (같은 맥에서 봇은 하나만 뜬다, SPEC 2.1).
set -eu

LABEL=com.zerosync.orchestrator
REPO=$(cd "$(dirname "$0")/.." && pwd)
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$TARGET"
  echo "해제했다: $TARGET"
  exit 0
fi

if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "가상환경이 없다: $REPO/.venv (AGENTS.md의 환경 준비를 먼저 한다)" >&2
  exit 1
fi
mkdir -p "$HOME/.zerosync" "$HOME/Library/LaunchAgents"
sed -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
  "$REPO/deploy/$LABEL.plist" > "$TARGET"
plutil -lint "$TARGET" >/dev/null
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET"
echo "등록했다: $TARGET"
echo "로그: $HOME/.zerosync/bot.err.log"
echo "상태: launchctl print $DOMAIN/$LABEL | head"
