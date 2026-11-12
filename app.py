import io
import re
import time
from pathlib import Path

import streamlit as st
from PIL import Image

import engine as E

# -----------------------------------
# CONFIG
# -----------------------------------
st.set_page_config(page_title="Ajvoro Story Video Studio", page_icon="🎬", layout="wide")

ROOT = Path(__file__).parent
OUTPUT_DIR = ROOT / "outputs"
ASSET_DIR = OUTPUT_DIR / "assets"
ASSET_DIR.mkdir(parents=True, exist_ok=True)

st.markdown("""
<style>
.stApp { background-color: #0b0d12; }
section[data-testid="stSidebar"] { background-color: #111620; }
h1, h2, h3 { color: #f3f4f6; }
div.stButton > button { border-radius: 9px; font-weight: bold; }
div.stButton > button[kind="primary"] { background-color: #6d28d9; color: white; border: 0; }
</style>
""", unsafe_allow_html=True)

ss = st.session_state


def init(key, default):
    if key not in ss:
        ss[key] = default


# -----------------------------------
# STATE DEFAULTS
# -----------------------------------
init("scenes", [])
init("next_id", 1)
init("story", "")
init("title", "My Story")
init("genre", "Horror")
init("aspect", "16:9 YouTube")
init("resolution", "720p")
init("fps", 24)
init("quality", "Standard")
init("fit_mode", E.FIT_MODES[0])
init("preset", "Horror (classic)")
init("effects", E.PRESETS["Horror (classic)"]["effects"])
init("grade", E.PRESETS["Horror (classic)"]["grade"])
init("strength", 0.65)
init("auto_effects", True)
init("letterbox", False)
init("fade_in_out", True)
init("transition", "Crossfade")
init("transition_len", 0.8)
init("captions", E.CAPTION_MODES[0])
init("caption_pos", "Bottom")
init("caption_size", 0.05)
init("caption_color", "#FFFFFF")
init("watermark", "")
init("title_text", "")
init("end_text", "")
init("narration", False)
init("tts_engine", E.TTS_ENGINES[0])
init("tts_rate", 150)
init("narration_vol", 1.0)
init("ambience", True)
init("amb_style", E.PRESETS["Horror (classic)"]["ambience"])
init("ambience_vol", 0.6)
init("sfx", True)
init("music_vol", 0.35)
init("duck", 0.6)
init("thumb_text", "")


# -----------------------------------
# CALLBACKS
# -----------------------------------
def apply_preset():
    p = E.PRESETS[ss.preset]
    ss.effects = list(p["effects"])
    ss.grade = p["grade"]
    ss.strength = p["strength"]
    ss.amb_style = p["ambience"]


def split_cb():
    parts = E.split_story_into(ss.story, len(ss.scenes))
    for sc, text in zip(ss.scenes, parts):
        ss[f"txt_{sc['id']}"] = text


def move_scene(i, d):
    j = i + d
    if 0 <= j < len(ss.scenes):
        ss.scenes[i], ss.scenes[j] = ss.scenes[j], ss.scenes[i]


def build_cfg() -> E.RenderConfig:
    return E.RenderConfig(
        aspect=ss.aspect, resolution=ss.resolution, fps=int(ss.fps), quality=ss.quality,
        fit_mode=ss.fit_mode, strength=ss.strength, effects=list(ss.effects),
        auto_effects=ss.auto_effects, grade=ss.grade, letterbox=ss.letterbox,
        fade_in_out=ss.fade_in_out, transition=ss.transition, transition_len=ss.transition_len,
        captions=ss.captions, caption_pos=ss.caption_pos, caption_size=ss.caption_size,
        caption_color=ss.caption_color, font_path=ss.get("font_path"),
        watermark=ss.watermark, title_text=ss.title_text, end_text=ss.end_text,
        narration=ss.narration, tts_engine=ss.tts_engine, tts_voice=ss.get("tts_voice"),
        tts_rate=int(ss.tts_rate), piper_model=ss.get("piper_model") or None,
        narration_vol=ss.narration_vol, ambience=ss.ambience, ambience_style=ss.amb_style,
        ambience_vol=ss.ambience_vol, sfx=ss.sfx, music_path=ss.get("music_path"),
        music_vol=ss.music_vol, duck=ss.duck,
    )

def require_password():

    expected_password = st.secrets["APP_PASSWORD"]

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:

        st.title("🔐 Ajvoro Story Video Studio")

        password = st.text_input(
            "Enter Password",
            type="password"
        )

        if st.button("Login"):
            if password == expected_password:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect password!")

        st.stop()

