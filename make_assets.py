"""
Generates the cheese-themed assets for Francheese FileCompare:
  * cheese.ico   - the app / window icon (a cheese wedge)
  * intro.png    - the splash intro screen shown on launch

If a photo is present (default: me.jpg), it is cropped to a circle and placed
in the middle of the cheesy splash. If no photo is found, a cheese-only splash
is produced so the app still works.

Run:  python make_assets.py [path-to-your-photo]
"""

import os
import sys
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

HERE = os.path.dirname(os.path.abspath(__file__))

CHEESE = (255, 201, 53)      # cheese yellow
CHEESE_DK = (224, 162, 20)   # darker rind
HOLE = (240, 176, 35)        # hole shading
INK = (90, 58, 0)            # dark brown text
WHITE = (255, 255, 255)


def _font(size, bold=True):
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_holes(draw, x, y, w, h, n=14, seed=7):
    rnd = random.Random(seed)
    for _ in range(n):
        r = rnd.randint(max(4, w // 40), max(8, w // 16))
        cx = rnd.randint(x + r, x + w - r)
        cy = rnd.randint(y + r, y + h - r)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=HOLE)


def make_icon():
    """Draw a cheese-wedge icon and save as multi-size cheese.ico."""
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Wedge: a triangle with a rounded front
    pad = 26
    top = [(pad, size - pad), (size - pad, pad + 40), (size - pad, size - pad)]
    d.polygon(top, fill=CHEESE)
    # rind along the top edge
    d.line([top[0], top[1]], fill=CHEESE_DK, width=14)
    d.line([top[1], top[2]], fill=CHEESE_DK, width=14)
    # holes
    rnd = random.Random(3)
    for _ in range(7):
        r = rnd.randint(10, 22)
        cx = rnd.randint(pad + 50, size - pad - 30)
        cy = rnd.randint(pad + 70, size - pad - 30)
        if cy > (size - pad) - (cx - pad) * (size - 2 * pad - 40) / (size - 2 * pad) - 10:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=HOLE)
    ico_path = os.path.join(HERE, "cheese.ico")
    img.save(ico_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", ico_path)
    return img


def cheesify(src):
    """Tint a photo so the skin reads as cheese: warm yellow overlay,
    boosted saturation, and a few translucent cheese-holes."""
    src = src.convert("RGB")
    tint = Image.new("RGB", src.size, CHEESE)
    blended = Image.blend(src, tint, 0.58)                 # Full Cheddar tint
    blended = ImageEnhance.Color(blended).enhance(1.35)    # extra vivid
    blended = ImageEnhance.Contrast(blended).enhance(1.12) # keep features readable
    out = blended.convert("RGBA")
    d = ImageDraw.Draw(out, "RGBA")
    w, h = out.size
    rnd = random.Random(42)
    for _ in range(10):                                    # more cheese holes
        r = rnd.randint(max(6, w // 22), max(10, w // 12))
        cx = rnd.randint(int(w * 0.2), int(w * 0.8))
        cy = rnd.randint(int(h * 0.28), int(h * 0.85))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(190, 132, 12, 75))
    return out.convert("RGB")


def _circular_photo(photo_path, diameter, cheese_skin=True):
    src = Image.open(photo_path).convert("RGBA")
    if cheese_skin:
        src = cheesify(src).convert("RGBA")
    # center-crop to square
    w, h = src.size
    side = min(w, h)
    src = src.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    src = src.resize((diameter, diameter), Image.LANCZOS)
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, diameter, diameter], fill=255)
    out = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    out.paste(src, (0, 0), mask)
    return out


def make_intro(photo_path=None):
    W, H = 680, 440
    img = Image.new("RGBA", (W, H), CHEESE)
    d = ImageDraw.Draw(img)
    # cheesy hole texture across the whole background
    _draw_holes(d, 0, 0, W, H, n=26, seed=11)
    # soft vignette band at bottom for the text
    band = Image.new("RGBA", (W, 120), (255, 201, 53, 0))
    bd = ImageDraw.Draw(band)
    for i in range(120):
        a = int(150 * (i / 120))
        bd.line([(0, i), (W, i)], fill=(224, 162, 20, a))
    img.alpha_composite(band, (0, H - 120))

    # photo (or a cheese placeholder circle) in the upper-center
    diameter = 190
    cx = W // 2
    cy = 150
    ring = 10
    # white ring + cheese ring behind the photo
    d.ellipse([cx - diameter // 2 - ring * 2, cy - diameter // 2 - ring * 2,
               cx + diameter // 2 + ring * 2, cy + diameter // 2 + ring * 2], fill=CHEESE_DK)
    d.ellipse([cx - diameter // 2 - ring, cy - diameter // 2 - ring,
               cx + diameter // 2 + ring, cy + diameter // 2 + ring], fill=WHITE)
    if photo_path and os.path.exists(photo_path):
        photo = _circular_photo(photo_path, diameter)
        img.alpha_composite(photo, (cx - diameter // 2, cy - diameter // 2))
        print("placed photo:", photo_path)
    else:
        # cheese-only placeholder face
        d.ellipse([cx - diameter // 2, cy - diameter // 2, cx + diameter // 2, cy + diameter // 2], fill=CHEESE)
        _draw_holes(d, cx - diameter // 2, cy - diameter // 2, diameter, diameter, n=8, seed=21)
        print("no photo found - cheese-only splash")

    # title + subtitle
    title = "Francheese FileCompare"
    tf = _font(40, bold=True)
    sf = _font(18, bold=False)
    tw = d.textlength(title, font=tf)
    d.text(((W - tw) // 2 + 2, H - 96 + 2), title, font=tf, fill=(60, 38, 0))  # shadow
    d.text(((W - tw) // 2, H - 96), title, font=tf, fill=WHITE)
    sub = "Safe, read-only folder verification   * cheese-grade *"
    sw = d.textlength(sub, font=sf)
    d.text(((W - sw) // 2, H - 44), sub, font=sf, fill=(70, 45, 0))

    out = os.path.join(HERE, "intro.png")
    img.convert("RGB").save(out, "PNG")
    print("wrote", out)


def find_photo(argv):
    if len(argv) > 1 and os.path.exists(argv[1]):
        return argv[1]
    for name in ("me.jpg", "me.jpeg", "me.png", "intro_raw.png", "photo.jpg"):
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            return p
    return None


if __name__ == "__main__":
    make_icon()
    make_intro(find_photo(sys.argv))
    print("done.")
