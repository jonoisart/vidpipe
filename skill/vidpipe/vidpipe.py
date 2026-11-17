#!/usr/bin/env python3
"""
vidpipe: a small ffmpeg toolkit an AI agent can drive to edit video.

The agent's loop:
  1. probe       -> what footage exists (duration, resolution, fps, audio)
  2. scenes      -> where each shot changes
  3. sheet       -> a contact-sheet image the agent can LOOK at to judge shots
  4. transcribe  -> speech-to-text, for talking/interview footage (optional)
  5. render      -> the agent writes an edit list (JSON), this renders it

Only dependency besides Python 3.10+ is ffmpeg/ffprobe. sheet needs the
drawtext filter and captions need subtitles, so use Homebrew's ffmpeg-full.
Transcription optionally uses mlx-whisper (Apple Silicon) or openai-whisper.

MIT License.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".lrf"}

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
        "duration": round(float(data["format"].get("duration", 0)), 3),
        "width": v and v["width"],
        "height": v and v["height"],
        "fps": fps,
        "rotation": rotation,
        "has_audio": a is not None,
        "created": data["format"].get("tags", {}).get("creation_time"),
    }


def ts(seconds: float) -> str:
    """Seconds -> HH:MM:SS.mmm for readable output."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


# ---------- commands ----------

def cmd_probe(args) -> None:
    """Probe a file or every video in a folder.

    A folder writes media.json. A single file only prints, unless --out is
    given, so checking a render never overwrites the footage inventory.
    """
    target = Path(args.path)
    files = [target] if target.is_file() else sorted(
        p for p in target.rglob("*") if p.suffix.lower() in VIDEO_EXTS)
    if not files:
        sys.exit(f"no video files found in {target}")
    media = [probe_file(f) for f in files]
    out = args.out or (None if target.is_file() else "media.json")
    if out:
        Path(out).write_text(json.dumps(media, indent=2))
    total = sum(m["duration"] for m in media)
    print(f"{len(media)} clips, {ts(total)} total" + (f" -> {out}" if out else ""))
    for m in media:
        print(f"  {Path(m['file']).name:40s} {ts(m['duration'])}  "
              f"{m['width']}x{m['height']}@{m['fps']}  audio={m['has_audio']}")


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


