# FlightPath, project scope for Claude Code

Read this before touching anything. It is the single source of truth for what
this project is, what is proven, what is frozen, and how to work in it.

## What it is

A camera-based golf launch monitor for the driving range. A GoPro HERO9 Black
on a tripod films the ball side-on at 1080p240. An Android app on the user's
phone (a Samsung S22 Ultra during development) joins the camera's WiFi, pulls
each clip, tracks the ball with OpenCV, and shows ball speed, launch angle and
modelled carry in a GolfTrak-style UI (dark forest green, kelly green accent,
pill tabs, big numbers).

Owner: AJ (github.com/Cloggedbat). Solo project, ADHD, so: small steps, one
thing at a time, say what changed and what to do next.

### The three goals, in order

1. Easy setup. Anyone with a GoPro and an Android phone can use it. One APK,
   a 3-step wizard (camera, position, calibrate), no PC at the range.
2. Safe for the user. The app must never open a door into their phone. See
   Security below. This is not negotiable for any feature.
3. Open source later. GitHub releases plus F-Droid. Licence not chosen yet.
   The repo is public already, so nothing secret ever goes in it.

### Lineage

Architecture and UI ideas come from OpenFlight (github.com/open-flight/openflight,
a radar launch monitor): local engine on device, browser UI on localhost:8080,
impact trigger, queue-and-sync, per-metric confidence. No OpenFlight code is
copied (it is AGPL). Do not go read the OpenFlight repo for more; its radar
DSP and hardware do not transfer to a camera.

## Repo layout

| path | what | notes |
|---|---|---|
| `engine/flightpath/` | Python package: CV, physics, GoPro client, worker, HTTP server, `ui/index.html` | the product |
| `engine/analyze.py`, `engine/run.py` | CLI for recorded clips; server for desktop testing | `run.py` on the PC is the fastest test rig |
| `engine/ui/index.html` | canonical UI file | `engine/flightpath/ui/index.html` is a copy |
| `android/` | Kotlin shell: WebView + Chaquopy Python + camera WiFi binding + live view + updater | `app/src/main/python/flightpath/` is a COPY of the engine |
| `updates/` | stdlib Python update server for Railway (`version.json` + APK) | not deployed yet |
| `sync-engine.sh` | copies `engine/` into `android/` | run before every APK build |
| `CLAUDE.md`, `README.md`, `engine/README.md`, `engine/RANGE.md` | docs | RANGE.md has the test plan and security notes |

Never edit `android/app/src/main/python/flightpath/` directly. Edit `engine/`
and run `./sync-engine.sh` (Git Bash on Windows).

## Current state (2026-09-17)

App version 0.1.13, versionCode 14.

0.1.13 answers NOT-proven item 1 from the binary, not the phone. The exact
cv2.so that ships in the APK (android/app/build/python/pip/debug/common/cv2/)
embeds its build configuration: the Video I/O section is empty apart from
one backend, ANDROID_MEDIANDK. OpenCV 4.5.1's MediaNDK backend opens the
file and creates a MediaCodec fine, but retrieveFrame converts a frame only
when the decoder reports colour format 19 or 21. Qualcomm and Samsung
decoders report a vendor format, so it logs "Unsupported video format" and
read() returns False on every frame. That is the 0.1.12 "could not decode
the clip". It is not a codec problem: H.264 and HEVC fail identically, and
the fourcc it reported was always "?" because that backend does not
implement CAP_PROP_FOURCC. Setting the camera to H.264 would have changed
nothing. Chaquopy offers no other OpenCV (only 4.1.2.30 and 4.5.1.48), and
OpenCV 4.10 still has the same two-format limit, so there was no upgrade path.

The fix is ClipDecoder.kt: MediaExtractor plus MediaCodec configured for
COLOR_FormatYUV420Flexible, reading frames through getOutputImage(), which
normalises any vendor layout and carries the crop rect (1080p decodes into
a 1088-row buffer). It returns only the luma plane, because every consumer
in the engine converts to grayscale as its first act. Python reaches it via
flightpath/nativecap.py, which wraps it in the slice of the VideoCapture API
the engine uses and falls back to cv2.VideoCapture on a PC or if the native
open fails. detect.py, worker.py and selftest.py go through
nativecap.open_capture(); detect.to_gray() accepts 2-D or 3-D frames. The
Python half is proven on the PC by a fake-Chaquopy harness (23 checks,
frames bit-identical to the cv2 path, tracked speed identical). The Kotlin
half is compiled but has not yet run on the phone. The wizard's Engine row
now names the decoder that answered. Also fixed: a download cut short by a
WiFi drop was saved as a complete clip; gopro.download() now checks the
byte count against the media list.

