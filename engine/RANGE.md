# FlightPath at the range

The app: Python on the phone, GoPro as the camera, browser UI at
`http://localhost:8080`. Same architecture as OpenFlight, which also runs a
small backend on the device and serves its UI to a browser.

```
HERO9  --(WiFi HTTP)-->  S22 Ultra running Python  -->  browser UI
  records                pulls clips, runs the CV         numbers
```

No Bluetooth today. Turning the camera's WiFi on by hand and joining it from
the phone gets the full control surface over plain HTTP, so one extra button
press buys you the whole Open GoPro API. BLE could remove that button press by
starting and stopping recording directly, which is why `README.md` sketches it
as a later step. It is not part of the current plan.

---

## Test indoors first, tonight, before the range

Three things can fail and you want to know which. Testing them all at once at
the range means standing in a field unable to tell whether the problem is the
phone, the camera, the light, or the maths.

**1. Does the phone run it at all? 15 minutes, on the sofa.** Install the APK,
open the wizard, and read the "Engine" row on the Camera step. `ok (H.264 +
HEVC)` means go. `H.264 only` means go, and set the HERO9 to H.264 in
Preferences, General, Video Compression. `cannot decode video` means the CV has
to move off OpenCV, to a native MediaCodec decoder or to a server, and nothing
downstream matters until it does.

**2. Does the camera talk to it? 10 minutes, on the kitchen table.** Camera
WiFi on, phone joined, then `python run.py --probe-only`. You want a list of
`ok` lines.

**3. Does the maths work on a real ball? 15 minutes, indoors.** Drop a golf
ball past the camera and run `dropcal`. Gravity is a number you already know,
so this is a full accuracy test with no launch monitor, no range and no
daylight:

```bash
python analyze.py dropcal drop.mp4 --camera hero9-1080p240
```

Only then go to the range, knowing the stack works and that the only untested
variable left is real light and a real strike.

---

## The drop test, and why it is worth doing properly

A falling ball follows `y = y0 + v0·t + ½gt²`. Fit a parabola to it in pixels
and the quadratic term is `½ · g · (pixels per metre)`. Since g is 9.80665,
**the scale falls out of the video with no reference object, no tape measure
and nothing to type.** On a synthetic clip this recovered 419.8 px/m against a
true 420.0, an error of 0.05%.

It also gives you the one number this app cannot guess: your sensor's rolling
shutter readout time. The timing error from row-by-row readout scales with how
much of the frame height the ball crosses, and a vertical drop crosses the
most possible, which makes a drop the most sensitive possible measurement of
it. Give `dropcal` the scale from your tap calibration and it solves for the
readout:

```bash
python analyze.py dropcal drop.mp4 --camera hero9-1080p240 --known-px-per-m 419.5
```

Tested against known readout times of 0, 2.0, 4.2, 7.0 and 10.0 ms, it
recovered 0.00, 1.90, 4.00, 7.00 and 9.95. Paste the result into
`flightpath/cameras.py` and the ~3% rolling shutter uncertainty goes away for
good.

**How to shoot it.** Camera on the tripod in portrait if you can, so the ball
crosses as much frame height as possible. Drop a ball from as high as you can
reach, past the lens, onto something soft. Record the whole fall. Plain wall
behind, nothing else moving. Roughly a metre of visible fall is plenty; the
tool reports what fraction of the frame the ball crossed, and you want that
number as large as you can get it.

---

## One-time phone setup, about 5 minutes

1. Install the FlightPath APK. The in-app updater handles every version after
   the first one.
2. Open it once. The Python engine ships inside the APK, so there is nothing to
   install, no `pip` step, and no terminal on the phone.
3. On the Camera step of the wizard, read the "Engine" row. It decodes a
   bundled H.264 clip and a bundled HEVC clip and runs the real tracker against
   them. That is the one thing which has to work before anything else matters.

Android will ask for location permission. It is used only to list nearby WiFi
networks so you can pick the camera out of them, and for nothing else.

### On a PC instead

The same engine runs on a desktop, which is the fastest way to test a change
without rebuilding the APK:

