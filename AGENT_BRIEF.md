# FlightPath — Systems Brief for a New Agent

You are picking up a solo, in-progress project. This brief gets you to full
context fast. The terse authority is `CLAUDE.md` (repo root) — read it too;
this brief is the wider onboarding around it. Everything below is current as of
app **0.1.12 / versionCode 13** (2026-09-17).

---

## 1. What it is

A camera-based golf launch monitor for the driving range. A **GoPro HERO9
Black** on a tripod films the ball side-on at 1080p240. An **Android app** on a
Samsung S22 Ultra joins the camera's WiFi, pulls each clip, tracks the ball
with **OpenCV**, and reports ball speed, launch angle and modelled carry.

Owner: AJ (github.com/Cloggedbat). Public repo. Solo, ADHD — work in small
steps, one change at a time, say what changed and what to do next.

**Goals, in order:** (1) easy setup — one APK, a 3-step wizard, no PC at the
range; (2) safe for the user — the app must never open a door into the phone;
(3) open source later. Lineage is OpenFlight (a radar monitor): local engine +
browser UI on localhost:8080. **Do not read the OpenFlight repo — it is AGPL.**

---

## 2. Architecture (three components)

```
HERO9  --WiFi HTTP-->  Android app (Chaquopy Python + WebView)  -->  browser UI
 records               joins camera WiFi for THIS APP ONLY,            numbers
                       runs the CV engine, serves UI on 127.0.0.1:8080
```

- **`engine/`** — the product. Python package `flightpath/`: `detect.py` (CV:
  background subtraction, RANSAC track fit, rolling-shutter correction),
  `physics.py` (drag+Magnus carry), `calibrate.py`, `drop.py` (gravity-based
  scale + readout), `cameras.py` (profiles), `lens.py`, `gopro.py` (HTTP
  client), `worker.py` (the background pipeline — poll, download, analyse),
  `server.py` (stdlib HTTP, loopback), `session.py`, `selftest.py`. Plus
  `analyze.py` (CLI), `run.py` (desktop server), `make_test_clip.py` (synthetic
  clip generator, the only regression harness), `ui/index.html` (the whole
  front end, vanilla JS, polls `/api/state` every 1s).
- **`android/`** — Kotlin shell. `MainActivity.kt` (boots Chaquopy Python,
  loads the UI in a WebView, exposes `window.FlightPathNative` bridge),
  `CameraWifi.kt` (the reason a native app exists — `WifiNetworkSpecifier` +
  `bindProcessToNetwork` joins the camera AP for this process only, phone stays
  on cellular), `WifiScanner.kt`, `LiveView.kt` (Media3 UDP preview),
  `GuideView.kt` (aiming overlay), `Updater.kt` (in-app OTA updater).
  **`android/app/src/main/python/flightpath/` is a COPY of `engine/flightpath/`.**
- **`updates/`** — stdlib update server, now deployed on Railway. Serves
  `version.json` + the APK. The in-app updater pulls from it.

---

## 3. Repo layout + the ONE rule that bites

**Never edit `android/app/src/main/python/flightpath/` directly.** Edit
`engine/` and run `./sync-engine.sh` (Git Bash on Windows). It also copies
`engine/ui/index.html` into `engine/flightpath/ui/`. After any engine edit:
`bash sync-engine.sh && diff -rq -x __pycache__ engine/flightpath android/app/src/main/python/flightpath`
must be clean. Commit both trees together.

`engine/ui/index.html` is canonical; the other two copies are generated.

---

## 4. Current state — proven vs not

**Proven (on real hardware unless noted):**
- CV pipeline on **synthetic** clips: ~0.1 mph error at 240/480 fps, all clubs.
- Tap-to-calibrate scale, drop-test readout solve, rolling-shutter correction.
- APK installs and runs on the S22; Python engine boots inside it.
- Camera WiFi join with cellular still on (calls work). Connection holds.
- Media list works with an SD card in. Shutter records on the real camera.
- Preview stream is valid MPEG-TS reaching the phone (500/500 0x47 sync bytes).

**NOT proven — in priority order:**
1. **Can the phone's OpenCV decode the camera's video?** THE crux, still open.
   Capture now records + downloads a real clip, but decoding it failed:
   "could not decode the clip". Strong suspicion: HERO9 records **HEVC** at
   1080p240 and the APK's OpenCV **4.5.1.48 (no FFmpeg)** cannot do HEVC. 0.1.12
   makes the error report the codec fourcc, size and open-state so we KNOW.
   Fix branch A: set camera to H.264 (Preferences > General > Video
   Compression). Fix branch B: move CV to a native MediaCodec decoder.
   Waiting on two phone reads: the wizard **Engine row** and the **codec line**.
