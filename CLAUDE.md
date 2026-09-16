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

## Current state (2026-09-16)

App version 0.1.4, versionCode 5.

The APK built on this PC is signed with the Android debug key, not the
permanent one. `android/keystore/flightpath.jks` and `signing.properties` are
not on this machine and were not found in Downloads, Desktop, Documents or
OneDrive. Restore them from `flightpath-signing-key.zip` before building
anything that goes to another person. Switching back to the permanent key
means uninstalling whatever the debug key installed.

This build reused versionCode 5 instead of bumping it. It installs only
because the previous copy was uninstalled first. The next build that will be
installed must be versionCode 6.

Proven:
- CV pipeline on synthetic clips: 0.1 mph error at 240 and 480 fps, all clubs.
- Tap-to-calibrate scale (0.1%), drop-test readout solve, rolling-shutter correction.
- Security hardening verified against a hostile fake camera.
- Real HERO9 answers the Open GoPro HTTP API at 10.5.5.9:8080.

NOT proven (in order of importance):
1. Whether OpenCV inside the APK can decode the camera's H.264/HEVC video.
   The Camera step of the wizard shows an "Engine" row that answers this.
2. Native WiFi join to the camera (`WifiNetworkSpecifier`). Last attempt
   said "cannot find the device". A WiFi scanner and a "use current WiFi"
   fallback were added; untested since.
3. Live view (GoPro UDP MPEG-TS preview via Media3). Works against a fake
   camera; never seen a real stream.
4. Any real golf ball. Every number ever produced is from a synthetic clip.
5. Rolling-shutter readout time of the HERO9 (drop test measures it).
6. That the 0.1.4 debug-signed APK installs and runs on the S22 Ultra, and
   that the Python engine boots inside it. This build has not been installed
   or launched on the phone.

## The active plan

Five steps, about 2 hours total, no new features until they are done:

1. Install the APK, open the wizard, read the Engine row (15 min, indoors).
   `ok (H.264 + HEVC)` proceed. `H.264 only` proceed and set the HERO9 to
   H.264 in Preferences, General, Video Compression. `cannot decode video`
   means the CV has to move (native MediaCodec decoder, or a server).
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
