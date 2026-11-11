"""별채 v2 봇.

Zero의 명령을 받아 에이전트(Claude, Codex)를 호출하고, 정해진 JSON 출력을 검증해
GitHub과 Obsidian 볼트에 옮겨 적는 결정론적 프로그램이다. 규칙은 docs/운영-규약.md를 따른다.

워크플로 로직은 Discord에 묶지 않는다. Discord 입출력은 bot.discord_io에만 둔다.
"""

BOT_NAME = "별채"
__version__ = "0.0.0"
