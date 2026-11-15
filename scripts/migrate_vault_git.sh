#!/bin/bash
# 볼트(zerohyeon-labs)의 .git을 iCloud 밖으로 옮긴다. 별채 봇이 도는 Mac에서 실행한다.
#
#   bash migrate_vault_git.sh            # 점검만 한다 (아무것도 바꾸지 않음)
#   bash migrate_vault_git.sh --apply    # 실제로 옮긴다
#   bash migrate_vault_git.sh --apply --cleanup
#                                        # 옮긴 뒤 충돌 사본·유니코드 중복 경로를 정리해 커밋·push한다
#
# 하는 일
#   1. 봇(launchd)을 잠시 멈춘다
#   2. .git 전체를 ~/.zerosync/backup/ 에 tar로 백업한다
#   3. iCloud가 .git 안에 만든 충돌 사본(이름에 " 2")과 refs/.DS_Store를 백업 폴더로 치운다
#   4. git init --separate-git-dir 로 .git을 ~/.zerosync/vault.git 으로 옮긴다
#      (볼트에는 위치를 가리키는 한 줄짜리 .git 파일만 남는다. 봇 코드는 그대로 동작한다)
#   5. (--cleanup) 원본과 내용이 같은 " 2" 충돌 사본과, 같은 파일이 NFC/NFD 두 이름으로
#      들어간 중복 경로를 git에서 지우고 커밋한 뒤 push한다
#   6. 봇을 다시 켠다
#
# 볼트의 md 파일은 iCloud에 그대로 남으므로 iPad ZeroDesk와 Obsidian은 지금처럼 동작한다.
set -euo pipefail

VAULT="${VAULT:-$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/zerohyeon-labs}"
GITDIR="${GITDIR:-$HOME/.zerosync/vault.git}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/.zerosync/backup}"
LABEL="com.zerosync.orchestrator"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

APPLY=0; CLEANUP=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --cleanup) CLEANUP=1 ;;
    *) echo "알 수 없는 옵션: $arg"; exit 2 ;;
  esac
done

say() { printf '\n== %s\n' "$*"; }
die() { printf '\n오류: %s\n' "$*" >&2; exit 1; }

# ---------- 점검 ----------
say "점검"
[ -d "$VAULT" ] || die "볼트가 없다: $VAULT"
cd "$VAULT"
if [ -f .git ]; then
  echo "이미 옮겨져 있다: $(cat .git)"; exit 0
fi
[ -d .git ] || die "볼트에 .git 폴더가 없다"
[ -e "$GITDIR" ] && die "옮길 위치가 이미 있다: $GITDIR"

ICLOUD_JUNK=$(find .git \( -name '* 2' -o -name '* 2.*' -o -name '.DS_Store' \) | sort)
echo "볼트:        $VAULT"
echo "새 git 위치: $GITDIR"
echo "브랜치:      $(git branch --show-current) ($(git status -sb | sed -n 1p))"
echo "커밋 안 된 변경: $(git status --porcelain | wc -l | tr -d ' ')개 (건드리지 않는다)"
echo ".git 안 iCloud 찌꺼기:"; [ -n "$ICLOUD_JUNK" ] && echo "$ICLOUD_JUNK" | sed 's/^/  /' || echo "  없음"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then BOT_RUNNING=1; echo "봇: 실행 중"; else BOT_RUNNING=0; echo "봇: 꺼져 있음"; fi

if [ "$APPLY" -ne 1 ]; then
  echo; echo "점검만 했다. 실제로 옮기려면 --apply 를 붙인다."; exit 0
fi

# ---------- 봇 멈춤 ----------
restart_bot() {
  if [ "$BOT_RUNNING" -eq 1 ]; then
    say "봇 다시 켜기"
    launchctl bootstrap "$DOMAIN" "$PLIST" && echo "켰다"
  fi
}
trap restart_bot EXIT
if [ "$BOT_RUNNING" -eq 1 ]; then
  say "봇 멈추기"
  launchctl bootout "$DOMAIN/$LABEL"
  sleep 3
fi

