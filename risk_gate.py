"""RISK gate for the XAUUSD dual-session ORB desk.

Pure Python, standard library only. Deterministic veto: the LLM agents can
never override it. It never places orders or sizes positions.

    python risk_gate.py plan.json candles.json [--pnl -120] [--config config.json]
        [--now 2026-11-03T13:41:00Z] [--news 2026-11-03T13:30:00Z ...]

Exit code 0 = APPROVE or NO_TRADE, 1 = REJECT, 2 = bad input.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

SESSIONS = ("London", "NewYork")
DIRECTIONS = ("long", "short", "flat")
VP_VALUES = ("strong", "moderate", "weak", "not_available")
WHALE_VALUES = ("supportive", "opposing", "neutral", "not_available")


@dataclass
class RiskConfig:
    min_rr: float = 1.8
    min_confidence: float = 0.55
    daily_loss_limit: float = 500.0  # account currency, positive number
    rr_target_index: int = 0  # which target R:R is measured to (0 = first)
    rr_tolerance: float = 0.15  # allowed gap between reported and computed R:R
    price_tolerance: float = 0.05  # $ slack when comparing levels to candles
    # Optional filter: skip when BOTH volume profile and whale data are missing
    # and confidence is below this level. Set skip_no_confluence False to disable.
    skip_no_confluence: bool = True
    no_confluence_min_confidence: float = 0.65
    # Opening-range windows in GMT/UTC, "HH:MM" start, length in minutes.
    orb_start: dict = field(default_factory=lambda: {"London": "08:00", "NewYork": "13:30"})
    orb_minutes: int = 30
    news_blackout_minutes: int = 10

    @classmethod
    def from_dict(cls, d: dict) -> "RiskConfig":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Decision:
    verdict: str  # APPROVE | REJECT | NO_TRADE
    reasons: list = field(default_factory=list)  # why it was rejected
    checks: list = field(default_factory=list)  # every check, pass or fail
    computed_rr: float | None = None
    computed_orb: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- helpers

def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def _get(c: dict, *keys):
    for k in keys:
        if k in c:
            return c[k]
    return None


def _parse_time(v) -> datetime | None:
    if v is None:
        return None
    if _is_num(v):  # epoch seconds (or ms)
        v = v / 1000 if v > 1e11 else v
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(v).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_candles(candles: list) -> list[dict]:
    """Accept {t,o,h,l,c} or {time,open,high,low,close}; return a uniform list."""
    out = []
    for i, c in enumerate(candles):
        h, l = _get(c, "h", "high"), _get(c, "l", "low")
        if not (_is_num(h) and _is_num(l)) or h < l:
            raise ValueError(f"candle {i} has a missing or inverted high/low")
        out.append({"t": _parse_time(_get(c, "t", "time", "timestamp", "datetime")),
                    "h": float(h), "l": float(l)})
    if not out:
        raise ValueError("no candles supplied")
    return out


def compute_orb(candles: list[dict], session: str, cfg: RiskConfig) -> dict | None:
    """High/low of the first `orb_minutes` of the session, from timestamped candles.

    Uses the latest session date present in the candles. Returns None when the
    candles carry no timestamps or none fall inside the window.
    """
    timed = [c for c in candles if c["t"] is not None]
    if not timed or session not in cfg.orb_start:
        return None
    hh, mm = (int(x) for x in cfg.orb_start[session].split(":"))
    for day in sorted({c["t"].astimezone(timezone.utc).date() for c in timed}, reverse=True):
        start = datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc)
        end = start + timedelta(minutes=cfg.orb_minutes)
        inside = [c for c in timed if start <= c["t"] < end]
        if inside:
            return {"date": day.isoformat(), "high": max(c["h"] for c in inside),
                    "low": min(c["l"] for c in inside), "bars": len(inside)}
    return None


# ---------------------------------------------------------------- schema

def validate_plan(plan) -> list[str]:
    """Validate ANALYST JSON. Returns a list of problems (empty = valid)."""
    if not isinstance(plan, dict):
        return ["plan is not a JSON object"]
    errs = []
    req = ["symbol", "session", "orb_high", "orb_low", "direction", "entry", "stop",
           "targets", "confidence", "reward_risk", "volume_profile_confluence",
           "whale_or_gamex_confluence", "key_levels", "invalidation", "reasoning"]
    for k in req:
        if k not in plan:
            errs.append(f"missing field: {k}")
    extra = set(plan) - set(req)
    if extra:
        errs.append(f"unexpected fields: {sorted(extra)}")
    if errs:
        return errs

    flat = plan["direction"] == "flat"
    if plan["symbol"] != "XAUUSD":
        errs.append("symbol must be 'XAUUSD'")
    if plan["session"] not in SESSIONS:
        errs.append(f"session must be one of {SESSIONS}")
    if plan["direction"] not in DIRECTIONS:
        errs.append(f"direction must be one of {DIRECTIONS}")
    for k in ("orb_high", "orb_low"):
        if not _is_num(plan[k]) or plan[k] <= 0:
            errs.append(f"{k} must be a positive number")
    if _is_num(plan["orb_high"]) and _is_num(plan["orb_low"]) and plan["orb_high"] <= plan["orb_low"]:
        errs.append("orb_high must be above orb_low")

    for k in ("entry", "stop"):
        v = plan[k]
        if not (_is_num(v) and v > 0) and not (flat and v is None):
            errs.append(f"{k} must be a positive number" + (" (or null when flat)" if flat else ""))
    t = plan["targets"]
    if flat and t in (None, []):
        pass
    elif not (isinstance(t, list) and len(t) == 2 and all(_is_num(x) and x > 0 for x in t)):
        errs.append("targets must be a list of exactly two positive numbers")

    c = plan["confidence"]
    if not _is_num(c) or not 0.0 <= c <= 1.0:
        errs.append("confidence must be a number between 0.0 and 1.0")
    rr = plan["reward_risk"]
    if not _is_num(rr) or rr < 0:
        errs.append("reward_risk must be a non-negative number")
    if plan["volume_profile_confluence"] not in VP_VALUES:
        errs.append(f"volume_profile_confluence must be one of {VP_VALUES}")
    if plan["whale_or_gamex_confluence"] not in WHALE_VALUES:
        errs.append(f"whale_or_gamex_confluence must be one of {WHALE_VALUES}")
    kl = plan["key_levels"]
    if not (isinstance(kl, list) and all(isinstance(x, str) for x in kl)):
        errs.append("key_levels must be a list of strings")
    for k in ("invalidation", "reasoning"):
        if not isinstance(plan[k], str) or not plan[k].strip():
            errs.append(f"{k} must be a non-empty string")
    return errs


# ---------------------------------------------------------------- gate

def evaluate(plan: dict, candles: list, realized_pnl_today: float = 0.0,
             cfg: RiskConfig | None = None, now=None, news_events=()) -> Decision:
    cfg = cfg or RiskConfig()
    d = Decision(verdict="APPROVE")

    def check(name: str, ok: bool, detail: str):
        d.checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            d.reasons.append(f"{name}: {detail}")

    schema = validate_plan(plan)
    check("schema", not schema, "; ".join(schema) or "ANALYST JSON is valid")
    if schema:
        d.verdict = "REJECT"
        return d

    try:
        bars = normalize_candles(candles)
    except (ValueError, TypeError) as e:
        check("candles", False, str(e))
        d.verdict = "REJECT"
        return d

    lo, hi = min(b["l"] for b in bars), max(b["h"] for b in bars)
    tol = cfg.price_tolerance
    inside = lambda p: lo - tol <= p <= hi + tol  # noqa: E731

    # ORB must come from the supplied candles, never invented.
    orb = compute_orb(bars, plan["session"], cfg)
    d.computed_orb = orb
    if orb:
        ok = abs(orb["high"] - plan["orb_high"]) <= tol and abs(orb["low"] - plan["orb_low"]) <= tol
        check("orb_matches_candles", ok,
              f"candles give {orb['low']}–{orb['high']} ({orb['bars']} bars), "
              f"plan says {plan['orb_low']}–{plan['orb_high']}")
    else:
        ok = inside(plan["orb_high"]) and inside(plan["orb_low"])
        check("orb_inside_candles", ok,
              f"no timestamped ORB window found; plan ORB {plan['orb_low']}–{plan['orb_high']} "
              f"vs candle range {lo}–{hi}")

    # Daily loss limit applies to every plan, including flat ones.
    hit = realized_pnl_today <= -abs(cfg.daily_loss_limit)
    check("daily_loss_limit", not hit,
          f"realized P&L today {realized_pnl_today} vs limit -{abs(cfg.daily_loss_limit)}")

    if plan["direction"] == "flat":
        d.verdict = "REJECT" if d.reasons else "NO_TRADE"
        return d

    entry, stop, targets = plan["entry"], plan["stop"], plan["targets"]
    levels = {"entry": entry, "stop": stop, "target1": targets[0], "target2": targets[1]}
    outside = [f"{k}={v}" for k, v in levels.items() if not inside(v)]
    check("levels_inside_candles", not outside,
          f"candle range {lo}–{hi}" + (f"; outside: {', '.join(outside)}" if outside else ""))

    if plan["direction"] == "long":
        geo = stop < entry < targets[0] <= targets[1]
        want = "stop < entry < target1 <= target2"
    else:
        geo = stop > entry > targets[0] >= targets[1]
        want = "stop > entry > target1 >= target2"
    check("level_order", geo, f"{plan['direction']} needs {want}")

    risk = abs(entry - stop)
    if geo and risk > 0:
        rr = abs(targets[cfg.rr_target_index] - entry) / risk
        d.computed_rr = round(rr, 3)
        check("min_reward_risk", rr >= cfg.min_rr,
              f"computed R:R {rr:.2f} to target{cfg.rr_target_index + 1} vs minimum {cfg.min_rr}")
        gap = abs(rr - plan["reward_risk"])
        check("reward_risk_matches", gap <= cfg.rr_tolerance,
              f"plan reports {plan['reward_risk']}, levels give {rr:.2f}")

    conf = plan["confidence"]
    check("min_confidence", conf >= cfg.min_confidence,
          f"confidence {conf} vs minimum {cfg.min_confidence}")

    if cfg.skip_no_confluence:
        both_missing = (plan["volume_profile_confluence"] == "not_available"
                        and plan["whale_or_gamex_confluence"] == "not_available")
        skip = both_missing and conf < cfg.no_confluence_min_confidence
        check("no_confluence_low_confidence", not skip,
              ("volume profile and whale data both not_available" if both_missing
               else "at least one confluence source supplied")
              + (f"; confidence {conf} < {cfg.no_confluence_min_confidence}" if skip else ""))

    now_dt = _parse_time(now) if now is not None else None
    if now_dt and news_events:
        hits = [e for e in (_parse_time(x) for x in news_events)
                if e <= now_dt < e + timedelta(minutes=cfg.news_blackout_minutes)]
        check("news_blackout", not hits,
              f"within {cfg.news_blackout_minutes} min after high-impact USD data"
              if hits else "outside post-news blackout")

    d.verdict = "REJECT" if d.reasons else "APPROVE"
    return d


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="XAUUSD ORB RISK gate")
    ap.add_argument("plan", help="ANALYST plan JSON file")
    ap.add_argument("candles", help="candles JSON file (list of {t,o,h,l,c})")
    ap.add_argument("--pnl", type=float, default=0.0, help="realized P&L today")
    ap.add_argument("--config", help="RiskConfig JSON file")
    ap.add_argument("--now", help="current time, ISO 8601 (UTC if no offset)")
    ap.add_argument("--news", nargs="*", default=[], help="high-impact USD release times")
    a = ap.parse_args(argv)
    try:
        with open(a.plan) as f:
            plan = json.load(f)
        with open(a.candles) as f:
            candles = json.load(f)
        cfg = RiskConfig()
        if a.config:
            with open(a.config) as f:
                cfg = RiskConfig.from_dict(json.load(f))
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"verdict": "ERROR", "reasons": [str(e)]}))
        return 2
    d = evaluate(plan, candles, a.pnl, cfg, a.now, a.news)
    print(json.dumps(d.to_dict(), indent=2, default=str))
    return 1 if d.verdict == "REJECT" else 0


if __name__ == "__main__":
    sys.exit(main())
