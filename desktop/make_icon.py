"""Draws Troupe's Maison seal (ink tile, brass rings, ivory T) and writes troupe.ico with all Windows sizes."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = 1024  # draw large, then downsample for crisp small sizes
INK, BRASS, IVORY = (28, 26, 23, 255), (194, 160, 106, 255), (246, 241, 231, 255)
here = Path(__file__).parent
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((0, 0, S - 1, S - 1), radius=224, fill=INK)
c, r1, r2 = S / 2, S * 0.36, S * 0.295
d.ellipse((c - r1, c - r1, c + r1, c + r1), outline=BRASS, width=22)
d.ellipse((c - r2, c - r2, c + r2, c + r2), outline=BRASS, width=12)
font = None
for cand in (here / "fonts" / "CormorantGaramond-SemiBold.ttf", Path("C:/Windows/Fonts/georgiab.ttf"),
             Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf")):
    if cand.exists():
        font = ImageFont.truetype(str(cand), int(S * 0.36))
        break
font = font or ImageFont.load_default()
d.text((c, c + S * 0.015), "T", font=font, fill=IVORY, anchor="mm")
out = here / "troupe.ico"
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save(here / "troupe.png")
print(f"wrote {out}")