def scene_obj(sc) -> E.Scene:
    i = sc["id"]
    return E.Scene(
        image=Image.open(io.BytesIO(sc["data"])).convert("RGB"),
        text=ss.get(f"txt_{i}", ""),
        duration=(ss.get(f"dur_{i}", 0.0) or None),
        camera=ss.get(f"cam_{i}", E.CAMERAS[0]),
        extra_effects=list(ss.get(f"fx_{i}", [])),
        jumpscare=ss.get(f"scare_{i}", False),
        narrate=ss.get(f"nar_{i}", True),
    )


# -----------------------------------
# HEADER + SIDEBAR
# -----------------------------------
require_password()
st.title("🎬 Ajvoro STORY VIDEO STUDIO")
st.caption("Images + story → YouTube-ready video. Fully offline: effects, camera, captions, "
           "narration, sound design, thumbnail and metadata.")

with st.sidebar:
    st.header("Video Settings")
    st.selectbox("Format", list(E.ASPECTS), key="aspect")
    st.selectbox("Resolution", list(E.RESOLUTIONS), key="resolution",
                 help="Use 360p/540p for quick test renders, 1080p for final upload.")
    st.selectbox("Frame rate", [24, 30], key="fps")
    st.selectbox("Encode quality", list(E.QUALITY), key="quality")
    st.selectbox("Image fitting", E.FIT_MODES, key="fit_mode",
                 help="Use the blurred-background mode when your image shape differs "
                      "from the video shape (e.g. portrait images in a 16:9 video).")
    st.divider()
    st.caption("Tip: do a 360p draft first, then render the final at 1080p.")

tab_story, tab_scenes, tab_look, tab_audio, tab_gen = st.tabs(
    ["📖 Story & Images", "🎞 Scenes", "🎨 Look & Effects", "🔊 Audio & Voice", "🚀 Generate"])

# -----------------------------------
# TAB 1: STORY & IMAGES
# -----------------------------------
with tab_story:
    c1, c2 = st.columns([1, 1])
    with c1:
        st.text_input("Video title", key="title")
        st.selectbox("Genre (for YouTube tags)", list(E.GENRE_TAGS), key="genre")
        st.text_area(
            "Full story", key="story", height=320,
            placeholder="Paste your whole story here. Then upload your images and press "
                        "'Auto-split story across images'.")
    with c2:
        files = st.file_uploader("Upload your images (one per scene, ordered by file name)",
                                 type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True)
        known = {s["sig"] for s in ss.scenes}
        sigs = set()
        for f in sorted(files or [], key=lambda x: x.name):
            sig = f"{f.name}:{f.size}"
            sigs.add(sig)
            if sig not in known:
                ss.scenes.append(dict(id=ss.next_id, sig=sig, name=f.name, data=f.getvalue()))
                ss.next_id += 1
        ss.scenes = [s for s in ss.scenes if s["sig"] in sigs]

        st.button("✂ Auto-split story across images", on_click=split_cb,
                  disabled=not (ss.scenes and ss.story.strip()), use_container_width=True)
        if ss.scenes:
            st.caption(f"{len(ss.scenes)} scene(s). Edit order and per-scene text in the Scenes tab.")
            cols = st.columns(4)
            for k, sc in enumerate(ss.scenes):
                cols[k % 4].image(sc["data"], caption=f"{k + 1}. {sc['name']}", use_container_width=True)

# -----------------------------------
# TAB 2: SCENES
# -----------------------------------
with tab_scenes:
    if not ss.scenes:
        st.info("Upload images in the first tab.")
    for i, sc in enumerate(ss.scenes):
        sid = sc["id"]
        init(f"txt_{sid}", "")
        init(f"dur_{sid}", 0.0)
        init(f"cam_{sid}", E.CAMERAS[0])
        init(f"fx_{sid}", [])
        init(f"scare_{sid}", False)
        init(f"nar_{sid}", True)
        with st.container(border=True):
            a, b = st.columns([1, 2])
            with a:
                st.image(sc["data"], use_container_width=True)
                m1, m2, _ = st.columns([1, 1, 2])
                m1.button("↑", key=f"up_{sid}", on_click=move_scene, args=(i, -1),
                          disabled=i == 0)
                m2.button("↓", key=f"dn_{sid}", on_click=move_scene, args=(i, 1),
                          disabled=i == len(ss.scenes) - 1)
            with b:
                st.markdown(f"**Scene {i + 1}**")
                st.text_area("Story text / narration", key=f"txt_{sid}", height=110)
                c1, c2 = st.columns(2)
                c1.number_input("Minimum duration (s, 0 = auto)", 0.0, 120.0, step=0.5, key=f"dur_{sid}")
                c2.selectbox("Camera", E.CAMERAS, key=f"cam_{sid}")
                st.multiselect("Extra effects for this scene", list(E.ALL_EFFECTS),
                               format_func=lambda k: E.ALL_EFFECTS[k], key=f"fx_{sid}")
                d1, d2, d3 = st.columns(3)
                d1.checkbox("Jump-scare (zoom punch + sting)", key=f"scare_{sid}")
                d2.checkbox("Narrate this scene", key=f"nar_{sid}")
                if d3.button("👁 Preview frame", key=f"pv_{sid}"):
                    try:
                        st.image(E.render_preview(scene_obj(sc), build_cfg()),
                                 caption="Preview (mid-scene, current look settings)")
                    except Exception as err:
                        st.error(f"Preview failed: {err}")

