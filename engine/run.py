#!/usr/bin/env python3
"""FlightPath range app. Start this, then open http://localhost:8080 on the phone.

  python run.py --ref 310,880,1180,880 --ref-inches 46

The reference is your scale. Without it the app runs but refuses to produce
numbers, because an uncalibrated launch monitor is worse than none.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import threading

from flightpath import cameras, gopro
from flightpath.server import serve
from flightpath.worker import Settings, Worker


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", type=str, default=None,
                    help="x1,y1,x2,y2 pixel endpoints of your scale reference")
    ap.add_argument("--ref-inches", type=float, default=46.0)
    ap.add_argument("--camera", default="hero9-1080p240",
                    help="camera profile (see: python analyze.py cameras)")
    ap.add_argument("--club", default="7i")
    ap.add_argument("--mode", default="queue", choices=("queue", "focus"))
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address. Loopback by default so nothing off the "
                         "phone can reach it. Use --lan to expose it.")
    ap.add_argument("--lan", action="store_true",
                    help="bind all interfaces and require a token, so a laptop "
                         "on the same network can open the UI")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--clips", default="clips")
    ap.add_argument("--session", default="session.json")
    ap.add_argument("--config", default="config.json",
                    help="where calibration and preferences are saved")
    ap.add_argument("--lens", default=None, help="lens model JSON, if you shot Wide")
    ap.add_argument("--gopro-host", default=gopro.DEFAULT_HOST)
    ap.add_argument("--gopro-port", type=int, default=gopro.DEFAULT_PORT)
    ap.add_argument("--probe-only", action="store_true",
                    help="check what the camera answers, then exit")
    args = ap.parse_args(argv)

    try:
        cameras.get(args.camera)
    except KeyError as exc:
        print(exc)
        return 2

    client = gopro.GoProClient(host=args.gopro_host, port=args.gopro_port)

    if args.probe_only:
        print(client.probe().summary())
        return 0

    ref = None
    if args.ref:
        try:
            ref = tuple(float(v) for v in args.ref.split(","))
            if len(ref) != 4:
                raise ValueError
        except ValueError:
            print("--ref must be four comma-separated numbers: x1,y1,x2,y2")
            return 2

    settings = Settings(
        camera_profile=args.camera,
        ref_px=ref,
        ref_inches=args.ref_inches,
        lens_model_path=args.lens,
        club=args.club,
        mode=args.mode,
        clip_dir=args.clips,
        session_path=args.session,
        config_path=args.config,
    )

    worker = Worker(settings, client)
    worker.start()

    host = "0.0.0.0" if args.lan else args.host
    token = secrets.token_urlsafe(16) if args.lan else ""

    httpd = serve(worker, host, args.port, token)
    print(f"FlightPath on http://localhost:{args.port}")
    print(f"  camera profile  {args.camera}")
    print(f"  mode            {args.mode}")
    calibrated = settings.ref_px is not None
    print(f"  calibrated      {'yes (saved)' if calibrated else 'no, the app will walk you through it'}")
    if token:
        print(f"  bind            0.0.0.0 (LAN). Anyone on this network can reach it.")
        print(f"  token           {token}")
        print( "                  send it as the X-FlightPath-Token header")
    else:
        print(f"  bind            {host} (loopback only)")
    print("  ctrl-c to stop")

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        while True:
            t.join(1)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.shutdown()
        worker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
