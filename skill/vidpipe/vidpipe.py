#!/usr/bin/env python3
"""
vidpipe: a small ffmpeg toolkit an AI agent can drive to edit video.

The agent's loop:
  1. probe       -> what footage exists (duration, resolution, fps, audio)
  2. scenes      -> where each shot changes
  3. sheet       -> a contact-sheet image the agent can LOOK at to judge shots
  4. transcribe  -> speech-to-text, for talking/interview footage (optional)
  5. plan        -> the agent writes an edit list (JSON); this prints it as a
                    shot list for a person to approve
  6. render      -> renders the approved edit list

Photos (JPG, PNG, HEIC, RAW) work too: probe lists them, sheet shows them, and
render holds one on screen for a clip's "duration", still or with a slow zoom
and pan (Ken Burns).

Only dependency besides Python 3.10+ is ffmpeg/ffprobe. sheet needs the
drawtext filter and captions need subtitles, so use Homebrew's ffmpeg-full.
Transcription optionally uses mlx-whisper (Apple Silicon) or openai-whisper.

MIT License.
"""

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".lrf"}
# Camera RAW formats macOS can decode (sips --formats). Support for a given
# camera model depends on the macOS version.
RAW_EXTS = {".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2",
            ".raf", ".orf", ".rw2", ".pef", ".srw", ".erf", ".3fr", ".fff", ".dcr",
            ".mrw", ".mos", ".rwl", ".iiq"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif"} | RAW_EXTS
SRGB_PROFILE = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"

# Output presets: name -> (width, height). Used by the edit list's "aspect".
ASPECTS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}

# Homebrew's default ffmpeg ships without drawtext/subtitles, which sheet and
# captions need. ffmpeg-full has them but is keg-only (not on PATH), so prefer
# it whenever it is installed.
for _keg in ("/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin"):
    if Path(_keg, "ffmpeg").exists():
        os.environ["PATH"] = _keg + os.pathsep + os.environ.get("PATH", "")
        break


# ---------- helpers ----------

def run(cmd: list[str], quiet: bool = True) -> subprocess.CompletedProcess:
    """Run a command; raise with stderr if it fails."""
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit(f"command failed: {' '.join(cmd[:6])}...\n{res.stderr[-2000:]}")
    if not quiet:
        print(res.stdout)
    return res


def need(binary: str) -> None:
    if not shutil.which(binary):
        sys.exit(f"missing dependency: {binary} (install with: brew install ffmpeg-full)")


def need_filter(name: str) -> None:
    """Exit with a useful hint if this ffmpeg build lacks a filter."""
    res = subprocess.run(["ffmpeg", "-hide_banner", "-h", f"filter={name}"],
                         capture_output=True, text=True)
    if "Unknown filter" in res.stdout + res.stderr:
        sys.exit(f"this ffmpeg build has no '{name}' filter "
                 f"(install with: brew install ffmpeg-full)")


def ff_quote(path: str) -> str:
    """Quote a file path for use as a filter option value.

    ffmpeg parses a filter string twice. The option parser treats \\ ' : as
    special, so escape those first; then the filtergraph parser handles quotes,
    where a literal ' inside a quoted string has to be written as '\\''.
    """
    s = path.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return "'" + s.replace("'", "'\\''") + "'"


