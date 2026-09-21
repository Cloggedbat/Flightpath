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

App version 0.1.28, versionCode 29.

0.1.28 fixes the real cause of "camera not answering", found by the
0.1.27 diagnostic on the first run (09:01, 2026-09-21):

  process bound: false; interface: wlan0;
  phone address: fe80::..., 10.5.5.100; camera address: 10.5.5.9;
  on the camera's 10.5.5.x network, so the link is good

The phone held 10.5.5.100, the camera was at 10.5.5.9, the WiFi link was
perfect, and bindProcessToNetwork had returned FALSE. Android routes every
unbound socket to the default network, so the engine's requests went out
over cellular and the camera was never contacted. It looked exactly like
an absent camera, and cost days: the flapping, the menu theory and the
"press Mode" advice were all downstream of this. The bind is now retried
up to 20 times over 5 s and again on onCapabilitiesChanged, rebind() runs
before every reconnect, and the wizard names an unbound process instead
of blaming the camera. bindCurrentWifi uses the same retry.

Lesson worth keeping: bindProcessToNetwork returns a Boolean and a single
silent false is fatal. Never ignore it, and surface it in the UI.

0.1.27 stops assuming the camera lives at 10.5.5.9. With the camera on
its normal shooting screen the engine still could not reach it after a
good WiFi join, so the menu theory is dead. An access point is the DHCP
server and gateway for its own network, so CameraWifi.cameraHost() reads
the camera's real address off the link (dhcpServerAddress on API 30+,
else the default route's gateway) and MainActivity hands it to
android_main.set_camera_host() before every reconnect, which repoints
GoProClient and clears its resolved endpoint paths. 10.5.5.9 is a
convention, not a promise, and a camera answering elsewhere looked
exactly like a camera that was not there. The link summary now prints
the camera address it found, and "no camera at ..." names the address
actually tried instead of a hardcoded one.

0.1.26: the Bluetooth wake WORKS on the real camera, first try (08:44,
2026-09-21). "Camera WiFi is on (HERO9 Black). Joining it", then
"Connected to HERO9 Black." So CameraBle.kt is proven and Quik is out of
the loop for good. Two things learned: this camera's WiFi SSID is
"HERO9 Black", not GP followed by digits, so the GP prefix match and
every "GP..." message were wrong for it; and after a successful join the
engine still could not reach 10.5.5.9 ("Still no camera"). The old
message blamed the GP network and 5G, which says nothing. CameraWifi now
exposes linkSummary(): whether bindProcessToNetwork succeeded, the
interface, and the phone's own addresses on it. A 10.5.5.x address means
the link is good and the camera is not serving, which on this camera
means it is sitting in a menu, so the wizard says to press Mode; no such
address means the join is at fault and says to wake it again. The
failure line now carries that summary and the engine's own camera_note.
Not yet known which it is.

0.1.25 asks the camera why a recording aborted instead of guessing. The
19:11 log (2026-09-18) killed the SD card theory on its own: "latest clip
on the card before the shutter: GX010553.MP4 (6.8 MB)", so the camera
does write real clips, and the stub is 27,639 bytes EXACTLY every time
(GX010551, GX010554), which a failing card would never produce. The
camera keeps its own counters, so now the app reads them. Status ids,
from GoPro's SDK (open_gopro/api/ble_statuses.py, whose doc links carry
the id in the anchor): 1 battery present, 2 battery bars, 6 overheating,
8 BUSY, 10 ENCODING, 13 encoding duration, 35 remaining video seconds,
54 card free KB, 70 battery percent, 111 SD CARD WRITE SPEED ERROR, 112
SD card errors, 117 card capacity. GoProClient.health() reads them and
health_line() prints one line; the camera test prints it before the
shutter and again the instant the 3 s window ends, and _stub_reason()
names the actual cause (card too slow, card errors, overheating, flat
battery) or, when the camera reports nothing wrong, says so and points
at the app's own shutter. That last branch is the likely one here.

