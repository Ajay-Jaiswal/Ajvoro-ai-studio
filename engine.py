"""
engine.py - Story Video Studio engine (offline, procedural 2D/2.5D animation)

Pipeline:  images + story  ->  scenes  ->  narration (optional)  ->  audio mix
           ->  frame-by-frame effects/camera/captions  ->  FFmpeg  ->  MP4 + SRT
"""

import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import imageio_ffmpeg
import numpy as np
from PIL import (
    Image,
    ImageColor,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageOps,
)

SR = 44100  # audio sample rate


# =====================================================================
# CATALOGS (used by the UI)
# =====================================================================

ALL_EFFECTS: Dict[str, str] = {
    "fog": "Rolling fog",
    "rain": "Rain",
    "snow": "Snow",
    "embers": "Floating embers / sparks",
    "lightning": "Lightning + thunder",
    "glow": "Bloom / glow",
    "grain": "Film grain",
    "scratches": "Old-film scratches & dust",
    "flicker": "Light flicker",
    "shake": "Handheld shake",
    "chromatic": "Chromatic aberration (RGB split)",
    "glitch": "Digital glitch bursts",
    "heartbeat": "Heartbeat red pulse",
    "dark": "Darken (night mood)",
}

KEYWORDS: Dict[str, List[str]] = {
    "fog": ["fog", "mist", "smoke", "haze", "cloud"],
    "rain": ["rain", "storm", "drizzle", "monsoon"],
    "snow": ["snow", "blizzard", "winter", "frozen"],
    "embers": ["fire", "ember", "burning", "flame", "spark", "candle"],
    "lightning": ["lightning", "thunder", "flash"],
    "glow": ["glow", "glowing", "eyes", "magic"],
    "grain": ["grain", "old film", "vintage"],
    "scratches": ["old film", "vintage", "ancient", "tape"],
    "flicker": ["flicker", "flickering", "candle", "lamp"],
    "shake": ["shake", "handheld", "earthquake", "chase"],
    "chromatic": ["glitch", "vhs", "distort", "possessed"],
    "glitch": ["glitch", "possessed", "demon", "static", "corrupt"],
    "heartbeat": ["heartbeat", "panic", "terror", "afraid", "scream"],
    "dark": ["dark", "horror", "night", "scary", "shadow", "midnight"],
}

GRADES: Dict[str, Optional[Tuple[Tuple[float, float, float], float, float]]] = {
    # name: (rgb multiplier, saturation, contrast)
    "None": None,
    "Cold horror": ((0.86, 0.98, 1.16), 0.75, 1.15),
    "Blood red": ((1.25, 0.78, 0.78), 0.90, 1.20),
    "Noir black & white": ((1.0, 1.0, 1.0), 0.0, 1.30),
    "Old sepia film": ((1.12, 1.0, 0.78), 0.45, 1.10),
    "Toxic green": ((0.82, 1.12, 0.85), 0.80, 1.15),
    "Moonlight blue": ((0.78, 0.92, 1.25), 0.70, 1.05),
}

CAMERAS = [
    "Auto (varied per scene)",
    "Slow zoom in",
    "Slow zoom out",
    "Pan left",
    "Pan right",
    "Tilt up",
    "Tilt down",
    "Diagonal drift",
    "Handheld",
    "Static",
]
AUTO_CYCLE = [
    "Slow zoom in", "Pan right", "Slow zoom out", "Tilt up",
    "Diagonal drift", "Pan left", "Tilt down",
]

TRANSITIONS = ["Crossfade", "Dip to black", "Flash", "Glitch cut", "Cut"]
CAPTION_MODES = ["Phrases (YouTube style)", "Full scene text", "Off"]
AMBIENCE_STYLES = [
    "Dark drone + wind",
    "Stormy night (wind + rain)",
    "Heartbeat tension",
    "Calm mystery pad",
]
ASPECTS = {
    "16:9 YouTube": (16, 9),
    "9:16 Shorts / Reels": (9, 16),
    "1:1 Square": (1, 1),
}
RESOLUTIONS = {"1080p": 1080, "720p": 720, "540p (preview)": 540, "360p (draft)": 360}
QUALITY = {
    "Draft (fast)": ("ultrafast", 28),
    "Standard": ("veryfast", 21),
    "High (YouTube)": ("medium", 18),
}
FIT_MODES = ["Fill (crop to frame)", "Fit with blurred background"]
TTS_ENGINES = ["System voice (pyttsx3)", "Piper (offline neural)"]

PRESETS = {
    "Horror (classic)": dict(
        effects=["fog", "lightning", "glow", "grain", "dark", "scratches"],
        grade="Cold horror", strength=0.65, ambience="Dark drone + wind"),
    "Haunted storm": dict(
        effects=["fog", "rain", "lightning", "flicker", "dark", "grain"],
        grade="Moonlight blue", strength=0.7, ambience="Stormy night (wind + rain)"),
    "Demonic / possessed": dict(
        effects=["glitch", "chromatic", "heartbeat", "flicker", "grain", "dark"],
        grade="Blood red", strength=0.75, ambience="Heartbeat tension"),
    "Old film mystery": dict(
        effects=["grain", "scratches", "flicker", "fog"],
        grade="Old sepia film", strength=0.6, ambience="Calm mystery pad"),
    "Winter / emotional": dict(
        effects=["snow", "fog", "glow"],
        grade="Moonlight blue", strength=0.5, ambience="Calm mystery pad"),
    "Fantasy magic": dict(
        effects=["embers", "glow", "fog"],
        grade="None", strength=0.55, ambience="Calm mystery pad"),
    "Clean (no effects)": dict(
        effects=[], grade="None", strength=0.5, ambience="Calm mystery pad"),
}

GENRE_TAGS = {
    "Horror": ["horror story", "scary story", "ghost story", "creepy", "animated horror"],
    "Mystery": ["mystery story", "detective story", "unsolved", "suspense"],
    "Fantasy": ["fantasy story", "magic", "fairy tale", "adventure story"],
    "Kids story": ["kids story", "bedtime story", "moral story", "storytelling"],
    "Motivational": ["motivational story", "inspiring story", "life lessons"],
    "Documentary": ["documentary", "history", "facts", "explained"],
}


