"""찰나 1분 이야기 - 세로 쇼츠 템플릿 생성기.

레이아웃 (1080x1920):
  상단: 채널 로고 -> 작은 문구 -> 큰 제목
  가운데: 영상 (4:3, 꽉 차게 자름) + 자막
  하단: 댓글 카드 (시간에 따라 교체)

사용법:
  python make_short.py config.json
  python make_short.py config.json --preview preview.png   # 첫 화면만 이미지로 확인
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_TITLE = os.path.join(HERE, "assets", "fonts", "BlackHanSans-Regular.ttf")
FONT_BODY = os.path.join(HERE, "assets", "fonts", "NotoSansKR.ttf")

W, H = 1080, 1920
LOGO_SIZE, LOGO_Y = 230, 60
SMALL_Y = 315
BIG_Y = 380
VIDEO_Y, VIDEO_W, VIDEO_H = 540, 1080, 810
CAPTION_Y = VIDEO_Y + VIDEO_H // 2
COMMENT_Y = VIDEO_Y + VIDEO_H + 40
ACCENT = (214, 32, 39)


def body_font(size, weight="Regular"):
    font = ImageFont.truetype(FONT_BODY, size)
    font.set_variation_by_name(weight)
    return font


def fit_font(path, text, max_w, start, weight=None):
    size = start
    while size > 20:
        font = body_font(size, weight) if weight else ImageFont.truetype(path, size)
        if font.getlength(text) <= max_w:
            return font
        size -= 4
    return font


def round_logo(path, size):
    logo = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    # 원 바깥의 흰 여백을 잘라냄
    ImageDraw.Draw(mask).ellipse((4, 4, size - 4, size - 4), fill=255)
    logo.putalpha(mask)
    return logo


def draw_title(draw, text, y, font):
    """[대괄호] 부분은 강조색으로 그림."""
    parts, buf, in_br = [], "", False
    for ch in text:
        if ch == "[" and not in_br:
            if buf:
                parts.append((buf, False))
            buf, in_br = ch, True
        elif ch == "]" and in_br:
            parts.append((buf + ch, True))
            buf, in_br = "", False
        else:
            buf += ch
    if buf:
        parts.append((buf, in_br))
    x = (W - font.getlength(text)) / 2
    for chunk, hl in parts:
        draw.text((x, y), chunk, font=font, fill=ACCENT if hl else "white")
        x += font.getlength(chunk)


def make_background(cfg, path):
    img = Image.new("RGBA", (W, H), "black")
    logo = round_logo(os.path.join(HERE, cfg.get("logo", "assets/logo.jpg")), LOGO_SIZE)
    img.alpha_composite(logo, ((W - LOGO_SIZE) // 2, LOGO_Y))
    draw = ImageDraw.Draw(img)
    small = cfg.get("title_small", "")
    if small:
        f = fit_font(FONT_BODY, small, 1000, 46, "Medium")
        draw.text(((W - f.getlength(small)) / 2, SMALL_Y), small, font=f, fill=(225, 225, 225))
    big = cfg.get("title_big", "")
    if big:
        draw_title(draw, big, BIG_Y, fit_font(FONT_TITLE, big, 1020, 104))
    img.convert("RGB").save(path)


def make_caption(text, path):
    img = Image.new("RGBA", (W, 140), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f = fit_font(FONT_BODY, text, 980, 50, "Bold")
    x = (W - f.getlength(text)) / 2
    draw.text((x, 30), text, font=f, fill="white", stroke_width=5, stroke_fill="black")
    img.save(path)
    return 140


def make_comment(c, path):
    cw, ch = 860, 210
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, cw - 1, ch - 1), radius=24, fill=(33, 33, 33, 255))
    user = c.get("user", "@viewer")
    # 아바타: 이름 첫 글자
    palette = [(90, 120, 200), (60, 150, 110), (200, 120, 60), (150, 90, 180), (190, 70, 90)]
    color = palette[sum(map(ord, user)) % len(palette)]
    draw.ellipse((28, 30, 108, 110), fill=color)
    initial = user.lstrip("@")[:1].upper() or "?"
    fi = body_font(40, "Bold")
    draw.text((68 - fi.getlength(initial) / 2, 42), initial, font=fi, fill="white")
    fu = body_font(30, "Medium")
    draw.text((130, 28), user, font=fu, fill=(170, 170, 170))
    draw.text((140 + fu.getlength(user), 28), c.get("ago", "1년 전"), font=body_font(28), fill=(120, 120, 120))
    ft = fit_font(FONT_BODY, c.get("text", ""), cw - 160, 38, "Regular")
    draw.text((130, 74), c.get("text", ""), font=ft, fill="white")
    fl = body_font(28)
    likes = f"좋아요 {c['likes']}" if c.get("likes") is not None else "좋아요"
    draw.text((130, 145), likes, font=fl, fill=(170, 170, 170))
    draw.text((170 + fl.getlength(likes), 145), "답글", font=body_font(28, "Bold"), fill=(170, 170, 170))
    img.save(path)
    return cw


def probe_duration(video):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def build(cfg, base_dir, preview=None):
    tmp = tempfile.mkdtemp(prefix="short_")
    bg = os.path.join(tmp, "bg.png")
    make_background(cfg, bg)

    # (png, x, y, start, end)
    overlays = []
    for i, cap in enumerate(cfg.get("captions", [])):
        p = os.path.join(tmp, f"cap{i}.png")
        h = make_caption(cap["text"], p)
        overlays.append((p, 0, CAPTION_Y - h // 2, cap["start"], cap["end"]))
    for i, c in enumerate(cfg.get("comments", [])):
        p = os.path.join(tmp, f"com{i}.png")
        cw = make_comment(c, p)
        overlays.append((p, (W - cw) // 2, COMMENT_Y, c["start"], c["end"]))

    video = os.path.join(base_dir, cfg["video"])
    inputs = ["-loop", "1", "-i", bg, "-i", video]
    for p, *_ in overlays:
        inputs += ["-i", p]

    fc = [
        f"[1:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_W}:{VIDEO_H},setsar=1[vid]",
        f"[0:v][vid]overlay=0:{VIDEO_Y}:shortest=1[v0]",
    ]
    last = "v0"
    for n, (_, x, y, s, e) in enumerate(overlays, start=1):
        fc.append(f"[{last}][{n + 1}:v]overlay={x}:{y}:enable='between(t,{s},{e})'[v{n}]")
        last = f"v{n}"

    if preview:
        t = cfg.get("preview_time", 0.5)
        cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(fc),
               "-map", f"[{last}]", "-ss", str(t), "-frames:v", "1", preview]
    else:
        out = os.path.join(base_dir, cfg.get("output", "output.mp4"))
        dur = probe_duration(video)
        cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(fc),
               "-map", f"[{last}]", "-map", "1:a?", "-t", f"{dur:.3f}",
               "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
               "-r", "30", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)
    print(preview or out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--preview", help="첫 화면을 PNG로 저장 (영상은 만들지 않음)")
    args = ap.parse_args()
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    build(cfg, os.path.dirname(os.path.abspath(args.config)), args.preview)


if __name__ == "__main__":
    sys.exit(main())
