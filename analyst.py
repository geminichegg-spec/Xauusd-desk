"""Ask Gemini or Grok (as ANALYST) for an ORB plan, then run it through the RISK gate.

    python3 analyst.py examples/candles_london.json --session London [--provider grok]
        [--context "VP: VAH 2662, POC 2655"] [--pnl -120] [--config config.json] [--jev]

Standard library only. The model never places orders; RISK has the final word.
Exit code 0 = APPROVE or NO_TRADE, 1 = REJECT, 2 = bad input or API error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

import jev
import keys
from risk_gate import RiskConfig, evaluate

HERE = os.path.dirname(os.path.abspath(__file__))


def analyst_prompt() -> str:
    with open(os.path.join(HERE, "prompts", "analyst.md"), encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"```text\n(.*?)```", text, re.S)
    return m.group(1).strip() if m else text


def _post(url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # Show the API's error message but never the request (it carries the key).
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from None


def call_gemini(system: str, user: str) -> str:
    model = keys.get_model("gemini")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    out = _post(url, {"x-goog-api-key": keys.get_key("gemini")}, {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    })
    return out["candidates"][0]["content"]["parts"][0]["text"]


def call_grok(system: str, user: str) -> str:
    out = _post("https://api.x.ai/v1/chat/completions",
                {"Authorization": "Bearer " + keys.get_key("grok")}, {
        "model": keys.get_model("grok"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    })
    return out["choices"][0]["message"]["content"]


CALLERS = {"gemini": call_gemini, "grok": call_grok}


def parse_plan(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def main(argv=None) -> int:
    keys.load_dotenv()
    ap = argparse.ArgumentParser(description="ANALYST (Gemini/Grok) + RISK gate")
    ap.add_argument("candles", help="candles JSON file (list of {t,o,h,l,c}, UTC)")
    ap.add_argument("--session", choices=["London", "NewYork"], required=True)
    ap.add_argument("--provider", choices=list(CALLERS), default=os.environ.get("DESK_PROVIDER", "gemini"))
    ap.add_argument("--context", default="", help="Volume Profile / whale / macro notes you have")
    ap.add_argument("--pnl", type=float, default=0.0, help="realized P&L today")
    ap.add_argument("--config", help="RiskConfig JSON file")
    ap.add_argument("--jev", action="store_true", help="add a TypeSafe AI (Jev) second opinion (advisory)")
    ap.add_argument("--now", help="current time, ISO 8601")
    ap.add_argument("--news", nargs="*", default=[], help="high-impact USD release times")
    a = ap.parse_args(argv)
    try:
        with open(a.candles) as f:
            candles = json.load(f)
        cfg = RiskConfig()
        if a.config:
            with open(a.config) as f:
                cfg = RiskConfig.from_dict(json.load(f))
        user = (f"Session: {a.session}\nCandles (UTC):\n{json.dumps(candles)}\n"
                f"Extra data: {a.context or 'none supplied'}\nReturn only the JSON plan.")
        plan = parse_plan(CALLERS[a.provider](analyst_prompt(), user))
    except (OSError, ValueError, KeyError, RuntimeError, urllib.error.URLError) as e:
        print(json.dumps({"verdict": "ERROR", "reasons": [str(e)]}))
        return 2
    d = evaluate(plan, candles, a.pnl, cfg, a.now, a.news)
    result = {"provider": a.provider, "plan": plan, "risk": d.to_dict()}
    if a.jev:
        try:
            result["jev_review"] = jev.review(plan, candles, a.context)
        except (RuntimeError, urllib.error.URLError) as e:
            result["jev_review"] = {"error": str(e)}
    print(json.dumps(result, indent=2, default=str))
    return 1 if d.verdict == "REJECT" else 0


if __name__ == "__main__":
    sys.exit(main())
