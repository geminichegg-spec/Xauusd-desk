"""JEV REVIEW: a typed second opinion on an ANALYST plan from TypeSafe AI (Jev).

Jev does not write text or plans. It answers fixed questions about the plan and
candles with probabilities, e.g. "does the data show a clean ORB break?".
It is advisory only: RISK (risk_gate.py) still has the final word.

    python3 jev.py examples/plan_long.json examples/candles_london.json [--context "VP: ..."]

Standard library only. Needs TYPESAFE_API_KEY in env or .env.
Exit code 0 = review done, 2 = bad input or API error.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

import keys

URL = "https://api.typesafe.ai/v1/systemone"

QUESTIONS = {
    "data_bias": {
        "type": "choice",
        "instructions": "Using only the candles and the opening range (orb_high/orb_low), which way has price broken?",
        "criteria": {
            "long": "Price has closed above the opening range high and is holding above it",
            "short": "Price has closed below the opening range low and is holding below it",
            "flat": "Price is still inside the range, or the break failed or is unclear",
        },
    },
    "breakout_quality": {
        "type": "score",
        "instructions": "How clean is the breakout in the plan's direction, judged from the candles only?",
        "criteria": [
            "No break, or price is back inside the range",
            "Weak or choppy break with little follow-through",
            "Clear break with some follow-through",
            "Strong break with steady follow-through and no retest failure",
        ],
    },
    "plan_consistent": {
        "type": "noul",
        "instructions": "The plan's direction, entry, stop and targets are consistent with the candles and opening range.",
    },
    "unsupported_claims": {
        "type": "noul",
        "instructions": "The plan's reasoning or confluence fields claim volume profile, whale, GameX or macro facts that are not present in the supplied data.",
    },
}


def build_state(plan: dict, candles: list, context: str = "") -> dict:
    return {"instrument": "XAUUSD", "plan": plan, "candles_utc": candles,
            "extra_data_supplied": context or "none"}


def review(plan: dict, candles: list, context: str = "") -> dict:
    body = {"model": keys.get_model("typesafe"), "state": build_state(plan, candles, context),
            "questions": QUESTIONS}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": "Bearer " + keys.get_key("typesafe"), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"TypeSafe HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from None
    return summarize(plan, out)


def summarize(plan: dict, out: dict) -> dict:
    a = out.get("answers", {})
    bias = a.get("data_bias", {})
    flags = []
    if bias.get("choice") and plan.get("direction") and bias["choice"] != plan["direction"]:
        flags.append(f"Jev reads the data as {bias['choice']}, plan says {plan['direction']}")
    if a.get("plan_consistent", {}).get("noul", 1) < 0.5:
        flags.append("Jev doubts the levels match the candles")
    if a.get("unsupported_claims", {}).get("noul", 0) > 0.5:
        flags.append("Jev thinks the plan claims data that was not supplied")
    return {"model": out.get("model"), "agrees": not flags, "flags": flags,
            "data_bias": bias.get("choice"), "data_bias_confidence": bias.get("confidence"),
            "breakout_quality_0_to_3": a.get("breakout_quality", {}).get("score"),
            "plan_consistent_p": a.get("plan_consistent", {}).get("noul"),
            "unsupported_claims_p": a.get("unsupported_claims", {}).get("noul"),
            "usage": out.get("usage")}


def main(argv=None) -> int:
    keys.load_dotenv()
    ap = argparse.ArgumentParser(description="JEV REVIEW of an ANALYST plan (advisory)")
    ap.add_argument("plan")
    ap.add_argument("candles")
    ap.add_argument("--context", default="")
    a = ap.parse_args(argv)
    try:
        with open(a.plan) as f:
            plan = json.load(f)
        with open(a.candles) as f:
            candles = json.load(f)
        print(json.dumps(review(plan, candles, a.context), indent=2))
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as e:
        print(json.dumps({"error": str(e)}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
