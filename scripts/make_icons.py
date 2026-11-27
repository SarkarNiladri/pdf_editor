"""Generate the PDF Suite app icons: indigo-to-violet tile with a white page and text lines.

Usage: python scripts/make_icons.py static/icons
"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(sys.argv[1]); S = 1024
A, B = (91, 76, 240), (139, 92, 246)

def gradient():
    g = Image.new('RGB', (S, S)); px = g.load()
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * S - 2)
            px[x, y] = tuple(round(A[i] + (B[i] - A[i]) * t) for i in range(3))
    return g

def glyph(img, scale):
    """Draw the page glyph centred, occupying `scale` of the tile."""
    d = ImageDraw.Draw(img); c = S / 2; w = S * scale * 0.72; h = S * scale
    x0, y0, x1, y1 = c - w / 2, c - h / 2, c + w / 2, c + h / 2
    fold = w * 0.30; r = S * scale * 0.06
    d.rounded_rectangle((x0, y0, x1, y1), radius=r, fill='white')
    # folded corner
    bg = grad.crop((int(x1 - fold), int(y0), int(x1 + 2), int(y0 + fold + 2)))
    mask = Image.new('L', bg.size, 0); ImageDraw.Draw(mask).polygon([(0, -1), (bg.size[0], -1), (bg.size[0], bg.size[1])], fill=255)
    img.paste(bg, (int(x1 - fold), int(y0)), mask)
    d.polygon([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)], fill=(221, 214, 254))
    # text lines
    lx0 = x0 + w * 0.16; lw = w * 0.68; lh = h * 0.045
    for i, frac in enumerate([0.55, 0.85, 0.85, 0.6]):
        ly = y0 + h * (0.38 + i * 0.12)
        col = A if i == 0 else (196, 181, 253)
        d.rounded_rectangle((lx0, ly, lx0 + lw * frac, ly + lh), radius=lh / 2, fill=col)

grad = gradient()

def tile(scale, rounded):
    img = grad.copy(); glyph(img, scale)
    if rounded:
        mask = Image.new('L', (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.22), fill=255)
        out = Image.new('RGBA', (S, S), (0, 0, 0, 0)); out.paste(img, (0, 0), mask); return out
    return img.convert('RGBA')

any_icon = tile(0.62, True)
maskable = tile(0.50, False)          # glyph sits well inside the 80% safe zone
apple = tile(0.58, False).convert('RGB')  # iOS rounds the corners itself; no transparency

for size in (192, 512):
    any_icon.resize((size, size), Image.LANCZOS).save(OUT / f'icon-{size}.png', optimize=True)
    maskable.resize((size, size), Image.LANCZOS).save(OUT / f'icon-maskable-{size}.png', optimize=True)
apple.resize((180, 180), Image.LANCZOS).save(OUT / 'apple-touch-icon.png', optimize=True)
any_icon.resize((32, 32), Image.LANCZOS).save(OUT / 'favicon-32.png', optimize=True)
any_icon.resize((48, 48), Image.LANCZOS).save(OUT / 'favicon.ico', sizes=[(16, 16), (32, 32), (48, 48)])
