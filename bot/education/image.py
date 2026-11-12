"""개념 노트 그림 규칙 (ADR-009).

교육팀장이 그림 설명과 한글 이름표를 쓰면 교육팀원(Codex)이 이미지 생성으로 그린다. Codex는 읽기
전용이라 그림은 Codex 폴더(`~/.codex/generated_images`)에 생기고, 봇이 실행 전후를 비교해 새로
생긴 PNG만 받는다. 이름표가 그림에 모두 있는지 macOS 글자 인식(Vision)으로 확인하고, 가로 1024로
줄여 볼트 `Assets/`에 둔다. 노트에는 그림마다 정한 절 제목 바로 아래(설명 위)에 그림과 한 줄 설명을
넣는다. 그림을 먼저 보고 아래 글로 확인하게 한다 (2026-11-12 Zero 결정).
"""

import asyncio
import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from bot.schemas import ConceptFigure

ASSETS_DIR = "Assets"
IMAGE_WIDTH = 1024  # 볼트에 두는 그림의 가로 (2026-11-03 Zero 결정)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FIGURE_BEFORE = "## 어디서 깨지나"  # 절 제목을 못 찾으면 이 절 바로 앞(= `## 흐름` 끝)에 넣는다
NO_FIGURE = "## 비유"  # 비유 장면은 그리지 않는다 (사람·캐릭터 금지)
# Obsidian 링크와 파일 이름에 쓸 수 없는 글자
_UNSAFE = str.maketrans({ch: "-" for ch in '/\\:*?"<>|#^[]'})
OCR_SCRIPT = Path(__file__).with_name("ocr.swift")
# 글자 인식 비교에서 빼는 것: 공백과 가운뎃점 모양들 (인식기가 `·`를 `•`로 읽기도 한다)
_IGNORED = re.compile(r"[\s·•・‧.∙]")


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


def insert_figure(note: str, section: str, asset: str, alt: str) -> str:
    """절 제목 줄 바로 아래에 그림과 한 줄 설명을 넣는다.

    제목이 없으면 `## 흐름` 끝, 그것도 없으면 맨 끝에 붙인다.
    """
    figure = f"![[{asset}]]\n*그림: {alt.strip()}*\n"
    lines = note.split("\n")
    if section.strip() in lines:
        at = lines.index(section.strip()) + 1
        rest = "\n".join(lines[at:]).lstrip("\n")
        return "\n".join(lines[:at]) + f"\n\n{figure}\n{rest}"
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


def check_figures(body: str, figures: Sequence[ConceptFigure]) -> list[str]:
    """교육팀장이 쓴 그림 설명들의 형식 문제.

    절마다 한 장, 이름표는 겹치지 않고 설명에서 자리가 정해져야 한다.
    """
    problems = []
    lines = {line.strip() for line in body.splitlines()}
    sections = [figure.section for figure in figures]
    if len(set(sections)) != len(sections):
        problems.append("figures에 같은 절(section)이 두 번 있다. 절마다 그림은 한 장이다")
    for n, figure in enumerate(figures, start=1):
        where = f"figures[{n}]"
        if figure.section not in lines:
            problems.append(
                f"{where}.section '{figure.section}'이 본문 제목 줄과 글자 그대로 같지 않다"
            )
        if figure.section == NO_FIGURE:
            problems.append(f"{where}: 비유 절에는 그림을 붙이지 않는다")
        if len(set(figure.labels)) != len(figure.labels):
            problems.append(f"{where}.labels에 같은 이름표가 두 번 있다")
        for label in figure.labels:
            if label not in figure.brief:
                problems.append(
                    f"{where}.brief에 이름표 '{label}'가 붙을 요소·화살표를 적지 않았다"
                )
        if any("\n" in label for label in figure.labels):
            problems.append(f"{where}.labels의 이름표는 한 줄이어야 한다")
    return problems


def missing_labels(labels: Sequence[str], recognized: Sequence[str]) -> list[str]:
    """그림에서 읽힌 글자에 없는 이름표. 공백과 가운뎃점 모양은 비교하지 않는다."""
    seen = _IGNORED.sub("", "".join(recognized))
    return [label for label in labels if _IGNORED.sub("", label) not in seen]


async def read_text(image: Path) -> list[str]:
    """macOS 내장 글자 인식으로 그림 속 글자를 줄 단위로 읽는다. 실패하면 OSError."""
    process = await asyncio.create_subprocess_exec(
        "swift",
        str(OCR_SCRIPT),
        str(image),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise OSError(f"글자 인식 실패: {stderr.decode('utf-8', 'replace').strip()[:300]}")
    return [line for line in stdout.decode("utf-8", "replace").splitlines() if line.strip()]
