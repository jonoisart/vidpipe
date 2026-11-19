---
name: vidpipe
description: Edit video agentically with ffmpeg — make selects, rough cuts, social reels (9:16, 1:1, 16:9), highlight edits, and photo slideshows from raw footage (drone, phone, camera, interviews) and still photos (JPG, PNG, HEIC, camera RAW). Use when the user asks to cut, trim, assemble, reframe, caption, or render video from files on disk.
---

# vidpipe: agentic video editing

You edit video by looking at footage, agreeing a plan with the user, and writing an edit list (EDL) that `vidpipe.py` renders with ffmpeg. You never hand-write ffmpeg commands for the edit itself, and you never render before the user has approved the plan.

`VP` below means: `python3 ~/.claude/skills/vidpipe/vidpipe.py`

## Workflow

1. **Inventory.** `VP probe <footage_dir> --out <work>/media.json`. Note durations, resolution, fps, and which clips have audio. Drone clips usually have none. Photos (JPG, PNG, HEIC, camera RAW) are listed too, with `"kind": "photo"` and their size as displayed (rotation already applied). Clips are flagged `HDR (HLG)`, `HDR (PQ)` or `no color tags`. You don't need to act on that: `render` and `sheet` convert every source to standard HD color.

2. **See the footage.** For each clip worth considering:
   - `VP sheet <clip> --frames 16` makes a timestamped contact sheet. **Read the image.** This is how you judge composition, motion, light, and subject.
   - `VP scenes <clip>` gives shot boundaries. The score is brightness-based, so it can miss cuts between similar-brightness shots. Trust the sheet over it.
   - For a long clip, zoom in: make a second sheet of only the interesting span (trim to a temp file with `ffmpeg -ss A -to B -c copy`, then sheet it with more frames).
   - Photos: `VP sheet <photo_folder> --out <work>/sheets/photos.jpg` makes sheets of 16 photos each, labelled by filename (`photos.jpg`, `photos.2.jpg`, ...). **Read every sheet** and pick photos by filename. You can't Read a HEIC directly, so this is how you see them.

3. **Hear the footage** (talking, interviews only). `VP transcribe <clip>` writes `<clip>.transcript.json` with timed segments. Cut on sentence boundaries, never mid-word. Pad 0.15s before `in` and 0.25s after `out`.

4. **Ask.** Before you write an EDL, ask the user what the footage can't tell you, in one round with AskUserQuestion. Skip anything their request already settled. "Get creative" or "surprise me" settles none of it. Cover:
   - Where it will be shown, and so which aspect.
   - Target length.
   - Sound: a music track (which file), clip sound, voices, or silent.
   - On-screen text: none, or which words.
   - Any speed change you think would help (speed-up, slow motion, timelapse). Say which shot and why.

5. **Propose a plan. Render nothing yet.**
   - Write the EDL as `<work>/edit.json` (schema below). Pick shots with intent: an opener that hooks, variety in scale and motion, a closer that resolves. Default shot lengths: 1.5–3s for social reels, 3–6s for scenic/drone pieces.
   - `VP plan <work>/edit.json` checks the EDL and prints it as a shot list. Show the user that table, one line on why each shot is there, and every choice you made for them.
   - If the plan lists anything under "Needs the user's OK", get that OK by name. A general "looks good" does not cover it unless the item was in the plan they saw.
   - Wait for their go-ahead. If the plan changes, run `VP plan` again and show the new one.

6. **Draft render**, only after the plan is approved. `VP render <work>/edit.json --draft` is fast and low quality. Then verify:
   - `VP probe <output>`: duration matches the target, aspect is correct.
   - `VP sheet <output> --frames 12`: read it. Check reframes keep the subject in frame, no black flashes, nothing awkward.
   - Fix the EDL and re-render until it's right.