2. **Live view is black** despite a clean MPEG-TS stream. Camera side works;
   this is an ExoPlayer/`TsExtractor` quirk with GoPro's TS (it tends to omit
   the PAT/PMT program tables ExoPlayer wants). Android-only fix. See
   `LiveView.kt:55` (UdpDataSource + TsExtractor, MODE_SINGLE_PMT).
3. Any real golf ball. Every number so far is from a synthetic clip.
4. Rolling-shutter readout time of this HERO9 (the drop test measures it).

---

## 5. Camera facts — hard-won, do not relearn

This HERO9 is on **firmware 2.0** and behaves unlike the docs assume. All of
this cost build-cycles to discover; it is also in `CLAUDE.md` Camera facts and
the memory file `hero9-shutter-needs-legacy-path`.

- **Two HTTP servers on two ports.** Open GoPro (`/gopro/...`), the media list
  (`/gp/gpMediaList`) and downloads (`/videos/DCIM/...`) are on **8080**.
  Legacy control (`/gp/gpControl/...`: shutter, settings, gpStream, status) is
  on **port 80**. A gpControl command sent to 8080 **hangs to timeout**, it
  does not 404. `GoProClient._url()` routes `/gp/gpControl` to 80.
- **HERO9 has NO Open GoPro shutter.** GoPro's own `openapi.json` lists
  `/gopro/camera/shutter/{mode}` for HERO10+ only. The shutter is legacy
  `http://10.5.5.9/gp/gpControl/command/shutter?p=1` (start) / `?p=0` (stop).
- **The shutter command errors/times out/500s but still records.** And the
  camera's **whole HTTP stack goes unresponsive while recording** (state
  returns 500 → timeout → RemoteDisconnected), recovering the instant it stops.
  **So confirm a recording by a new clip in the media list, never by state.**
  `worker.trigger()` fires start (ignoring the reply), records the full window,
  then retries stop until state recovers to a clean idle (proof it stopped +
  runaway guard). `probe()` pins neither shutter nor stream; `_call()` resolves
  each on first use. **Never pin an endpoint family** — `state` answering does
  not mean the rest do.
- **The camera must be on its shooting screen**, not the WiFi/Connect Device
  menu. In a menu it records nothing and previews nothing, and HERO9 has no
  `set_ui_controller` over HTTP to pull it out. The wizard tells the user to
  press Mode.
- **Preview:** MPEG-TS over UDP to port 8554 of the requester. Legacy start is
  `/gp/gpControl/execute?p1=gpStream&c1=restart` (port 80); it needs periodic
  HTTP traffic on the control port to stay alive (`worker` GETs it each tick
  while previewing). No SD card → status works but both media-list paths 404.

---

## 6. How to work

### Test on the PC (fast), but know its limit
```
cd engine
.venv/Scripts/python.exe analyze.py selftest        # decode + tracker sanity
.venv/Scripts/python.exe make_test_clip.py --speed-mph 120 --angle 19 --fps 240 --out t.mp4
.venv/Scripts/python.exe analyze.py shot t.mp4 --camera hero9-1080p240 --readout-ms 0 --ref 120,980,611,980 --ref-inches 46
```
The venv is Python 3.10 but **OpenCV 5.0.0** (requirements.txt floats `>=4.8`),
while the APK is pinned to **4.5.1.48**. So a green PC selftest proves the
tracker/physics, **not** phone decoding. Do not use it to answer NOT-proven #1.

**The scenario harness is NOT in the repo** — it lives at
`<scratchpad>/test_worker_scenarios.py` (session-local). It has ~60 checks
(t1–t28) covering the flap fix, calibration gate, shutter/clip logic, the port
split, and the camera diagnostic. If you continue this work, consider moving it
into the repo as a real test file; right now it does not travel.

### Build the APK (Windows)
```
JAVA_HOME must be set — the JDK is installed but not on PATH:
  C:\Program Files\Eclipse Adoptium\jdk-17.0.20.101-hotspot
Without it, gradlew exits 49 ("JAVA_HOME is not set").

./sync-engine.sh
cd android && ./gradlew.bat assembleDebug
```
Chaquopy 17 pins Python 3.10, numpy 1.26.2, opencv-python 4.5.1.48 (only
versions with Android wheels — do not "upgrade"). **Bump `versionCode` (+1) and
`versionName` in `android/app/build.gradle.kts` before every installed build**
— Android refuses same-code updates and the OTA updater compares versionCode.

### The gate (run before every commit)
1. `py_compile` the changed modules.
2. Run the scratchpad harness — all pass.
3. `sync-engine.sh` + `diff -rq` clean.
4. `analyze.py selftest` — `ok` and `hevc_ok` true.
5. Em-dash check: none in user-visible text (2 known ones exist in `gopro.py`
   comments — not user-visible, leave them).

### Get a build onto the phone
- **adb never worked** on this S22 (USB debugging would not enable). Do not rely
  on it.
