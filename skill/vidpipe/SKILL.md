---
name: vidpipe
description: Edit video agentically with ffmpeg — make selects, rough cuts, social reels (9:16, 1:1, 16:9), and highlight edits from raw footage (drone, phone, camera, interviews). Use when the user asks to cut, trim, assemble, reframe, caption, or render video from files on disk.
---

# vidpipe: agentic video editing

You edit video by looking at footage, deciding the cut, and writing an edit list (EDL) that `vidpipe.py` renders with ffmpeg. You never hand-write ffmpeg commands for the edit itself.

`VP` below means: `python3 ~/.claude/skills/vidpipe/vidpipe.py`

## Workflow

1. **Inventory.** `VP probe <footage_dir> --out <work>/media.json`. Note durations, resolution, fps, and which clips have audio. Drone clips usually have none.

2. **See the footage.** For each clip worth considering:
   - `VP sheet <clip> --frames 16` makes a timestamped contact sheet. **Read the image.** This is how you judge composition, motion, light, and subject.
   - `VP scenes <clip>` gives shot boundaries. The score is brightness-based, so it can miss cuts between similar-brightness shots. Trust the sheet over it.
   - For a long clip, zoom in: make a second sheet of only the interesting span (trim to a temp file with `ffmpeg -ss A -to B -c copy`, then sheet it with more frames).

3. **Hear the footage** (talking, interviews only). `VP transcribe <clip>` writes `<clip>.transcript.json` with timed segments. Cut on sentence boundaries, never mid-word. Pad 0.15s before `in` and 0.25s after `out`.

4. **Write the EDL** as `<work>/edit.json` (schema below). Pick shots with intent: an opener that hooks, variety in scale and motion, a closer that resolves. Default shot lengths: 1.5–3s for social reels, 3–6s for scenic/drone pieces.

5. **Draft render.** `VP render <work>/edit.json --draft` is fast and low quality. Then verify:
   - `VP probe <output>`: duration matches the target, aspect is correct.
   - `VP sheet <output> --frames 12`: read it. Check reframes keep the subject in frame, no black flashes, nothing awkward.
   - Fix the EDL and re-render until it's right.

6. **Show the user** the draft and the sheet, plus a one-line summary per shot. Only after they approve: `VP render <work>/edit.json` (final quality).

## EDL schema

Paths are relative to the EDL file.

```json
{
  "output": "out/reel_9x16.mp4",
  "aspect": "9:16",
  "fps": 30,
  "lut": "luts/warm.cube",
  "fade": 0.4,
  "captions": "caps.srt",
  "music": {"src": "music/track.mp3", "volume": 0.7, "source_volume": 0.0},
  "clips": [
    {"src": "footage/DJI_0001.MP4", "in": 12.5, "out": 15.0, "crop_x": 0.35},
    {"src": "footage/DJI_0004.MP4", "in": 3.0, "out": 9.0, "speed": 2.0},
    {"src": "footage/IMG_0101.MOV", "in": 0.0, "out": 2.5, "fit": "pad"}
  ]
}
```

| Field | Meaning | Default |
|---|---|---|
| `aspect` | `16:9`, `9:16`, `1:1`, `4:5` | `16:9` |
| `fps` | Output frame rate | 30 |
| `lut` | `.cube` color LUT applied to every clip | none |
| `fade` | Fade in/out seconds, video and audio | 0 |
| `captions` | `.srt` to burn in | none |
| `music.volume` / `music.source_volume` | Music level / original audio level under it (0 = music only) | 0.8 / 0 |
| clip `in` / `out` | Source seconds | required |
| clip `speed` | 2.0 = twice as fast. Source audio is dropped on speed changes | 1.0 |
| clip `fit` | `crop` fills the frame, `pad` letterboxes | `crop` |
| clip `crop_x` / `crop_y` | 0..1, which part to keep when cropping. 0 = left/top, 1 = right/bottom | 0.5 |
| clip `mute` | Drop this clip's audio | false |

## Reframing for vertical

Wide footage cropped to 9:16 keeps only ~32% of the width. For every clip, check its sheet and set `crop_x` to where the subject is. For a pan, pick the point the shot ends on. If nothing fits, use `"fit": "pad"` or skip the shot.

## Rules

- Never overwrite or move source footage. Write everything under a work folder.
- Before writing any `in`/`out`, check it against the clip's duration from probe. render rejects out-of-range values.
- Large files: render drafts first. Final renders of long 4K timelines take minutes. Tell the user.
- Captions: build the `.srt` from transcript segments, max ~42 characters per line.
- If the user wants color grading beyond a LUT, keyframed motion, or titles, say so. Those need Resolve or Remotion, not this tool.