```bash
cd engine
pip install -r requirements.txt
python run.py                         # UI at http://127.0.0.1:8080
python run.py --gopro-host 10.5.5.9   # PC joined to the camera's WiFi
```

---

## Every session, about 5 minutes

1. **Camera on, WiFi on.** On the HERO9: Preferences, Connections, Connect
   Device, GoPro App. Note the network name and password.
2. **Join it from the phone.** Android will warn that the network has no
   internet. Accept and stay connected, or it will silently drop back to
   cellular and nothing will work.
3. **Check the camera answers:**

   ```bash
   python run.py --probe-only
   ```

   You want a list of `ok` lines. If you get "no answer", the phone is not
   actually on the camera's network.
4. **Set up the shot.** Tripod 8 to 10 ft to your side, lens perpendicular to
   the target line, low, 6 to 8 ft of open space in front of the ball.
   Alignment stick on the ground in the ball's flight plane.
5. **Calibrate.** Record two seconds, pull the clip, export a frame, read the
   pixel coordinates of both ends of the stick:

   ```bash
   python analyze.py frame clips/GX010001.MP4 --index 0 --out ref.png
   ```

6. **Start the app:**

   ```bash
   python run.py --ref 310,880,1180,880 --ref-inches 46 --club 7i
   ```

7. Open `http://localhost:8080` in the phone's browser.

---

## Using it

**Queue mode** is the default. Keep hitting. Clips pile up on the camera, the
phone pulls and processes them in the background, and numbers appear when they
are ready. Nothing blocks you.

**Focus mode** takes one shot at a time and waits for the result before
accepting the next. Use it when you are working on one specific thing and want
the number while the swing is still fresh.

Tap a club chip before hitting so shots get tagged. The gapping table at the
bottom builds itself.

### Triggering

Two options:

- **Manual.** Tap "Record a shot". Records three seconds.
- **GoPro Labs Sound Pressure Level trigger.** Flash Labs and the camera starts
  recording on impact by itself. This is OpenFlight's SEN-14262 sound trigger
  in firmware, and it is the better setup once you trust it.

---

## The latency, honestly

Transfer is the slow step and there is no way around it. The HERO9's preview
stream is 480p and cannot be changed, so it is useless for measurement, which
means every shot's clip has to come off the camera as a file.

A three second 1080p240 clip is roughly 30 MB, and HERO9 WiFi runs a few MB/s
in practice. Call it 10 to 20 seconds per shot. Keep clips short. Queue mode
exists because of this number.

---

## Calibrate your rolling shutter once

This is the biggest accuracy lever you control after aim.

The app applies a rolling shutter correction using the camera profile's assumed
readout time. In end-to-end testing against clips with **no** rolling shutter,
the app read about 2.5% high, which is exactly the size of the correction. That
is the correct behaviour and it is also the point: the correction matters, so
the readout number should be yours, not an assumption.

To find it, hit one shot with a real launch monitor alongside, then:

```bash
python analyze.py shot clips/GX010001.MP4 --camera hero9-1080p240 \
    --ref 310,880,1180,880 --ref-inches 46 --readout-ms 0
python analyze.py shot clips/GX010001.MP4 --camera hero9-1080p240 \
    --ref 310,880,1180,880 --ref-inches 46 --readout-ms 4.2
```

Whichever bracket lands on the known number tells you your readout time. Then
edit `hero9-1080p240` in `flightpath/cameras.py` and set `readout_s`,
`readout_lo_s` and `readout_hi_s` to that value.

Until you do, frame so the ball crosses as little of the image height as
possible. The error shrinks toward zero as that shrinks.

---

## Troubleshooting

1. **"no answer from 10.5.5.9"** — the phone dropped the camera's WiFi for
   cellular. Rejoin and accept the no-internet prompt.
2. **Speeds read exactly half or double** — frame rate mismatch. Check the
   camera is actually in 1080p240 and that `--camera` matches.
3. **"no ball track found"** — shoot brighter, or lower `--threshold`. Check
   the ball is in frame for at least four frames after impact.
