"""Can an in-cockpit browser (the F-4E's Virtual Browser, RCtrl+V) show a page the engine serves?

    python3 -m tools.browser_probe [--port 8765] [--host 127.0.0.1]

Serves one test page at http://<host>:<port>/. It shows a clock that ticks in JavaScript, and a
"live" panel that asks this server for fresh data every 2 s (fetch /state). If both move, the
browser runs scripts and can poll the engine, which is what a live war overview needs.
Use --host 0.0.0.0 to reach it from another machine (e.g. clients of a dedicated server).
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Campaign browser test</title>
<style>
  body { background: #1d2329; color: #e8e4d8; font: 20px/1.4 sans-serif; margin: 24px; }
  h1 { font-size: 30px; margin: 0 0 16px; }
  .card { background: #2a3238; border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; }
  .big { font-size: 40px; font-variant-numeric: tabular-nums; }
  table { border-collapse: collapse; } td { padding: 4px 16px 4px 0; }
  .ok { color: #8fd18f; } .bad { color: #f08a7a; }
</style></head>
<body>
<h1>Campaign browser test</h1>
<div class="card">JavaScript clock (should tick): <span id="clock" class="big">--</span></div>
<div class="card">Live from the engine (every 2 s): <span id="live" class="bad">waiting...</span>
  <div>Requests answered: <span id="count">0</span></div></div>
<div class="card"><table>
  <tr><td>Flight</td><td>Colt 1-1, 2x F-4E, CAS</td></tr>
  <tr><td>Takeoff</td><td>Khalkhalah 14:20L</td></tr>
  <tr><td>Target</td><td>armour column, 012&deg; / 18 nm from Khalkhalah</td></tr>
  <tr><td>Threats</td><td>SA-8 reported 020&deg; / 25 nm; ZSU-23-4 with the column</td></tr>
  <tr><td>Altitude</td><td>ingress 1,500 ft AGL, egress 15,000 ft</td></tr>
</table></div>
<script>
  setInterval(() => { document.getElementById('clock').textContent = new Date().toLocaleTimeString(); }, 250);
  let n = 0;
  async function poll() {
    try {
      const r = await fetch('/state', {cache: 'no-store'});
      const s = await r.json();
      n += 1;
      const el = document.getElementById('live');
      el.textContent = 'engine time ' + s.time + ' (request ' + s.request + ')';
      el.className = 'ok';
      document.getElementById('count').textContent = n;
    } catch (e) {
      const el = document.getElementById('live');
      el.textContent = 'fetch failed: ' + e; el.className = 'bad';
    }
  }
  poll(); setInterval(poll, 2000);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    requests = 0

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/state"):
            Handler.requests += 1
            body = json.dumps({"time": time.strftime("%H:%M:%S"), "request": Handler.requests}).encode()
            ctype = "application/json"
        elif self.path in ("/", "/index.html"):
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        print(f"HTTP    {self.client_address[0]} {fmt % args} {self.headers.get('User-Agent', '')[:80]}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"serving http://{args.host}:{args.port}/ (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
