"""Compose the brand files from the mark: icon (square), HA brand logo (wide), README logo with tagline."""
from PIL import Image, ImageDraw, ImageFont
TEAL = (15, 76, 92); GREY = (96, 104, 118)
FONT = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"; FONT_R = "/System/Library/Fonts/Supplemental/Georgia.ttf"
mark = Image.open("icon@2x.png").convert("RGBA")          # 512, trimmed + centred
def logo(height, tagline, out):
    m = mark.resize((height, height), Image.LANCZOS)
    f1 = ImageFont.truetype(FONT, int(height * 0.50)); f2 = ImageFont.truetype(FONT_R, int(height * 0.16))
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    w1 = tmp.textbbox((0, 0), "Sextant", font=f1); w2 = tmp.textbbox((0, 0), tagline, font=f2) if tagline else (0, 0, 0, 0)
    gap = int(height * 0.10); tw = max(w1[2] - w1[0], w2[2] - w2[0])
    W = height + gap + tw + int(height * 0.06); H = height
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0)); im.paste(m, (0, 0), m)
    d = ImageDraw.Draw(im)
    th = (w1[3] - w1[1]) + ((w2[3] - w2[1]) + int(height * 0.06) if tagline else 0)
    y = (H - th) // 2 - w1[1]
    d.text((height + gap, y), "Sextant", font=f1, fill=TEAL)
    if tagline:
        d.text((height + gap + 2, y + (w1[3] - w1[1]) + int(height * 0.06) - w2[1] + w1[1]), tagline, font=f2, fill=GREY)
    im.save(out)
    return im.size
print("logo@2x", logo(256, None, "logo@2x.png")); print("logo", logo(128, None, "logo.png"))
print("readme", logo(280, "Powered by Bermuda", "readme-logo.png"))
mark.resize((256, 256), Image.LANCZOS).save("icon.png")
Image.open("readme-logo.png").convert("RGBA").save("readme-preview.png")
bg = Image.new("RGBA", Image.open("readme-logo.png").size, (255, 255, 255, 255)); fg = Image.open("readme-logo.png").convert("RGBA"); bg.alpha_composite(fg); bg.convert("RGB").save("readme-preview.png")