4. **Everything reads slow** — the camera is not square to the target line.
   Cosine error, same as radar.
5. **App runs but numbers look wrong** — check your reference stick was in the
   ball's flight plane, not nearer or further from the camera.

---

## Security

Short version: this is about as safe as a local tool gets, and the one real
risk is the camera itself, which the code now treats as untrusted.

**What it does not do.** No telemetry, no analytics, no accounts, no cloud. The
UI loads zero external resources: no CDN, no web fonts, no third-party scripts.
Two dependencies, OpenCV and NumPy, and the web server is pure standard
library. No `eval`, no `exec`, no `pickle`, no shell commands anywhere.

**The one call that does leave the phone.** The Android app checks for its own
updates. That is the only outbound request in the product, and it happens only
if an update server is set, either baked in at build time with `-PupdateUrl` or
saved in Settings. With none set, the app talks to nothing but the camera. When
one is set, the app checks about four seconds after launch and again whenever
you tap Check for updates. The request is a plain HTTP GET for `version.json`,
and it sends no identifiers and no usage data. Downloading a new APK hands the
URL to your browser. The engine by itself, on a PC or inside the APK, makes no
outbound calls at all.

**The server binds 127.0.0.1.** Nothing off the phone can reach it. `--lan`
opens it to the network and mints a random token you must send as
`X-FlightPath-Token`; it prints a warning when you do.

**A web page you browse cannot drive it.** POSTs require
`Content-Type: application/json`, which a cross-origin page cannot send without
a preflight, so a hostile site cannot silently wipe your session or fire your
shutter while the app is running. The Host header must be localhost or a
private IP literal, which blocks DNS rebinding.

**The camera is treated as hostile.** Anyone who can answer at `10.5.5.9`, an
evil twin of the camera's SSID at a busy range or another device on its
network, chooses both the filenames and the file contents this app writes to
your phone. So: filenames are matched against a strict pattern and must be a
plain name with a media extension, downloads are checked to land inside the
clip directory, transfers are capped at the declared size plus slack, partial
files are cleaned up on failure, and every camera-supplied string is escaped
before it reaches the page. Verified against a deliberately hostile stand-in
camera: four malicious filenames offered, four rejected, nothing written
outside the clip folder.

**Bodies and numbers are bounded.** Request bodies cap at 64 KB, trigger
duration is clamped, video decoding is capped by a memory budget rather than a
frame count so a 4K clip cannot OOM the phone.

**The honest residual risk.** OpenCV bundles FFmpeg, and that decoder is what
parses video files. If a hostile camera feeds you a malformed MP4, the most
severe realistic outcome in this whole stack is a memory-safety bug in
libavcodec, which is not something this app can defend against. On a PC, keep
OpenCV current. In the APK you cannot: Chaquopy 17 pins
`opencv-python==4.5.1.48`, the only build with an Android wheel, so do not
"fix" that pin unless you have checked that a newer wheel exists. In practice,
at a driving range, on the camera's own WPA2 network, with one device on it,
this is a remote risk.

**Two things that are your call, not the code's.** Installing the APK means
sideloading, which is a trust decision about this project and its signing key.
GoPro Labs is signed firmware from GoPro, so it is not a security risk, but it
is experimental and the risk is instability, reversible by reflashing stock.

---

## API

The UI is just a client. Everything is available directly. All POSTs require
`Content-Type: application/json`:

| method | path | purpose |
|---|---|---|
| GET | `/api/state` | full snapshot: camera, queue, shots, gapping |
| GET | `/api/session` | the session JSON |
| GET | `/api/probe` | re-run camera discovery |
| POST | `/api/mode` | `{"mode":"queue"}` or `"focus"` |
| POST | `/api/club` | `{"club":"7i"}` |
| POST | `/api/reference` | `{"x1":..,"y1":..,"x2":..,"y2":..,"inches":46}` |
| POST | `/api/trigger` | `{"seconds":3}` |
| POST | `/api/clear` | wipe the session |

The server binds loopback. Run with `--lan` if you want a laptop on the same
network to open it; that prints a token you must send as `X-FlightPath-Token`
on every request.
