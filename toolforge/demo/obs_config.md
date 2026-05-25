# OBS Studio Configuration for Backup Recording

The backup video is your safety net. If the live demo fails catastrophically (laptop dies, projector dies, you freeze), you cut to this video and your talk survives. Record it before any rehearsal that you log as "clean".

## Canvas and output

- **Base canvas resolution:** 1920 x 1080
- **Output resolution:** 1920 x 1080 (no downscale)
- **Frame rate:** 30 fps
- **Output format:** MP4 (Settings, Output, Recording, Recording Format = mp4)
- **Encoder:** Hardware (NVENC, AMF, or Apple VT) if available, otherwise x264 with `veryfast` preset and CRF 20
- **Audio bitrate:** 192 kbps
- **Audio sample rate:** 48 kHz

## Sources

Two display-capture sources arranged side by side, each occupying half the canvas width.

1. **Source 1: Left terminal**
   - Type: Display Capture (or Window Capture if you want to hide your desktop chrome)
   - Position: x=0, y=0, size 960 x 1080
   - Crop so only the terminal window shows, no menu bar, no dock

2. **Source 2: Right terminal**
   - Type: Display Capture or Window Capture
   - Position: x=960, y=0, size 960 x 1080
   - Same crop discipline

## Font hint

In the terminal app, set font to a clean monospace (JetBrains Mono, Cascadia Code, Menlo, Fira Code) at 18pt minimum, 24pt preferred. Anti-aliasing on. OBS will downscale per-pixel, so larger source text reads better in the final video.

## Audio

- **System audio:** ON, captured as a dedicated source
- **Microphone:** OFF (mute or do not add the mic source at all)
- Rationale: the backup video plays without you narrating live. Silence with terminal sounds is fine and avoids stale commentary contradicting your live talk.

## Filename pattern

Output filename pattern in OBS (Settings, Advanced, Recording, Filename Formatting):

```
toolforge_demo_%CCYY%MM%DD_%hh%mm
```

Which produces files like:

```
toolforge_demo_20260525_1430.mp4
```

Keep all recordings in one directory, sort by mtime, the most recent file is your active backup.

## 30-second smoke test (run BEFORE any dry run)

1. Hit Start Recording in OBS.
2. Type one command in each terminal so both show fresh output.
3. Wait 30 seconds.
4. Hit Stop Recording.
5. Open the resulting MP4 in your default video player.
6. Verify: both terminals visible, text legible at the back of a room (zoom out to fit the screen as a sanity check), audio plays system sounds, no mic input, file size sensible (roughly 5 to 30 MB for 30 seconds at 1080p30).

If any of these fail, fix the source or settings BEFORE running a dry run. A dry run without a recording is wasted because you cannot use it as a backup.

## Arming OBS for the live talk

- Start Recording 60 seconds before walking on stage
- Confirm the red REC indicator is on
- Confirm the disk has at least 5 GB free
- Do not Stop Recording until you are off the stage and your laptop is closed