# =====================================================================
# DATA CLASSES
# =====================================================================

@dataclass
class Scene:
    image: Image.Image
    text: str = ""
    duration: Optional[float] = None          # minimum seconds (None = auto)
    camera: str = CAMERAS[0]
    extra_effects: List[str] = field(default_factory=list)
    jumpscare: bool = False
    narrate: bool = True


@dataclass
class RenderConfig:
    # video
    aspect: str = "16:9 YouTube"
    resolution: str = "1080p"
    fps: int = 24
    quality: str = "Standard"
    fit_mode: str = FIT_MODES[0]
    # look
    strength: float = 0.65
    effects: List[str] = field(default_factory=lambda: ["fog", "glow", "grain", "dark"])
    auto_effects: bool = True
    grade: str = "Cold horror"
    letterbox: bool = False
    fade_in_out: bool = True
    transition: str = "Crossfade"
    transition_len: float = 0.8
    # text
    captions: str = CAPTION_MODES[0]
    caption_pos: str = "Bottom"
    caption_size: float = 0.05
    caption_color: str = "#FFFFFF"
    font_path: Optional[str] = None
    watermark: str = ""
    title_text: str = ""
    title_len: float = 4.0
    end_text: str = ""
    end_len: float = 4.0
    # timing
    min_scene: float = 4.0
    default_scene: float = 5.0
    words_per_sec: float = 2.6
    # narration
    narration: bool = False
    tts_engine: str = TTS_ENGINES[0]
    tts_voice: Optional[str] = None
    tts_rate: int = 150
    piper_model: Optional[str] = None
    narration_vol: float = 1.0
    # audio
    ambience: bool = True
    ambience_style: str = AMBIENCE_STYLES[0]
    ambience_vol: float = 0.6
    sfx: bool = True
    music_path: Optional[str] = None
    music_vol: float = 0.35
    duck: float = 0.6
    seed: int = 2026


@dataclass
class RenderResult:
    video_path: Path
    srt_path: Path
    duration: float
    chapters: List[Tuple[float, str]]
    warnings: List[str]


# =====================================================================
# SMALL HELPERS
# =====================================================================

