"""별직원 원화로 Discord 프로필 사진 만들기 (ADR-012 15팀 41명과 별채, ADR-015).

원화(`output/characters/원화/<팀>/<이름>.png`)에서 캐릭터 영역을 찾아 가운데에 두고,
원화 배경색으로 채운 정사각형 512px PNG를 `assets/avatars/<별 이름>.png`로 저장한다.
Discord가 프로필 사진을 원으로 자르므로 캐릭터 둘레에 여백을 둔다.

봇 의존성이 아니라서 Pillow를 따로 설치해 실행한다.

    pip install pillow
    python scripts/make_avatars.py
"""

import argparse
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "output" / "characters" / "원화"
OUT = ROOT / "assets" / "avatars"
SAVE = 512
MARGIN = 1.3  # 캐릭터 영역의 긴 변 대비 사진 한 변. 원으로 잘려도 캐릭터가 남게
THRESHOLD = 28  # 배경색과 이만큼 다르면 캐릭터로 본다

# 원화 파일 이름(한글) → 사진 파일 이름(별 이름 영문 소문자)
NAMES = {
    "별채": "byeolchae",  # [별채] 게시와 봇 계정 아이콘 (2026-11-19)
    "폴라리스": "polaris",
    "두베": "dubhe",
    "투반": "thuban",
    "미라": "mira",
    "카펠라": "capella",
    "카노푸스": "canopus",
    "프로키온": "procyon",
    "데네브": "deneb",
    "레굴루스": "regulus",
    "메라크": "merak",
    "카스토르": "castor",
    "베가": "vega",
    "스피카": "spica",
    "포말하우트": "fomalhaut",
    "아르크투루스": "arcturus",
    "알데바란": "aldebaran",
    "시리우스": "sirius",
    "폴룩스": "pollux",
    "알비레오": "albireo",
    "알타이르": "altair",
    "베텔게우스": "betelgeuse",
    "리겔": "rigel",
    "알니탁": "alnitak",
    "민타카": "mintaka",
    "알닐람": "alnilam",
    "벨라트릭스": "bellatrix",
    "미자르": "mizar",
    "알코르": "alcor",
    "샤울라": "shaula",
    "피콕": "peacock",
    "아크룩스": "acrux",
    "하다르": "hadar",
    "알페카": "alphecca",
    "엘나스": "elnath",
    "아케르나르": "achernar",
    "안카": "ankaa",
    "안타레스": "antares",
    "데네볼라": "denebola",
    "사드르": "sadr",
    "코카브": "kochab",
    "알카이드": "alkaid",
}


def background(image: Image.Image) -> tuple[int, int, int]:
    """네 모서리 색의 중간값을 배경색으로 본다."""
    w, h = image.size
    corners = [image.getpixel(p) for p in ((2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3))]
    return tuple(sorted(c[i] for c in corners)[1] for i in range(3))


def avatar(path: Path) -> Image.Image:
    image = Image.open(path).convert("RGB")
    bg = background(image)
    diff = ImageChops.difference(image, Image.new("RGB", image.size, bg)).convert("L")
    box = diff.point(lambda v: 255 if v > THRESHOLD else 0).getbbox() or (0, 0, *image.size)
    left, top, right, bottom = box
    side = int(max(right - left, bottom - top) * MARGIN)
    canvas = Image.new("RGB", (side, side), bg)
    crop = image.crop(box)
    canvas.paste(crop, ((side - crop.width) // 2, (side - crop.height) // 2))
    return canvas.resize((SAVE, SAVE), Image.LANCZOS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    seen = set()
    for path in sorted(args.source.glob("*/*.png")):
        key = NAMES.get(path.stem)
        if key is None:
            raise SystemExit(f"사진 파일 이름이 정해지지 않은 원화: {path}")
        avatar(path).save(args.out / f"{key}.png", optimize=True)
        seen.add(path.stem)
    missing = sorted(set(NAMES) - seen)
    if missing:
        raise SystemExit(f"원화가 없는 직원: {', '.join(missing)}")
    print(f"{len(seen)}장의 사진을 {args.out}에 만들었다")


if __name__ == "__main__":
    main()