# ---------- 백업 ----------
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="$BACKUP_ROOT/vault-git-$STAMP"
mkdir -p "$BACKUP"
say "백업: $BACKUP/dot-git.tar.gz"
tar -czf "$BACKUP/dot-git.tar.gz" .git

# ---------- .git 안 찌꺼기 치우기 ----------
if [ -n "$ICLOUD_JUNK" ]; then
  say ".git 안 iCloud 찌꺼기를 백업 폴더로 치움"
  while IFS= read -r f; do
    mkdir -p "$BACKUP/junk/$(dirname "$f")"
    mv "$f" "$BACKUP/junk/$f"
    echo "  $f"
  done <<< "$ICLOUD_JUNK"
fi

# ---------- .git 옮기기 ----------
say ".git → $GITDIR"
mkdir -p "$(dirname "$GITDIR")"
git init -q --separate-git-dir "$GITDIR"
echo "볼트의 .git: $(cat .git)"
git config core.precomposeunicode true
git fsck --no-dangling --no-progress >/dev/null && echo "git 검사 통과"
git status -sb | sed -n 1p

# ---------- 정리 커밋 (선택) ----------
if [ "$CLEANUP" -eq 1 ]; then
  say "충돌 사본·중복 경로 정리"
  git fetch -q origin
  BEHIND=$(git rev-list --count HEAD..@{u})
  [ "$BEHIND" -eq 0 ] || die "GitHub에 이 Mac에 없는 커밋이 $BEHIND개 있다. 먼저 맞춘 뒤 --cleanup 만 다시 실행한다"

  # 1) 원본과 내용이 같은 " 2" 충돌 사본 (.obsidian 설정은 내용이 달라 목록만 보여준다)
  REMOVED=0
  while IFS= read -r -d '' f; do
    base="${f% 2.*}.${f##*.}"
    if [ -f "$base" ] && cmp -s "$f" "$base"; then
      git rm -q --cached -- "$f" 2>/dev/null || true
      mkdir -p "$BACKUP/copies/$(dirname "$f")"; mv "$f" "$BACKUP/copies/$f"
      REMOVED=$((REMOVED + 1))
    else
      echo "  남김(원본과 다름): $f"
    fi
  done < <(find . -path ./.git -prune -o -regex '.* 2\.[A-Za-z]*' -print0)
  echo "  같은 내용 사본 $REMOVED개를 지웠다 (백업: $BACKUP/copies)"

  # 2) 같은 파일이 NFC/NFD 두 이름으로 들어간 경로: NFD 쪽 색인 항목만 지운다
  NFD_COUNT=$(git -c core.quotepath=off ls-files -s -z | /usr/bin/python3 -c '
import sys, unicodedata, collections, subprocess
groups = collections.defaultdict(list)
for rec in sys.stdin.buffer.read().decode().split("\0"):
    if not rec: continue
    meta, path = rec.split("\t", 1)
    groups[unicodedata.normalize("NFC", path)].append((path, meta.split()[1]))
drop = []
for nfc, items in groups.items():
    if len(items) > 1 and len({b for _, b in items}) == 1:
        drop += [p for p, _ in items if p != nfc]
if drop:
    data = "".join(f"0 0000000000000000000000000000000000000000\t{p}\0" for p in drop)
    subprocess.run(["git", "update-index", "-z", "--index-info"], input=data.encode(), check=True)
print(len(drop))')
  echo "  NFC/NFD 중복 경로 $NFD_COUNT개를 정리했다"

  if ! git diff --cached --quiet; then
    # 색인에 올린 것은 위의 정리뿐이다. 커밋 안 된 다른 변경은 색인에 올리지 않았으므로 섞이지 않는다
    git commit -q -m "chore(vault): iCloud 충돌 사본과 유니코드 중복 경로 정리"
    git push -q origin HEAD && echo "  커밋하고 GitHub에 올렸다: $(git log --oneline -1)"
  else
    echo "  정리할 것이 없었다"
  fi
fi

say "완료. 백업은 $BACKUP 에 있다. 문제가 없으면 나중에 지워도 된다"
