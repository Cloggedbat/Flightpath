# FlightPath V0

Camera-based golf launch monitor, offline prototype. Takes a high-speed clip
and returns ball speed, launch angle and carry.

This is the proof step. No app yet, on purpose: if the computer vision does not
work on clips you have already recorded, nothing downstream matters.

**Validated on synthetic clips** (motion blur, sensor noise, moving clutter),
at both 240 and 480 fps:

| club | true | recovered |
|---|---|---|
| driver | 165 mph / 11° | 164.9 / 11.0 |
| 3w | 150 / 13 | 149.9 / 13.0 |
| 7i | 120 / 19 | 119.9 / 19.0 |
| pw | 85 / 30 | 84.9 / 30.0 |
| sw | 60 / 38 | 60.0 / 38.0 |

**The range app** lives in [RANGE.md](RANGE.md): Python on the phone, GoPro as
the camera, browser UI at `http://localhost:8080`. Start there if you want to
use this rather than work on it.

---

## Camera: use the HERO9

| | HERO9 Black | Insta360 X5 | S22 Ultra |
|---|---|---|---|
| best usable rate | **1080p240** | 1080p120 single lens | 240 (480 in short bursts) |
| distortion | fixable in camera (Linear) | severe, 360 fisheye | mild |
| programmable | **yes, officially** | app only | blocked by Samsung |
| verdict | **primary** | direction only, later | backup |

The HERO9 wins on the only axis that matters first, which is frames per second
through the impact zone. At 240 fps a 120 mph ball moves 8.6 in between frames;
at 120 fps it moves 17 in, and you get half as many samples to fit.

It also dodges the Samsung problem entirely. Samsung locks high frame rates
away from third-party apps, so the phone can never become a live tool. The
HERO9 has a documented API and official experimental firmware.

### Recommended settings

```
1080p240
Linear lens          (NOT Wide; see distortion below)
Hypersmooth OFF      stabilisation warps geometry, which is the thing you measure
Exposure locked      auto-exposure shifting mid-clip changes the ball's apparent size
```

Linear is available at 1080p240 on the HERO9 and corrects barrel distortion in
camera. That is the whole fix and it costs nothing. If you are ever stuck with
Wide footage, `flightpath/lens.py` will calibrate it out from a checkerboard,
but shooting Linear is better.

### GoPro Labs: yes, flash it

GoPro Labs is GoPro's own experimental firmware, officially supported on the
HERO9, not a jailbreak. Three features matter here:

1. **Sound Pressure Level trigger.** Start capture on a loud sound. This is
   OpenFlight's SEN-14262 impact trigger, in firmware, for free, with no
   soldering. It is the single best reason to flash.
2. **GPS time sync and LTC timecode.** Frame-accurate sync between multiple
   cameras, which is what a second camera needs to be worth anything.
3. **QR code control.** Reconfigure the camera by showing it a QR code, so a
   range session does not mean thumbing through menus.

Stock firmware is enough to start. Flash when you want the trigger.

### Programmatic control

The Open GoPro API supports the HERO9 from firmware v01.70.00, with a Python
SDK on PyPI as `open_gopro`. Split of duties:

- **Bluetooth**: start and stop recording, change settings, read camera state.
- **WiFi**: download the files, live preview.

So your instinct was right. Bluetooth triggers, WiFi collects. The eventual
loop is: BLE start, hit balls, BLE stop, WiFi pull, run this pipeline.

### Where the X5 fits

Not as the primary. 1080p120 single lens is half the frame rate, and a 360
camera's fisheye is exactly the distortion you do not want anywhere near a
scale measurement.

It has one real use. In 360 mode at 4K120 it sees the entire scene, so it
captures launch *direction* — the left and right that a single side-on camera
is blind to. That is the missing axis for a GolfTrak-style dispersion compass.
Angular resolution is about 10.7 px per degree, so a ball at 10 ft is roughly
9 px across: marginal, but detectable. Park this until the HERO9 side works,
and sync the two with a hand clap, which spikes both audio tracks.

No need to modify it. Nothing here asks you to.

---

## Rolling shutter, and why it costs you 3 percent

