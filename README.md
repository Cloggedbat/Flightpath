# FlightPath

Camera-based golf launch monitor for the driving range. A GoPro HERO9 on a
tripod films the ball side-on at 240 fps; an Android app pulls the clip,
tracks the ball, and reports ball speed, launch angle and carry.

Architecture and style borrow from [OpenFlight](https://github.com/open-flight/openflight)
(local engine + browser UI, impact-triggered capture, per-metric confidence).
No OpenFlight code is used; the radar DSP does not transfer to a camera.

| path | what it is |
|---|---|
| `CLAUDE.md` | Project scope: what is proven, what is frozen, and the active plan. Read it first. |
| `engine/` | Python package (`flightpath/`), CLI (`analyze.py`), desktop server (`run.py`), and the web UI. Start with `engine/README.md` and `engine/RANGE.md`. |
| `android/` | Native Android app. Bundles the engine with Chaquopy, joins the camera WiFi for the app only, shows the UI in a WebView. |
| `updates/` | Tiny update server (Railway). Serves `version.json` and the latest APK to the in-app updater. |

## Working on it

```bash
# engine, on a PC
cd engine && pip install -r requirements.txt && python run.py

# android
cd android && cp keystore/signing.properties.example keystore/signing.properties   # then fill in
./gradlew assembleDebug -PupdateUrl=https://YOUR-UPDATE-SERVER
```

`android/app/src/main/python/flightpath/` is a copy of `engine/flightpath/`.
After editing the engine, run `./sync-engine.sh` before building the app.

## Not in git, on purpose

- `android/keystore/` (the release signing key; losing it means users must reinstall)
- built APKs (use GitHub Releases or the update server)

## Status

Prototype. Ball tracking is validated on synthetic clips only; first real-ball
range test is pending. The active plan, and the full list of what is proven and
what is not, are in `CLAUDE.md`. Security notes are in `engine/RANGE.md`.