- **MTP File Transfer:** copy the APK to `Internal storage/Download` via the
  Shell.Application COM API in PowerShell. The phone reverts to **charge-only**
  USB mode on every reconnect — the user must pick "File transfer" each time
  (or set Developer options > Default USB configuration > File transfer). Files
  written this way may not show under the My Files "Downloads" shortcut until
  re-index; look in the actual `Internal storage/Download` folder.
- **OTA (now the primary path):** the Railway update server is live at
  `https://flightpath-updates-production.up.railway.app`. Release loop:
  ```
  cp android/app/build/outputs/apk/debug/app-debug.apk updates/public/FlightPath.apk
  # bump updates/public/version.json (versionCode + versionName + notes)
  cd updates && railway up -y -c
  ```
  `railway` CLI is logged in as AJ; the project is linked. `updates/.railwayignore`
  re-includes the gitignored APK (`*.apk`) so it actually deploys — without it
  the server serves a 404 for the APK. The phone's in-app updater (or Chrome on
  that URL) pulls the new build over cellular.

### Signing
The APK is signed with **this PC's debug key**. The permanent key
(`android/keystore/flightpath.jks`) is **not on this machine** and was not found
in Downloads/Desktop/Documents/OneDrive — it is in `flightpath-signing-key.zip`
somewhere. A key change forces one uninstall for the user. Never generate a new
key, never commit it, never print its password.

---

## 7. Conventions & hard constraints

- Small commits, one change each; message says what and why.
- **No em dashes** in any user-visible text (docs, UI copy, error strings).
- No engine dependencies beyond numpy and opencv-python.
- `sync-engine.sh` before every build; update `CLAUDE.md` when state/plan change.
- Security (goal 2, non-negotiable): server binds 127.0.0.1 only; POST requires
  `application/json`, body ≤ 64 KB; Host must be an IP literal (anti DNS
  rebind); the camera is untrusted input (`SAFE_NAME`, realpath-contained
  downloads, byte-budgeted decode); all camera-supplied UI text HTML-escaped;
  the only outbound call is the user-triggered update check.
- Commit attribution: end messages with the `Co-Authored-By:` line your own
  session reminder gives you (it has changed across this project's history).

---

## 8. Active plan & frozen list

Five steps, no new features until done: (1) read the Engine row; (2) connect +
auto-configure the camera (1080p240, Linear, HyperSmooth off); (3) `dropcal`
for px/m + readout; (4) hit ONE 7-iron (~110–125 mph, 17–21°); (5) hit twenty.

**Frozen until step 5:** second camera / Insta360 X5 / dispersion, spoken
metrics, overhead view, shot tags, CSV export, F-Droid, licence, spin, club
speed, smash factor. (Railway OTA was on this list; the owner lifted it to
enable remote testing. Treat it as a dev tool, not a public release.)

---

## 9. The immediate next moves

1. **Get the codec answer.** From the phone on 0.1.12: the wizard **Engine row**
   (bundled H.264 + HEVC decode test — needs no camera) and the **codec line**
   from a failed Capture. `avc1` = H.264, `hvc1`/`hev1` = HEVC.
2. If HEVC: set the camera to H.264 and re-capture. If it decodes → calibrate →
   step 4. If H.264 still fails on-device → the CV must move to a native
   MediaCodec decoder (or a laptop-side decode). That is an architecture fork.
3. **Live view:** the stream is confirmed clean MPEG-TS, so rework the Android
   receive/decode (`LiveView.kt`) — bind the UDP socket to the camera `Network`
   explicitly, surface `onPlayerError`, and expect to fight GoPro's missing
   PAT/PMT. Testable via OTA now.

---

## 10. Where knowledge lives

- **`CLAUDE.md`** (repo root) — the terse scope + full changelog + Camera facts.
  Always current; update it when state changes.
- **`engine/README.md`** — CV method, camera-choice rationale, honest limits.
- **`engine/RANGE.md`** — the at-the-range runbook + security notes + API table.
- **User memory** (`.claude/projects/.../memory/`) — `hero9-shutter-needs-legacy-path`
  (the definitive camera behaviour), `engine-tests-cannot-run-on-this-pc`
  (the venv/OpenCV-version gap), `known-bugs-found-by-review` (calibration-clip
  race etc.), `goal-review-open-findings` (sound trigger records after impact,
  screen-off kills the worker, clips never deleted, failed download not
  retried — all deferred, none fixed).

**Deferred, real, unfixed** (from an adversarial review, in memory): the GoPro
Labs sound trigger records the follow-through not the strike; no
FLAG_KEEP_SCREEN_ON so the worker dies when the screen sleeps; clips accumulate
forever; a failed download is added to `seen` and never retried. Address these
before the twenty-ball step, not now.