# -----------------------------------
# TAB 3: LOOK & EFFECTS
# -----------------------------------
with tab_look:
    p1, p2 = st.columns([3, 1])
    p1.selectbox("Style preset", list(E.PRESETS), key="preset")
    p2.button("Apply preset", on_click=apply_preset, use_container_width=True)

    l, r = st.columns(2)
    with l:
        st.subheader("Effects")
        st.multiselect("Effects on every scene", list(E.ALL_EFFECTS),
                       format_func=lambda k: E.ALL_EFFECTS[k], key="effects")
        st.checkbox("Auto-add effects from words in each scene's text (rain, fire, fog, ghost...)",
                    key="auto_effects")
        st.slider("Effect intensity", 0.0, 1.0, key="strength")
        st.selectbox("Colour grade", list(E.GRADES), key="grade")
        st.checkbox("Cinematic letterbox bars", key="letterbox")
        st.checkbox("Fade in / fade out", key="fade_in_out")
        st.selectbox("Transition between scenes", E.TRANSITIONS, key="transition")
        st.slider("Transition length (s)", 0.2, 2.0, step=0.1, key="transition_len")
    with r:
        st.subheader("Text on screen")
        st.selectbox("Captions", E.CAPTION_MODES, key="captions")
        cc1, cc2 = st.columns(2)
        cc1.selectbox("Caption position", ["Bottom", "Middle"], key="caption_pos")
        cc2.color_picker("Caption colour", key="caption_color")
        st.slider("Caption size", 0.03, 0.09, step=0.005, key="caption_size")
        font_up = st.file_uploader("Custom font (.ttf/.otf) - needed for Hindi/other scripts",
                                   type=["ttf", "otf"])
        if font_up:
            fp = ASSET_DIR / font_up.name
            fp.write_bytes(font_up.getvalue())
            ss["font_path"] = str(fp)
        st.text_input("Channel watermark (top-right)", key="watermark")
        st.text_input("Intro title card text (blank = none)", key="title_text")
        st.text_input("End card text (blank = none)", key="end_text",
                      placeholder="Subscribe for more stories!")

# -----------------------------------
# TAB 4: AUDIO & VOICE
# -----------------------------------
with tab_audio:
    a1, a2 = st.columns(2)
    with a1:
        st.subheader("Narration (offline TTS)")
        st.checkbox("Generate narration from scene text", key="narration")
        st.selectbox("Voice engine", E.TTS_ENGINES, key="tts_engine")
        if ss.tts_engine.startswith("Piper"):
            st.text_input("Path to Piper voice model (.onnx)", key="piper_model")
        else:
            voices = E.list_voices()
            if voices:
                ids = [v[0] for v in voices]
                names = dict(voices)
                init("tts_voice", ids[0])
                if ss.tts_voice not in ids:
                    ss.tts_voice = ids[0]
                st.selectbox("System voice", ids, format_func=lambda v: names[v], key="tts_voice")
            else:
                st.warning("No system voices found. On Linux install espeak-ng; "
                           "or use Piper.")
        st.slider("Speaking rate (system voice)", 90, 260, key="tts_rate")
        st.slider("Narration volume", 0.2, 1.5, key="narration_vol")
    with a2:
        st.subheader("Sound design")
        st.checkbox("Generated ambience", key="ambience")
        st.selectbox("Ambience style", E.AMBIENCE_STYLES, key="amb_style")
        st.slider("Ambience volume", 0.0, 1.5, key="ambience_vol")
        st.checkbox("Sound effects (thunder on lightning, jump-scare stings)", key="sfx")
        music_up = st.file_uploader("Background music (optional)", type=["mp3", "wav", "m4a", "ogg"])
        if music_up:
            mp = ASSET_DIR / music_up.name
            mp.write_bytes(music_up.getvalue())
            ss["music_path"] = str(mp)
        else:
            ss["music_path"] = None
        st.slider("Music volume", 0.0, 1.0, key="music_vol")
        st.slider("Lower music/ambience while narrator speaks", 0.0, 0.9, key="duck")