def probe_file(path: Path) -> dict:
    """Return the facts an editor needs about one clip."""
    res = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)])
    data = json.loads(res.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    fps = None
    if v and v.get("avg_frame_rate", "0/0") != "0/0":
        num, den = v["avg_frame_rate"].split("/")
        fps = round(int(num) / int(den), 3)
    # Phones/drones store rotation as metadata; report it so reframing is right.
    rotation = 0
    if v:
        for sd in v.get("side_data_list", []):
            if "rotation" in sd:
                rotation = int(sd["rotation"])
        rotation = int(v.get("tags", {}).get("rotate", rotation))
    return {
        "file": str(path),
        "kind": "video",
        "duration": round(float(data["format"].get("duration", 0)), 3),
        "width": v and v["width"],
        "height": v and v["height"],
        "fps": fps,
        "rotation": rotation,
        "has_audio": a is not None,
        "created": data["format"].get("tags", {}).get("creation_time"),
        # Color facts: render uses these to bring every clip to standard HD color
        "hdr": HDR_TRANSFERS.get((v or {}).get("color_transfer")),  # "HLG", "PQ" or None
        "matrix": _tag((v or {}).get("color_space")),               # None = no color tags
    }


def _tag(value: str | None) -> str | None:
    """ffprobe writes a missing color tag as 'unknown' or leaves it out."""
    return None if value in (None, "unknown", "unspecified", "reserved") else value


# Transfer curves that mean high dynamic range
HDR_TRANSFERS = {"arib-std-b67": "HLG", "smpte2084": "PQ"}

# The end of every clip's filter chain. All segments leave as standard HD video
# (BT.709, limited range, 8-bit) and say so in their tags. Segments with mixed or
# missing tags inside one file make players guess, and colors shift.
TO_BT709 = ["scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int",
            "setsar=1", "format=yuv420p",
            "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"]

# HDR to standard range. Go to linear light, move BT.2020 colors into BT.709,
# then compress highlights. With these numbers an 18% gray card lands at about
# 46% and HDR reference white at about 91%, for both HLG and PQ, measured on
# gray test bands. npl is the brightness (nits) treated as "full white".
TONEMAP = ["zscale=t=linear:npl=160", "format=gbrpf32le", "zscale=p=bt709",
           "tonemap=tonemap=mobius:param=0.5:desat=0",
           "zscale=t=bt709:m=bt709:r=tv", "format=yuv420p"]


def color_filters(info: dict) -> tuple[list[str], list[str]]:
    """Filters that bring one clip toward standard color, as (head, body).

    head goes first in the chain. body goes after the resize, so tone mapping
    runs on the small output frame instead of the 4K source.
    """
    head, body = [], []
    if info.get("hdr"):
        body = list(TONEMAP)
    elif info.get("matrix") is None:
        # No color tags: assume what players assume, BT.709 for HD and BT.601
        # for SD, and say so, so later conversions don't have to guess.
        guess = "bt709" if (info.get("height") or 0) >= 720 else "smpte170m"
        head = [f"setparams=colorspace={guess}"]
    return head, body


def is_photo(path: Path) -> bool:
    return path.suffix.lower() in PHOTO_EXTS


def find_media(folder: Path, exts: set[str]) -> list[Path]:
    """Files under a folder with one of these extensions, skipping hidden
    files and vidpipe's own contact sheets (which are .jpg too)."""
    return sorted(p for p in folder.rglob("*")
                  if p.suffix.lower() in exts and p.is_file()
                  and not p.name.startswith(".") and ".sheet." not in p.name)


def is_raw(path: Path) -> bool:
    return path.suffix.lower() in RAW_EXTS


def bmp_size(header: bytes) -> tuple[int, int]:
    """Width and height from the first 26 bytes of a BMP."""
    width, height = struct.unpack("<ii", header[18:26])
    return width, abs(height)  # BMP stores a negative height for top-down rows


def photo_to_bmp(src: Path, dest: Path, max_side: int | None = None) -> None:
    """Decode a photo to an uncompressed, upright, sRGB temp image.

    On macOS this uses sips, which reads RAW through Apple's RAW engine,
    converts wide-gamut photos (Display P3, Adobe RGB) to sRGB, and bakes the
    rotation into the pixels. max_side shrinks the result while decoding.

    Without sips, or if sips fails on a non-RAW file, ffmpeg does a plain
    decode. With no filters it merges HEIC tiles and applies rotation (filters
    on a HEIC fail), but it ignores color profiles and cannot read RAW.
    """
    dest.unlink(missing_ok=True)  # never mistake an earlier photo for this one
    if shutil.which("sips") and Path(SRGB_PROFILE).exists():
        cmd = ["sips", "-s", "format", "bmp", "-m", SRGB_PROFILE]
        if max_side:
            cmd += ["-Z", str(max_side)]  # only ever shrinks
        res = subprocess.run(cmd + [str(src), "--out", str(dest)],
                             capture_output=True, text=True)
        if res.returncode == 0 and dest.exists():
            return
        if is_raw(src):
            sys.exit(f"cannot decode RAW photo {src}\n{(res.stderr or res.stdout)[-500:]}")
    elif is_raw(src):
        sys.exit(f"RAW photos need macOS (sips): {src}")
    run(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-frames:v", "1", str(dest)])


def probe_photo(path: Path) -> dict:
    """Facts about one photo, with its size as displayed.

    ffprobe can't be trusted here: it reports a HEIC as one 512px tile, ignores
    rotation, and sees only the tiny preview inside a RAW file.
    """
    if is_raw(path):
        # Full-size RAW decodes are slow, so decode small to learn which way
        # up the photo is, and take the pixel size from the file header.
        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp) / "small.bmp"
            photo_to_bmp(path, small, max_side=256)
            sw, sh = bmp_size(small.read_bytes()[:26])
        res = run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)])
        dims = dict(re.findall(r"(pixelWidth|pixelHeight): (\d+)", res.stdout))
        width, height = int(dims["pixelWidth"]), int(dims["pixelHeight"])
        if (sw > sh) != (width > height):  # stored sideways, shown rotated
            width, height = height, width
    else:
        # Decode to a BMP on a pipe and read its header: that gives the size
        # after HEIC tiles are merged and rotation is applied.
        res = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
                              "-c:v", "bmp", "-f", "image2pipe", "-"], capture_output=True)
        if res.returncode != 0 or len(res.stdout) < 26:
            sys.exit(f"cannot read photo {path}\n{res.stderr.decode()[-500:]}")
        width, height = bmp_size(res.stdout)
    return {
        "file": str(path),
        "kind": "photo",
        "duration": 0.0,
        "width": width,
        "height": height,
        "fps": None,
        "rotation": 0,          # already applied to width/height
        "has_audio": False,
        "created": None,
    }


