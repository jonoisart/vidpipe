# vidpipe

Agentic video editing with ffmpeg. Claude looks at your footage through contact sheets and transcripts, writes an edit list, and `vidpipe.py` renders it.

No Resolve, no cloud uploads, no GPU needed. Runs on any Mac.

## Install (macOS)

```zsh
brew install ffmpeg
pip3 install mlx-whisper            # optional: transcription for talking footage

# Install as a Claude Code skill (folder is self-contained)
cp -R skill/vidpipe ~/.claude/skills/
```

## Use

From Claude Code, in a folder that has your footage:

> Make a 30-second 9:16 reel from the drone clips in ./footage, warm LUT from ./luts, music from ./music/track.mp3. Draft first.

Claude probes the clips, reads contact sheets, writes `edit.json`, renders a draft, checks it, and shows you before the final render.

## Commands

| Command | Does |
|---|---|
| `probe <dir>` | List clips: duration, resolution, fps, audio → `media.json` |
| `scenes <clip>` | Shot boundaries → `<clip>.scenes.json` |
| `sheet <clip>` | Timestamped contact-sheet image (how Claude "sees" footage) |
| `transcribe <clip>` | Timed transcript via mlx-whisper / whisper |
| `render <edit.json> [--draft]` | Render an edit list: trims, reframes, speed, LUT, captions, music, fades |

Edit list schema: see `skill/vidpipe/SKILL.md`.

## Limits

- No keyframed motion, transitions beyond fades, or titles. Use Resolve or Remotion for those.
- Grading is LUT-only.
- `scenes` is brightness-based and can miss cuts between similar-looking shots. The skill has Claude check contact sheets instead of relying on it.

## Where to run it

Run it on the machine where the footage lives. Rendering 4K is CPU-heavy, so prefer a Mac with free RAM and disk over a busy agent server.

MIT License.