Real bug found on the way: is_recording() read status 8, which is BUSY,
not ENCODING (10). It now reads 10, and the stop loop waits on a new
is_idle() (neither encoding nor busy), which is what it always meant.
The fake serves all these statuses; the harness has a camera health
scenario (51 checks total).

0.1.24 removes the GoPro app from the loop. CameraBle.kt sends the
Bluetooth request that makes a HERO9 switch its WiFi on, so Quik is not
needed at all. This is GoPro's own published protocol (Open GoPro), not a
workaround: the camera does not care which app asks. From GoPro's
tutorial source, verified 2026-09-18: service FEA6; command request
b5f90072-aa8d-11e3-9046-0002a5d5c51b, response b5f90073; WiFi SSID
b5f90002 and password b5f90003; write 03:17:01:01 (length 3, command
0x17 AP control, one payload byte, 1 = on) and a response whose third
byte is 0x00 means the WiFi is coming up. Android allows one GATT
operation at a time, so the class is a state machine: scan for FEA6 or a
GoPro name, connect, discover, read SSID, read password, subscribe to
responses, write the command, wait 2.5 s for the access point, then join
it with the existing CameraWifi path. The wizard is now one green button,
"Turn on camera WiFi and connect"; the scan and manual SSID and password
fields are behind "Do it by hand instead". The password goes from the
camera to the WiFi layer inside the process and never reaches the page.
New permissions: BLUETOOTH_SCAN (with neverForLocation, so no location is
derivable) and BLUETOOTH_CONNECT, requested at the button, plus the
pre-31 BLUETOOTH and BLUETOOTH_ADMIN capped at maxSdkVersion 30. The
camera must be bonded to the phone once, which is what Connect Device,
GoPro App is for; after that it reconnects silently. Compiled, not yet
run on the phone: the whole path needs the camera to test.

0.1.23 corrects 0.1.21. With the phone's WiFi on and the camera on its
pairing screen, no GP network was visible anywhere on the phone. A HERO9
only switches its WiFi on when an app asks over Bluetooth; Wireless
Connections: On and the pairing screen both only advertise Bluetooth.
Every instruction now says: open Quik until it shows the camera
connected (that is the Bluetooth request), force-stop Quik, Mode back to
the shooting screen, then connect FlightPath. The next feature, a
prerequisite for goal 1 rather than something on the frozen list, is
FlightPath sending that request itself over BLE (Open GoPro: service
FEA6, command characteristic b5f90072, AP control 0x17; SSID and
password readable from the WiFi AP service), with the Bluetooth
permissions that needs and a one-time bond made on the pairing screen.
It needs the phone and camera to test and is not built yet.

0.1.22: the phone could not see the camera's WiFi even on the pairing
screen. The app never checked whether the phone's own WiFi radio was on;
with it off every scan is empty and the wizard blamed the camera. The
scan now reports wifi_off and the wizard says to turn the phone's WiFi
on (data stays on 5G). The join-failure hint no longer points at the
pairing screen and says to type the exact name from Camera Info if Quik
renamed the camera to something not starting with GP, which the "any
GoPro" match (GP prefix) would never find.

0.1.21: copy only. Every instruction in the app, the probe's hint and
RANGE.md told the user to turn the camera's WiFi on through Preferences,
Connections, Connect Device, GoPro App. That is the pairing screen: it
parks the camera on a "waiting for app" menu, and a camera in a menu
records nothing, or (plausibly) the stubs of 0.1.20. The right switch is
Preferences, Connections, Wireless Connections: On, once, which stays on
and needs no GoPro app; the name and password are under Camera Info; then
Mode back to the shooting screen. Also spelled out: Quik and FlightPath
cannot share the camera (FlightPath holds the WiFi link, Quik pokes the
camera over Bluetooth), so Quik is force-stopped before FlightPath.

