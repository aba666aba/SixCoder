#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""SixCoder 程序图标生成器（可复现，不塞二进制到仓库）。

设计：`<6>` 锁定组合
    - 底：深靛蓝圆角方块，135 度渐变 + 中心青色辉光 + 顶部一圈 1px 内高光（"玻璃边"）
    - 纹理：上下各两行极淡的代码条（低透明度圆角短线，带一小段青色高亮当"语法高亮"）
    - 主体：白色（上白下淡蓝）的大号数字 6，字体 Consolas Bold —— Visual Studio 的
      默认代码字体，选它既保证小尺寸可读，也让"代码"语义来自字体本身
    - 代码元素：两侧一青一青的两个尖括号 < >，圆头圆角、等粗描边，和 6 组成 <6> 标签

为什么带 6：产品名 SixCoder；为什么带 < >：一眼看出是"写代码/命令行"的东西。
6 独占视觉中心，尖括号在 16px 下退化成两个小色块，不会抢戏。

用法（构建机需要 Pillow；安装包运行时不需要）：
    python packaging/make_icon.py                  # 生成 + 打印自检（含字符画预览）
    python packaging/make_icon.py --no-preview     # 只生成
    python packaging/make_icon.py --out D:\\x.ico  # 换输出路径（调试用）

输出：
    shell/build/icon.ico             多尺寸 ICO（16/20/24/32/40/48/64/128/256）
    packaging/assets/icon-preview.png  512 预览图（给人看）
    packaging/assets/icon-sizes.png    各尺寸排排站（检查小尺寸是否糊）

