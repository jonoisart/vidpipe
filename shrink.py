#!/usr/bin/env python3
"""shrink.py: re-encode video to 1080p HEVC. One file, or a folder largest first.

  shrink.py <video>                 1080p copy beside the original
  shrink.py <folder> --out <dir> [--after keep|evict] [--limit N] [--dry-run]

- Originals are only read. Nothing is changed, moved or deleted.
- Same speed and frame rate as the source. Short side is scaled down to 1080,
  never up. Audio is copied when the mp4 container allows it.
- If the destination volume is too full to be sure the result fits, the encode
  runs on the local disk first and is moved over only if there is room.
- Files that live in iCloud only are downloaded one at a time. With
  --after evict, each local download is released again once its 1080p copy
  has been checked (macOS can refuse; the script then says so).
- Safe to stop (Ctrl-C, or create a file named STOP in <out>) and run again:
  finished files are skipped.
- Folder mode prints sizes and counts only. File names go to
  <out>/shrink_log.jsonl, which stays on this Mac.
"""
import argparse, json, os, shutil, subprocess, sys, time
from pathlib import Path

for _keg in ("/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin"):
    if Path(_keg, "ffmpeg").exists():
        os.environ["PATH"] = _keg + os.pathsep + os.environ.get("PATH", ""); break

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".wmv", ".flv", ".ts", ".mts", ".mpg", ".mpeg", ".3gp"}
MP4_AUDIO_OK = {"aac", "ac3", "eac3", "mp3", "alac"}
SF_DATALESS = 0x40000000   # file is an iCloud placeholder: content not on disk
SMALL_MBPS = 2.5           # at or under 1080p and under this bitrate: leave alone
MIN_GAIN = 0.9             # discard a result that isn't at least 10% smaller
DISK_MARGIN = 10e9         # keep this much disk free beyond the file being fetched
LOCAL_WORK = Path.home() / "Movies" / "shrink_tmp"   # local, outside iCloud


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

def free(p):
    return shutil.disk_usage(p).free

def is_dataless(p):
    return bool(p.stat().st_flags & SF_DATALESS)

EVICT_JXA = """function run(argv) {
  ObjC.import("Foundation");
  var err = $();
  return $.NSFileManager.defaultManager.evictUbiquitousItemAtURLError(
    $.NSURL.fileURLWithPath(argv[0]), err) ? "ok" : "refused";
}"""

def evict(p):
    """Ask macOS to drop the local copy of an iCloud file (the cloud copy stays).
    macOS can refuse, e.g. while the file is open. Returns True if it is gone."""
    run(["osascript", "-l", "JavaScript", "-e", EVICT_JXA, str(p)])
    time.sleep(1)
    return is_dataless(p)

def probe(p):
    d = json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height,pix_fmt,color_transfer,"
                        "color_primaries,color_space,bit_rate:stream_side_data=rotation:"
                        "format=duration,bit_rate", "-of", "json", str(p)]).stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return None
    rot = 0
    for sd in v.get("side_data_list", []):
        rot = int(float(sd.get("rotation", 0) or 0))
    w, h = int(v["width"]), int(v["height"])
    if abs(rot) in (90, 270):          # ffmpeg applies rotation before filters
        w, h = h, w
    fmt = d.get("format", {})
    return {"w": w, "h": h, "pix_fmt": v.get("pix_fmt", ""), "trc": v.get("color_transfer"),
            "prim": v.get("color_primaries"), "matrix": v.get("color_space"),
            "dur": float(fmt.get("duration") or 0),
            "mbps": float(v.get("bit_rate") or fmt.get("bit_rate") or 0) / 1e6,
            "audio": [s["codec_name"] for s in d["streams"] if s.get("codec_type") == "audio"]}

