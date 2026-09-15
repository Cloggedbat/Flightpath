"""FlightPath update server. Serves the latest APK and a version manifest.

Tiny on purpose: stdlib only, one process, no database. Railway builds it from
the Dockerfile. Redeploying with a new APK in public/ IS the release process.

  GET /              landing page with a download button
  GET /version.json  {"versionCode": 3, "versionName": "0.1.2", "apk": "/FlightPath.apk", "sha256": "..."}
  GET /FlightPath.apk
"""
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(HERE, "public")
PORT = int(os.environ.get("PORT", "8080"))


def manifest() -> dict:
    with open(os.path.join(PUBLIC, "version.json")) as fh:
        m = json.load(fh)
    apk = os.path.join(PUBLIC, "FlightPath.apk")
    if os.path.exists(apk):
        h = hashlib.sha256()
        with open(apk, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        m["sha256"] = h.hexdigest()
        m["bytes"] = os.path.getsize(apk)
    m["apk"] = "/FlightPath.apk"
    return m


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>FlightPath</title>
<style>body{{font-family:system-ui,sans-serif;background:#12241A;color:#F2F7F3;margin:0;padding:32px 20px;text-align:center}}
h1{{font-size:26px;margin:0 0 4px}}p{{color:#8FAE9C;margin:0 0 22px}}
a.b{{display:inline-block;background:#38C13F;color:#0B1A10;font-weight:800;text-decoration:none;padding:16px 28px;border-radius:14px;font-size:17px}}
small{{display:block;margin-top:18px;color:#8FAE9C;font-size:12px;font-family:ui-monospace,monospace}}</style>
<h1>FlightPath</h1><p>Version {versionName} &middot; {mb:.1f} MB</p>
<a class=b href="/FlightPath.apk">Download APK</a>
<small>Android will ask to allow installs from your browser the first time.<br>sha256 {sha}</small>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, f, *a):
        pass

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/version.json":
            return self._send(200, json.dumps(manifest()).encode(), "application/json",
                              {"Access-Control-Allow-Origin": "*"})
        if path == "/FlightPath.apk":
            apk = os.path.join(PUBLIC, "FlightPath.apk")
            if not os.path.exists(apk):
                return self._send(404, b"no apk yet", "text/plain")
            with open(apk, "rb") as fh:
                data = fh.read()
            return self._send(200, data, "application/vnd.android.package-archive",
                              {"Content-Disposition": 'attachment; filename="FlightPath.apk"'})
        if path in ("/", "/index.html"):
            m = manifest()
            body = PAGE.format(versionName=m.get("versionName", "?"),
                               mb=m.get("bytes", 0) / 1048576, sha=m.get("sha256", "")[:16])
            return self._send(200, body.encode(), "text/html; charset=utf-8")
        if path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        return self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    print(f"flightpath-updates on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
