"""Generate assets/icon.ico (simple camera/calendar glyph) with Pillow."""
from pathlib import Path

from PIL import Image, ImageDraw

out = Path(__file__).resolve().parent.parent / "assets"
out.mkdir(exist_ok=True)
S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=52, fill=(31, 34, 42, 255))
d.rounded_rectangle((44, 70, S - 44, S - 52), radius=24, fill=(79, 140, 255, 255))
d.rectangle((44, 70, S - 44, 104), fill=(47, 158, 98, 255))
for i in range(3):
    for j in range(3):
        x0, y0 = 66 + i * 44, 118 + j * 28
        d.rounded_rectangle((x0, y0, x0 + 30, y0 + 18), radius=4, fill=(230, 232, 238, 255))
d.rounded_rectangle((82, 50, 96, 86), radius=6, fill=(230, 232, 238, 255))
d.rounded_rectangle((S - 96, 50, S - 82, 86), radius=6, fill=(230, 232, 238, 255))
img.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(out / "icon.png")
print("icon written")