def smoothstep(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def hash_noise(n: float) -> float:
    x = math.sin(n * 12.9898 + 78.233) * 43758.5453
    return x - math.floor(x)


def prompt_to_effects(prompt: str) -> List[str]:
    """Detect effects from words in a scene's text."""
    text = (prompt or "").lower()
    words = re.findall(r"\w+", text)
    found = []
    for effect, kws in KEYWORDS.items():
        for kw in kws:
            hit = (kw in text) if " " in kw else any(w.startswith(kw) for w in words)
            if hit:
                found.append(effect)
                break
    return found


def get_size(aspect: str, resolution: str) -> Tuple[int, int]:
    s = RESOLUTIONS[resolution]
    aw, ah = ASPECTS[aspect]
    if aw >= ah:
        h, w = s, s * aw / ah
    else:
        w, h = s, s * ah / aw
    return int(round(w / 2) * 2), int(round(h / 2) * 2)


def fmt_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_clock(t: float) -> str:
    t = int(t)
    m, s = divmod(t, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def split_story_into(story: str, n: int) -> List[str]:
    """Split a story into n parts of roughly equal word count (sentence aware)."""
    story = (story or "").strip()
    if n <= 0:
        return []
    if not story:
        return [""] * n
    sentences = [s.strip() for s in re.split(r"(?<=[.!?\u0964])\s+|\n{2,}", story) if s.strip()]
    total = sum(len(s.split()) for s in sentences)
    target = max(1, total / n)
    parts, cur, cur_words = [], [], 0
    for sent in sentences:
        cur.append(sent)
        cur_words += len(sent.split())
        if cur_words >= target and len(parts) < n - 1:
            parts.append(" ".join(cur))
            cur, cur_words = [], 0
    if cur:
        parts.append(" ".join(cur))
    while len(parts) < n:
        parts.append("")
    return parts[:n]


# =====================================================================
# FONTS + TEXT LAYERS
# =====================================================================

FONT_CANDIDATES = [
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "arialbd.ttf",
]


def load_font(size: int, path: Optional[str] = None):
    for p in ([path] if path else []) + FONT_CANDIDATES:
        try:
            return ImageFont.truetype(str(p), size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def wrap_text(text: str, font, max_w: float) -> List[str]:
    meas = ImageDraw.Draw(Image.new("L", (1, 1)))
    lines: List[str] = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            test = (cur + " " + word).strip()
            if meas.textlength(test, font=font) <= max_w or not cur:
                cur = test
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines


def render_text_layer(text, width, height, font, fill=(255, 255, 255),
                      stroke_w=3, valign="bottom", halign="center", pad=0.06):
    """Returns (rgb float32 HxWx3, alpha float32 HxWx1) for a block of text."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad_px = int(width * pad)
    lines = wrap_text(text, font, width - 2 * pad_px)
    size = getattr(font, "size", 16)
    lh = int(size * 1.28)
    th = lh * len(lines)
    if valign == "bottom":
        y = height - th - int(height * 0.04)
    elif valign == "top":
        y = int(height * 0.06)
    else:
        y = (height - th) // 2
    y = max(0, y)
    for line in lines:
        w = d.textlength(line, font=font)
        if halign == "right":
            x = width - w - pad_px
        elif halign == "left":
            x = pad_px
        else:
            x = (width - w) / 2
        d.text((x, y), line, font=font, fill=tuple(fill) + (255,),
               stroke_width=stroke_w, stroke_fill=(0, 0, 0, 255))
        y += lh
    arr = np.asarray(img, dtype=np.float32)
    return arr[..., :3].copy(), arr[..., 3:4] / 255.0


def chunk_words(text: str, max_words: int = 6) -> List[str]:
    chunks, cur = [], []
    for w in text.split():
        cur.append(w)
        end = w[-1] in ".!?\u0964"
        soft = w[-1] in ",;:" and len(cur) >= max_words - 2
        if len(cur) >= max_words or end or soft:
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        chunks.append(" ".join(cur))
    return chunks


# =====================================================================
# COLOUR GRADING / BLOOM
# =====================================================================

def apply_grade(frame: np.ndarray, name: str, strength: float) -> np.ndarray:
    g = GRADES.get(name)
    if not g:
        return frame
    mult, sat, con = g
    gray = (frame @ np.array([0.299, 0.587, 0.114], np.float32))[..., None]
    out = gray + (frame - gray) * sat
    out = out * np.array(mult, np.float32)
    out = (out - 128.0) * con + 128.0
    m = min(1.0, 0.45 + 0.7 * strength)
    return frame * (1 - m) + out * m


def bloom(frame: np.ndarray, strength: float, pulse: float = 1.0) -> np.ndarray:
    h, w, _ = frame.shape
    small = Image.fromarray(np.clip(frame, 0, 255).astype(np.uint8)).resize(
        (max(8, w // 4), max(8, h // 4)), Image.Resampling.BILINEAR)
    bright = np.clip(np.asarray(small, np.float32) - 135.0, 0, None) * 2.2
    blur = Image.fromarray(np.clip(bright, 0, 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(max(3, h // 220)))
    big = np.asarray(blur.resize((w, h), Image.Resampling.BILINEAR), np.float32)
    return frame + big * (0.95 * strength * pulse)


# =====================================================================
# CAMERA
# =====================================================================

def camera_state(mode: str, p: float, s: float, t: float):
    """Returns (zoom, cx, cy). cx/cy are 0..1 positions inside the movable range."""
    e = smoothstep(p)
    z = 0.12 + 0.30 * s
    pz = 1.10 + 0.25 * s
    if mode == "Slow zoom in":
        return 1 + z * e, 0.5, 0.5
    if mode == "Slow zoom out":
        return 1 + z * (1 - e), 0.5, 0.5
    if mode == "Pan left":
        return pz, 1 - e, 0.5
    if mode == "Pan right":
        return pz, e, 0.5
    if mode == "Tilt up":
        return pz, 0.5, 1 - e
    if mode == "Tilt down":
        return pz, 0.5, e
    if mode == "Diagonal drift":
        return 1.10 + 0.30 * s * e, 0.15 + 0.7 * e, 0.2 + 0.6 * e
    if mode == "Handheld":
        return (1.08,
                0.5 + 0.30 * math.sin(t * 1.3) + 0.10 * math.sin(t * 3.1),
                0.5 + 0.30 * math.cos(t * 1.1) + 0.10 * math.sin(t * 2.7 + 1))
    return 1.0, 0.5, 0.5


def prepare_base(image: Image.Image, bw: int, bh: int, mode: str) -> Image.Image:
    image = image.convert("RGB")
    cover = ImageOps.fit(image, (bw, bh), Image.Resampling.LANCZOS)
    if mode.startswith("Fill"):
        return cover
    bg = cover.filter(ImageFilter.GaussianBlur(max(8, bw // 40)))
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    iw, ih = image.size
    sc = min(bw / iw, bh / ih)
    fg = image.resize((max(1, int(iw * sc)), max(1, int(ih * sc))), Image.Resampling.LANCZOS)
    bg.paste(fg, ((bw - fg.width) // 2, (bh - fg.height) // 2))
    return bg


# =====================================================================
# SCENE RENDERER  (camera + every visual effect for ONE image)
# =====================================================================

class SceneRenderer:
    def __init__(self, image, effects, camera, cfg: RenderConfig, size, seed, duration, scare=False):
        self.W, self.H = size
        W, H = self.W, self.H
        self.fx = set(effects)
        self.camera = camera
        self.s = cfg.strength
        self.grade = cfg.grade
        self.seed = int(seed)
        self.dur = duration
        self.scare = scare
        self.bw = (int(W * 1.45) // 2) * 2
        self.bh = (int(H * 1.45) // 2) * 2
        self.base = prepare_base(image, self.bw, self.bh, cfg.fit_mode)
        rng = np.random.default_rng(self.seed)
        scale = max(1.0, H / 540.0)

        yy, xx = np.ogrid[-1:1:complex(H), -1:1:complex(W)]
        r2 = (xx * xx + yy * yy).astype(np.float32)
        self.vig = (1 - np.clip(r2 * 0.42 * (0.35 + self.s), 0, 0.78))[..., None].astype(np.float32)
        self.edge = (np.clip(r2 / 2, 0, 1) ** 1.3)[..., None].astype(np.float32)

        if "fog" in self.fx:
            nh, nw = max(3, H // 110), max(3, W // 110)
            noise = np.tile(rng.random((nh, nw)).astype(np.float32), (1, 2))
            tex = Image.fromarray((noise * 255).astype(np.uint8)).resize(
                (W * 2, H), Image.Resampling.BICUBIC)
            tex = tex.filter(ImageFilter.GaussianBlur(max(10, H // 28)))
            f = np.asarray(tex, np.float32) / 255.0
            f = (f - f.min()) / (f.max() - f.min() + 1e-6)
            self.fog_tex = f
            self.fog_grad = np.linspace(0.6, 1.25, H, dtype=np.float32)[:, None]

        n = 500
        self.rx = rng.uniform(0, W, n)
        self.ry = rng.uniform(0, H + 80, n)
        self.rsp = rng.uniform(0.9, 1.5, n) * H * 1.1
        self.rlen = rng.uniform(0.02, 0.05, n) * H
        self.rval = rng.integers(70, 150, n)
        self.rw = np.where(rng.random(n) < 0.3, 2, 1) * max(1, H // 540)

        self.sx = rng.uniform(0, W, 400)
        self.sy = rng.uniform(0, H, 400)
        self.ssp = rng.uniform(0.06, 0.18, 400) * H
        self.sr = rng.uniform(1.2, 3.8, 400) * scale
        self.sph = rng.uniform(0, 6.28, 400)
        self.samp = rng.uniform(0.02, 0.05, 400) * W
        self.sfq = rng.uniform(0.4, 1.2, 400)

        self.ex = rng.uniform(0, W, 160)
        self.ey = rng.uniform(0, H, 160)
        self.esp = rng.uniform(0.05, 0.16, 160) * H
        self.er = rng.uniform(1.0, 3.0, 160) * scale
        self.eph = rng.uniform(0, 6.28, 160)
        self.eamp = rng.uniform(0.01, 0.04, 160) * W
        self.efq = rng.uniform(0.5, 1.6, 160)

        self.flash_times: List[float] = []
        if "lightning" in self.fx:
            t = float(rng.uniform(0.8, 2.2))
            while t < duration:
                self.flash_times.append(t)
                t += float(rng.uniform(3.0, 6.5))

    # ---- particle layers -------------------------------------------------
    def _rain(self, t):
        W, H = self.W, self.H
        img = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(img)
        n = int(120 + 380 * self.s)
        ys = (self.ry[:n] + self.rsp[:n] * t) % (H + 80) - 40
        for i in range(n):
            x, y, l = self.rx[i], ys[i], self.rlen[i]
            d.line((x, y, x - l * 0.2, y + l), fill=int(self.rval[i]), width=int(self.rw[i]))
        return np.asarray(img, np.float32)[..., None] * np.array([0.85, 0.9, 1.0], np.float32)

    def _snow(self, t):
        W, H = self.W, self.H
        img = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(img)
        n = int(80 + 320 * self.s)
        for i in range(n):
            y = (self.sy[i] + self.ssp[i] * t) % H
            x = (self.sx[i] + self.samp[i] * math.sin(t * self.sfq[i] + self.sph[i])) % W
            r = self.sr[i]
            d.ellipse((x - r, y - r, x + r, y + r), fill=235)
        return np.asarray(img, np.float32)[..., None] * 0.85

    def _embers(self, t):
        W, H = self.W, self.H
        img = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(img)
        n = int(40 + 120 * self.s)
        for i in range(n):
            y = (self.ey[i] - self.esp[i] * t) % H
            x = (self.ex[i] + self.eamp[i] * math.sin(t * self.efq[i] + self.eph[i])) % W
            r = self.er[i]
            tw = 0.6 + 0.4 * math.sin(t * 7 + self.eph[i] * 3)
            d.ellipse((x - r * 2.4, y - r * 2.4, x + r * 2.4, y + r * 2.4),
                      fill=(int(70 * tw), int(28 * tw), 4))
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, int(150 + 50 * tw), 50))
        return np.asarray(img, np.float32)

    def _flash(self, t):
        v = 0.0
        for ft in self.flash_times:
            d = t - ft
            if d < 0:
                break
            if d < 0.07:
                v = max(v, 1.0)
            elif d < 0.15:
                v = max(v, 0.25)
            elif d < 0.25:
                v = max(v, 0.8)
            elif d < 0.9:
                v = max(v, 0.8 * math.exp(-(d - 0.25) * 5))
        return v

    def apply_glitch(self, f, gidx, strong=False):
        rng = np.random.default_rng((self.seed * 7919 + gidx) & 0xFFFFFFFF)
        H, W = self.H, self.W
        for _ in range(int(rng.integers(3, 9))):
            y = int(rng.integers(0, max(1, H - H // 10)))
            h = int(rng.integers(max(2, H // 90), max(3, H // 12)))
            dx = int(rng.integers(-W // 10, W // 10))
            f[y:y + h] = np.roll(f[y:y + h], dx, axis=1)
        k = max(2, H // 110) * (2 if strong else 1)
        f[..., 0] = np.roll(f[..., 0], k, axis=1)
        f[..., 2] = np.roll(f[..., 2], -k, axis=1)
        return f

    # ---- main --------------------------------------------------------------
    def frame(self, t: float, gidx: int) -> np.ndarray:
        W, H, s, fx = self.W, self.H, self.s, self.fx
        p = min(max(t / max(self.dur, 1e-6), 0.0), 1.0)
        zoom, cx, cy = camera_state(self.camera, p, s, t)
        if "shake" in fx:
            zoom = max(zoom, 1.07)
            amp = 0.25 + 0.3 * s
            cx += amp * (0.6 * math.sin(t * 17.0) + 0.4 * math.sin(t * 31.0 + 1.3))
            cy += amp * (0.6 * math.sin(t * 19.0 + 0.5) + 0.4 * math.sin(t * 27.0))
        scare_on = self.scare and t < 0.35
        if scare_on:
            zoom *= 1 + 0.3 * (1 - t / 0.35) ** 2
        cx, cy = min(max(cx, 0), 1), min(max(cy, 0), 1)
        vw, vh = self.bw / zoom, self.bh / zoom
        x0, y0 = (self.bw - vw) * cx, (self.bh - vh) * cy
        img = self.base.resize((W, H), Image.Resampling.BICUBIC, box=(x0, y0, x0 + vw, y0 + vh))
        f = np.asarray(img, dtype=np.float32)

        rng = np.random.default_rng((self.seed * 1000003 + gidx) & 0xFFFFFFFF)

        f = apply_grade(f, self.grade, s)
        if "dark" in fx:
            f = f * (1 - 0.22 * s)

        if "fog" in fx:
            off = int((t * W * 0.03) % W)
            fog = self.fog_tex[:, off:off + W] * self.fog_grad
            alpha = np.clip(fog * (0.55 * s), 0, 0.5)[..., None]
            f = f * (1 - alpha) + 185.0 * alpha

        if "snow" in fx:
            f = f + self._snow(t)
        if "rain" in fx:
            f = f + self._rain(t)
        if "embers" in fx:
            f = f + self._embers(t)

        if "lightning" in fx:
            v = self._flash(t)
            if v > 0:
                f = f + v * s * np.array([140, 150, 175], np.float32)
        if scare_on and t < 0.1:
            f = f + 160.0

        if "glow" in fx:
            f = bloom(f, s, 0.85 + 0.15 * math.sin(t * 2.2))

        if "flicker" in fx:
            f = f * (0.82 + 0.18 * hash_noise(gidx * 0.5 + self.seed))

        if "heartbeat" in fx:
            ph = (t * 1.1) % 1.0
            pulse = max(math.exp(-((ph - 0.08) * 14) ** 2), 0.6 * math.exp(-((ph - 0.32) * 14) ** 2))
            red = self.edge * (pulse * s)
            f[..., 0] += 90.0 * red[..., 0]
            f = f * (1 - 0.3 * red)

        if "chromatic" in fx:
            k = max(1, int(H / 540 * (1 + 3 * s)))
            f[..., 0] = np.roll(f[..., 0], k, axis=1)
            f[..., 2] = np.roll(f[..., 2], -k, axis=1)

        if scare_on or ("glitch" in fx and hash_noise(int(t * 3) + self.seed) > 0.86):
            f = self.apply_glitch(f, gidx, strong=scare_on)

        if "grain" in fx:
            f = f + rng.normal(0, 2 + 8 * s, (H, W, 1)).astype(np.float32)
        if "scratches" in fx:
            if rng.random() < 0.35:
                for _ in range(int(rng.integers(1, 4))):
                    x = int(rng.integers(0, W))
                    f[:, x:x + max(1, W // 1000)] += float(rng.uniform(25, 60))
            for _ in range(int(rng.integers(0, 6))):
                y, x = int(rng.integers(0, H - 4)), int(rng.integers(0, W - 4))
                f[y:y + 3, x:x + 3] += float(rng.choice([-70, 70]))

        return f * self.vig


# =====================================================================
# AUDIO
# =====================================================================

def _ma(x: np.ndarray, k: int) -> np.ndarray:
    """Fast centred moving average (same length as input)."""
    k = max(1, min(k, len(x)))
    cs = np.cumsum(np.concatenate([[0.0], x.astype(np.float64)]))
    out = (cs[k:] - cs[:-k]) / k
    left = k // 2
    return np.pad(out, (left, len(x) - len(out) - left), mode="edge")


def load_audio(ffmpeg: str, path, sr: int = SR) -> np.ndarray:
    p = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"],
        capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("Could not read audio: " + p.stderr.decode("utf-8", "replace")[-500:])
    return np.frombuffer(p.stdout, dtype=np.float32).copy()


def _heartbeat(n, rng=None):
    sig = np.zeros(n, np.float32)
    L = int(0.22 * SR)
    tt = np.arange(L) / SR
    thump = (np.sin(2 * np.pi * 52 * tt) * np.exp(-tt * 22)).astype(np.float32)
    for k in range(int(n / SR * 1.1) + 1):
        for off, g in ((0.0, 1.0), (0.27, 0.6)):
            i = int((k / 1.1 + off) * SR)
            if i >= n:
                continue
            m = min(L, n - i)
            sig[i:i + m] += thump[:m] * g
    return sig


def make_ambience(n: int, style: str, rng) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / SR
    drone = (0.5 * np.sin(2 * np.pi * 55 * t) + 0.35 * np.sin(2 * np.pi * 55.7 * t)
             + 0.25 * np.sin(2 * np.pi * 82.4 * t))
    drone = (drone * (0.55 + 0.45 * np.sin(2 * np.pi * 0.06 * t + 1.0))).astype(np.float32)

    wind = _ma(rng.normal(0, 1, n), 300)
    wind = (wind / (np.abs(wind).max() + 1e-9)).astype(np.float32)
    wind *= (0.5 + 0.5 * np.sin(2 * np.pi * 0.09 * t + 2.0)).astype(np.float32)

    if style == "Stormy night (wind + rain)":
        white = rng.normal(0, 1, n)
        rain = (white - _ma(white, 24))
        rain = (rain / (np.abs(rain).max() + 1e-9)).astype(np.float32)
        return drone * 0.08 + wind * 0.5 + rain * 0.14
    if style == "Heartbeat tension":
        return drone * 0.18 + wind * 0.15 + _heartbeat(n) * 0.7
    if style == "Calm mystery pad":
        pad = sum(np.sin(2 * np.pi * f * t) for f in (220.0, 277.2, 329.6)) / 3.0
        pad = pad * (0.6 + 0.4 * np.sin(2 * np.pi * 0.15 * t))
        return (pad * 0.12 + wind * 0.08).astype(np.float32)
    return drone * 0.16 + wind * 0.35


def _add(dst: np.ndarray, src: np.ndarray, at: float, gain: float = 1.0):
    i = int(at * SR)
    if i >= len(dst) or i < 0:
        return
    m = min(len(src), len(dst) - i)
    dst[i:i + m] += src[:m] * gain


def _thunder(sfx, at, rng):
    L = int(3.2 * SR)
    x = _ma(rng.normal(0, 1, L), 500)
    x = x / (np.abs(x).max() + 1e-9)
    ar = np.arange(L)
    rumble = x * np.exp(-ar / SR / 1.1) * np.minimum(1, ar / (0.15 * SR))
    _add(sfx, rumble.astype(np.float32), at + 0.2, 0.9)
    c = int(0.07 * SR)
    crack = rng.normal(0, 1, c) * np.exp(-np.arange(c) / SR * 45)
    _add(sfx, crack.astype(np.float32), at, 0.5)


def _sting(sfx, at, rng):
    L = int(2.0 * SR)
    t = np.arange(L) / SR
    tone = (np.sin(2 * np.pi * 233 * t) + np.sin(2 * np.pi * 246.9 * t)
            + 0.6 * np.sin(2 * np.pi * 466 * t)) / 2.6
    hit = rng.normal(0, 1, L) * np.exp(-t * 18)
    _add(sfx, (tone * np.exp(-t * 3.0) * 0.5 + hit * 0.5).astype(np.float32), at, 0.9)


# =====================================================================
# TEXT TO SPEECH
# =====================================================================

def list_voices():
    """[(voice_id, display_name)] for the system TTS (empty if unavailable)."""
    try:
        import pyttsx3
        eng = pyttsx3.init()
        voices = [(v.id, v.name) for v in eng.getProperty("voices")]
        eng.stop()
        return voices
    except Exception:
        return []


def synthesize(text: str, wav_path: Path, cfg: RenderConfig):
    wav_path = Path(wav_path)
    if wav_path.exists():
        wav_path.unlink()
    if cfg.tts_engine.startswith("Piper"):
        if not cfg.piper_model:
            raise RuntimeError("Piper voice model (.onnx) path is not set.")
        exe = shutil.which("piper")
        cmd = [exe] if exe else [sys.executable, "-m", "piper"]
        cmd += ["-m", cfg.piper_model, "-f", str(wav_path)]
        r = subprocess.run(cmd, input=text, capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            raise RuntimeError("Piper failed: " + (r.stderr or "")[-400:])
    else:
        import pyttsx3
        eng = pyttsx3.init()
        if cfg.tts_voice:
            eng.setProperty("voice", cfg.tts_voice)
        eng.setProperty("rate", int(cfg.tts_rate))
        eng.save_to_file(text, str(wav_path))
        eng.runAndWait()
        eng.stop()
    if not wav_path.exists() or wav_path.stat().st_size == 0:
        raise RuntimeError("The TTS engine produced no audio.")


# =====================================================================
# SEGMENTS (timeline)
# =====================================================================

@dataclass
class Seg:
    index: int
    kind: str                       # "title" | "scene" | "end"
    start: float
    dur: float
    renderer: SceneRenderer
    caps: List[Tuple[float, float, str]]
    overlay: str
    narr: Optional[np.ndarray]
    lead: float
    scare: bool

    @property
    def end(self):
        return self.start + self.dur


def build_segments(scenes: List[Scene], cfg: RenderConfig, size, narr: List[Optional[np.ndarray]]):
    overlap = cfg.transition_len if cfg.transition == "Crossfade" else 0.0
    lead, tail = 0.3, overlap + 0.4
    items = []
    if cfg.title_text.strip() and scenes:
        items.append(dict(kind="title", image=scenes[0].image, text="", overlay=cfg.title_text,
                          dur=cfg.title_len, camera="Slow zoom in", extra=[], narr=None, scare=False))
    scene_no = 0
    for i, sc in enumerate(scenes):
        words = len(sc.text.split())
        nar = narr[i] if i < len(narr) else None
        nlen = len(nar) / SR if nar is not None else 0.0
        if nlen:
            dur = lead + nlen + tail
        elif words:
            dur = max(cfg.min_scene, lead + words / cfg.words_per_sec + tail + 0.5)
        else:
            dur = cfg.default_scene
        if sc.duration:
            dur = max(dur if nlen else 0.0, float(sc.duration))
        cam = sc.camera
        if cam not in CAMERAS or cam == CAMERAS[0]:
            cam = AUTO_CYCLE[scene_no % len(AUTO_CYCLE)]
        items.append(dict(kind="scene", image=sc.image, text=sc.text, overlay="", dur=dur, camera=cam,
                          extra=sc.extra_effects, narr=nar, scare=sc.jumpscare, nlen=nlen))
        scene_no += 1
    if cfg.end_text.strip() and scenes:
        items.append(dict(kind="end", image=scenes[-1].image, text="", overlay=cfg.end_text,
                          dur=cfg.end_len, camera="Slow zoom out", extra=[], narr=None, scare=False))

    segs, cur = [], 0.0
    for idx, it in enumerate(items):
        fx = set(cfg.effects) | set(it["extra"])
        if cfg.auto_effects and it["text"]:
            fx |= set(prompt_to_effects(it["text"]))
        r = SceneRenderer(it["image"], fx, it["camera"], cfg, size, cfg.seed + idx * 101,
                          it["dur"], it["scare"])
        caps = []
        text = it["text"].strip()
        if text:
            a0 = lead
            a1 = (lead + it.get("nlen", 0)) if it.get("nlen") else max(a0 + 1.0, it["dur"] - tail)
            if cfg.captions == "Full scene text":
                caps = [(a0, a1, text)]
            else:
                chunks = chunk_words(text)
                wts = np.array([len(c.split()) + 0.6 for c in chunks])
                edges = a0 + (a1 - a0) * np.concatenate([[0], np.cumsum(wts) / wts.sum()])
                caps = [(float(edges[k]), float(edges[k + 1]), chunks[k]) for k in range(len(chunks))]
        segs.append(Seg(idx, it["kind"], cur, it["dur"], r, caps, it["overlay"], it["narr"], lead, it["scare"]))
        cur += it["dur"] - overlap
    total = segs[-1].end if segs else 0.0
    return segs, total


# =====================================================================
# COMPOSER (turns segments into final frames)
# =====================================================================

class Composer:
    def __init__(self, segs: List[Seg], cfg: RenderConfig, size, total: float):
        self.segs, self.cfg, self.total = segs, cfg, total
        self.W, self.H = size
        self.fps = cfg.fps
        self.overlap = cfg.transition_len if cfg.transition == "Crossfade" else 0.0
        self.bar = int(self.H * 0.115) if cfg.letterbox else 0
        self.cap_font = load_font(max(12, int(min(self.W, self.H) * cfg.caption_size)), cfg.font_path)
        try:
            self.cap_rgb = ImageColor.getrgb(cfg.caption_color)
        except Exception:
            self.cap_rgb = (255, 255, 255)
        self.strip_h = int(self.H * 0.28)
        if cfg.caption_pos == "Middle":
            self.cap_y = (self.H - self.strip_h) // 2 + int(self.H * 0.10)
        else:
            self.cap_y = self.H - self.bar - self.strip_h - int(self.H * 0.015)
        self.cap_cache, self.ov_cache = {}, {}
        self.wm = None
        if cfg.watermark.strip():
            wf = load_font(max(10, int(self.H * 0.028)), cfg.font_path)
            self.wm_h = int(self.H * 0.09)
            self.wm = render_text_layer(cfg.watermark.strip(), self.W, self.wm_h, wf,
                                        stroke_w=2, valign="top", halign="right")
            self.wm_y = self.bar

    @staticmethod
    def _blit(f, y, layer, k=1.0):
        rgb, a = layer
        h = rgb.shape[0]
        y2 = min(f.shape[0], y + h)
        h = y2 - y
        if h <= 0:
            return
        a = a[:h] * k
        f[y:y2] = f[y:y2] * (1 - a) + rgb[:h] * a

    def _overlay(self, f, s, tl):
        if s.index not in self.ov_cache:
            font = load_font(max(16, int(min(self.W, self.H) * 0.085)), self.cfg.font_path)
            self.ov_cache[s.index] = render_text_layer(
                s.overlay, self.W, self.H, font, fill=(255, 255, 255),
                stroke_w=max(3, self.H // 160), valign="center")
        f *= 0.55
        k = min(smoothstep(tl / 0.8), smoothstep((s.dur - tl) / 0.6))
        self._blit(f, 0, self.ov_cache[s.index], k)

    def _caption(self, f, s, tl):
        if self.cfg.captions == "Off":
            return
        for i, (a, b, txt) in enumerate(s.caps):
            if a <= tl < b:
                key = (s.index, i)
                if key not in self.cap_cache:
                    self.cap_cache[key] = render_text_layer(
                        txt, self.W, self.strip_h, self.cap_font, fill=self.cap_rgb,
                        stroke_w=max(2, getattr(self.cap_font, "size", 28) // 14))
                self._blit(f, self.cap_y, self.cap_cache[key], min(1.0, (tl - a) / 0.12))
                return

    def _seg_frame(self, s, t, g):
        tl = t - s.start
        f = s.renderer.frame(tl, g)
        tr, T = self.cfg.transition, max(0.05, self.cfg.transition_len)
        last = s.index == len(self.segs) - 1
        if tr == "Dip to black":
            if s.index > 0:
                f = f * min(1.0, tl / T)
            if not last:
                f = f * min(1.0, max(0.0, (s.dur - tl) / T))
        elif tr == "Flash" and s.index > 0 and tl < 0.3:
            f = f + 220.0 * (1 - tl / 0.3)
        elif tr == "Glitch cut" and s.index > 0 and tl < 0.45:
            f = s.renderer.apply_glitch(f, g, strong=True)
        if s.overlay:
            self._overlay(f, s, tl)
        return f

    def frame_at(self, g: int) -> np.ndarray:
        t = g / self.fps
        active = [s for s in self.segs if s.start <= t < s.end] or [self.segs[-1]]
        active = active[-2:]
        frames = [self._seg_frame(s, t, g) for s in active]
        if len(frames) == 2 and self.overlap > 0:
            a = smoothstep((t - active[1].start) / self.overlap)
            out = frames[0] * (1 - a) + frames[1] * a
        else:
            out = frames[-1]
        if self.bar:
            out[:self.bar] = 0
            out[self.H - self.bar:] = 0
        for s in active:
            self._caption(out, s, t - s.start)
        if self.wm is not None:
            self._blit(out, self.wm_y, self.wm, 0.65)
        if self.cfg.fade_in_out:
            out = out * min(1.0, smoothstep(t / 0.7), smoothstep((self.total - t) / 1.4))
        return np.clip(out, 0, 255).astype(np.uint8)


# =====================================================================
# AUDIO MIX
# =====================================================================

def build_audio(total: float, segs: List[Seg], cfg: RenderConfig, ffmpeg: str, warnings: List[str]):
    n = int(total * SR) + 1
    rng = np.random.default_rng(cfg.seed)
    bg = np.zeros(n, np.float32)
    sfx = np.zeros(n, np.float32)

    if cfg.ambience:
        bg += make_ambience(n, cfg.ambience_style, rng) * cfg.ambience_vol
    if cfg.sfx:
        for s in segs:
            for ft in s.renderer.flash_times:
                _thunder(sfx, s.start + ft, rng)
            if s.scare:
                _sting(sfx, s.start, rng)
    if cfg.music_path and Path(cfg.music_path).exists():
        try:
            music = load_audio(ffmpeg, cfg.music_path)
            if len(music):
                reps = int(math.ceil(n / len(music)))
                bg += np.tile(music, reps)[:n] * cfg.music_vol
        except Exception as e:
            warnings.append(f"Background music skipped: {e}")

    narr = np.zeros(n, np.float32)
    for s in segs:
        if s.narr is not None:
            _add(narr, s.narr, s.start + s.lead, cfg.narration_vol)

    blk = SR // 50
    nb = n // blk
    if nb > 2 and np.abs(narr).max() > 0:
        rms = np.sqrt((narr[:nb * blk].reshape(nb, blk) ** 2).mean(axis=1))
        act = (rms > 0.01).astype(np.float64)
        env = np.convolve(act, np.ones(25) / 25, mode="same")
        env = np.interp(np.arange(n), (np.arange(nb) + 0.5) * blk, env).astype(np.float32)
        bg *= 1 - cfg.duck * env

    out = bg + sfx * 0.8 + narr
    t = np.arange(n) / SR
    out *= np.minimum(1.0, t / 0.5) * np.clip((total - t) / 1.5, 0, 1)
    return np.tanh(out * 1.1).astype(np.float32)


def write_wav(path: Path, audio: np.ndarray):
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


# =====================================================================
# MAIN RENDER
# =====================================================================

def render_video(scenes: List[Scene], cfg: RenderConfig, output_path,
                 progress: Optional[Callable[[float, str], None]] = None) -> RenderResult:
    if not scenes:
        raise ValueError("Add at least one image.")
    if cfg.fps <= 0:
        raise ValueError("FPS must be positive.")
    cb = progress or (lambda f, m: None)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    size = get_size(cfg.aspect, cfg.resolution)
    W, H = size
    warnings: List[str] = []

    # 1) narration ---------------------------------------------------------
    narr: List[Optional[np.ndarray]] = [None] * len(scenes)
    if cfg.narration:
        with tempfile.TemporaryDirectory() as tmp:
            for i, sc in enumerate(scenes):
                if not (sc.text.strip() and sc.narrate):
                    continue
                cb(0.01 + 0.07 * i / len(scenes), f"Narrating scene {i + 1}/{len(scenes)} ...")
                try:
                    wav = Path(tmp) / f"n{i}.wav"
                    synthesize(sc.text, wav, cfg)
                    a = load_audio(ffmpeg, wav)
                    peak = float(np.abs(a).max()) if len(a) else 0
                    narr[i] = (a / peak * 0.9) if peak > 0 else None
                except Exception as e:
                    warnings.append(f"Scene {i + 1} narration failed: {e}")
                    if len(warnings) == 1 and "pyttsx3" in str(e).lower():
                        break

    # 2) timeline + audio ----------------------------------------------------
    cb(0.09, "Building timeline ...")
    segs, total = build_segments(scenes, cfg, size, narr)
    comp = Composer(segs, cfg, size, total)
    cb(0.10, "Mixing audio ...")
    audio = build_audio(total, segs, cfg, ffmpeg, warnings)

    # 3) frames -> ffmpeg ----------------------------------------------------
    preset, crf = QUALITY.get(cfg.quality, QUALITY["Standard"])
    total_frames = max(1, int(round(total * cfg.fps)))
    with tempfile.TemporaryDirectory() as tmp:
        wav_path = Path(tmp) / "mix.wav"
        write_wav(wav_path, audio)
        cmd = [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(cfg.fps), "-i", "-",
            "-i", str(wav_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-shortest",
            "-movflags", "+faststart", str(output_path),
        ]
        err = tempfile.TemporaryFile()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        t0 = time.time()
        try:
            for g in range(total_frames):
                proc.stdin.write(comp.frame_at(g).tobytes())
                if g % max(1, cfg.fps // 4) == 0 or g == total_frames - 1:
                    done = g + 1
                    eta = (time.time() - t0) / done * (total_frames - done)
                    cb(0.10 + 0.88 * done / total_frames,
                       f"Rendering frame {done}/{total_frames}  (about {int(eta)}s left)")
            proc.stdin.close()
        except BrokenPipeError:
            pass
        except Exception:
            proc.kill()
            proc.wait()
            raise
        rc = proc.wait()
        if rc != 0:
            err.seek(0)
            raise RuntimeError("FFmpeg error: " + err.read().decode("utf-8", "replace")[-2000:])
        err.close()

    # 4) subtitles + chapters --------------------------------------------------
    srt_path = output_path.with_suffix(".srt")
    lines, k = [], 1
    for s in segs:
        for a, b, txt in s.caps:
            lines.append(f"{k}\n{fmt_srt(s.start + a)} --> {fmt_srt(s.start + b)}\n{txt}\n")
            k += 1
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    chapters = [(s.start, f"Scene {n}") for n, s in enumerate([x for x in segs if x.kind == "scene"], 1)]
    cb(1.0, "Done")
    return RenderResult(output_path, srt_path, total, chapters, warnings)


def render_preview(scene: Scene, cfg: RenderConfig, at: float = 0.5) -> Image.Image:
    """Single still frame of one scene using current look settings (fast)."""
    c = replace(cfg, resolution="540p (preview)", title_text="", end_text="",
                fade_in_out=False, narration=False)
    size = get_size(c.aspect, c.resolution)
    segs, total = build_segments([scene], c, size, [None])
    comp = Composer(segs, c, size, total)
    return Image.fromarray(comp.frame_at(int(total * at * c.fps)))


# =====================================================================
# THUMBNAIL + YOUTUBE METADATA
# =====================================================================

def make_thumbnail(image: Image.Image, title: str, out_path, grade: str = "Cold horror",
                   font_path: Optional[str] = None, size=(1280, 720)) -> Path:
    W, H = size
    img = ImageOps.fit(image.convert("RGB"), size, Image.Resampling.LANCZOS)
    f = apply_grade(np.asarray(img, np.float32), grade, 0.8)
    f = (f - 128) * 1.12 + 128
    yy, xx = np.ogrid[-1:1:complex(H), -1:1:complex(W)]
    f *= (1 - np.clip((xx * xx + yy * yy) * 0.35, 0, 0.6))[..., None]
    f *= (0.35 + 0.65 * np.clip(np.linspace(0, 1, W) / 0.75, 0, 1))[None, :, None]
    img = Image.fromarray(np.clip(f, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    text = (title or "").strip().upper() or "WATCH NOW"
    fs = int(H * 0.17)
    while fs > 30:
        font = load_font(fs, font_path)
        lines = wrap_text(text, font, W * 0.62)
        if len(lines) * fs * 1.2 <= H * 0.8 and len(lines) <= 4:
            break
        fs -= 6
    y = (H - len(lines) * fs * 1.2) / 2
    for i, line in enumerate(lines):
        d.text((int(W * 0.04), y), line, font=font, fill=(255, 214, 10) if i == 0 else (255, 255, 255),
               stroke_width=max(4, fs // 14), stroke_fill=(0, 0, 0))
        y += fs * 1.2
    out_path = Path(out_path)
    img.save(out_path, quality=92)
    return out_path


STOP = set("""the a an and or but of to in on at for with from by is was were be been it its this that as he she
they them his her their you we our my me not no so if then than there when where who what which while into out
up down over under again very just had has have do did does will would could should about because after before
through""".split())


def build_metadata(title: str, story: str, chapters: List[Tuple[float, str]], genre: str = "Horror",
                   channel: str = "") -> Dict[str, str]:
    words = [w for w in re.findall(r"[A-Za-z]{4,}", (story or "").lower()) if w not in STOP]
    freq: Dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    top = [w for w, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:8]]
    tags = list(dict.fromkeys(GENRE_TAGS.get(genre, []) + top + ["animated story", "storytelling"]))
    hook = (story or "").strip().split("\n")[0][:180]
    desc = [f"{title}", "", hook + ("..." if len(hook) >= 180 else ""), ""]
    if len(chapters) >= 3 and all(b[0] - a[0] >= 10 for a, b in zip(chapters, chapters[1:])):
        desc.append("Chapters:")
        desc += [f"{fmt_clock(t)} {label}" for t, label in chapters]
        desc.append("")
    if channel:
        desc.append(f"Subscribe to {channel} for more stories!")
    desc.append(" ".join("#" + t.replace(" ", "") for t in tags[:4]))
    return dict(title=title, description="\n".join(desc), tags=", ".join(tags))