0.1.20: the first 0.1.19 camera test (06:41, 2026-09-18) changes the
diagnosis. For 25 s after the shutter, the media list AND the download
server both said GX010551.MP4 was 27,639 bytes. That is not a lagging
list, it is a closed file. A 3 s clip at 1080p240 is about 25 MB. The
camera starts recording on the app's shutter and stops almost at once,
writing a stub, and every "0 MB" and "0.0 MB" seen the day before was
almost certainly the same thing. The 20 s of refused connections and the
camera's screen going dark and coming back are the camera recovering from
that aborted recording. Known causes on a HERO9: an SD card too slow for
1080p240 HEVC, a nearly flat battery, or overheating; each puts a warning
on the camera's screen. Unknown which, or whether the camera's own
shutter button records normally. The app now says so: a new clip that
closes under MIN_CLIP_BYTES is reported as a stub with those three causes
(capture, camera test verdict, and the poll loop), never as "press
Mode", and the camera test prints the size of the newest clip on the card
before it fires, so a clip recorded with the camera's button can be
compared in the app. The fake has stub_clips; the harness covers it (11
more checks). CLIP_SETTLE_S is a class attribute so tests can shorten it.

0.1.19 is the step from reactive to proven. engine/tests/fake_hero9.py is
a HERO9 that misbehaves exactly like this one: 404 on the Open GoPro
shutter, a legacy shutter that times out and records anyway, connections
dropped while recording, 500s while finalising, a media list that says 0
and then a few KB for seconds after the file is whole, 409 on a second
stream start, Linear refused at 240 fps, UDP TS with or without a 12 byte
header. engine/tests/test_capture_flow.py runs the real Worker against
it, poll loop and all: connect and baseline, Apply camera settings, a
calibration capture through to a 1920x1080 reference frame, a shot
through to 123.5 mph on the session (the profile's 4.2 ms readout applied
to a synthetic clip that has none; 119.9 without), and the wizard's camera
test through to its verdict, with a watcher asserting the connection
never drops. 30 checks, about 90 s. It found two bugs before the phone
did: the poll loop's shot path took the media list's size at face value
and would have downloaded a stub, and the camera test's log went nowhere
when run without begin_diagnostic(). From the 22:17 phone log (0.1.18):
"new clip: GX010550.MP4 (0.0 MB, size settled after 2 s)", so the list
reports a fresh clip at a few KB, stable, twenty seconds after idle. Both
paths now ask the download server for the file's size (a one byte ranged
GET, Content-Range; GoProClient.clip_size), the list is the fallback, and
anything under MIN_CLIP_BYTES (256 KB) is not a clip yet. The camera test
traces both sizes each second. Same log: a heartbeat sent just before
Test camera came back 500 nine seconds later and was counted as a strike;
a failed poll now re-checks busy before counting. Also: the screen stays
on while the app is open (FLAG_KEEP_SCREEN_ON), since the worker dies
with the screen. GoProClient takes control_port so the fake can run on
any port; a real camera is still 80.