def ts(seconds: float) -> str:
    """Seconds -> HH:MM:SS.mmm for readable output."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


# ---------- commands ----------

def cmd_probe(args) -> None:
    """Probe a file, or every clip and photo in a folder.

    A folder writes media.json. A single file only prints, unless --out is
    given, so checking a render never overwrites the footage inventory.
    """
    target = Path(args.path)
    files = [target] if target.is_file() else find_media(target, VIDEO_EXTS | PHOTO_EXTS)
    if not files:
        sys.exit(f"no video or photo files found in {target}")
    media = [probe_photo(f) if is_photo(f) else probe_file(f) for f in files]
    out = args.out or (None if target.is_file() else "media.json")
    if out:
        Path(out).write_text(json.dumps(media, indent=2))
    clips = [m for m in media if m["kind"] == "video"]
    photos = [m for m in media if m["kind"] == "photo"]
    total = sum(m["duration"] for m in clips)
    summary = f"{len(clips)} clips, {ts(total)} total"
    if photos:
        summary += f", {len(photos)} photos"
    print(summary + (f" -> {out}" if out else ""))
    for m in clips:
        color = f"  HDR ({m['hdr']})" if m["hdr"] else "" if m["matrix"] else "  no color tags"
        print(f"  {Path(m['file']).name:40s} {ts(m['duration'])}  "
              f"{m['width']}x{m['height']}@{m['fps']}  audio={m['has_audio']}{color}")
    for m in photos:
        print(f"  {Path(m['file']).name:40s} photo         {m['width']}x{m['height']}")


def cmd_scenes(args) -> None:
    """Detect shot changes with ffmpeg's scene score; write <clip>.scenes.json."""
    src = Path(args.file)
    dur = probe_file(src)["duration"]
    # select frames whose scene-change score beats the threshold, print their times
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(src), "-an",
         "-vf", f"select='gt(scene,{args.threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True)
    cuts = [round(float(t), 3) for t in re.findall(r"pts_time:([\d.]+)", res.stderr)]
    # Turn cut points into [in, out] shots; drop shots shorter than --min seconds
    bounds = [0.0] + cuts + [dur]
    shots = [{"in": a, "out": b, "len": round(b - a, 3)}
             for a, b in zip(bounds, bounds[1:]) if b - a >= args.min]
    out = src.with_suffix(src.suffix + ".scenes.json")
    out.write_text(json.dumps({"file": str(src), "duration": dur, "shots": shots}, indent=2))
    print(f"{len(shots)} shots -> {out}")
    for i, s in enumerate(shots):
        print(f"  #{i:02d} {ts(s['in'])} -> {ts(s['out'])} ({s['len']}s)")


