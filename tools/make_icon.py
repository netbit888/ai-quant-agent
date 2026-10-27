# -*- coding: utf-8 -*-
"""把 assets/aqa_src.png 母版转成 Windows 多尺寸 assets/aqa.ico。

用法（用隔离 venv，不装进项目 .venv）：
    C:\\Users\\35688\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tools/make_icon.py

流程：
1. 自动检测底板 bbox（背景色差异阈值）并裁到板材边缘 —— AI 出的图右下角常带水印，
   把留白一起裁掉，水印自然消失，不留修补痕迹
2. 生成 256/128/64/48/32/24/16 七档，小尺寸轻微锐化+提对比（缩放糊是小图标第一杀手）
3. 额外输出 assets/_preview_small.png：小尺寸并排放大图，用来看 16px 到底认不认得出
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SRC = ASSETS / "aqa_src.png"      # AI 原始输出（保留留白+水印）
MASTER = ASSETS / "aqa.png"       # 裁好的 1024 母版
ICO = ASSETS / "aqa.ico"
PREVIEW = ASSETS / "_preview_small.png"

SIZES = [256, 128, 64, 48, 32, 24, 16]
SMALL = (32, 24, 16)              # 这三档额外锐化


def crop_to_plate(im: Image.Image, tol: int = 12) -> Image.Image:
    """裁到「跟背景不一样」的那块，也就是底板本身。水印落在留白里就一起没了。"""
    bg = Image.new("RGB", im.size, im.getpixel((0, 0)))
    diff = ImageChops.difference(im, bg).convert("L")
    bbox = diff.point(lambda v: 255 if v > tol else 0).getbbox()
    if not bbox:
        return im
    return im.crop(bbox)


def sharpen_small(img: Image.Image) -> Image.Image:
    img = ImageEnhance.Contrast(img).enhance(1.10)
    return img.filter(ImageFilter.UnsharpMask(radius=0.8, percent=85,
                                              threshold=0))


def main() -> int:
    src = SRC if SRC.is_file() else MASTER
    if not src.is_file():
        print(f"母版不存在：{SRC} / {MASTER}", file=sys.stderr)
        return 1
    im = Image.open(src).convert("RGB")
    plate = crop_to_plate(im)
    w, h = plate.size
    side = max(w, h)
    if (w, h) != (side, side):                      # 补成正方（居中）
        box = Image.new("RGB", (side, side), plate.getpixel((1, 1)))
        box.paste(plate, ((side - w) // 2, (side - h) // 2))
        plate = box
    plate = plate.resize((1024, 1024), Image.LANCZOS)
    plate.save(MASTER)
    print(f"裁到板材 bbox {w}x{h} → 母版 1024：{MASTER}")

    frames = {}
    for s in SIZES:
        f = plate.resize((s, s), Image.LANCZOS)
        frames[s] = sharpen_small(f) if s in SMALL else f

    frames[256].save(ICO, format="ICO", sizes=[(s, s) for s in SIZES],
                     append_images=[frames[s] for s in SIZES if s != 256])
    print(f"ICO 写出：{ICO}（{ICO.stat().st_size // 1024} KB）")

    cell, gap = 192, 18
    sheet = Image.new("RGB", (len(SMALL) * (cell + gap) + gap, cell + gap * 2),
                      (250, 250, 252))
    for i, s in enumerate(SMALL):
        sheet.paste(frames[s].resize((cell, cell), Image.NEAREST),
                    (gap + i * (cell + gap), gap))
    sheet.save(PREVIEW)
    print(f"小尺寸预览：{PREVIEW}")

    with Image.open(ICO) as back:
        print("回到 ICO 校验尺寸档：", sorted(back.info.get("sizes", [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