0.1.12: capture now records and downloads a real clip (the 0.1.11 shutter fix
works), but decoding it with the APK's OpenCV failed with "could not decode
the clip". The error now reports the clip name, size, codec fourcc and whether
OpenCV opened the file, so we know whether it is the codec (the APK's OpenCV
4.5.1.48 has no FFmpeg and likely cannot do HEVC, which is what the HERO9
records at high bit rates) or a bad download. If HEVC, set the camera to H.264.
This is NOT-proven item 1 finally being answered on a real clip.

An over-the-air update server is deployed on Railway at
https://flightpath-updates-production.up.railway.app for AJ's own remote
testing (owner lifted the freeze on Railway to iterate while away from this
PC). The release loop: bump version, build, copy the APK to
updates/public/FlightPath.apk, `railway up` from updates/. The phone's in-app
updater pulls it over cellular. updates/.railwayignore re-includes the
gitignored APK so it deploys.

The APK built on this PC is signed with the Android debug key, not the
permanent one. `android/keystore/flightpath.jks` and `signing.properties` are
not on this machine and were not found in Downloads, Desktop, Documents or
OneDrive. Restore them from `flightpath-signing-key.zip` before building
anything that goes to another person. Switching back to the permanent key
means uninstalling whatever the debug key installed.

0.1.5 fixes the camera status flapping on and off every 3 s. The poll loop's
3-strike counter never reset after the first drop, liveness was judged on the
slow media list while reconnect was judged on any endpoint, and probe()
restarted the live stream on every reconnect. Status now follows a cheap
`state()` heartbeat, and a media list failure is a queue hiccup, not a lost
camera.

0.1.6 fixes calibration capture reporting "camera produced no clip" for every
failure. trigger()'s result was ignored, so a shutter the camera refused or
ignored looked like a missing clip. It now checks the camera is actually
encoding, stops the live view without letting that abort the shot, and the
status row says "no media list (SD card in?)" when the media list has never
answered, which is what a missing SD card looks like.

0.1.7 makes the shutter work on this HERO9. The camera answers
/gopro/camera/state but returns 404 for /gopro/camera/shutter/start, and
probe() had pinned the shutter to that path by family guess with no
fallback. Every capture since the first build had hit that 404; 0.1.5 hid
it as "camera produced no clip", 0.1.6 showed it as "HTTP 404". The shutter
and stream paths are no longer pinned; _call() tries both candidates on
first real use and falls through to the legacy /gp/gpControl shutter.

0.1.7 also stops the poll loop from picking up the calibration clip as a
golf shot. capture_reference_frame() and _tick() both discovered it, both
downloaded it to the same .part, and the tick then queued a 2 s clip of a
still club for analysis. While a capture is in flight (calib_stage recording
or fetching, capped at 60 s so a hung capture cannot stall shots) the tick
leaves new clips alone.

0.1.8 analyses the strike wherever it lands in the clip. detect.load_frames
kept only the head of a clip up to a 512 MiB memory cap, which at 1080p is
258 frames, 1.07 s at 240 fps, while Record a shot recorded 3 s. A golfer
cannot strike inside the first second after tapping, so every real shot
would have said "no ball track found" for a reason unrelated to the CV. The
shot path now scans the clip with the same rule find_impact_frame uses and
keeps 24 frames before and 60 after the first hard change; memory is bounded
by that window, not by clip length. The drop test still reads the head, as
it must. Record a shot is 6 s. Proven on synthetic clips only, including one
with the strike 3 s in.

0.1.9 sends legacy gpControl commands to port 80. The camera runs two HTTP
servers: Open GoPro, the media list and downloads on 8080; legacy control
on 80. The client had put everything on 8080, so every legacy fallback hung
until timeout instead of answering. On this HERO9, which 404s the Open GoPro
shutter, that was "shutter start timed out" and a black live view. The
keep_alive fallback that used to be a sleep command is now a status GET, and
the worker GETs the control port every tick while a preview is on, which the
legacy stream needs to stay up. Source: GoPro's own issue tracker and the
community HERO9 API docs; the Open GoPro spec site could not be read.

0.1.11 makes capture work. Real-camera evidence: the shutter start 404s on
Open GoPro and times out or 500s on legacy, the state polls fail with 500,
timeout and RemoteDisconnected, and then a clip appears. The camera records
even when every HTTP call around it errors, because its server is simply
unresponsive during recording. So trigger() no longer reads state to decide
success. It fires start and ignores the reply, records the full window, then
sends stop and retries until state recovers to a clean idle (the proof it
stopped and the guard against a runaway). Whether a clip resulted is decided
by the caller from the media list, the only reliable signal. capture reports
"no new clip appeared, make sure the camera is on its shooting screen (press
Mode), not a menu" when nothing recorded.

