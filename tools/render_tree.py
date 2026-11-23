"""Render docs/output-tree.png: a terminal-style tree of an organized output folder."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

root = Path(sys.argv[1])
dest = Path(sys.argv[2])
years_to_show = sys.argv[3].split(",") if len(sys.argv) > 3 else None

lines = ["Output/"]
years = sorted(p for p in root.iterdir() if p.is_dir() and p.name.isdigit())
if years_to_show:
    years = [y for y in years if y.name in years_to_show]
for yi, y in enumerate(years):
    ylast = yi == len(years) - 1
    lines.append(("└── " if ylast else "├── ") + y.name + "/")
    months = sorted(p for p in y.iterdir() if p.is_dir())
    for mi, m in enumerate(months):
        mlast = mi == len(months) - 1
        pre = "    " if ylast else "│   "
        lines.append(pre + ("└── " if mlast else "├── ") + m.name + "/")
        files = sorted(m.iterdir(), key=lambda p: (p.stem.split("-")[0],
                       int(p.stem.split("-")[1]) if "-" in p.stem else 1))
        for fi, f in enumerate(files):
            flast = fi == len(files) - 1
            lines.append(pre + ("    " if mlast else "│   ") + ("└── " if flast else "├── ") + f.name)
lines.append("└── _organizer_logs/   (CSV report, summary, undo manifest)")

font = ImageFont.truetype("consola.ttf", 26)
lh, pad = 36, 36
w = int(max(font.getlength(l) for l in lines)) + pad * 2
h = lh * len(lines) + pad * 2 + 40
img = Image.new("RGB", (max(w, 900), h), (22, 24, 29))
d = ImageDraw.Draw(img)
for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
    d.ellipse((pad + i * 30, 18, pad + i * 30 + 16, 34), fill=c)
for i, l in enumerate(lines):
    y = pad + 30 + i * lh
    color = (79, 140, 255) if l.rstrip().endswith("/") else (230, 232, 238)
    if "_organizer_logs" in l:
        color = (154, 163, 178)
    if "-" in l.split("── ")[-1] and not l.rstrip().endswith("/") and "_organizer" not in l:
        color = (224, 180, 80)  # collision-suffixed names
    d.text((pad, y), l, font=font, fill=color)
img.save(dest)
print(f"{dest} ({len(lines)} lines)")
