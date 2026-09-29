# Experience completion — 2026-09-29

The previously deferred subtitle scrolling and recording-save controls are now integrated and pushed.

## Commits

- `68c3b4d4c9d8a5e86d87c540a3a87bb28156c554` — complete subtitle scrolling and confirmed recording control.
- Earlier pushed commits remain in `main`: window destruction guard, absolute log time, and closeout docs.

## Verified

- Python: `944 passed, 4 skipped, 17 deselected, 1 xfailed, 1 warning`.
- Focused recording/contract/time tests: `87 passed`.
- Frontend `npm run check`: exit 0.
- Recording controller: all focused cases passed.
- Headless Chromium production-module/CSS validation: `5 viewport/DPR/language configurations; 25 evidence records`, exit 0.
- Scroll behavior verified: overflow region, controls remain visible, long text wraps, near-bottom follow, history reading is not pulled down, draft updates preserve position, wheel scrolling, clear, and A/B/C/D mode transitions.

## Recording semantics

The switch controls confirmed microphone WAV saving in microphone mode. It does not start/stop system-audio capture or recognition. Backend state is authoritative; unknown, rejected, disconnected, unsupported, and idle-only states are shown honestly. Changes are only accepted while the pipeline is fully settled.

## Not run

Real microphone/loopback, real model inference, hardware acceleration, packaged installer, and formal Release validation were not run. No user configuration, model data, or Release files were touched.

## Delivery

Remote: `https://github.com/tuotuonuts/VoxSub.git`
Branch: `main`
Verified remote HEAD: `68c3b4d4c9d8a5e86d87c540a3a87bb28156c554`
Working tree: clean
