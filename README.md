# vidpipe

Agentic video editing with ffmpeg. Claude looks at your footage through contact sheets and transcripts, writes an edit list, and `vidpipe.py` renders it. Works with video clips and still photos (JPG, PNG, HEIC).

No Resolve, no cloud uploads, no GPU needed. Runs on any Mac.

## Install (macOS)

```zsh
brew install ffmpeg-full                    # not plain ffmpeg: that build has no drawtext/subtitles
uv tool install --python 3.14 mlx-whisper   # optional: transcription for talking footage

# Install as a Claude Code skill (folder is self-contained)
cp -R skill/vidpipe ~/.claude/skills/
```

`ffmpeg-full` is keg-only, so Homebrew leaves it off your `PATH`. vidpipe finds it without any setup. Contact sheets need its `drawtext` filter and burned captions need `subtitles`; the default `ffmpeg` formula has neither. Homebrew's Python refuses a system-wide `pip3 install` (PEP 668), which is why whisper goes in with `uv tool install`.

## Use

From Claude Code, in a folder that has your footage:

> Make a 30-second 9:16 reel from the drone clips in ./footage, warm LUT from ./luts, music from ./music/track.mp3. Draft first.

Claude probes the clips, reads contact sheets, writes `edit.json`, renders a draft, checks it, and shows you before the final render.

## Commands

| Command | Does |
|---|---|
| `probe <dir>` | List clips (duration, resolution, fps, audio) and photos (size) → `media.json` |
| `scenes <clip>` | Shot boundaries → `<clip>.scenes.json` |
| `sheet <clip>` | Timestamped contact-sheet image (how Claude "sees" footage) |
| `sheet <photo folder>` | Photo contact sheets, 16 per sheet, labelled by filename |
| `transcribe <clip>` | Timed transcript via mlx-whisper / whisper |
| `render <edit.json> [--draft]` | Render an edit list: trims, reframes, speed, LUT, captions, music, fades, held photos |

Edit list schema: see `skill/vidpipe/SKILL.md`.

## Limits

- No keyframed motion, transitions beyond fades, or titles. Use Resolve or Remotion for those.
- Grading is LUT-only.
- Photos are held as still frames (no zoom or pan). RAW/DNG is not supported.
- Color is not normalized across sources yet. Photos render in standard HD color (BT.709) and match standard footage; mixing in HDR clips or clips without color tags can shift colors.
- `scenes` is brightness-based and can miss cuts between similar-looking shots. The skill has Claude check contact sheets instead of relying on it.

## Where to run it

Run it on the machine where the footage lives. Rendering 4K is CPU-heavy, so prefer a Mac with free RAM and disk over a busy agent server.

MIT License.