def cmd_sheet(args) -> None:
    """Make a contact sheet: N evenly spaced frames, each stamped with its time.

    This is how the agent 'sees' footage: it reads the PNG, judges shots,
    and picks in/out points by timestamp.
    """
    need_filter("drawtext")
    src = Path(args.file)
    info = probe_file(src)
    dur, n = info["duration"], args.frames
    cols = args.cols
    rows = -(-n // cols)  # ceiling division
    step = dur / n
    out = Path(args.out or src.with_suffix(src.suffix + ".sheet.jpg"))
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(n):
            t = min(step * i + step / 2, max(dur - 0.05, 0))
            frame = Path(tmp) / f"f{i:03d}.jpg"
            # ffmpeg's filter parser treats ":" as a separator, so escape it
            label = ts(t).replace(":", r"\:")
            # fast seek (-ss before -i), scale to thumb, burn timestamp label
            run(["ffmpeg", "-hide_banner", "-y", "-ss", f"{t:.3f}", "-i", str(src),
                 "-frames:v", "1",
                 "-vf", f"scale={args.width}:-2,"
                        f"drawtext=text='{label}':x=6:y=6:fontsize=18:"
                        f"fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=4",
                 "-q:v", "4", str(frame)])
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


def build_clip_filter(clip: dict, w: int, h: int, fps: int, lut: str | None) -> str:
    """Video filter chain that turns one source clip into a uniform segment."""
    f = []
    speed = clip.get("speed", 1.0)
    if speed != 1.0:
        f.append(f"setpts=PTS/{speed}")
    if lut:
        f.append(f"lut3d={ff_quote(lut)}")
    mode = clip.get("fit", "crop")
    if mode == "crop":
        # Fill the frame, then crop. crop_x/crop_y (0..1) pick which part to keep,
        # e.g. crop_x=0.3 keeps the left-of-center subject in a 9:16 reframe.
        cx, cy = clip.get("crop_x", 0.5), clip.get("crop_y", 0.5)
        f.append(f"scale={w}:{h}:force_original_aspect_ratio=increase")
        f.append(f"crop={w}:{h}:(iw-{w})*{cx}:(ih-{h})*{cy}")
    else:
        # "pad": fit whole frame inside, letterbox with black
        f.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease")
        f.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
    f += [f"fps={fps}", "setsar=1", "format=yuv420p"]
    return ",".join(f)


def cmd_render(args) -> None:
    """Render an edit list (JSON) to a finished video.

    Each clip is normalized to the same size/fps/audio format first, then the
    segments are joined losslessly, then an optional music bed is mixed in.
    """
    edl_path = Path(args.edl)
    edl = json.loads(edl_path.read_text())
    base = edl_path.parent  # clip paths in the EDL are relative to the EDL file
    aspect = edl.get("aspect", "16:9")
    if aspect not in ASPECTS:
        sys.exit(f"unknown aspect {aspect!r} (use one of: {', '.join(ASPECTS)})")
    w, h = ASPECTS[aspect]
    fps = edl.get("fps", 30)
    lut = edl.get("lut")
    if lut:
        lut = str((base / lut).resolve())
    fade = edl.get("fade", 0.0)
    out = (base / edl.get("output", "out.mp4")).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    clips = edl["clips"]
    if not clips:
        sys.exit("edit list has no clips")
    # Catch bad values before rendering anything
    for i, clip in enumerate(clips):
        if not clip.get("speed", 1.0) > 0:
            sys.exit(f"clip {i}: speed must be greater than 0 (got {clip['speed']})")
    planned = sum((c["out"] - c["in"]) / c.get("speed", 1.0) for c in clips)
    if fade * 2 > planned:
        sys.exit(f"fade of {fade}s is too long for a {planned:.2f}s timeline "
                 f"(max {planned / 2:.2f}s)")
    if edl.get("captions"):
        need_filter("subtitles")
    # Draft mode renders fast and small for review; final uses higher quality.
    preset, crf = ("ultrafast", 30) if args.draft else ("medium", 18)

    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for i, clip in enumerate(clips):
            src = (base / clip["src"]).resolve()
            info = probe_file(src)
            t_in, t_out = clip["in"], clip["out"]
            if not 0 <= t_in < t_out <= info["duration"] + 0.05:
                sys.exit(f"clip {i}: bad in/out {t_in}-{t_out} "
                         f"(source is {info['duration']}s)")
            seg_len = (t_out - t_in) / clip.get("speed", 1.0)
            part = Path(tmp) / f"p{i:03d}.mp4"
            vf = build_clip_filter(clip, w, h, fps, lut)
            cmd = ["ffmpeg", "-hide_banner", "-y", "-ss", f"{t_in}", "-to", f"{t_out}",
                   "-i", str(src)]
            # Keep source audio only if it exists and isn't muted; otherwise add
            # silence so every segment has an audio track (concat needs that).
            keep_audio = info["has_audio"] and not clip.get("mute", False) \
                and clip.get("speed", 1.0) == 1.0
            if not keep_audio:
                cmd += ["-f", "lavfi", "-t", f"{seg_len}",
                        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            cmd += ["-vf", vf, "-map", "0:v:0", "-map", "0:a:0" if keep_audio else "1:a:0",
                    "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                    "-t", f"{seg_len}", str(part)]
            run(cmd)
            parts.append(part)
            print(f"  [{i + 1}/{len(clips)}] {src.name} {t_in}-{t_out}s")

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
        if fade:
            vf += [f"fade=t=in:st=0:d={fade}", f"fade=t=out:st={total - fade}:d={fade}"]
        if edl.get("captions"):
            srt = ff_quote(str((base / edl["captions"]).resolve()))
            vf.append(f"subtitles={srt}:force_style='FontSize=14,Outline=2'")
        music = edl.get("music")
        if music:
            inputs += ["-stream_loop", "-1", "-i", str((base / music["src"]).resolve())]
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

    s = sub.add_parser("probe", help="list clips with duration/res/fps/audio")
    s.add_argument("path")
    s.add_argument("--out", help="default: media.json for a folder, print only for one file")
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("scenes", help="detect shot changes in a clip")
    s.add_argument("file")
    s.add_argument("--threshold", type=float, default=0.3, help="0..1, lower = more cuts")
    s.add_argument("--min", type=float, default=1.0, help="drop shots shorter than this")
    s.set_defaults(fn=cmd_scenes)

    s = sub.add_parser("sheet", help="contact sheet image of a clip")
    s.add_argument("file"); s.add_argument("--out")
    s.add_argument("--frames", type=int, default=16)
    s.add_argument("--cols", type=int, default=4)
    s.add_argument("--width", type=int, default=480)
    s.set_defaults(fn=cmd_sheet)

    s = sub.add_parser("transcribe", help="speech to text (needs whisper)")
    s.add_argument("file"); s.add_argument("--outdir")
    s.add_argument("--model", default="small", help="tiny/base/small/medium/large-v3")
    s.set_defaults(fn=cmd_transcribe)

    s = sub.add_parser("render", help="render an edit list JSON")
    s.add_argument("edl"); s.add_argument("--draft", action="store_true",
                                          help="fast low-quality preview render")
    s.set_defaults(fn=cmd_render)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