def sheet_photos(src: Path, args) -> None:
    """Contact sheets for photos: one thumbnail per photo, labelled by filename.

    A folder gets one sheet per --frames photos (16 by default) so each sheet
    stays readable. Cells are square because a set of photos mixes portrait
    and landscape.
    """
    photos = [src] if src.is_file() else find_media(src, PHOTO_EXTS)
    if not photos:
        sys.exit(f"no photos found in {src}")
    per, cols, cell = args.frames, args.cols, args.width
    # A folder's sheet goes next to the folder, not inside it
    first = Path(args.out or (str(src).rstrip("/") + ".sheet.jpg" if src.is_dir()
                              else src.with_suffix(src.suffix + ".sheet.jpg")))
    with tempfile.TemporaryDirectory() as tmp:
        bmp, label = Path(tmp) / "decoded.bmp", Path(tmp) / "label.txt"
        for page, start in enumerate(range(0, len(photos), per)):
            batch = photos[start:start + per]
            for i, photo in enumerate(batch):
                photo_to_bmp(photo, bmp, max_side=cell)
                # The label is read from a file, so any filename is safe
                label.write_text(photo.name)
                run(["ffmpeg", "-hide_banner", "-y", "-i", str(bmp), "-frames:v", "1",
                     "-vf", f"scale={cell}:{cell}:force_original_aspect_ratio=decrease,"
                            f"pad={cell}:{cell}:(ow-iw)/2:(oh-ih)/2:color=0x202020,"
                            f"drawtext=textfile={ff_quote(str(label))}:expansion=none:"
                            f"x=6:y=6:fontsize=18:fontcolor=white:"
                            f"box=1:boxcolor=black@0.6:boxborderw=4",
                     "-q:v", "4", str(Path(tmp) / f"t{i:03d}.jpg")])
            rows = -(-len(batch) // cols)  # ceiling division
            out = first if page == 0 else first.with_suffix(f".{page + 1}.jpg")
            run(["ffmpeg", "-hide_banner", "-y", "-framerate", "1",
                 "-i", str(Path(tmp) / "t%03d.jpg"),
                 "-vf", f"tile={cols}x{rows}:padding=4:margin=4", "-frames:v", "1",
                 "-q:v", "3", str(out)])
            for thumb in Path(tmp).glob("t*.jpg"):  # don't leak into the next page
                thumb.unlink()
            print(f"{len(batch)} photos ({batch[0].name} .. {batch[-1].name}) -> {out}")


def cmd_sheet(args) -> None:
    """Make a contact sheet: N evenly spaced frames, each stamped with its time.

    This is how the agent 'sees' footage: it reads the image, judges shots,
    and picks in/out points by timestamp. Given a photo or a folder, it makes
    photo sheets instead (see sheet_photos).
    """
    need_filter("drawtext")
    src = Path(args.file)
    if src.is_dir() or is_photo(src):
        return sheet_photos(src, args)
    info = probe_file(src)
    dur, n = info["duration"], args.frames
    cols = args.cols
    rows = -(-n // cols)  # ceiling division
    step = dur / n
    out = Path(args.out or src.with_suffix(src.suffix + ".sheet.jpg"))
    # Same color handling as render, so the sheet shows what the edit will
    head, body = color_filters(info)
    if body:
        need_filter("zscale")
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(n):
            t = min(step * i + step / 2, max(dur - 0.05, 0))
            frame = Path(tmp) / f"f{i:03d}.jpg"
            # ffmpeg's filter parser treats ":" as a separator, so escape it
            label = ts(t).replace(":", r"\:")
            vf = head + [f"scale={args.width}:-2"] + body + [
                f"drawtext=text='{label}':x=6:y=6:fontsize=18:"
                f"fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=4"]
            # fast seek (-ss before -i), scale to thumb, burn timestamp label
            run(["ffmpeg", "-hide_banner", "-y", "-ss", f"{t:.3f}", "-i", str(src),
                 "-frames:v", "1", "-vf", ",".join(vf), "-q:v", "4", str(frame)])
        # tile all thumbnails into one grid image
        run(["ffmpeg", "-hide_banner", "-y", "-framerate", "1",
             "-i", str(Path(tmp) / "f%03d.jpg"),
             "-vf", f"tile={cols}x{rows}:padding=4:margin=4", "-frames:v", "1",
             "-q:v", "3", str(out)])
    print(f"{n} frames ({ts(step)} apart) -> {out}")


def cmd_transcribe(args) -> None:
    """Speech-to-text with word timing. Prefers mlx-whisper on Apple Silicon."""
    src = Path(args.file)
    outdir = Path(args.outdir or src.parent)
    if shutil.which("mlx_whisper"):
        run(["mlx_whisper", str(src), "--model", f"mlx-community/whisper-{args.model}-mlx",
             "--output-format", "json", "--output-dir", str(outdir)])
    elif shutil.which("whisper"):
        run(["whisper", str(src), "--model", args.model,
             "--output_format", "json", "--output_dir", str(outdir)])
    else:
        sys.exit("no whisper found. Install one:\n"
                 "  uv tool install --python 3.14 mlx-whisper   # Apple Silicon, fastest\n"
                 "  pip3 install openai-whisper                 # anywhere")
    raw = outdir / (src.stem + ".json")
    data = json.loads(raw.read_text())
    # Compact form the agent can read cheaply: one line per segment
    segs = [{"in": round(s["start"], 2), "out": round(s["end"], 2),
             "text": s["text"].strip()} for s in data.get("segments", [])]
    out = src.with_suffix(src.suffix + ".transcript.json")
    out.write_text(json.dumps({"file": str(src), "segments": segs}, indent=2))
    print(f"{len(segs)} segments -> {out}")


# Ken Burns. zoompan can only place its window on whole pixels, which shows as
# judder on a slow move. Working at this multiple of the output size turns
# those steps into fractions of an output pixel.
KB_SUPERSAMPLE = 4
KB_MAX_PIXELS = 100e6   # cap on the enlarged working image
KB_MAX_ZOOM = 4.0


def _pair(value, name: str, n: int) -> tuple[float, float]:
    """A number is a fixed setting; [start, end] moves over the photo's duration."""
    def is_num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if is_num(value):
        return float(value), float(value)
    if isinstance(value, list) and len(value) == 2 and all(is_num(v) for v in value):
        return float(value[0]), float(value[1])
    sys.exit(f"clip {n}: {name} must be a number or [start, end] (got {value!r})")


def photo_motion(clip: dict, n: int) -> dict | None:
    """Zoom/pan settings for a photo clip, or None if it is simply held still.

    zoom 1.0 is the widest view that still fills the frame. crop_x/crop_y say
    where in the photo the frame sits (0 = left/top, 1 = right/bottom).
    """
    zoom = _pair(clip.get("zoom", 1.0), "zoom", n)
    cx = _pair(clip.get("crop_x", 0.5), "crop_x", n)
    cy = _pair(clip.get("crop_y", 0.5), "crop_y", n)
    if not all(1.0 <= z <= KB_MAX_ZOOM for z in zoom):
        sys.exit(f"clip {n}: zoom must be between 1.0 and {KB_MAX_ZOOM} (got {clip['zoom']!r})")
    for name, pair in (("crop_x", cx), ("crop_y", cy)):
        if not all(0.0 <= v <= 1.0 for v in pair):
            sys.exit(f"clip {n}: {name} must be between 0 and 1 (got {clip[name]!r})")
    if zoom == (1.0, 1.0) and cx[0] == cx[1] and cy[0] == cy[1]:
        return None
    if clip.get("fit", "crop") != "crop":
        sys.exit(f"clip {n}: zoom and moving crops need \"fit\": \"crop\"")
    return {"zoom": zoom, "crop_x": cx, "crop_y": cy}


def kenburns_filter(motion: dict, pw: int, ph: int, w: int, h: int, fps: int,
                    seconds: float, lut: str | None) -> str:
    """Filter chain for a photo (pw x ph) that zooms and/or pans.

    The photo is enlarged and set on a canvas shaped like the output, then
    zoompan slides a window across it at constant speed, start to end.
    """
    aspect = w / h
    # Widest window shaped like the output that fits inside the photo (zoom 1.0)
    cover_w, cover_h = (ph * aspect, ph) if pw / ph > aspect else (pw, pw / aspect)
    # Enlarge so the tightest window is KB_SUPERSAMPLE x the output width
    k = min(KB_SUPERSAMPLE * w * max(motion["zoom"]) / cover_w, math.sqrt(KB_MAX_PIXELS / (pw * ph)))
    iw, ih = round(pw * k), round(ph * k)
    cover_w, cover_h = cover_w * iw / pw, cover_h * ih / ph   # now in enlarged pixels
    # zoompan stretches its window to the output, so the canvas must have the
    # output's exact shape. The bars this adds are never inside the window.
    g = math.gcd(w, h)
    m = math.ceil(max(iw / (w // g), ih / (h // g)))
    cw, ch = m * (w // g), m * (h // g)
    ox, oy = (cw - iw) // 2, (ch - ih) // 2
    # The window at the start and at the end: (width, left, top) on the canvas
    ends = []
    for z, cx, cy in zip(motion["zoom"], motion["crop_x"], motion["crop_y"]):
        ends.append((cover_w / z, ox + (iw - cover_w / z) * cx, oy + (ih - cover_h / z) * cy))
    (w0, l0, t0), (w1, l1, t1) = ends
    frames = max(round(seconds * fps), 1)
    # zoompan numbers its output frames from 0
    prog = f"min(on/{max(frames - 1, 1)},1)"       # 0 on the first frame, 1 on the last
    # Width and position each move in a straight line from start to end, so the
    # frame travels evenly and stays inside the photo the whole way.
    def line(a, b):
        return f"({a:.4f}+({b - a:.4f})*{prog})"
    f = [f"scale={iw}:{ih}:flags=lanczos", "format=gbrp", f"pad={cw}:{ch}:{ox}:{oy}",
         # zoompan's own zoom is canvas width over window width
         f"zoompan=z='{cw}/{line(w0, w1)}':x='{line(l0, l1)}':y='{line(t0, t1)}'"
         f":d={frames}:s={w}x{h}:fps={fps}"]
    if lut:
        f.append(f"lut3d={ff_quote(lut)}")
    return ",".join(f + TO_BT709)


def clip_length(clip: dict, n: int) -> float:
    """Seconds clip number n takes on the timeline. Exits on values that can't work."""
    if is_photo(Path(clip["src"])):
        dur = clip.get("duration")
        if not isinstance(dur, (int, float)) or not dur > 0:
            sys.exit(f"clip {n}: a photo needs \"duration\" in seconds (got {dur!r})")
        if "speed" in clip:
            sys.exit(f"clip {n}: speed does not apply to a photo")
        return float(dur)
    speed = clip.get("speed", 1.0)
    if not speed > 0:
        sys.exit(f"clip {n}: speed must be greater than 0 (got {speed})")
    return (clip["out"] - clip["in"]) / speed


def build_clip_filter(clip: dict, w: int, h: int, fps: int, lut: str | None,
                      info: dict | None = None, hold: float | None = None) -> str:
    """Filter chain that turns one source into a uniform segment.

    info: probe facts for a video clip; they drive its color handling.
    hold: for a photo, how many seconds to keep its single frame on screen.

    Order matters. Resize first, so the color work runs on the small output
    frame. Then color: HDR tone mapping, then the LUT. Letterbox bars come
    after the LUT so it can't tint them. Every chain ends in TO_BT709.
    """
    head, body = color_filters(info) if info else ([], [])
    f = list(head)
    speed = clip.get("speed", 1.0)
    if speed != 1.0 and hold is None:
        f.append(f"setpts=PTS/{speed}")
    mode = clip.get("fit", "crop")
    if mode == "crop":
        # Fill the frame, then crop. crop_x/crop_y (0..1) pick which part to keep,
        # e.g. crop_x=0.3 keeps the left-of-center subject in a 9:16 reframe.
        cx, cy = clip.get("crop_x", 0.5), clip.get("crop_y", 0.5)
        f.append(f"scale={w}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2")
        f.append(f"crop={w}:{h}:(iw-{w})*{cx}:(ih-{h})*{cy}")
    else:
        # "pad": fit the whole frame inside; the black bars are added below
        f.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2")
    f += body
    if lut:
        f.append(f"lut3d={ff_quote(lut)}")
    if mode != "crop":
        f.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
    if hold is not None:
        # Everything above ran once on the photo's single frame; repeat it
        f.append(f"tpad=stop_mode=clone:stop_duration={hold}")
    f += [f"fps={fps}"] + TO_BT709
    return ",".join(f)


def read_srt(path: Path) -> list[tuple[float, float, str]]:
    """Caption lines as (start, end, text), for showing in a plan."""
    lines = []
    for block in re.split(r"\n\s*\n", path.read_text().strip()):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)\s*\n(.*)",
                      block, re.S)
        if m:
            g = m.groups()
            start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
            end = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000
            lines.append((start, end, " / ".join(g[8].strip().splitlines())))
    return lines


def load_edl(edl_path: Path) -> dict:
    """Read an edit list and check everything that can be checked without
    rendering. plan and render both start here, so a plan that prints is a
    plan that renders.

    Returns the settings plus one entry per clip in "shots":
    {"clip", "src", "length", "start", "info", "motion"}; info is None for a
    photo, motion is None unless a photo zooms or pans.
    """
    edl = json.loads(edl_path.read_text())
    base = edl_path.parent  # paths in the EDL are relative to the EDL file
    aspect = edl.get("aspect", "16:9")
    if aspect not in ASPECTS:
        sys.exit(f"unknown aspect {aspect!r} (use one of: {', '.join(ASPECTS)})")
    if not edl.get("clips"):
        sys.exit("edit list has no clips")
    shots, t = [], 0.0
    for n, clip in enumerate(edl["clips"], 1):
        src = (base / clip["src"]).resolve()
        if not src.is_file():
            sys.exit(f"clip {n}: file not found: {src}")
        length = clip_length(clip, n)
        info = motion = None
        if is_photo(src):
            motion = photo_motion(clip, n)
        else:
            info = probe_file(src)
            t_in, t_out = clip["in"], clip["out"]
            if not 0 <= t_in < t_out <= info["duration"] + 0.05:
                sys.exit(f"clip {n}: bad in/out {t_in}-{t_out} "
                         f"(source is {info['duration']}s)")
            if "zoom" in clip or any(isinstance(clip.get(k), list) for k in ("crop_x", "crop_y")):
                sys.exit(f"clip {n}: zoom and moving crops only work on photos")
        shots.append({"clip": clip, "src": src, "length": length, "start": t,
                      "info": info, "motion": motion})
        t += length
    fade = edl.get("fade", 0.0)
    if fade * 2 > t:
        sys.exit(f"fade of {fade}s is too long for a {t:.2f}s timeline (max {t / 2:.2f}s)")
    for what, rel in (("lut", edl.get("lut")), ("captions", edl.get("captions")),
                      ("music", (edl.get("music") or {}).get("src"))):
        if rel and not (base / rel).is_file():
            sys.exit(f"{what} file not found: {(base / rel).resolve()}")
    music = edl.get("music")
    if music:
        start, length = music.get("start", 0.0), probe_file(base / music["src"])["duration"]
        if not 0 <= start < length:
            sys.exit(f"music start {start}s is outside the track ({length}s long)")
    return {"edl": edl, "base": base, "aspect": aspect, "size": ASPECTS[aspect],
            "fps": edl.get("fps", 30), "fade": fade, "shots": shots, "total": t}


def cmd_plan(args) -> None:
    """Print the edit list as a shot list for a person to approve. Renders nothing.

    Anything that changes how footage plays (a speed change) is called out at
    the end, so it can't slip through unnoticed.
    """
    p = load_edl(Path(args.edl))
    edl, base, (w, h) = p["edl"], p["base"], p["size"]
    print(f"Plan: {edl.get('output', 'out.mp4')}")
    print(f"{p['aspect']} ({w}x{h}), {p['fps']} fps, {p['total']:.1f}s, {len(p['shots'])} shots\n")
    print("| # | Timeline | Length | Source | From | Notes |")
    print("|---|---|---|---|---|---|")
    needs_ok = []
    for n, shot in enumerate(p["shots"], 1):
        clip, info, notes = shot["clip"], shot["info"], []
        if info is None:
            origin = "photo"
        else:
            origin = f"{clip['in']}-{clip['out']}s"
            speed = clip.get("speed", 1.0)
            if speed != 1.0:
                notes.append(f"SPEED {speed}x")
                needs_ok.append(f"shot {n} plays at {speed}x speed")
            if not info["has_audio"]:
                notes.append("no sound in source")
            elif clip.get("mute"):
                notes.append("muted")
            elif speed != 1.0:
                notes.append("sound dropped")
            if info["hdr"]:
                notes.append(f"HDR ({info['hdr']}), tone-mapped")
        if clip.get("fit", "crop") == "pad":
            notes.append("letterboxed")
        if shot["motion"]:
            for key, pair in shot["motion"].items():
                if pair[0] != pair[1]:
                    notes.append(f"{key} {pair[0]:g} to {pair[1]:g}")
                elif key in clip:
                    notes.append(f"{key} {pair[0]:g}")
        else:
            notes += [f"{k} {clip[k]}" for k in ("crop_x", "crop_y") if k in clip]
        end = shot["start"] + shot["length"]
        print(f"| {n} | {shot['start']:.1f}-{end:.1f} | {shot['length']:.1f}s | "
              f"{shot['src'].name} | {origin} | {', '.join(notes)} |")
    print()
    print(f"Fade: {p['fade']}s in and out" if p["fade"] else "Fade: none")
    print(f"LUT: {edl['lut']}" if edl.get("lut") else "LUT: none")
    music = edl.get("music")
    if music:
        print(f"Music: {Path(music['src']).name}, from {music.get('start', 0.0)}s into the track, "
              f"volume {music.get('volume', 0.8)}, clip sound at {music.get('source_volume', 0.0)}")
    else:
        print("Music: none")
    if edl.get("captions"):
        lines = read_srt(base / edl["captions"])
        print(f"On-screen text: {len(lines)} lines from {edl['captions']}")
        for start, end, text in lines:
            print(f"  {start:.1f}-{end:.1f}  {text}")
    else:
        print("On-screen text: none")
    if needs_ok:
        print("\nNeeds the user's OK before rendering:")
        for item in needs_ok:
            print(f"  - {item}")


def cmd_render(args) -> None:
    """Render an edit list (JSON) to a finished video.

    Each clip is normalized to the same size/fps/audio format first, then the
    segments are joined losslessly, then an optional music bed is mixed in.
    """
    p = load_edl(Path(args.edl))
    edl, base, (w, h), fps, fade = p["edl"], p["base"], p["size"], p["fps"], p["fade"]
    lut = edl.get("lut")
    if lut:
        lut = str((base / lut).resolve())
    out = (base / edl.get("output", "out.mp4")).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    shots = p["shots"]
    if edl.get("captions"):
        need_filter("subtitles")
    # Draft mode renders fast and small for review; final uses higher quality.
    preset, crf = ("ultrafast", 30) if args.draft else ("medium", 18)

    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, shot in enumerate(shots):
            clip, src, seg_len, info = shot["clip"], shot["src"], shot["length"], shot["info"]
            part = Path(tmp) / f"p{i:03d}.mp4"
            # Every segment needs an audio track (concat needs that), so clips
            # with no usable sound get silence of the same length.
            silence = ["-f", "lavfi", "-t", f"{seg_len}",
                       "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            if is_photo(src):
                # One decoded frame in; the filter chain holds it for seg_len
                bmp = Path(tmp) / f"s{i:03d}.bmp"
                # Twice the output size is plenty for any reframe and keeps
                # 48 MP photos fast
                photo_to_bmp(src, bmp, max_side=2 * max(w, h))
                if shot["motion"]:
                    pw, ph = bmp_size(bmp.read_bytes()[:26])
                    vf = kenburns_filter(shot["motion"], pw, ph, w, h, fps, seg_len, lut)
                else:
                    vf = build_clip_filter(clip, w, h, fps, lut, hold=seg_len)
                cmd = ["ffmpeg", "-hide_banner", "-y", "-framerate", str(fps),
                       "-i", str(bmp), *silence]
                keep_audio = False
                what = f"photo, {seg_len}s" + (", moving" if shot["motion"] else "")
            else:
                t_in, t_out = clip["in"], clip["out"]
                if info["hdr"]:
                    need_filter("zscale")  # tone mapping needs it
                vf = build_clip_filter(clip, w, h, fps, lut, info=info)
                cmd = ["ffmpeg", "-hide_banner", "-y", "-ss", f"{t_in}", "-to", f"{t_out}",
                       "-i", str(src)]
                # Keep source audio only if it exists and isn't muted
                keep_audio = info["has_audio"] and not clip.get("mute", False) \
                    and clip.get("speed", 1.0) == 1.0
                if not keep_audio:
                    cmd += silence
                what = f"{t_in}-{t_out}s"
            cmd += ["-vf", vf, "-map", "0:v:0", "-map", "0:a:0" if keep_audio else "1:a:0",
                    "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                    "-t", f"{seg_len}", str(part)]
            run(cmd)
            parts.append(part)
            print(f"  [{i + 1}/{len(shots)}] {src.name} {what}")

        # Join segments without re-encoding (they share identical formats now)
        listing = Path(tmp) / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        joined = Path(tmp) / "joined.mp4"
        run(["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0",
             "-i", str(listing), "-c", "copy", str(joined)])
        total = float(probe_file(joined)["duration"])

        # Final pass: fades, optional music bed, optional burned captions
        inputs = ["-i", str(joined)]
        vf = []
        # Captions first, so they fade with the picture instead of sitting on black
        if edl.get("captions"):
            srt = ff_quote(str((base / edl["captions"]).resolve()))
            vf.append(f"subtitles={srt}:force_style='FontSize=14,Outline=2'")
        if fade:
            vf += [f"fade=t=in:st=0:d={fade}", f"fade=t=out:st={total - fade}:d={fade}"]
        music = edl.get("music")
        if music:
            # "start" skips into the track, e.g. past dead air at its beginning
            inputs += ["-stream_loop", "-1", "-ss", str(music.get("start", 0.0)),
                       "-i", str((base / music["src"]).resolve())]
            vol = music.get("volume", 0.8)
            src_vol = music.get("source_volume", 0.0)  # 0 = music only
            af = (f"[0:a]volume={src_vol}[s];[1:a]volume={vol},"
                  f"afade=t=in:d=1,afade=t=out:st={max(total - 2, 0)}:d=2[m];"
                  f"[s][m]amix=inputs=2:duration=first:normalize=0[a]")
            amap = ["-filter_complex", af, "-map", "0:v", "-map", "[a]"]
        else:
            amap = ["-map", "0:v", "-map", "0:a"]
            if fade:
                amap += ["-af", f"afade=t=in:d={fade},afade=t=out:st={total - fade}:d={fade}"]
        cmd = ["ffmpeg", "-hide_banner", "-y", *inputs, *amap]
        if vf:
            cmd += ["-vf", ",".join(vf), "-c:v", "libx264", "-preset", preset,
                    "-crf", str(crf)]
        else:
            cmd += ["-c:v", "copy"]
        cmd += ["-c:a", "aac", "-b:a", "192k", "-t", f"{total}",
                "-movflags", "+faststart", str(out)]
        run(cmd)
    print(f"rendered {ts(total)} @ {w}x{h} -> {out}")


# ---------- CLI ----------

def main() -> None:
    need("ffmpeg"); need("ffprobe")
    p = argparse.ArgumentParser(prog="vidpipe", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("probe", help="list clips (duration/res/fps/audio) and photos (size)")
    s.add_argument("path")
    s.add_argument("--out", help="default: media.json for a folder, print only for one file")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("scenes", help="detect shot changes in a clip")
    s.add_argument("file")
    s.add_argument("--threshold", type=float, default=0.3, help="0..1, lower = more cuts")
    s.add_argument("--min", type=float, default=1.0, help="drop shots shorter than this")
    s.set_defaults(fn=cmd_scenes)

    s = sub.add_parser("sheet", help="contact sheet of a clip, a photo, or a folder of photos")
    s.add_argument("file"); s.add_argument("--out")
    s.add_argument("--frames", type=int, default=16,
                   help="frames from a clip, or photos per sheet for a folder")
    s.add_argument("--cols", type=int, default=4)
    s.add_argument("--width", type=int, default=480)
    s.set_defaults(fn=cmd_sheet)

    s = sub.add_parser("transcribe", help="speech to text (needs whisper)")
    s.add_argument("file"); s.add_argument("--outdir")
    s.add_argument("--model", default="small", help="tiny/base/small/medium/large-v3")
    s.set_defaults(fn=cmd_transcribe)

    s = sub.add_parser("plan", help="check an edit list and print its shot list; renders nothing")
    s.add_argument("edl")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("render", help="render an edit list JSON")
    s.add_argument("edl"); s.add_argument("--draft", action="store_true",
                                          help="fast low-quality preview render")
    s.set_defaults(fn=cmd_render)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
