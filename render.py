#!/usr/bin/env python3
"""
TheEconmaster render engine.
Input : job.json (script built by Claude in Make)
Output: out/video.mp4, out/thumbnail.jpg, out/subtitles.srt, out/meta.json

Env:
  ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL (optional)
  PEXELS_API_KEY
  TARGET_MB   (optional, max final file size, default 95 -> fits Make Core 100MB)
  MOCK=1      (offline test: fake voice + generated visuals)
"""
import base64, json, math, os, random, re, shutil, subprocess, sys, textwrap, time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 1920, 1080, 30
ROOT = Path(__file__).parent
WORK = Path(os.environ.get("WORK_DIR", "work"))
OUT = Path(os.environ.get("OUT_DIR", "out"))
MOCK = os.environ.get("MOCK") == "1"
TARGET_MB = float(os.environ.get("TARGET_MB", "95"))
FONT_HEAD = ROOT / "fonts" / "Anton-Regular.ttf"
FONT_BODY = ROOT / "fonts" / "Inter.ttf"
ACCENT = (255, 204, 0)
CHAPTER_GAP = 0.45  # seconds of breathing room between chapters


# ---------------------------------------------------------------- helpers
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + " ".join(map(str, cmd))[:600] + "\n" + r.stderr[-2500:])
    return r


def probe_duration(p):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)])
    return float(r.stdout.strip())


def font(path, size, weight=None):
    try:
        f = ImageFont.truetype(str(path), size)
        if weight:
            try:
                f.set_variation_by_axes([14 if size < 40 else 32, weight])
            except Exception:
                pass
        return f
    except Exception:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)


def http_get(url, headers=None, tries=4, stream=False):
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers or {}, timeout=60, stream=stream)
            if r.status_code == 429:
                time.sleep(5 * (i + 1)); continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def download(url, dest):
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    r = http_get(url, stream=True)
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    return dest


# ---------------------------------------------------------------- voice
def tts_chunk(text, prev_text, next_text, out_mp3):
    """Returns list of (char, start, end) relative to chunk."""
    if MOCK:
        dur = max(1.0, len(text) / 15.0)
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=220:duration={dur}",
             "-af", "volume=0.05", "-c:a", "libmp3lame", "-b:a", "128k", str(out_mp3)])
        step = dur / max(1, len(text))
        return [(c, i * step, (i + 1) * step) for i, c in enumerate(text)]

    voice = os.environ["ELEVENLABS_VOICE_ID"]
    model = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128"
    body = {"text": text, "model_id": model,
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.15, "use_speaker_boost": True}}
    if prev_text: body["previous_text"] = prev_text[-600:]
    if next_text: body["next_text"] = next_text[:600]
    for i in range(4):
        r = requests.post(url, json=body, timeout=300,
                          headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]})
        if r.status_code in (429, 500, 502, 503):
            time.sleep(10 * (i + 1)); continue
        if r.status_code != 200:
            raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:400]}")
        break
    d = r.json()
    out_mp3.write_bytes(base64.b64decode(d["audio_base64"]))
    al = d.get("alignment") or d.get("normalized_alignment")
    return list(zip(al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"]))


def build_voice(job):
    """Voice per chapter; returns (voice.wav, scenes_timing, chapter_starts, words)."""
    chapters = job["chapters"]
    texts = [" ".join(s["narration"].strip() for s in ch["scenes"]) for ch in chapters]
    parts, t0 = [], 0.0
    scene_times, chapter_starts, words = [], [], []
    for ci, ch in enumerate(chapters):
        text = texts[ci]
        mp3 = WORK / f"voice_{ci:02d}.mp3"
        log(f"voice chapter {ci+1}/{len(chapters)} ({len(text)} chars)")
        align = tts_chunk(text, texts[ci - 1] if ci else "", texts[ci + 1] if ci + 1 < len(texts) else "", mp3)
        wav = WORK / f"voice_{ci:02d}.wav"
        run(["ffmpeg", "-y", "-i", str(mp3), "-ar", "48000", "-ac", "2", str(wav)])
        dur = probe_duration(wav)
        chapter_starts.append(t0)

        # scene boundaries from character offsets
        offs, pos = [], 0
        for s in ch["scenes"]:
            offs.append(pos)
            pos += len(s["narration"].strip()) + 1
        starts = []
        for k, o in enumerate(offs):
            o = min(o, len(align) - 1)
            starts.append(0.0 if k == 0 else align[o][1])
        for k, s in enumerate(ch["scenes"]):
            a = t0 + starts[k]
            b = t0 + (starts[k + 1] if k + 1 < len(starts) else dur + (CHAPTER_GAP if ci + 1 < len(chapters) else 0))
            scene_times.append((ci, k, a, b))

        # words for subtitles
        cur, ws = "", None
        for c, s, e in align:
            if c.isspace():
                if cur: words.append((cur, t0 + ws, t0 + we)); cur = ""
            else:
                if not cur: ws = s
                cur += c; we = e
        if cur: words.append((cur, t0 + ws, t0 + we))

        parts.append(wav)
        t0 += dur
        if ci + 1 < len(chapters):
            gap = WORK / f"gap_{ci:02d}.wav"
            run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo", "-t", str(CHAPTER_GAP), str(gap)])
            parts.append(gap)
            t0 += CHAPTER_GAP

    lst = WORK / "voice_list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    voice = WORK / "voice.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(voice)])
    return voice, scene_times, chapter_starts, words