0.1.18: the camera test ends with a verdict line ("verdict: shutter ok,
26.3 MB clip; stream ok, 500 datagrams, MPEG-TS found") and labels the
shutter's 404, timeout and refused lines as this camera's normal
behaviour. AJ read two passing tests as failures because the log prints
the camera's raw errors, and on this HERO9 a working shutter produces
several. Nothing else changed. On the question of the GoPro Quik app
interfering: unlikely. The probe is clean before every shutter, the
errors sit exactly inside the record-and-close window, and "Connection
refused" means the camera's server is down, not busy with another
client. Cheap to rule out anyway: force-stop Quik and disable its
auto-connect.

0.1.17: the 0.1.16 camera test log (21:43, 2026-09-17) confirms the stop
loop ("idle confirmed after 20 s") and shows the next link in the chain:
"new clip: GX010549.MP4 (0 MB)". One second after the camera reports idle
the media list names the clip but gives its size as 0; the camera is still
writing the file. The calibration capture took the first sighting and
would have downloaded a stub. _wait_for_new_clip() now waits until the
clip's size is non-zero and identical on two consecutive reads a second
apart, up to 25 s, ignoring media list errors while the camera recovers;
the capture and the camera test both use it, and the test reports the
settled size and how long it took. Harness: 9 checks.

0.1.16: the 0.1.15 camera test log (19:22, 2026-09-17) shows the flap
gone (no "camera poll failed" lines) and the test at 29 s, and it shows the
next problem before it could bite the calibration capture. After
"recorded 3.0 s" the camera refused every connection (Errno 111) for 19 s
before reporting idle. trigger() confirmed the stop with a six-try loop
that fast refusals exhaust in about 4 s, and capture_reference_frame()
treated an unconfirmed stop as a failed shutter and never looked for the
clip. Now _stop_and_confirm() keeps sending stop until state reports idle
or 30 s pass (STOP_CONFIRM_S), shared by trigger() and the camera test;
trigger() returns "unconfirmed" rather than a message; and the
calibration capture looks for its clip regardless, failing only if none
appears, with a "press the shutter button" hint when the stop was never
confirmed. Harness: 12 checks. The test's "new clip" line now shows the
clip size, which is the only way to know how long the camera really
recorded (1080p240 HEVC is roughly 8 to 10 MB per second). Also in that
log: the preview stream was raw TS at byte 0 with 3 to 7 packets per
datagram, unlike the 18:31 run's 12 byte header. Almost certainly the two
start paths (Open GoPro stream/start vs legacy gpStream) give two formats;
TsUdpDataSource.kt finds the offset per datagram so both play.

0.1.15 fixes the flapping, from one camera test log (18:31, 2026-09-17).
The tick's heartbeat kept polling state() every 3 s while a capture or a
camera test had the camera recording, and the camera's HTTP stack is
unresponsive then by design, so every capture racked up three strikes,
dropped the connection, and reconnected. The tick now stands down entirely
while a trigger, a camera test or a calibration capture is in flight: no
heartbeat, no strikes, no media list. Proven by a harness with a fake
client (20 checks). Same log, second bug: the camera test timed its 3 s
record window by six state() reads, each of which blocked for its 8 s
timeout, so the test recorded for 32 s (GX010543). It now sleeps a
wall-clock 3 s and reuses trigger()'s stop loop, and its clip is marked
seen so the poll loop does not analyse it as a shot. Same log, third:
the preview datagrams are 1328 bytes, which is a 12 byte header plus seven
188 byte TS packets, so TsExtractor never synced. TsUdpDataSource.kt
replaces UdpDataSource and serves only the TS bytes of each datagram; the
camera test now reports the offset it finds. Also: Apply camera settings
and Test camera are disabled while the camera is busy, and Apply refuses
with a reason instead of "could not read back" four times over.

0.1.14: on 2026-09-17 at 17:19 the S22 Ultra's Engine row read "ok (H.264 +
HEVC, MediaCodec)". ClipDecoder.kt decodes both codecs on the phone and
the tracker finds the ball through it. Old NOT-proven item 1 is closed; the
camera stays on HEVC. Two fixes from that first session on the phone.
One: backing out of the app and reopening it gave a blank screen and
"Engine failed to start: OSError: [Errno 98] Address already in use".
android_main.stop() called httpd.shutdown(), which stops the serve loop
but leaves the socket listening, and the Python process outlives the
Activity, so the next start() could not bind 8080. It now calls
server_close() as well. Two: the Position step's Live view failed with
"HTTP Error 409: Conflict". That is the Open GoPro stream/start endpoint
existing on this HERO9 (unlike the shutter, which 404s) and refusing
because the camera thought it was busy, most likely a stream still marked
on from the camera test, whose stop was sent while the camera was
unresponsive after the shutter. start_preview() now answers a 409 with a
stop and one retry. Also seen: right after the camera test, the Camera step
read all four settings as raw 0 (Resolution "code 0", 240 fps, Lens Wide,
HyperSmooth Off). AJ confirms Apply camera settings works and always has,
so that was the camera's state being stale while it recovered from the
shutter. A settings read straight after Test camera is not to be trusted;
tap Apply and it reads back correctly.

