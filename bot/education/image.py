"""개념 노트 그림 규칙 (ADR-009).

교육팀장이 그림 설명을 쓰면 교육팀원(Codex)이 이미지 생성으로 그린다. Codex는 읽기 전용이라
그림은 Codex 폴더(`~/.codex/generated_images`)에 생기고, 봇이 실행 전후를 비교해 새로 생긴 PNG만
받아 가로 1024로 줄여 볼트 `Assets/`에 둔다. 노트에는 `## 흐름` 끝에 그림과 한 줄 설명을 넣는다.
"""

import asyncio
import shutil
from pathlib import Path

ASSETS_DIR = "Assets"
IMAGE_WIDTH = 1024  # 볼트에 두는 그림의 가로 (2026-11-03 Zero 결정)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FIGURE_BEFORE = "## 어디서 깨지나"  # 그림은 이 절 바로 앞(= `## 흐름` 끝)에 넣는다
# Obsidian 링크와 파일 이름에 쓸 수 없는 글자
_UNSAFE = str.maketrans({ch: "-" for ch in '/\\:*?"<>|#^[]'})


def asset_path(day: str, label: str) -> str:
    """볼트 기준 그림 경로. 기존 Assets 이름 규칙(`날짜 - 이름`)을 따른다."""
    return f"{ASSETS_DIR}/{day} - {label.translate(_UNSAFE).strip()}.png"


def snapshot(folder: Path) -> frozenset[Path]:
    """Codex 그림 폴더의 지금 PNG 목록. 폴더가 없으면 빈 목록."""
    if not folder.is_dir():
        return frozenset()
    return frozenset(path.resolve() for path in folder.rglob("*.png"))


def check_image(reported: str, folder: Path, before: frozenset[Path]) -> list[str]:
    """교육팀원이 알려 준 경로가 이번 실행에서 새로 생긴 PNG인지 본다. 문제 목록을 돌려준다."""
    path = Path(reported)
    if not path.is_absolute():
        return ["image_path는 절대 경로여야 한다"]
    path = path.resolve()
    if folder.resolve() not in path.parents:
        return [f"image_path는 이미지 생성 도구가 저장한 곳({folder}) 아래여야 한다"]
    if not path.is_file():
        return ["image_path에 파일이 없다. 이미지 생성 도구로 그림을 만들었는지 확인한다"]
    if path in before:
        return ["image_path가 이번에 새로 만든 그림이 아니다"]
    if png_size(path.read_bytes()[:24]) is None:
        return ["image_path가 PNG 그림이 아니다"]
    return []


def png_size(head: bytes) -> tuple[int, int] | None:
    """PNG 머리(24바이트)의 가로·세로. PNG가 아니면 None."""
    if len(head) < 24 or not head.startswith(PNG_SIGNATURE) or head[12:16] != b"IHDR":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def insert_figure(note: str, asset: str, alt: str) -> str:
    """노트의 `## 흐름` 끝에 그림과 한 줄 설명을 넣는다. 절이 없으면 맨 끝에 붙인다."""
    figure = f"![[{asset}]]\n*그림: {alt.strip()}*\n"
    marker = f"\n{FIGURE_BEFORE}"
    if marker not in note:
        return note.rstrip("\n") + "\n\n" + figure
    head, tail = note.split(marker, 1)
    return f"{head.rstrip()}\n\n{figure}{marker}{tail}"


async def shrink_png(source: Path, target: Path, width: int = IMAGE_WIDTH) -> None:
    """가로가 width보다 크면 macOS `sips`로 줄여 저장하고, 아니면 그대로 복사한다."""
    target.parent.mkdir(parents=True, exist_ok=True)
    size = png_size(source.read_bytes()[:24])
    if size is None or size[0] <= width:
        shutil.copyfile(source, target)
        return
    process = await asyncio.create_subprocess_exec(
        "sips",
        "--resampleWidth",
        str(width),
        str(source),
        "--out",
        str(target),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        raise OSError(f"sips 실패: {stderr.decode('utf-8', 'replace').strip()}")