# ---------------------------------------------------------------- visuals
class Media:
    def __init__(self):
        self.used = set()
        self.key = os.environ.get("PEXELS_API_KEY", "")
        self.n = 0

    def _mock(self, query):
        self.n += 1
        p = WORK / f"mock_{self.n}.jpg"
        rnd = random.Random(query + str(self.n))
        c1 = tuple(rnd.randint(20, 120) for _ in range(3)); c2 = tuple(rnd.randint(80, 200) for _ in range(3))
        img = Image.new("RGB", (2400, 1350), c1)
        d = ImageDraw.Draw(img)
        for i in range(0, 2400, 120):
            d.rectangle([i, 0, i + 60, 1350], fill=c2)
        d.text((100, 600), query, font=font(FONT_BODY, 90), fill="white")
        img.save(p, quality=90)
        return ("photo", p)

    def get(self, query):
        if MOCK:
            return self._mock(query)
        h = {"Authorization": self.key}
        # 1) stock video
        try:
            r = http_get("https://api.pexels.com/videos/search?" + requests.compat.urlencode(
                {"query": query, "orientation": "landscape", "per_page": 15}), h).json()
            for v in r.get("videos", []):
                if ("v", v["id"]) in self.used or v.get("duration", 0) < 4:
                    continue
                files = [f for f in v["video_files"] if f.get("width") and f["width"] >= 1280
                         and f.get("file_type") == "video/mp4"]
                if not files:
                    continue
                f = min(files, key=lambda f: abs(f["width"] - 1920))
                self.used.add(("v", v["id"]))
                return ("video", download(f["link"], WORK / f"v_{v['id']}.mp4"))
        except Exception as e:
            log("pexels video error", query, e)
        # 2) photo
        try:
            r = http_get("https://api.pexels.com/v1/search?" + requests.compat.urlencode(
                {"query": query, "orientation": "landscape", "per_page": 15}), h).json()
            for p in r.get("photos", []):
                if ("p", p["id"]) in self.used:
                    continue
                self.used.add(("p", p["id"]))
                return ("photo", download(p["src"]["large2x"], WORK / f"p_{p['id']}.jpg"))
        except Exception as e:
            log("pexels photo error", query, e)
        return None

    def photo(self, query):
        if MOCK:
            return self._mock(query)[1]
        r = http_get("https://api.pexels.com/v1/search?" + requests.compat.urlencode(
            {"query": query, "orientation": "landscape", "per_page": 5}), {"Authorization": self.key}).json()
        ph = r.get("photos", [])
        return download(ph[0]["src"]["original"], WORK / f"thumb_src.jpg") if ph else None