# -----------------------------------
# TAB 5: GENERATE
# -----------------------------------
with tab_gen:
    n_scenes = len(ss.scenes)
    words = sum(len(ss.get(f"txt_{s['id']}", "").split()) for s in ss.scenes)
    st.write(f"**{n_scenes} scene(s)**, about **{words} words** → roughly "
             f"{int(words / 2.6 + n_scenes * 1.5)} seconds of video "
             f"(exact length is set by narration or reading speed).")

    def progress_cb(frac, msg):
        bar.progress(min(max(frac, 0.0), 1.0))
        status.info(msg)

    if st.button("🎬 GENERATE VIDEO", type="primary", use_container_width=True,
                 disabled=n_scenes == 0):
        bar = st.progress(0.0)
        status = st.empty()
        try:
            cfg = build_cfg()
            scenes = [scene_obj(s) for s in ss.scenes]
            slug = re.sub(r"[^A-Za-z0-9_-]+", "_", ss.title).strip("_") or "video"
            out = OUTPUT_DIR / f"{slug}_{int(time.time())}.mp4"
            res = E.render_video(scenes, cfg, out, progress=progress_cb)
            thumb_title = ss.thumb_text or ss.title
            thumb = E.make_thumbnail(scenes[0].image, thumb_title, out.with_suffix(".jpg"),
                                     grade=ss.grade, font_path=ss.get("font_path"))
            meta = E.build_metadata(ss.title, ss.story, res.chapters, ss.genre, ss.watermark)
            ss["result"] = dict(video=str(res.video_path), srt=str(res.srt_path),
                                thumb=str(thumb), meta=meta, warnings=res.warnings,
                                duration=res.duration)
            status.success(f"Video generated ({res.duration:.0f}s).")
        except Exception as err:
            status.empty()
            st.error(f"Video generation failed: {err}")
            st.exception(err)

    res = ss.get("result")
    if res and Path(res["video"]).exists():
        for w in res["warnings"]:
            st.warning(w)
        st.video(res["video"])
        d1, d2, d3 = st.columns(3)
        d1.download_button("⬇ Video (MP4)", Path(res["video"]).read_bytes(),
                           file_name=Path(res["video"]).name, mime="video/mp4",
                           use_container_width=True)
        d2.download_button("⬇ Subtitles (SRT)", Path(res["srt"]).read_bytes(),
                           file_name=Path(res["srt"]).name, mime="text/plain",
                           use_container_width=True)
        d3.download_button("⬇ Thumbnail (JPG)", Path(res["thumb"]).read_bytes(),
                           file_name=Path(res["thumb"]).name, mime="image/jpeg",
                           use_container_width=True)

        st.subheader("Thumbnail")
        tc1, tc2 = st.columns([1, 1])
        with tc1:
            st.image(res["thumb"], use_container_width=True)
        with tc2:
            st.text_input("Thumbnail text", key="thumb_text", placeholder=ss.title)
            idx = st.selectbox("Use image from scene", range(1, len(ss.scenes) + 1)) - 1
            if st.button("Re-make thumbnail"):
                sc = scene_obj(ss.scenes[idx])
                E.make_thumbnail(sc.image, ss.thumb_text or ss.title, res["thumb"],
                                 grade=ss.grade, font_path=ss.get("font_path"))
                st.rerun()

        st.subheader("YouTube upload helper")
        meta = res["meta"]
        t = st.text_input("Title", meta["title"])
        dsc = st.text_area("Description", meta["description"], height=220)
        tg = st.text_area("Tags", meta["tags"], height=70)
        st.download_button("⬇ Save metadata (.txt)",
                           f"TITLE:\n{t}\n\nDESCRIPTION:\n{dsc}\n\nTAGS:\n{tg}\n",
                           file_name="youtube_metadata.txt")

with st.expander("Application Information"):
    st.write("""
    **What this does**
    - Many images + one story → one video, with scenes ordered and timed automatically
    - Camera moves per scene (zoom, pan, tilt, drift, handheld), crossfade / dip / flash / glitch transitions
    - Effects: fog, rain, snow, embers, lightning + thunder, bloom, grain, scratches, flicker, shake,
      RGB split, glitch, heartbeat pulse, colour grades, letterbox
    - Burned-in captions (YouTube style) + SRT file, channel watermark, intro and end cards
    - Offline narration (system voice or Piper), generated ambience, sound effects, background music with auto-ducking
    - Landscape (YouTube), vertical (Shorts/Reels) and square output
    - Auto thumbnail and YouTube title / description / tags / chapters

    Animation is procedural 2D/2.5D (camera + effects on your images), not neural character motion.
    """)