注入顺序说明：ICO 里 <=64 的帧写成 DIB(BMP) 而不是 PNG —— 老工具链对 PNG 帧支持不齐；
128/256 才用 PNG（Vista+ 支持，且能省掉 256KB 的体积）。
"""
from __future__ import annotations

import argparse
import io
import os
import struct
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover - 构建机缺 Pillow 时的友好提示
    print("ERROR: Pillow is required for icon generation.")
    print("       python -m pip install pillow")
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ICO = ROOT / "shell" / "build" / "icon.ico"
PREVIEW_PNG = ROOT / "packaging" / "assets" / "icon-preview.png"
SIZES_PNG = ROOT / "packaging" / "assets" / "icon-sizes.png"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# ------------------------------------------------------------------ 配色
C_BG_IN = (27, 37, 89)        # 左上：靛蓝
C_BG_OUT = (7, 10, 23)        # 右下：近黑
C_GLOW = (56, 189, 248)       # 中心辉光：天蓝
C_DIGIT_TOP = (255, 255, 255)
C_DIGIT_BOT = (183, 199, 255)
C_BRACKET_TOP = (110, 231, 249)
C_BRACKET_BOT = (34, 184, 240)

# 代码条纹理：(y, x0, x1, alpha, 是否高亮)。只在大尺寸画，小尺寸画了就是噪点。
TEXTURE_ROWS = (
    (0.098, 0.155, 0.268, 34, True),
    (0.098, 0.300, 0.660, 26, False),
    (0.150, 0.155, 0.520, 20, False),
    (0.150, 0.552, 0.648, 26, True),
    (0.845, 0.155, 0.600, 26, False),
    (0.845, 0.632, 0.728, 34, True),
    (0.897, 0.155, 0.700, 20, False),
)

# Consolas Bold 是 Windows 上最稳的等宽粗体（VS 默认代码字体）
FONT_CANDIDATES = (
    r"{windir}\Fonts\consolab.ttf",
    r"{windir}\Fonts\CascadiaMono.ttf",
    r"{windir}\Fonts\seguisb.ttf",
    r"{windir}\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/Library/Fonts/Menlo.ttc",
)


# ------------------------------------------------------------------ 小工具
def _font_path() -> str:
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    for cand in FONT_CANDIDATES:
        p = cand.format(windir=windir)
        if Path(p).is_file():
            return p
    raise SystemExit("ERROR: no usable bold font found; add one to FONT_CANDIDATES")


def _mix(c0, c1, t: float):
    return tuple(round(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))


def _diagonal_gradient(size: int, c0, c1) -> Image.Image:
    """135 度线性渐变：用小图算再放大，省时间且足够平滑。"""
    n = 64
    small = Image.new("RGB", (n, n))
    px = small.load()
    for y in range(n):
        for x in range(n):
            px[x, y] = _mix(c0, c1, (x + y) / (2 * (n - 1)))
    return small.resize((size, size), Image.BICUBIC)


def _vertical_gradient(size: int, c0, c1) -> Image.Image:
    col = Image.new("RGB", (1, size))
    px = col.load()
    for y in range(size):
        px[0, y] = _mix(c0, c1, y / max(1, size - 1))
    return col.resize((size, size), Image.BICUBIC)


def _rounded_mask(size: int, radius: float) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def _radial(size: int, cx: float, cy: float, radius: float, peak: float, invert: bool) -> Image.Image:
    """径向遮罩：invert=False 是中间亮（辉光），True 是边缘亮（暗角）。"""
    n = 96
    small = Image.new("L", (n, n), 0)
    px = small.load()
    for y in range(n):
        for x in range(n):
            dx = (x + 0.5) / n - cx
            dy = (y + 0.5) / n - cy
            d = ((dx * dx + dy * dy) ** 0.5) / radius
            v = 0.0
            if invert:
                if d > 0.55:
                    v = min(1.0, (d - 0.55) / 0.55) ** 2
            elif d < 1.0:
                v = (1.0 - d) ** 2
            px[x, y] = int(peak * v)
    return small.resize((size, size), Image.BICUBIC)


# ------------------------------------------------------------------ 字形零件
def _digit_ink(target_h: float) -> Image.Image:
    """渲染数字 6 的墨迹位图（紧贴裁切），墨迹高度约等于 target_h。"""
    path = _font_path()
    probe = 400
    bbox = ImageFont.truetype(path, probe).getbbox("6")
    h0 = max(1, bbox[3] - bbox[1])
    px = max(6, round(probe * target_h / h0))
    font = ImageFont.truetype(path, px)
    canvas = Image.new("L", (px * 2, px * 3), 0)
    ImageDraw.Draw(canvas).text((px // 2, px), "6", font=font, fill=255)
    box = canvas.getbbox()
    if not box:  # pragma: no cover
        raise SystemExit("ERROR: digit rasterisation produced empty bitmap")
    return canvas.crop(box)


def _chevron(height: float, depth: float, thick: float, left: bool) -> Image.Image:
    """圆头尖括号。返回的位图外框正好是 (depth+thick) x height。"""
    t = max(2.0, thick)
    w = depth + t
    img = Image.new("L", (max(3, round(w)), max(3, round(height))), 0)
    d = ImageDraw.Draw(img)
    # '<' 的尖端在左、开口在右；'>' 反过来。两种情况的圆头都在开口那两端。
    x_tip = t / 2 if left else t / 2 + depth
    x_open = t / 2 + depth if left else t / 2
    pts = [(x_open, t / 2), (x_tip, height / 2), (x_open, height - t / 2)]
    d.line(pts, fill=255, width=int(round(t)), joint="curve")
    r = t / 2
    for p in (pts[0], pts[-1]):
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=255)
    return img


def _lockup(size: int, compact: bool = False) -> tuple[Image.Image, Image.Image]:
    """返回 (数字遮罩, 尖括号遮罩)，均为 size x size 的 L 图。

    compact=True 用于 16/20/24px：只留一个大号 6，不画尖括号。
    实测 <6> 在 24px 以下会糊成三个色块，小尺寸的规矩是"一个元素、越粗越好"。
    """
    S = size
    digit_mask = Image.new("L", (S, S), 0)
    bracket_mask = Image.new("L", (S, S), 0)
    cy = 0.5 * S

    if compact:
        ink = _digit_ink(0.62 * S)
        digit_mask.paste(ink, (round((S - ink.width) / 2.0), round(cy - ink.height / 2.0)))
        return digit_mask, bracket_mask

    # 先用参考高度量出"数字墨迹宽 / 墨迹高"的比值，再解出铺满目标宽度的高度。
    ref_h = 0.50 * S
    ref = _digit_ink(ref_h)
    k = ref.width / ref.height
    bh_r, t_r, gap_r = 0.70, 0.135, 0.085     # 相对数字墨迹高度的比例
    depth_r = 0.42 * bh_r
    per_bracket = depth_r + t_r
    target_w = 0.82 * S
    dh = target_w / (2 * per_bracket + 2 * gap_r + k)
    ink = _digit_ink(dh)
    dw, dh_ink = ink.width, ink.height

    bh = bh_r * dh_ink
    thick = t_r * dh_ink
    depth = depth_r * dh_ink
    gap = gap_r * dh_ink
    bw = depth + thick

    total = bw * 2 + gap * 2 + dw
    # 6 的墨迹偏左（开口在右上），整体右移一点点才"看着"居中
    x = (S - total) / 2.0 + 0.006 * S

    digit_mask.paste(ink, (round(x + bw + gap), round(cy - dh_ink / 2.0)))

    lb = _chevron(bh, depth, thick, left=True)
    rb = _chevron(bh, depth, thick, left=False)
    bracket_mask.paste(lb, (round(x), round(cy - bh / 2.0)))
    bracket_mask.paste(rb, (round(x + bw + gap + dw + gap), round(cy - bh / 2.0)))
    return digit_mask, bracket_mask


# ------------------------------------------------------------------ 主渲染
def render(size: int, detail: bool, compact: bool = False) -> Image.Image:
    S = size
    radius = 0.225 * S

    canvas = _diagonal_gradient(S, C_BG_IN, C_BG_OUT).convert("RGBA")

    # 中心辉光
    glow = Image.new("RGB", (S, S), C_GLOW).convert("RGBA")
    canvas = Image.composite(glow, canvas, _radial(S, 0.5, 0.45, 0.60, 66, invert=False))

    # 暗角（把注意力压回中间）
    vig = Image.new("RGB", (S, S), (3, 4, 10)).convert("RGBA")
    canvas = Image.composite(vig, canvas, _radial(S, 0.5, 0.5, 0.78, 84, invert=True))

    # 代码条纹理
    if detail:
        tex = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(tex)
        h = max(2, round(0.017 * S))
        for y, x0, x1, alpha, hot in TEXTURE_ROWS:
            col = (110, 231, 249, alpha) if hot else (143, 163, 216, alpha)
            y0 = round(y * S)
            d.rounded_rectangle([round(x0 * S), y0, round(x1 * S), y0 + h], radius=h // 2, fill=col)
        canvas = Image.alpha_composite(canvas, tex)

    digit_mask, bracket_mask = _lockup(S, compact=compact)

    # 投影：让白字从辉光里"浮"起来
    shadow_src = Image.new("L", (S, S), 0)
    shadow_src.paste(digit_mask, (0, 0), digit_mask)
    shadow_src.paste(bracket_mask, (0, 0), bracket_mask)
    blur = shadow_src.filter(ImageFilter.GaussianBlur(max(1.0, 0.020 * S)))
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow.putalpha(blur.point(lambda v: int(v * 0.42)))
    canvas.alpha_composite(shadow, (0, max(1, round(0.012 * S))))

    canvas.paste(_vertical_gradient(S, C_DIGIT_TOP, C_DIGIT_BOT).convert("RGBA"), (0, 0), digit_mask)
    canvas.paste(_vertical_gradient(S, C_BRACKET_TOP, C_BRACKET_BOT).convert("RGBA"), (0, 0), bracket_mask)

    # 内高光边
    rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    inset = 0.013 * S
    ImageDraw.Draw(rim).rounded_rectangle(
        [inset, inset, S - 1 - inset, S - 1 - inset],
        radius=max(1.0, radius - inset),
        outline=(255, 255, 255, 46),
        width=max(1, round(0.008 * S)),
    )
    canvas.alpha_composite(rim)

    canvas.putalpha(_rounded_mask(S, radius))
    return canvas


def render_size(sz: int) -> Image.Image:
    """每个尺寸单独从超采样画，别拿 256 硬缩到 16（那样 16px 会糊）。"""
    ss = 16 if sz <= 24 else 8 if sz <= 64 else 4 if sz <= 128 else 2
    big = render(sz * ss, detail=sz >= 96, compact=sz <= 24)
    return big.resize((sz, sz), Image.LANCZOS)


# ------------------------------------------------------------------ 写文件
def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _dib_bytes(img: Image.Image) -> bytes:
    """ICO 里的 DIB 帧：BITMAPINFOHEADER + 自下而上 BGRA + 1bpp AND 掩码。"""
    w, h = img.size
    px = img.load()
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = px[x, y]
            xor += bytes((b, g, r, a))
    stride = ((w + 31) // 32) * 4
    andm = bytearray()
    for y in range(h - 1, -1, -1):
        row = bytearray(stride)
        for x in range(w):
            if px[x, y][3] < 8:
                row[x >> 3] |= 0x80 >> (x & 7)
        andm += row
    bih = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(xor) + len(andm), 0, 0, 0, 0)
    return bytes(bih) + bytes(xor) + bytes(andm)


def write_ico(path: Path, frames) -> int:
    """写 ICO。

    所有尺寸都写成 DIB(BMP) 帧，**不用 PNG 帧**：实测 128px 的 PNG 帧能让
    System.Drawing.Icon 构造成功但 ToBitmap() 抛 "Requested range extends past the end
    of the array"；rcedit / 老 NSIS 对 PNG 帧的支持也不齐。DIB 帧的代价只是体积
    （256px 一帧 256 KB），而安装包本来就是 139 MB，不值得为省 300 KB 冒"图标丢了"的险。
    """
    n = len(frames)
    header = struct.pack("<HHH", 0, 1, n)
    entries = bytearray()
    blobs = []
    offset = 6 + 16 * n
    for sz, img in frames:
        blob = _dib_bytes(img)
        entries += struct.pack("<BBBBHHII", sz % 256, sz % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs.append(blob)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(entries) + b"".join(blobs))
    return path.stat().st_size


def _contact_sheet(frames, gap: int = 12) -> Image.Image:
    """各尺寸排排站，放到浅色棋盘格上，方便肉眼判断小尺寸是否还认得出。"""
    total_w = sum(sz for sz, _ in frames) + gap * (len(frames) + 1)
    side = max(sz for sz, _ in frames) + gap * 2
    sheet = Image.new("RGB", (total_w, side), (232, 234, 240))
    d = ImageDraw.Draw(sheet)
    for i in range(0, total_w, 16):
        for j in range(0, side, 16):
            if (i // 16 + j // 16) % 2 == 0:
                d.rectangle([i, j, i + 15, j + 15], fill=(246, 247, 250))
    x = gap
    for sz, img in frames:
        sheet.paste(img, (x, (side - sz) // 2), img)
        x += sz + gap
    return sheet


# ------------------------------------------------------------------ 自检
_RAMP = " .:-=+*#%@"


def _ascii(img: Image.Image, cols: int) -> str:
    im = img.convert("RGBA")
    if im.width != cols:
        im = im.resize((cols, cols), Image.LANCZOS)
    px = im.load()
    out = []
    for y in range(im.height):
        line = []
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if a < 40:
                line.append(" ")
                continue
            lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) * (a / 255.0)
            line.append(_RAMP[min(len(_RAMP) - 1, int(lum / 255.0 * len(_RAMP)))])
        out.append("".join(line))
    return "\n".join(out)


def _stats(img: Image.Image) -> dict:
    px = img.load()
    w, h = img.size
    opaque = white = cyan = 0
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a > 128:
                opaque += 1
                if r > 200 and g > 200 and b > 200:
                    white += 1
                elif b > 150 and g > 130 and r < 150:
                    cyan += 1
    n = float(w * h)
    return {"opaque": opaque / n, "white": white / n, "cyan": cyan / n}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="generate the SixCoder icon")
    ap.add_argument("--out", default=str(DEFAULT_ICO), help="output .ico path")
    ap.add_argument("--no-preview", action="store_true", help="skip ascii preview / contact sheet")
    ap.add_argument("--sizes", default=",".join(str(s) for s in ICO_SIZES))
    ap.add_argument("--ascii", default="48,24,16", help="sizes to print as ascii art")
    args = ap.parse_args(argv)

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    frames = [(sz, render_size(sz)) for sz in sizes]

    out = Path(args.out)
    nbytes = write_ico(out, frames)
    print(f"[icon] {out}  {nbytes} bytes  sizes={','.join(str(s) for s, _ in frames)}")

    # 回读校验：ICO 目录项数量/尺寸必须和写进去的一致
    raw = out.read_bytes()
    if raw[:4] != b"\x00\x00\x01\x00":
        raise SystemExit("ERROR: bad ICO header")
    count = struct.unpack("<H", raw[4:6])[0]
    seen = []
    for i in range(count):
        e = raw[6 + 16 * i: 6 + 16 * (i + 1)]
        w = e[0] or 256
        seen.append(w)
        if struct.unpack("<H", e[6:8])[0] != 32:
            raise SystemExit("ERROR: frame is not 32bpp")
    print(f"[icon] reread ok: {count} frames -> {seen}")

    for sz, img in frames:
        st = _stats(img)
        print(
            f"[icon] {sz:>3}px  opaque={st['opaque'] * 100:5.1f}%  "
            f"white_ink={st['white'] * 100:5.2f}%  cyan_ink={st['cyan'] * 100:5.2f}%"
            + ("  (compact: digit only)" if sz <= 24 else "")
        )
        if st["white"] <= 0.0:
            raise SystemExit(f"ERROR: {sz}px lost the digit (no white ink)")
        # 16~24px 是简化版（只留 6），不该有尖括号
        if sz > 24 and st["cyan"] <= 0.0:
            raise SystemExit(f"ERROR: {sz}px lost the brackets (no cyan ink)")

    if not args.no_preview:
        PREVIEW_PNG.parent.mkdir(parents=True, exist_ok=True)
        render_size(512).save(PREVIEW_PNG)
        _contact_sheet(frames).save(SIZES_PNG)
        print(f"[icon] preview: {PREVIEW_PNG}")
        print(f"[icon] sheet  : {SIZES_PNG}")
        known = dict(frames)
        for sz in [int(s) for s in args.ascii.split(",") if s.strip()]:
            img = known.get(sz) or render_size(sz)
            cols = max(16, sz if sz <= 96 else 84)
            print(f"\n--- {sz}px ---")
            print(_ascii(img, cols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