# ---------------------------------------------------------------- overlays (PIL)
def wrap(draw, text, fnt, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= max_w:
            cur = t
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines


def overlay_png(kind, text, sub, path):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if kind == "chapter":
        shade = Image.new("RGBA", (W, H), (0, 0, 0, 150)); img.alpha_composite(shade)
        d = ImageDraw.Draw(img)
        if sub:
            f2 = font(FONT_BODY, 40, 700)
            d.text((W / 2, 400), sub.upper(), font=f2, fill=ACCENT, anchor="mm")
        f = font(FONT_HEAD, 120)
        lines = wrap(d, text.upper(), f, W - 300)
        y = 540 - (len(lines) - 1) * 70
        for ln in lines:
            d.text((W / 2, y), ln, font=f, fill="white", anchor="mm", stroke_width=2, stroke_fill="black")
            y += 140
        d.rectangle([W / 2 - 80, y - 40, W / 2 + 80, y - 32], fill=ACCENT)
    elif kind == "stat":
        f = font(FONT_HEAD, 190); f2 = font(FONT_BODY, 46, 600)
        grad = Image.new("RGBA", (W, H), (0, 0, 0, 0)); gd = ImageDraw.Draw(grad)
        for y in range(H // 2, H):
            gd.line([(0, y), (W, y)], fill=(0, 0, 0, int(200 * (y - H / 2) / (H / 2))))
        img.alpha_composite(grad); d = ImageDraw.Draw(img)
        d.text((W / 2, 760), text, font=f, fill=ACCENT, anchor="mm", stroke_width=3, stroke_fill="black")
        if sub:
            for i, ln in enumerate(wrap(d, sub, f2, W - 400)[:2]):
                d.text((W / 2, 900 + i * 60), ln, font=f2, fill="white", anchor="mm")
    elif kind in ("date", "place", "label"):
        f = font(FONT_BODY, 44, 700); f2 = font(FONT_BODY, 32, 500)
        tw = max(d.textlength(text.upper(), font=f), d.textlength(sub or "", font=f2)) + 80
        box_h = 130 if sub else 90
        x0, y0 = 90, H - 110 - box_h
        d.rectangle([x0, y0, x0 + tw, y0 + box_h], fill=(10, 10, 10, 210))
        d.rectangle([x0, y0, x0 + 10, y0 + box_h], fill=ACCENT)
        d.text((x0 + 40, y0 + 22), text.upper(), font=f, fill="white")
        if sub: d.text((x0 + 40, y0 + 80), sub, font=f2, fill=(210, 210, 210))
    elif kind == "quote":
        shade = Image.new("RGBA", (W, H), (0, 0, 0, 160)); img.alpha_composite(shade)
        d = ImageDraw.Draw(img)
        f = font(FONT_BODY, 60, 600); f2 = font(FONT_BODY, 38, 500)
        lines = wrap(d, f"“{text}”", f, W - 500)[:5]
        y = H / 2 - len(lines) * 40
        for ln in lines:
            d.text((W / 2, y), ln, font=f, fill="white", anchor="mm"); y += 80
        if sub: d.text((W / 2, y + 30), "— " + sub, font=f2, fill=ACCENT, anchor="mm")
    img.save(path)
    return path


# ---------------------------------------------------------------- shots
GRADE = "eq=contrast=1.06:saturation=1.08,vignette=PI/5"


def render_shot(media, frames, out, overlay=None, ov_frames=None, fade_in=False, fade_out=False, seed=0):
    rnd = random.Random(seed)
    dur = frames / FPS
    kind, path = media
    inputs, pre = [], ""
    if kind == "video":
        clip_d = probe_duration(path)
        ss = rnd.uniform(0, max(0, clip_d - dur - 0.5)) if clip_d > dur + 1 else 0
        inputs = ["-stream_loop", "-1", "-ss", f"{ss:.2f}", "-i", str(path)]
        pre = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1"
    else:
        inputs = ["-loop", "1", "-framerate", str(FPS), "-i", str(path)]
        mode = rnd.choice(["zin", "zout", "pl", "pr"])
        if mode in ("zin", "zout"):
            z = f"1+0.10*on/{frames}" if mode == "zin" else f"1.10-0.10*on/{frames}"
            pre = (f"[0:v]scale=2880:1620:force_original_aspect_ratio=increase,crop=2880:1620,"
                   f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},setsar=1")
        else:
            sw, sh = int(W * 1.12), int(H * 1.12)
            x = f"(iw-ow)*(n/{frames})" if mode == "pr" else f"(iw-ow)*(1-n/{frames})"
            pre = (f"[0:v]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
                   f"crop={W}:{H}:x='{x}':y='(ih-oh)/2',setsar=1")
    chain = pre + "," + GRADE
    if fade_in: chain += ",fade=t=in:st=0:d=0.35"
    if fade_out: chain += f",fade=t=out:st={max(0, dur - 0.35):.3f}:d=0.35"
    chain += "[base]"
    fc = chain
    if overlay:
        inputs += ["-loop", "1", "-framerate", str(FPS), "-i", str(overlay)]
        od = (ov_frames or frames) / FPS
        fc += (f";[1:v]format=rgba,fade=t=in:st=0.15:d=0.35:alpha=1"
               f",fade=t=out:st={max(0.5, od - 0.35):.3f}:d=0.35:alpha=1[ov]"
               f";[base][ov]overlay=0:0:enable='lte(t,{od:.3f})':shortest=0[v]")
        outmap = "[v]"
    else:
        outmap = "[base]"
    run(["ffmpeg", "-y", *inputs, "-filter_complex", fc, "-map", outmap, "-frames:v", str(frames),
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p",
         "-r", str(FPS), str(out)])


def plan_and_render(job, scene_times, total_dur):
    media = Media()
    chapters = job["chapters"]
    shots = []
    fallback = job.get("fallback_queries") or ["business city skyline", "office work", "stock market screen"]
    for (ci, k, a, b) in scene_times:
        sc = chapters[ci]["scenes"][k]
        dur = b - a
        n = max(1, round(dur / float(job.get("shot_seconds", 5.5))))
        qs = sc.get("queries") or fallback
        for i in range(n):
            sa = a + dur * i / n
            sb = a + dur * (i + 1) / n
            shots.append(dict(ci=ci, k=k, i=i, n=n, a=sa, b=sb, q=qs[i % len(qs)], sc=sc))
    # frame-exact timeline
    total_frames = round(total_dur * FPS)
    for j, s in enumerate(shots):
        fa = round(s["a"] * FPS)
        fb = total_frames if j == len(shots) - 1 else round(s["b"] * FPS)
        s["frames"] = max(1, fb - fa)
    log(f"{len(shots)} shots, {total_frames} frames")

    files = []
    for j, s in enumerate(shots):
        m = media.get(s["q"]) or media.get(fallback[j % len(fallback)]) or media._mock(s["q"])
        ov, ovf = None, None
        first_of_chapter = s["k"] == 0 and s["i"] == 0
        ch = chapters[s["ci"]]
        if first_of_chapter and s["ci"] > 0:
            ov = overlay_png("chapter", ch["title"], f"Chapter {s['ci']}", WORK / f"ov_{j}.png")
            ovf = min(s["frames"], int(2.8 * FPS))
        elif s["i"] == 0 and s["sc"].get("overlay") and s["sc"]["overlay"].get("type") not in (None, "none"):
            o = s["sc"]["overlay"]
            ov = overlay_png(o["type"], o.get("text", ""), o.get("sub", ""), WORK / f"ov_{j}.png")
            ovf = min(s["frames"], int(float(o.get("seconds", 4.5)) * FPS))
        last_of_chapter = (j + 1 == len(shots)) or shots[j + 1]["ci"] != s["ci"]
        out = WORK / f"shot_{j:04d}.mp4"
        log(f"shot {j+1}/{len(shots)} [{m[0]}] {s['q']}")
        render_shot(m, s["frames"], out, ov, ovf, fade_in=(j == 0 or first_of_chapter),
                    fade_out=last_of_chapter, seed=j)
        files.append(out)
    lst = WORK / "shots.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in files))
    silent = WORK / "silent.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(silent)])
    return silent, media


# ---------------------------------------------------------------- final mix + encode
def final_encode(silent, voice, dur, out):
    music_files = sorted((ROOT / "music").glob("*.mp3"))
    inputs = ["-i", str(silent), "-i", str(voice)]
    if music_files:
        inputs += ["-stream_loop", "-1", "-i", str(random.choice(music_files))]
        af = ("[1:a]loudnorm=I=-15:TP=-1.5:LRA=11,asplit=2[vo][sc];"
              "[2:a]volume=0.22,afade=t=in:d=2[mu];"
              "[mu][sc]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];"
              f"[vo][duck]amix=inputs=2:duration=first:normalize=0,afade=t=out:st={max(0, dur-2):.2f}:d=2[a]")
    else:
        af = "[1:a]loudnorm=I=-15:TP=-1.5:LRA=11[a]"
    abr = 160
    vbr = int((TARGET_MB * 8 * 1024 * 0.96) / dur - abr)  # kbit/s
    vbr = max(700, min(vbr, 8000))
    log(f"final encode: {dur:.1f}s, video {vbr}k, target {TARGET_MB}MB, music={bool(music_files)}")
    common = ["-c:v", "libx264", "-preset", "medium", "-b:v", f"{vbr}k", "-pix_fmt", "yuv420p",
              "-g", str(FPS * 2), "-r", str(FPS)]
    passlog = str(WORK / "x264pass")
    run(["ffmpeg", "-y", "-i", str(silent), *common, "-pass", "1", "-passlogfile", passlog, "-an", "-f", "mp4", os.devnull])
    run(["ffmpeg", "-y", *inputs, "-filter_complex", af, "-map", "0:v", "-map", "[a]", *common,
         "-pass", "2", "-passlogfile", passlog, "-c:a", "aac", "-b:a", f"{abr}k", "-ar", "48000",
         "-t", f"{dur:.3f}", "-movflags", "+faststart", str(out)])


# ---------------------------------------------------------------- thumbnail
def thumbnail(job, media, out):
    t = job.get("thumbnail", {})
    src = None
    try:
        src = media.photo(t.get("query") or job["title"])
    except Exception as e:
        log("thumb photo error", e)
    TW, TH = 1280, 720
    if src:
        im = Image.open(src).convert("RGB")
        r = max(TW / im.width, TH / im.height)
        im = im.resize((int(im.width * r) + 1, int(im.height * r) + 1), Image.LANCZOS)
        x, y = (im.width - TW) // 2, (im.height - TH) // 2
        im = im.crop((x, y, x + TW, y + TH))
    else:
        im = Image.new("RGB", (TW, TH), (25, 25, 30))
    im = Image.eval(im, lambda v: int(v * 0.85))
    grad = Image.new("L", (TW, TH))
    gd = ImageDraw.Draw(grad)
    for x in range(TW):
        gd.line([(x, 0), (x, TH)], fill=int(235 * max(0, 1 - x / (TW * 0.75))))
    im = Image.composite(Image.new("RGB", (TW, TH), (0, 0, 0)), im, grad).convert("RGBA")
    d = ImageDraw.Draw(im)
    text = (t.get("text") or job["title"]).upper()
    hl = (t.get("highlight") or text.split()[-1]).upper()
    size = 150
    while size > 70:
        f = font(FONT_HEAD, size)
        lines = wrap(d, text, f, TW * 0.62)
        if len(lines) <= 3: break
        size -= 10
    y = TH / 2 - len(lines) * size * 0.55
    for ln in lines:
        x = 60
        for w in ln.split():
            col = ACCENT if w.strip(".,!?") in hl.split() else "white"
            d.text((x, y), w, font=f, fill=col, stroke_width=6, stroke_fill="black")
            x += d.textlength(w + " ", font=f)
        y += size * 1.08
    im.convert("RGB").save(out, quality=92)


# ---------------------------------------------------------------- subtitles + description
def fmt_srt(t):
    ms = int(round(t * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(words, out):
    cues, cur = [], []
    for w in words:
        cand = " ".join(x[0] for x in cur + [w])
        if cur and (len(cand) > 42 or w[2] - cur[0][1] > 3.5 or re.search(r"[.!?]$", cur[-1][0])):
            cues.append(cur); cur = []
        cur.append(w)
    if cur: cues.append(cur)
    out.write_text("\n".join(f"{i+1}\n{fmt_srt(c[0][1])} --> {fmt_srt(c[-1][2])}\n{' '.join(x[0] for x in c)}\n"
                             for i, c in enumerate(cues)), encoding="utf-8")


def fmt_ts(t):
    t = int(t); h, r = divmod(t, 3600); m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def description(job, chapter_starts):
    lines = [job.get("description", "").strip(), "", "Chapters:"]
    for i, (ch, t) in enumerate(zip(job["chapters"], chapter_starts)):
        lines.append(f"{fmt_ts(0 if i == 0 else t)} {ch['title']}")
    if job.get("sources"):
        lines += ["", "Sources:"] + [f"- {s}" for s in job["sources"]]
    lines += ["", "Stock footage: Pexels. Narration voice generated with AI."]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- main
def validate(job):
    assert job.get("title"), "missing title"
    chs = job.get("chapters") or []
    assert len(chs) >= 3, "need at least 3 chapters (YouTube chapters rule)"
    for c in chs:
        assert c.get("title") and c.get("scenes"), "chapter without title/scenes"
        for s in c["scenes"]:
            assert s.get("narration", "").strip(), "scene without narration"


def main(job_path):
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    if isinstance(job.get("script"), str):  # Make may send the script as a JSON string
        job = {**job, **json.loads(re.sub(r"^```(json)?|```$", "", job["script"].strip()))}
    validate(job)
    WORK.mkdir(exist_ok=True, parents=True); OUT.mkdir(exist_ok=True, parents=True)
    random.seed(job.get("title"))

    voice, scene_times, chapter_starts, words = build_voice(job)
    total = probe_duration(voice)
    log(f"narration {total/60:.1f} min")
    silent, media = plan_and_render(job, scene_times, total)
    final_encode(silent, voice, total, OUT / "video.mp4")
    thumbnail(job, media, OUT / "thumbnail.jpg")
    write_srt(words, OUT / "subtitles.srt")
    meta = {
        "title": job["title"][:100],
        "description": description(job, chapter_starts)[:4900],
        "tags": job.get("tags", [])[:15],
        "duration_seconds": round(total, 1),
        "size_mb": round((OUT / "video.mp4").stat().st_size / 1048576, 1),
    }
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log("DONE", meta["size_mb"], "MB")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "job.json")
