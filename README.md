# XAUUSD Cardinal desk — London + New York ORB

CARDINAL routes work and asks for human approval, ANALYST writes the ORB plan as JSON, and RISK (`risk_gate.py`, pure Python, standard library only) vetoes it. Nothing here places orders or sizes positions.

ANALYST can be run by **Google Gemini** or **Grok (xAI)**, and **TypeSafe AI (Jev)** can give each plan a typed second opinion. Keys live only in a local `.env` file or in GitHub Actions secrets, never in the code.

| File | What it is |
|---|---|
| `prompts/cardinal.md` | CARDINAL system prompt + suggested first message |
| `prompts/analyst.md` | ANALYST system prompt and JSON schema |
| `risk_gate.py` | Schema validation (`validate_plan`) + RISK gate (`evaluate`) + CLI |
| `config.example.json` | Every gate threshold, with defaults |
| `examples/` | Synthetic plan + candles (not real prices) |
| `tests/` | `python3 -m unittest discover -s tests` (no keys needed) |
| `analyst.py` | Sends candles + the ANALYST prompt to Gemini or Grok, then runs the plan through RISK |
| `jev.py` | JEV REVIEW: TypeSafe AI checks the plan against the candles (bias, breakout quality, consistency, invented data). Advisory only |
| `keys.py` | Loads `GEMINI_API_KEY` / `XAI_API_KEY` / `TYPESAFE_API_KEY` from env or `.env`; `python3 keys.py` shows which are set |
| `.env.example` | Template for your keys (copy to `.env`) |
| `.github/workflows/desk.yml` | Runs tests on every push; manual ANALYST run using repo secrets |
| `hud.html` | Heads-up display: open in a browser, load or drop a plan, candles, config or bundle JSON. Mirrors the RISK gate; never fetches prices. |

## Add your API keys

**Never paste keys into code, issues, or chat. Never commit `.env`** (it is already in `.gitignore`).

### On your computer (`.env`)

1. Get keys: Gemini at https://aistudio.google.com/app/apikey, Grok at https://console.x.ai, TypeSafe at https://console.typesafe.ai/keys
2. In the repo folder: `cp .env.example .env` (Windows: `copy .env.example .env`)
3. Open `.env` and paste each key after the `=`:
   ```
   GEMINI_API_KEY=your-gemini-key
   XAI_API_KEY=your-grok-key
   TYPESAFE_API_KEY=your-typesafe-key
   ```
4. Check: `python3 keys.py` should say `key found` for each one you added.
5. Run ANALYST + RISK:
   ```
   python3 analyst.py examples/candles_london.json --session London --provider gemini
   python3 analyst.py examples/candles_london.json --session London --provider grok
   ```
   Add `--jev` to either command to get the TypeSafe second opinion, or review a saved plan on its own:
   `python3 jev.py examples/plan_long.json examples/candles_london.json`
   Model names can be changed in `.env` (`GEMINI_MODEL`, `GROK_MODEL`, `JEV_MODEL`).

### On GitHub (Actions secrets)

1. Repo > **Settings** > **Secrets and variables** > **Actions** > **New repository secret**
2. Add `GEMINI_API_KEY`, `XAI_API_KEY` and (optional) `TYPESAFE_API_KEY` (exact names).
3. **Actions** tab > **desk** > **Run workflow**, pick session and provider.

Secrets are masked in logs and are not passed to workflows from forks.

If a key ever leaks, revoke it in the provider's console and make a new one.

## How TypeSafe AI (Jev) fits

Gemini or Grok writes the plan. Jev does not write text; it answers fixed questions with probabilities:

| Question | Type | Use |
|---|---|---|
| `data_bias` | choice long/short/flat | Which way the candles actually broke the ORB |
| `breakout_quality` | score 0–3 | How clean the break is |
| `plan_consistent` | yes-probability | Levels and direction fit the data |
| `unsupported_claims` | yes-probability | Plan cites VP/whale/macro data that was not supplied |

If Jev disagrees, `jev_review.agrees` is false with the reasons in `flags`. CARDINAL should show this to you before approval. Jev never overrides RISK; the Python gate still decides.

## Run the gate

```
python3 risk_gate.py plan.json candles.json --pnl -120 --config config.json \
    [--now 2026-11-03T13:41:00Z --news 2026-11-03T13:30:00Z]
```

Candles are a JSON list of `{t, o, h, l, c}` (or `time/open/high/low/close`), times in UTC. Exit code 0 = APPROVE or NO_TRADE, 1 = REJECT, 2 = bad input.

## What RISK rejects

| Check | Rule (default) |
|---|---|
| `schema` | Plan must match the ANALYST JSON exactly (fields, enums, 0–1 confidence, two targets). Flat plans may use null entry/stop and empty targets. |
| `orb_matches_candles` | With timestamped candles, the gate recomputes the ORB (London 08:00–08:30, NY 13:30–14:00 GMT) and rejects a plan whose ORB differs by more than $0.05. Without timestamps it only checks the ORB lies inside the candle range. |
| `daily_loss_limit` | Realized P&L today at or below −500. Applies to flat plans too. |
| `levels_inside_candles` | Entry, stop and both targets must be within the supplied candles' low–high. |
| `level_order` | Long: stop < entry < T1 ≤ T2. Short: the mirror. |
| `min_reward_risk` | R:R measured to T1 must be ≥ 1.8. |
| `reward_risk_matches` | Reported `reward_risk` must be within 0.15 of what the levels give. |
| `min_confidence` | Confidence ≥ 0.55. |
| `no_confluence_low_confidence` | Optional (on by default): skip when volume profile and whale data are both `not_available` and confidence < 0.65. |
| `news_blackout` | Only if `--now` and `--news` are given: reject within 10 min after a high-impact USD release. |

Change thresholds by copying `config.example.json`, never mid-trade.