0.1.13 answers old NOT-proven item 1 from the binary, not the phone. The exact
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
- CameraBle.kt wakes the camera's WiFi over Bluetooth on the real HERO9
  and the phone then joins it, 2026-09-21. The GoPro Quik app is not
  needed for anything.
- ClipDecoder.kt decodes H.264 and HEVC on the S22 Ultra and the tracker
  runs on its frames: Engine row "ok (H.264 + HEVC, MediaCodec)", 2026-09-17.
- Apply camera settings puts the HERO9 in 1080p240 Linear, HyperSmooth
  off, and reads it back. AJ: it has always worked.
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
1. That the camera records a real clip on the app's shutter at all. On
   2026-09-18 it closed GX010551.MP4 at 27,639 bytes for a 3 s window
   (see 0.1.20). Whether the camera's own button records normally is the
   next fact to get: record 3 s by hand, then Test camera reads its size.
   The chain after the clip is proven against tests/fake_hero9.py.
2. Live view. The stream format is now understood (Camera facts) and
   TsUdpDataSource.kt strips the header; the phone has not yet shown a
   frame of it.
3. Any real golf ball. Every number ever produced is from a synthetic clip.
4. Rolling-shutter readout time of the HERO9 (drop test measures it).

## The active plan

Five steps, about 2 hours total, no new features until they are done:

1. DONE 2026-09-17: Engine row reads ok (H.264 + HEVC, MediaCodec).
2. DONE: Apply camera settings works and always has. If the panel shows
   "code 0" after Test camera, that is a stale read; tap Apply again.
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

```
.venv/Scripts/python.exe tests/test_capture_flow.py   # the whole capture chain against a fake HERO9
```