0.1.10 had trusted state during recording and so reported working shots as
"camera did not start", and it left the camera recording twice. Both fixed
here. _call() errors still name every URL tried, with its port.

0.1.10 also makes the phone its own test rig, because adb never came up and
every guess about the camera cost a build. The wizard's Camera step has a
Test camera button that probes the endpoints, fires the shutter and watches
the camera's state, checks whether a new clip appeared, starts the preview
and counts what reaches UDP 8554 in this process, and prints one line per
step. The bottom of the page has Show log, backed by GET /api/log with the
last 200 engine lines. The wizard no longer tells the user to leave the
camera on the WiFi menu; it says to press Mode and return to the shooting
screen, since a camera in a menu neither records nor previews.

Proven:
- CV pipeline on synthetic clips: 0.1 mph error at 240 and 480 fps, all clubs.
- Tap-to-calibrate scale (0.1%), drop-test readout solve, rolling-shutter correction.
- Security hardening verified against a hostile fake camera.
- Real HERO9 answers the Open GoPro HTTP API at 10.5.5.9:8080.
- The debug-signed APK installs and runs on the S22 Ultra; the Python engine
  boots inside it and serves the wizard.
- Native WiFi join to the camera via `WifiNetworkSpecifier`, the wizard's
  "Connect to camera" button. The phone stays on cellular for calls.
- The 0.1.5 connection holds with cellular on. No airplane mode needed.
- Media list works once the SD card is in. With no card the HERO9 answers
  status but 404s on both media list paths.

NOT proven (in order of importance):
1. That ClipDecoder.kt returns frames on the S22 Ultra. The APK's OpenCV
   cannot (proven from its binary, see 0.1.13), so the phone now decodes
   through MediaCodec. The wizard's Engine row runs both bundled clips
   through it and names the decoder. Expected: "ok (H.264 + HEVC,
   c2.qti.hevc.decoder)" or similar. "cannot decode video, MediaCodec" with
   an error means the Kotlin needs a fix; Show log will have the reason.
2. That calibration capture works end to end on the real camera. 0.1.5 said
   "camera produced no clip" with the card in. 0.1.6 tells apart a shutter the
   camera ignored from a clip that never appeared; the next attempt says which.
3. Live view (GoPro UDP MPEG-TS preview via Media3). Works against a fake
   camera; never seen a real stream.
4. Any real golf ball. Every number ever produced is from a synthetic clip.
5. Rolling-shutter readout time of the HERO9 (drop test measures it).

## The active plan

Five steps, about 2 hours total, no new features until they are done:

1. Update to 0.1.13, open the wizard, read the Engine row (5 min, no
   camera needed). `ok (H.264 + HEVC, <decoder name>)` proceed; the camera
   can stay on HEVC. `cannot decode video, MediaCodec` means the Kotlin
   decoder failed on this phone: tap Show log, copy the lines, and fix
   ClipDecoder.kt from the error. Do not change the camera's codec; that
   was never the problem.
2. Connect and auto-configure the camera; read back 1080p, 240, Linear,
   HyperSmooth off (20 min, indoors). SD card in.
3. `dropcal`: drop a ball past the lens, get px/m and readout time (30 min).
4. Hit ONE 7-iron outdoors. Expect roughly 110 to 125 mph at 17 to 21 degrees.
5. Hit twenty. Check consistency, not accuracy.

Frozen until step 5 is done: Termux path (dead, do not maintain), second
camera / Insta360 X5 / left-right dispersion, spoken metrics, overhead view,
shot tags, CSV export, F-Droid, licence choice, spin, club speed, smash
factor, over-the-air engine bundle, Railway deployment. If asked to build any
of these, point at this list first.

## How to work

### Test Python changes on the PC, not by rebuilding the APK

```
cd engine
pip install -r requirements.txt
python run.py                      # UI at http://127.0.0.1:8080
python run.py --gopro-host 10.5.5.9   # PC joined to the camera's WiFi
python analyze.py selftest         # decode + tracker sanity check
python make_test_clip.py --speed-mph 120 --angle 19 --fps 240 --out t.mp4
python analyze.py shot t.mp4 --camera hero9-1080p240 --readout-ms 0 --ref 120,980,611,980 --ref-inches 46
```

Run the synthetic clip test after any change to `detect.py`, `physics.py` or
`calibrate.py`. Match the generated `--fps` to the profile or you read half.