def encode_cmd(src, dest, info, crf, preset, progress):
    vf = []
    if min(info["w"], info["h"]) > 1080:
        vf = ["-vf", ("scale=-2:1080" if info["w"] >= info["h"] else "scale=1080:-2") + ":flags=lanczos"]
    ten_bit = "10" in info["pix_fmt"]
    color = []
    for flag, key in (("-color_primaries", "prim"), ("-color_trc", "trc"), ("-colorspace", "matrix")):
        if info[key] and info[key] != "unknown":
            color += [flag, info[key]]     # keeps HDR (HLG/PQ) sources HDR
    audio = ["-c:a", "copy"] if set(info["audio"]) <= MP4_AUDIO_OK else ["-c:a", "aac", "-b:a", "160k"]
    return ["ffmpeg", "-hide_banner", "-nostdin", "-n", "-v", "error", "-nostats", "-progress", str(progress),
            "-i", str(src), "-map", "0:v:0", "-map", "0:a?", *vf,
            "-c:v", "libx265", "-preset", preset, "-crf", str(crf), "-x265-params", "log-level=error",
            "-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p", *color, "-tag:v", "hvc1",
            *audio, "-map_metadata", "0", "-movflags", "+faststart", "-f", "mp4", str(dest)]

def check(out, info):
    """Return an error string, or None if the output looks right."""
    o = probe(out)
    if not o:
        return "output has no video stream"
    if abs(o["dur"] - info["dur"]) > max(0.5, info["dur"] * 0.005):
        return f"length differs: {o['dur']:.1f}s vs {info['dur']:.1f}s"
    r = run(["ffmpeg", "-v", "error", "-sseof", "-10", "-i", str(out), "-f", "null", "-"])
    if r.returncode or r.stderr.strip():
        return "end of output does not decode cleanly"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="one video, or a folder of videos")
    ap.add_argument("--out", type=Path, help="where the 1080p files go. Needed for a folder (must be outside it); "
                                             "for one video the default is the video's own folder")
    ap.add_argument("--after", choices=("keep", "evict"), default="keep",
                    help="evict: release the local download of an iCloud original once its copy is checked")
    ap.add_argument("--limit", type=int, help="only the N largest videos")
    ap.add_argument("--crf", type=int, default=24)
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--dry-run", action="store_true", help="list totals; download and encode nothing")
    a = ap.parse_args()

    target = a.source.resolve()
    single = target.is_file()
    if single:
        src_root, vids = target.parent, [target]
        out_root = (a.out or target.parent).resolve()
    elif target.is_dir():
        if not a.out:
            sys.exit("--out is needed for a folder")
        src_root, out_root = target, a.out.resolve()
        if out_root == src_root or src_root in out_root.parents:
            sys.exit("--out must be outside the source folder")
        vids = sorted((p for p in src_root.rglob("*") if p.is_file() and not p.name.startswith(".")
                       and p.suffix.lower() in VIDEO_EXTS and ".1080p." not in p.name),
                      key=lambda p: -p.stat().st_size)[: a.limit]
    else:
        sys.exit(f"not found: {target}")

    # output path per source; two sources that differ only by extension keep theirs in the name
    dests = {p: (out_root / p.relative_to(src_root)).with_suffix(".1080p.mp4") for p in vids}
    clash = {d for d in dests.values() if list(dests.values()).count(d) > 1}
    dests = {p: (p_out if p_out not in clash else p_out.with_name(p.name + ".1080p.mp4"))
             for p, p_out in dests.items()}
    total = sum(p.stat().st_size for p in vids)
    cloud = sum(is_dataless(p) for p in vids)
    print(f"{len(vids)} video(s), {total / 1e9:.1f} GB ({cloud} in iCloud only) -> {out_root}", flush=True)
    if a.dry_run:
        done = sum(d.exists() for d in dests.values())
        print(f"already converted: {done}. free disk: {free(Path.home()) / 1e9:.0f} GB. dry run, nothing done.")
        return

    out_root.mkdir(parents=True, exist_ok=True)
    log_path = None if single else out_root / "shrink_log.jsonl"
    stop = out_root / "STOP"
    saved = 0.0
    def log(rec):
        if log_path:
            with log_path.open("a") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S %Z"), **rec}) + "\n")

    for n, src in enumerate(vids, 1):
        tag = f"[{n}/{len(vids)}]"
        if stop.exists():
            print("STOP file found, stopping."); break
        size = src.stat().st_size
        dest = dests[src]
        if dest.exists():
            print(f"{tag} {size / 1e9:.2f} GB: already converted, skipped"); continue
        was_cloud = is_dataless(src)
        if was_cloud and free(Path.home()) < size + DISK_MARGIN:
            print(f"{tag} {size / 1e9:.2f} GB: not enough free disk to download it. stopping."); break
        dest.parent.mkdir(parents=True, exist_ok=True)
        # destination too full to be sure the result fits: encode on the local disk, move it after
        tight = free(dest.parent) < size
        work = LOCAL_WORK if tight else dest.parent
        work.mkdir(parents=True, exist_ok=True)
        part, prog = work / (dest.name + ".partial"), work / (dest.name + ".progress")
        try:
            t0 = time.time()
            if was_cloud:
                print(f"{tag} {size / 1e9:.2f} GB: downloading from iCloud...", flush=True)
                with src.open("rb") as f:   # first read blocks until macOS has fetched the file
                    f.read(1)
            t_dl = time.time() - t0
            info = probe(src)
            if not info or not info["dur"]:
                print(f"{tag} unreadable, skipped"); log({"src": str(src), "status": "unreadable"}); continue
            if min(info["w"], info["h"]) <= 1080 and 0 < info["mbps"] <= SMALL_MBPS:
                print(f"{tag} {size / 1e9:.2f} GB: already small ({info['mbps']:.1f} Mbps), left alone")
                log({"src": str(src), "status": "already small"}); continue
            part.unlink(missing_ok=True)
            print(f"{tag} {size / 1e9:.2f} GB, {info['w']}x{info['h']}, {info['dur'] / 60:.1f} min: encoding"
                  + (" on the local disk (destination is nearly full)" if tight else "") + "...", flush=True)
            t1 = time.time()
            r = run(["caffeinate", "-i", *encode_cmd(src, part, info, a.crf, a.preset, prog)])
            t_enc = time.time() - t1
            err = (r.stderr.strip()[-300:] or "ffmpeg failed") if r.returncode else check(part, info)
            new = part.stat().st_size if part.exists() else 0
            if not err and new > size * MIN_GAIN:
                err = "no real saving, original kept as is"
            if err:
                part.unlink(missing_ok=True)
                print(f"{tag} {size / 1e9:.2f} GB: FAILED ({err})")
                log({"src": str(src), "status": "failed", "error": err}); continue
            st = src.stat(); os.utime(part, (st.st_atime, st.st_mtime))   # keep the original's date
            if tight and free(dest.parent) < new + 0.5e9:
                final = work / dest.name       # no room on the destination: leave the result local
                part.rename(final)
                where = f" (no room on the destination, left in {work})"
            else:
                final = dest
                shutil.move(str(part), str(dest))
                where = ""
            saved += size - new
            print(f"{tag} {size / 1e9:.2f} GB -> {new / 1e9:.2f} GB ({100 * new / size:.1f}%), "
                  f"download {t_dl / 60:.0f} min, encode {t_enc / 60:.0f} min{where}. "
                  f"saved so far {saved / 1e9:.1f} GB", flush=True)
            log({"src": str(src), "out": str(final), "status": "ok", "bytes_in": size, "bytes_out": new,
                 "download_s": round(t_dl), "encode_s": round(t_enc)})
        except KeyboardInterrupt:
            part.unlink(missing_ok=True)
            print("\ninterrupted. the file in progress was dropped; run again to resume."); break
        finally:
            prog.unlink(missing_ok=True)
            # release only what this run downloaded; a file that was local stays local
            if a.after == "evict" and was_cloud and not is_dataless(src) and not evict(src):
                print(f"{tag} note: macOS would not release the local copy of the original")
    print(f"done. saved {saved / 1e9:.1f} GB." + (f" log: {log_path}" if log_path else ""))


if __name__ == "__main__":
    main()
