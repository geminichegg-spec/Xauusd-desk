"""Local control server for the desk HUD.

    python3 serve.py [--port 8765] [--config config.json]

Then open http://127.0.0.1:8765 in your browser. The HUD can then:
  * show which API keys are set (yes/no only, never the key),
  * run ANALYST (Gemini, Grok or Groq) on the candles you loaded, with an optional Jev review,
  * re-check any plan with the real Python RISK gate,
  * talk to CARDINAL (voice or text) about the desk state, answered by Gemini (or Groq / Grok),
  * list runs still waiting on you, and record your approve / reject decision in runs/ (gitignored).

Keys stay in .env or your environment, are read only by this process, and never reach
the browser. The server listens on 127.0.0.1 only, accepts only JSON POSTs from a
localhost Host, and sends no CORS headers, so other websites cannot drive it.
Nothing here places orders or sizes positions. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import analyst
import cardinal_chat
import jev
import keys
from risk_gate import RiskConfig, evaluate

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs")
MAX_BODY = 5 * 1024 * 1024
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[A-Za-z]+-[a-z]+(-[0-9]+)?$")


def load_config(path: str | None) -> RiskConfig:
    path = path or os.path.join(HERE, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return RiskConfig.from_dict(json.load(f))
    return RiskConfig()


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def save_run(record: dict) -> str:
    os.makedirs(RUNS, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-{record.get('session', 'x')}-{record.get('provider', 'x')}"
    run_id, n = base, 1
    while os.path.exists(os.path.join(RUNS, run_id + ".json")):
        n += 1
        run_id = f"{base}-{n}"
    with open(os.path.join(RUNS, run_id + ".json"), "w", encoding="utf-8") as f:
        json.dump({"run_id": run_id, **record}, f, indent=2, default=str)
    return run_id


def pending_runs(limit: int = 20) -> list[dict]:
    """Saved runs you have not approved or rejected yet, newest first (for the HUD's queue)."""
    if not os.path.isdir(RUNS):
        return []
    out = []
    for name in sorted(os.listdir(RUNS), reverse=True):
        if not name.endswith(".json") or not RUN_ID.match(name[:-5]):
            continue
        try:
            with open(os.path.join(RUNS, name), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if rec.get("decision"):
            continue
        res = rec.get("result") or {}
        jr = res.get("jev_review") or None
        out.append({"run_id": rec.get("run_id", name[:-5]), "session": rec.get("session"),
                    "provider": res.get("provider"), "plan": res.get("plan"),
                    "risk": {"verdict": (res.get("risk") or {}).get("verdict"),
                             "reasons": (res.get("risk") or {}).get("reasons", [])},
                    "jev": {"agrees": jr.get("agrees")} if isinstance(jr, dict) and "agrees" in jr else None})
        if len(out) >= limit:
            break
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "XAUUSDDesk/1"
    cfg: RiskConfig = RiskConfig()

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):  # keep the console quiet and key-free
        sys.stderr.write(f"[desk] {self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name in ALLOWED_HOSTS

    def _send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise ValueError("Content-Type must be application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            raise ValueError("request body missing or too large")
        body = json.loads(self.rfile.read(n))
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    # ------------------------------------------------------------ routes
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        path = self.path.split("?")[0]
        if path in ("/", "/hud.html"):
            with open(os.path.join(HERE, "hud.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, {
                "ok": True,
                "keys": keys.status(),
                "models": {p: keys.get_model(p) for p in keys.PROVIDERS},
                "default_provider": os.environ.get("DESK_PROVIDER", "gemini"),
                "chat_provider": _chat_provider(),
                "config": self.cfg.__dict__,
            })
        if path == "/api/runs":
            return self._send(200, {"runs": pending_runs()})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        path = self.path.split("?")[0]
        try:
            b = self._json_body()
        except (ValueError, json.JSONDecodeError) as e:
            return self._send(400, {"error": str(e)})
        try:
            if path == "/api/gate":
                d = evaluate(b.get("plan"), b.get("candles") or [], _num(b.get("pnl")), self.cfg,
                             b.get("now"), b.get("news") or [])
                return self._send(200, {"risk": d.to_dict()})
            if path == "/api/run":
                candles = b.get("candles")
                if not isinstance(candles, list) or not candles:
                    return self._send(400, {"error": "load candles first (a JSON list of {t,o,h,l,c})"})
                result = analyst.run_desk(
                    candles, b.get("session", "London"), b.get("provider", "gemini"),
                    str(b.get("context") or ""), _num(b.get("pnl")), self.cfg, bool(b.get("jev")),
                    b.get("now"), b.get("news") or [])
                result["run_id"] = save_run({"session": b.get("session"), "provider": result["provider"],
                                             "context": b.get("context"), "pnl": _num(b.get("pnl")),
                                             "result": result, "decision": None})
                return self._send(200, result)
            if path == "/api/ask":
                return self._send(200, cardinal_chat.ask(str(b.get("question") or ""), b.get("state") or {},
                                                         b.get("history"), b.get("provider"),
                                                         str(b.get("name") or "Mazhar")))
            if path == "/api/jev":
                return self._send(200, {"jev_review": jev.review(b.get("plan"), b.get("candles") or [],
                                                                 str(b.get("context") or ""))})
            if path == "/api/decision":
                run_id, decision = str(b.get("run_id") or ""), b.get("decision")
                if not RUN_ID.match(run_id) or decision not in ("approved", "rejected"):
                    return self._send(400, {"error": "need a valid run_id and decision approved|rejected"})
                p = os.path.join(RUNS, run_id + ".json")
                if not os.path.exists(p):
                    return self._send(404, {"error": "run not found"})
                with open(p, encoding="utf-8") as f:
                    rec = json.load(f)
                rec["decision"] = {"value": decision, "note": str(b.get("note") or "")[:500],
                                   "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(rec, f, indent=2, default=str)
                return self._send(200, {"ok": True, "run_id": run_id, "decision": rec["decision"]})
        except (ValueError, KeyError, RuntimeError, urllib.error.URLError) as e:
            return self._send(502 if isinstance(e, (RuntimeError, urllib.error.URLError)) else 400,
                              {"error": str(e)})
        return self._send(404, {"error": "not found"})


def _chat_provider():
    try:
        return cardinal_chat.pick_provider()
    except RuntimeError:
        return None


def make_server(port: int = 8765, cfg: RiskConfig | None = None) -> ThreadingHTTPServer:
    handler = type("DeskHandler", (Handler,), {"cfg": cfg or RiskConfig()})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main(argv=None) -> int:
    keys.load_dotenv()
    ap = argparse.ArgumentParser(description="Local control server for the desk HUD")
    ap.add_argument("--port", type=int, default=int(os.environ.get("DESK_PORT", 8765)))
    ap.add_argument("--config", help="RiskConfig JSON file (default: config.json if present)")
    a = ap.parse_args(argv)
    srv = make_server(a.port, load_config(a.config))
    found = ", ".join(f"{k} {'set' if v else 'missing'}" for k, v in keys.status().items())
    print(f"Desk HUD on http://127.0.0.1:{a.port}  (keys: {found})  Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
