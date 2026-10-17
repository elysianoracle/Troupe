"""Draws the Troupe icon (three figures on a blue tile) and writes troupe.ico with all Windows sizes."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((0, 0, S - 1, S - 1), radius=56, fill=(47, 111, 222, 255))
white = (255, 255, 255, 255)
for cx, cy, r in ((80, 104, 28), (176, 104, 28)):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255, 215))
    d.pieslice((cx - 52, cy + 34, cx + 52, cy + 138), 180, 360, fill=(255, 255, 255, 215))
d.ellipse((128 - 34, 88 - 34, 128 + 34, 88 + 34), fill=white)
d.pieslice((128 - 64, 136, 128 + 64, 264), 180, 360, fill=white)
out = Path(__file__).with_name("troupe.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(out.with_suffix(".png"))
print(f"wrote {out}")