Run that after any change to `worker.py`, `gopro.py` or `server.py`. It is
the real Worker against `tests/fake_hero9.py`, which reproduces every
measured quirk of this camera, and it must pass before any APK is built.
Add every new camera behaviour learned on the phone to the fake first.

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
  and change (camera join), FINE_LOCATION (only to list WiFi networks),
  BLUETOOTH_SCAN with neverForLocation and BLUETOOTH_CONNECT (only to wake
  the camera's WiFi, which a HERO9 does not do on its own).
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
- Preview stream format, measured 2026-09-17: 500 datagrams of exactly
  1328 bytes, first bytes 84 10 00 01 00 00 00 00. 1328 is a 12 byte
  header plus seven 188 byte MPEG-TS packets, so the stream is TS wrapped
  in a small per-datagram header. Media3's UdpDataSource passed the header
  through and TsExtractor never synced, which is why live view was black.
  TsUdpDataSource.kt finds the first 0x47 that repeats 188 bytes later
  and serves from there; the camera test reports the offset it found.
- On the app's shutter the camera has been closing recordings almost at
  once: 27,639 bytes for a 3 s window, the SAME size every time
  (GX010551, GX010554), agreed by the media list and the download server
  for 25 s (2026-09-18). A file under MIN_CLIP_BYTES is a stub, not a
  lag. A failing card gives varying sizes, and GX010553.MP4 on the same
  card is 6.8 MB, so the camera does record. Ask the camera rather than
  guess: statuses 111 (card write speed errors), 112 (card errors), 6
  (overheating) and 70 (battery) via GoProClient.health(). Still not
  known whether the camera's own shutter button records normally; the
  camera test's "latest clip on the card" line answers that.
- Camera status ids that matter: 6 overheating, 8 BUSY, 10 ENCODING, 13
  encoding duration in seconds, 54 card free KB, 70 battery percent, 111
  card write speed errors, 112 card errors. 8 is busy, NOT recording; it
  stays set for the whole twenty seconds the camera spends closing a
  file. Read 10 for encoding and wait on both for idle.
- The media list can also lag the file right after idle (0 bytes one
  second after idle). Never trust the list's size for a fresh clip. Ask the download server (a one byte ranged
  GET; GoProClient.clip_size reads Content-Range), require MIN_CLIP_BYTES,
  and require the same size on two reads a second apart
  (_wait_for_new_clip, and the poll loop's _pending_size).
- While the camera records, and for about 20 s after, every HTTP call
  fails (Errno 111 refused) or blocks to its timeout. The worker's poll loop stands down for
  the whole of a trigger, a camera test or a calibration capture. Before
  0.1.15 it counted three strikes and dropped the connection around every
  capture; that was the flapping. Never time a record window by polling
  state: each read blocks to its timeout, and six of them once turned a
  3 s test clip into 32 s.
- Legacy preview stream: `GET /gp/gpControl/execute?p1=gpStream&c1=restart`
  on port 80, MPEG-TS over UDP to port 8554 of the requester. It stops
  without periodic HTTP traffic on the control port.
- Preview stream: `GET /gopro/camera/stream/start`, camera pushes MPEG-TS
  over UDP to port 8554 of the requester. It stops the moment recording
  starts; the worker stops it before every capture. This endpoint does
  exist on this HERO9 (unlike the Open GoPro shutter): it answers 409
  Conflict when the camera is busy, which includes "already streaming"
  after a stop that never reached it. start_preview() stops and retries
  once on 409.
- Preview is fixed low-res, so clips must be transferred for analysis:
  10 to 20 s per shot over WiFi. That latency is a known limit.
- Joining the camera's WiFi is not enough: the process must be BOUND to
  that network or Android sends every socket to cellular and the camera
  looks absent. bindProcessToNetwork can return false while the link is
  still coming up, and it did on the S22 with a perfectly good link
  (10.5.5.100 to 10.5.5.9). Retry it, rebind before each reconnect, and
  never drop the Boolean it returns.
- This camera's WiFi SSID is "HERO9 Black", NOT "GP" plus digits. Never
  assume the GP prefix: CameraWifi falls back to a GP pattern only when
  no SSID is known, the scanner badges anything containing "hero" too,
  and user-facing text must not say "the GP network".
- Camera WiFi is NOT broadcast until an app asks for it over Bluetooth.
  GoPro's own words: "camera WiFi must be enabled upon each connection via
  BLE." Wireless Connections: On only enables Bluetooth advertising, and
  the Connect Device pairing screen only advertises Bluetooth too.
  Measured 2026-09-18: on the pairing screen, phone WiFi on, no GP network
  visible anywhere. Since 0.1.24 the app sends that request itself
  (CameraBle.kt, service FEA6, write 03:17:01:01 to b5f90072; SSID at
  b5f90002 and password at b5f90003). The camera needs to be bonded to the
  phone once, on Connect Device, GoPro App. Do not leave it on that screen
  afterwards: it is a menu, and a camera in a menu does not record. The
  WiFi name and password are also under Preferences, Connections, Camera
  Info for the manual path.
- Quik and FlightPath cannot share the camera. FlightPath holds the WiFi
  link, and Quik auto-connects over Bluetooth in the background and can
  command the camera mid-recording. Force-stop Quik before FlightPath.
- Rolling-shutter time model: `t = frame/fps + (y/H) * readout`.

### Conventions

- Small commits, one change each, message says what and why.
- No em dashes in any text the user sees, including docs and UI copy.
- Do not add dependencies to the engine beyond numpy and opencv-python.
- When something fails, report the exact error and the one thing to try
  next. Do not offer five options.
- Update this file when the state or plan changes. It is the scope.
