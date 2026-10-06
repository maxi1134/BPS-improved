"""Sextant mark, simplified: frame arms, limb arc, index arm, telescope, pivot. Filled shapes only."""
import math, sys
TEAL = "#0F4C5C"; ACCENT = "#E05A47"
AX, AY = 200, 90          # pivot
R = 200                   # limb radius from the pivot
HALF = 34                 # half-angle of the limb, degrees
BAND = 34                 # limb band thickness
ARM_W = 26
def pt(r, deg):
    t = math.radians(deg)
    return AX + r * math.sin(t), AY + r * math.cos(t)
def fmt(p): return f"{p[0]:.1f} {p[1]:.1f}"
def thick_line(a, b, w):
    dx, dy = b[0]-a[0], b[1]-a[1]; L = math.hypot(dx, dy); nx, ny = -dy/L*w/2, dx/L*w/2
    return f'<polygon points="{fmt((a[0]+nx,a[1]+ny))} {fmt((b[0]+nx,b[1]+ny))} {fmt((b[0]-nx,b[1]-ny))} {fmt((a[0]-nx,a[1]-ny))}"/>'
def rot_rect(cx, cy, w, h, deg, rx=0):
    t = math.radians(deg); c, s = math.cos(t), math.sin(t)
    pts = [(-w/2,-h/2),(w/2,-h/2),(w/2,h/2),(-w/2,h/2)]
    return '<polygon points="' + " ".join(fmt((cx + x*c - y*s, cy + x*s + y*c)) for x, y in pts) + '"/>'
def mark_svg(size=400, teal=TEAL, accent=ACCENT, bg=None):
    o1, o2 = pt(R, -HALF), pt(R, HALF); i1, i2 = pt(R-BAND, HALF), pt(R-BAND, -HALF)
    limb = (f'M {fmt(o1)} A {R} {R} 0 0 0 {fmt(o2)} L {fmt(i1)} A {R-BAND} {R-BAND} 0 0 1 {fmt(i2)} Z')
    g = [f'<g fill="{teal}">', f'<path d="{limb}"/>']
    for s in (-1, 1):
        g.append(thick_line((AX, AY), pt(R - BAND/2, s*HALF), ARM_W))
        g.append(f'<circle cx="{fmt(pt(R-BAND/2, s*HALF)).replace(" ", chr(34)+" cy="+chr(34))}" r="{ARM_W/2}"/>')
    # telescope: diagonal tube across the frame below the pivot; horizon mirror at the right end, shade block at the left
    tx, ty, ang = 206, 168, -24
    t = math.radians(ang); ux, uy = math.cos(t), math.sin(t)
    g.append(rot_rect(tx, ty, 200, 28, ang))
    g.append(rot_rect(tx + 92*ux, ty + 92*uy, 34, 56, ang))
    g.append(rot_rect(tx - 108*ux, ty - 108*uy, 20, 44, ang))
    g.append('</g>')
    # index arm (accent): pivot straight down through the limb
    tip = (AX, AY + R + 24)
    g.append(f'<g fill="{accent}">{thick_line((AX, AY), tip, 22)}<circle cx="{AX}" cy="{tip[1]}" r="18"/></g>')
    g.append(f'<circle cx="{AX}" cy="{AY}" r="34" fill="{teal}"/><circle cx="{AX}" cy="{AY}" r="15" fill="{bg or "#FFFFFF"}"/>')
    body = "".join(g)
    bgrect = f'<rect width="400" height="400" fill="{bg}"/>' if bg else ""
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" width="{size}" height="{size}">{bgrect}{body}</svg>'
if __name__ == "__main__":
    open(sys.argv[1], "w").write(mark_svg(int(sys.argv[2]) if len(sys.argv) > 2 else 400))
