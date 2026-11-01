import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from risk_gate import RiskConfig, evaluate, validate_plan  # noqa: E402

# Synthetic London session: 07:30–09:55 GMT, 5-minute bars. Not real prices.
CANDLES = []
for i in range(30):
    m = 7 * 60 + 30 + 5 * i
    t = f"2026-11-03T{m // 60:02d}:{m % 60:02d}:00Z"
    if 8 * 60 <= m < 8 * 60 + 30:  # ORB window: 2650–2660
        h, l = 2660.0, 2650.0
    elif m < 8 * 60:
        h, l = 2656.0, 2645.0
    else:  # post-ORB drift up to 2690
        h, l = 2660.0 + (i - 12) * 1.7, 2655.0
    CANDLES.append({"t": t, "o": l, "h": h, "l": l, "c": h})

PLAN = {
    "symbol": "XAUUSD", "session": "London", "orb_high": 2660.0, "orb_low": 2650.0,
    "direction": "long", "entry": 2661.0, "stop": 2655.0, "targets": [2673.0, 2680.0],
    "confidence": 0.7, "reward_risk": 2.0,
    "volume_profile_confluence": "moderate", "whale_or_gamex_confluence": "not_available",
    "key_levels": ["ORB high 2660", "VAH 2662"], "invalidation": "15m close back below 2655",
    "reasoning": "Break of ORB high into LVN above VAH.",
}


def plan(**kw):
    p = copy.deepcopy(PLAN)
    p.update(kw)
    return p


def failed(d):
    return {c["check"] for c in d.checks if not c["ok"]}


class Schema(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_plan(PLAN), [])

    def test_missing_and_extra(self):
        p = plan(size_lots=1.0)
        del p["invalidation"]
        errs = validate_plan(p)
        self.assertTrue(any("missing field: invalidation" in e for e in errs))
        self.assertTrue(any("unexpected" in e for e in errs))

    def test_bad_enums(self):
        errs = validate_plan(plan(session="Asia", volume_profile_confluence="none",
                                  whale_or_gamex_confluence="bullish", direction="buy"))
        self.assertEqual(len(errs), 4)

    def test_bad_numbers(self):
        self.assertTrue(validate_plan(plan(confidence=1.4)))
        self.assertTrue(validate_plan(plan(targets=[2673.0])))
        self.assertTrue(validate_plan(plan(entry="2661")))
        self.assertTrue(validate_plan(plan(orb_high=2640.0)))
        self.assertTrue(validate_plan(plan(confidence=True)))

    def test_flat_allows_nulls(self):
        self.assertEqual(validate_plan(plan(direction="flat", entry=None, stop=None,
                                            targets=[], reward_risk=0)), [])
        self.assertTrue(validate_plan(plan(entry=None)))  # not when long


class Gate(unittest.TestCase):
    def test_approve(self):
        d = evaluate(PLAN, CANDLES)
        self.assertEqual(d.verdict, "APPROVE", d.reasons)
        self.assertEqual(d.computed_rr, 2.0)
        self.assertEqual(d.computed_orb["high"], 2660.0)

    def test_low_rr(self):
        d = evaluate(plan(targets=[2670.0, 2680.0], reward_risk=1.5), CANDLES)
        self.assertEqual(failed(d), {"min_reward_risk"})

    def test_rr_misreported(self):
        d = evaluate(plan(reward_risk=3.0), CANDLES)
        self.assertEqual(failed(d), {"reward_risk_matches"})

    def test_low_confidence(self):
        d = evaluate(plan(confidence=0.4), CANDLES)
        self.assertIn("min_confidence", failed(d))

    def test_daily_loss_limit(self):
        d = evaluate(PLAN, CANDLES, realized_pnl_today=-500)
        self.assertEqual(failed(d), {"daily_loss_limit"})
        self.assertEqual(evaluate(PLAN, CANDLES, -499.99).verdict, "APPROVE")

    def test_daily_loss_blocks_flat_too(self):
        p = plan(direction="flat", entry=None, stop=None, targets=[], reward_risk=0)
        self.assertEqual(evaluate(p, CANDLES).verdict, "NO_TRADE")
        self.assertEqual(evaluate(p, CANDLES, -600).verdict, "REJECT")

    def test_levels_outside_candles(self):
        d = evaluate(plan(targets=[2673.0, 2720.0]), CANDLES)
        self.assertEqual(failed(d), {"levels_inside_candles"})

    def test_invented_orb(self):
        d = evaluate(plan(orb_high=2662.0), CANDLES)
        self.assertEqual(failed(d), {"orb_matches_candles"})

    def test_orb_without_timestamps_uses_range(self):
        bare = [{"high": c["h"], "low": c["l"]} for c in CANDLES]
        self.assertEqual(evaluate(PLAN, bare).verdict, "APPROVE")
        d = evaluate(plan(orb_low=2600.0), bare)
        self.assertIn("orb_inside_candles", failed(d))

    def test_level_order(self):
        d = evaluate(plan(direction="short"), CANDLES)
        self.assertIn("level_order", failed(d))

    def test_short_approve(self):
        p = plan(direction="short", entry=2655.0, stop=2659.0, targets=[2647.0, 2646.0],
                 reward_risk=2.0)
        self.assertEqual(evaluate(p, CANDLES).verdict, "APPROVE")

    def test_no_confluence_skip(self):
        p = plan(volume_profile_confluence="not_available", confidence=0.6)
        self.assertEqual(failed(evaluate(p, CANDLES)), {"no_confluence_low_confidence"})
        self.assertEqual(evaluate(plan(volume_profile_confluence="not_available",
                                       confidence=0.7), CANDLES).verdict, "APPROVE")
        off = RiskConfig(skip_no_confluence=False)
        self.assertEqual(evaluate(p, CANDLES, cfg=off).verdict, "APPROVE")

    def test_news_blackout(self):
        news = ["2026-11-03T13:30:00Z"]
        self.assertIn("news_blackout",
                      failed(evaluate(PLAN, CANDLES, now="2026-11-03T13:36:00Z", news_events=news)))
        self.assertEqual(evaluate(PLAN, CANDLES, now="2026-11-03T13:41:00Z",
                                  news_events=news).verdict, "APPROVE")

    def test_bad_schema_short_circuits(self):
        d = evaluate({"symbol": "XAUUSD"}, CANDLES)
        self.assertEqual(d.verdict, "REJECT")
        self.assertEqual(len(d.checks), 1)

    def test_ny_window(self):
        ny = [{"t": "2026-11-03T13:30:00Z", "h": 2700.0, "l": 2690.0},
              {"t": "2026-11-03T13:55:00Z", "h": 2702.0, "l": 2691.0},
              {"t": "2026-11-03T14:00:00Z", "h": 2730.0, "l": 2700.0}]
        p = plan(session="NewYork", orb_high=2702.0, orb_low=2690.0, entry=2703.0,
                 stop=2697.0, targets=[2715.0, 2725.0], reward_risk=2.0)
        d = evaluate(p, ny)
        self.assertEqual(d.verdict, "APPROVE", d.reasons)
        self.assertEqual(d.computed_orb["bars"], 2)


if __name__ == "__main__":
    unittest.main()