Both cameras read the sensor row by row rather than all at once. A detection is
not captured at the frame's nominal time but at:

```
t = frame / fps + (y / height) * readout_time
```

The ball climbs through the frame as it flies, so each detection carries a
different timing offset and the apparent time span comes out wrong.

Measured on a synthetic clip with a 4.2 ms readout, a true 120.0 mph shot at
19 degrees:

```
uncorrected    116.6 mph      2.8% low
corrected      120.0 mph      exact
```

The correction is exact when you know the readout time. When you do not, the
tool reports the band instead of guessing:

```
rolling shutter band  119.9 to 123.5 mph  (2.9% of reading)
```

Two ways to shrink it. Frame wider so the ball crosses less of the image
height, which shrinks the error toward zero regardless. Or measure your readout
time once and pass `--readout-ms`.

---

## Use

```bash
pip install -r requirements.txt

python analyze.py cameras                      # list known profiles

# 1. export a still and read your reference object's pixel coordinates off it
python analyze.py frame 7i_001.mp4 --index 0 --out ref.png

# 2. one shot
python analyze.py shot 7i_001.mp4 \
    --camera hero9-1080p240 --ref 310,880,1180,880 --ref-inches 46 --club 7i

# 3. a range session, with a gapping table
python analyze.py session ./clips \
    --camera hero9-1080p240 --ref 310,880,1180,880 --ref-inches 46 \
    --out session.json
```

`--camera` supplies frame rate and readout time together. `--fps`,
`--readout-ms` and `--lens` override it.

### Placement

1. Tripod, camera landscape, 8 to 10 ft directly to your side.
2. Lens perpendicular to the target line. Square this up properly. Every
   degree off makes the ball read slow, the same cosine error that bites radar.
3. Lens low, ball height to knee height.
4. Frame 6 to 8 ft of open space in front of the ball, and keep the ball's
   vertical travel small within the frame.
5. Bright daylight, sun behind or beside the camera.

Lay an alignment stick on the ground **in the ball's flight plane** as the
scale reference. Same distance from the camera as the ball, or the scale is
wrong. Name clips `7i_001.mp4` and session mode reads the club off the name.

### Verify your true frame rate once

Film a stopwatch and count frames between the 0.10 s and 0.20 s marks. Twenty
four frames means 240 fps. This matters most on the phone: Samsung writes
slow-motion already time stretched with a 30 fps container rate, and trusting
it turned a real 120 mph shot into 7.5 mph in testing. The tool catches
implausible speeds and back-computes what the rate should have been.

### When it finds no track

1. Frame rate is wrong.
2. `--threshold 15` for a dull or overcast clip.
3. `--min-circularity 0.1`, because a fast ball smears into a streak.
4. `--start N` to point it at the actual impact frame.
5. `--min-points 3` if the ball leaves frame very quickly.

---

## Test rig

```bash
python make_test_clip.py --speed-mph 165 --angle 11 --fps 240 --out test.mp4
python analyze.py shot test.mp4 --camera hero9-1080p240 --readout-ms 0 \
    --ref 120,980,611,980 --ref-inches 46
```

Renders a clip with a ball at a speed you specify, plus motion blur, noise and
moving clutter to reject. Run it after any detector change. Match the generated
`--fps` to the profile's rate or you will read exactly half, which is how the
frame-rate mismatch was caught during development.

---

## What this does not do

- **Spin is estimated, not measured.** From a launch-angle curve, not your
  ball. OpenFlight's own radar spin detection is experimental and it does not
  feed spin into carry either. Carry inherits that uncertainty.
- **No club data.** No club speed, no smash factor, no path or face angle.
- **No left-right dispersion.** One side-on camera cannot see it. That is the
  X5's job later.
- **Carry is modeled**, from drag and Magnus integration.

## Honest expectations

ShotVision is a funded iOS product and MyGolfSpy still found it inconsistent
outdoors. Ball speed is the metric most likely to come out usable, because it
depends only on scale and frame timing, both of which you control. Treat launch
angle as decent and everything downstream of spin as indicative.

## Licence note

If you lift code from OpenFlight rather than its ideas, you inherit AGPL-3.0.
Fine personally. Run a modified version as a network service and you owe users
the source.