7. **Show the user** the draft and the sheet, plus a one-line summary per shot. Only after they approve: `VP render <work>/edit.json` (final quality).

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
    {"src": "footage/IMG_0101.MOV", "in": 0.0, "out": 2.5, "fit": "pad"},
    {"src": "photos/IMG_0102.HEIC", "duration": 2.5, "crop_y": 0.3},
    {"src": "photos/IMG_0103.JPG", "duration": 3.5, "zoom": [1.0, 1.2], "crop_y": 0.35}
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
| `music.start` | Seconds into the music file to begin from, e.g. to skip dead air at its start | 0 |
| clip `in` / `out` | Source seconds | required for video |
| clip `duration` | Photos only: seconds to hold the photo on screen. Replaces `in`/`out` | required for photos |
| clip `speed` | 2.0 = twice as fast. Source audio is dropped on speed changes. Not allowed on photos | 1.0 |
| clip `fit` | `crop` fills the frame, `pad` letterboxes | `crop` |
| clip `crop_x` / `crop_y` | 0..1, which part to keep when cropping. 0 = left/top, 1 = right/bottom. On a photo, `[start, end]` pans | 0.5 |
| clip `zoom` | Photos only. 1.0 is the widest view that fills the frame, 1.2 is 20% tighter. `[start, end]` zooms over the photo's duration | 1.0 |
| clip `mute` | Drop this clip's audio | false |

## Reframing for vertical

Wide footage cropped to 9:16 keeps only ~32% of the width. For every clip, check its sheet and set `crop_x` to where the subject is. For a pan, pick the point the shot ends on. If nothing fits, use `"fit": "pad"` or skip the shot.

## Photos

A photo is a clip with `duration` in place of `in`/`out`. It has silent audio. By default it is held as a still frame; `fit`, `crop_x` and `crop_y` work as they do for video.

**Ken Burns (zoom and pan).** Give `zoom`, `crop_x` or `crop_y` as `[start, end]` and the frame travels in a straight, even line from the first framing to the second over the photo's duration. Needs `"fit": "crop"` (the default).

- Push in: `"zoom": [1.0, 1.2]` with `crop_x`/`crop_y` set to where the subject is. Pull back: `"zoom": [1.3, 1.0]`. Pan: a fixed `zoom` above 1 with a moving `crop_x` or `crop_y`.
- At zoom `z` the frame shows `1/z` of the widest view. Work out from the photo sheet where the subject sits (as fractions of the photo) and check that both the start and the end framing contain it.
- Keep moves slow: a zoom change of 0.2 to 0.4 over 3 to 4 seconds. Vary the move from photo to photo.
- Add motion when the user asks for it (Ken Burns, a moving slideshow), and list each move in the plan. After the draft, read its sheet and check the first and last frame of every moving photo.

- Hold 2–3s in a social reel, 3–5s in a slower piece.
- Cropped to 9:16, a landscape 4:3 photo keeps ~42% of its width and a portrait 3:4 photo keeps 75%. Set `crop_x`/`crop_y` from the sheet, or use `"fit": "pad"`.
- A Live Photo's video half is a normal `.mov` clip.
- Camera RAW (DNG/ProRAW, CR2/CR3, NEF, ARW, RAF and others) is decoded by macOS `sips` with Apple's default rendering. Edits made in the Photos app are not in an original RAW file; if the user wants them, they must export the edited version.
- Photos, HDR clips and clips with missing color tags all come out as standard HD color (BT.709), so they match each other. HDR is tone-mapped with fixed settings; if an HDR clip looks too dark or bright on the draft sheet, tell the user.

## Rules

- Never overwrite or move source footage. Write everything under a work folder. `sheet` takes `--out`; `scenes` and `transcribe` write next to the file they are given, so first make a free clone in the work folder (`cp -c <clip> <work>/clones/`) and run them on the clone.
- Never set `speed` unless the user asked for that speed change or approved it in the plan. Footage plays at real speed by default. If a speed change would help, ask first.
- Before writing any `in`/`out`, check it against the clip's duration from probe. `plan` and `render` reject out-of-range values.
- Large files: render drafts first. Final renders of long 4K timelines take minutes. Tell the user.
- Captions: build the `.srt` from transcript segments, max ~42 characters per line.
- If the user wants color grading beyond a LUT, zoom or pan on a video clip, or titles, say so. Those need Resolve or Remotion, not this tool.