The PC venv runs opencv-python 5.0.0 and numpy 2.2.6 because requirements.txt
floats at >=4.8. The APK is pinned to 4.5.1.48 / numpy 1.26.2. So a green PC
selftest is evidence about the tracker and physics, not about decoding on the
phone. Do not treat it as answering NOT-proven item 1.

### Build and install the APK (Windows)

```
./sync-engine.sh
cd android
gradlew.bat assembleDebug            # add -PupdateUrl=https://... to bake in an update server
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Before every build that will be installed: bump `versionCode` (+1) and
`versionName` in `android/app/build.gradle.kts`. Android refuses same-code
updates.

Requirements: JDK 17, Android SDK 34, build-tools 34, platform-tools.
Chaquopy 17.0.0 pins Python 3.10, numpy 1.26.2, opencv-python 4.5.1.48;
these are the only versions with Android wheels. Do not "upgrade" them.
Kotlin daemon warnings during the build are normal; check the APK exists.

### Signing key

`android/keystore/flightpath.jks` and `signing.properties` are git-ignored
and must be present locally (AJ has them in `flightpath-signing-key.zip`).
Never generate a new key, never commit the key, never print its password.
A different key means every user must uninstall.

### Security rules (goal 2)

- Server binds 127.0.0.1 only. `--lan` needs a token. Never 0.0.0.0.
- POST requires `Content-Type: application/json`; body capped at 64 KB.
- Host header must be an IP literal (anti DNS rebinding).
- The camera is an untrusted input. Filenames match `SAFE_NAME`, paths are
  realpath-contained, downloads are size-capped, decode is byte-budgeted.
- All UI text from the engine is HTML-escaped. CSP headers stay on.
- Android permissions are the minimum: INTERNET (updater only), WiFi state
  and change (camera join), FINE_LOCATION (only to list WiFi networks).
  Cleartext HTTP is allowed only to 127.0.0.1, localhost and 10.5.5.9.
- Anything that talks to the internet from the phone must be explicit,
  user-triggered, and say what it sends. Today that is only the update check.

### Camera facts (HERO9 Black)

- Open GoPro HTTP at `http://10.5.5.9:8080`. Setting IDs: 2 resolution
  (1080p = 9), 3 fps (240 = 0, 120 = 1), 121 lens (Linear = 4, Linear +
  Horizon = 8, which is what the camera offers at 240), 135 HyperSmooth
  (off = 0). Preset group 1000 = video.
- Legacy gpControl is on port 80, not 8080: `http://10.5.5.9/gp/gpControl/...`.
  The media list and downloads (`/gp/gpMediaList`, `/videos/DCIM/...`) stay on
  8080. A gpControl command sent to 8080 hangs until timeout, it does not 404.
- This HERO9 (firmware 2.0) answers Open GoPro state and media but 404s the
  Open GoPro shutter. GoPro's openapi.json lists the HTTP shutter for HERO10
  and later only. The shutter works through legacy gpControl on port 80. That
  command errors, times out, or returns 500 yet still starts the recording,
  and once recording the camera's WHOLE HTTP stack goes unresponsive (state
  returns 500, then times out, then drops the connection) until recording
  ends. So state cannot confirm a recording is in progress; only a new clip in
  the media list can. `trigger()` records the full window, ignores the
  shutter's reply, and confirms by clip, sending stop until state recovers to
  a clean idle. Never assume the family that answers `state` answers the rest;
  `_call()` resolves each endpoint on first use.
- Preview stream: on a real test the camera emitted 500 UDP datagrams to the
  phone, so the camera side works. The first bytes were not 0x47, so the
  payload may not be the plain MPEG-TS that `LiveView.kt`'s `TsExtractor`
  expects. The camera test now reports the 0x47 fraction; live view being
  black is an Android decode problem, not a camera one.
- Legacy preview stream: `GET /gp/gpControl/execute?p1=gpStream&c1=restart`
  on port 80, MPEG-TS over UDP to port 8554 of the requester. It stops
  without periodic HTTP traffic on the control port.
- Preview stream: `GET /gopro/camera/stream/start`, camera pushes MPEG-TS
  over UDP to port 8554 of the requester. It stops the moment recording
  starts; the worker stops it before every capture.
- Preview is fixed low-res, so clips must be transferred for analysis:
  10 to 20 s per shot over WiFi. That latency is a known limit.
- Camera WiFi is not broadcast until turned on from the camera's menu.
- Rolling-shutter time model: `t = frame/fps + (y/H) * readout`.

### Conventions

- Small commits, one change each, message says what and why.
- No em dashes in any text the user sees, including docs and UI copy.
- Do not add dependencies to the engine beyond numpy and opencv-python.
- When something fails, report the exact error and the one thing to try
  next. Do not offer five options.
- Update this file when the state or plan changes. It is the scope.
