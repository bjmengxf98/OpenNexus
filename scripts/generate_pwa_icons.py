"""Generate the OpenNexus PWA icon set with Pillow."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


def _font(size: int):
    candidates = (
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    )
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def make_icon(size: int, *, maskable: bool = False) -> Image.Image:
    image = Image.new("RGB", (size, size), "#4f46e5")
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            tx, ty = x / max(size - 1, 1), y / max(size - 1, 1)
            mix = (tx + ty) / 2
            pixels[x, y] = (
                round(63 * mix + 96 * (1 - mix)),
                round(70 * mix + 165 * (1 - mix)),
                round(229 * mix + 250 * (1 - mix)),
            )

    margin = round(size * (0.18 if maskable else 0.10))
    radius = round(size * 0.22)
    image = image.convert("RGBA")
    if not maskable:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
        image.putalpha(mask)

    inner = (margin, margin, size - margin, size - margin)
    glass = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glass_draw = ImageDraw.Draw(glass)
    glass_draw.rounded_rectangle(inner, radius=round(size * 0.18), fill=(255, 255, 255, 34), outline=(255, 255, 255, 105), width=max(2, size // 96))
    image = Image.alpha_composite(image, glass)
    draw = ImageDraw.Draw(image)
    font = _font(round(size * 0.33))
    text = "NX"
    box = draw.textbbox((0, 0), text, font=font)
    x = (size - (box[2] - box[0])) / 2 - box[0]
    y = (size - (box[3] - box[1])) / 2 - box[1] - size * 0.012
    draw.text((x, y), text, font=font, fill="white")
    return image


def main() -> None:
    STATIC.mkdir(exist_ok=True)
    for filename, size, maskable in (
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, True),
        ("apple-touch-icon.png", 180, False),
    ):
        make_icon(size, maskable=maskable).save(STATIC / filename, optimize=True)
        print(f"generated {filename}")


if __name__ == "__main__":
    main()
