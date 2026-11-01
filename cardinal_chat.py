"""CARDINAL voice/chat: answer the operator's question from the desk state the HUD sends.

The model sees only the snapshot the HUD sends (plan, RISK verdict, sessions, P&L, macro,
Jev) and a short history. It must not invent prices, it never places orders, and it
cannot approve anything: it can only suggest one of the HUD's own local actions.

Provider: CHAT_PROVIDER in .env (gemini | groq | grok), else the first one with a key,
in that order. Standard library only.
"""
from __future__ import annotations

import json
import os
import re

import keys
from analyst import _post

ACTIONS = ("none", "london_brief", "ny_brief", "status", "waiting", "run_analyst",
           "check_gate", "focus")

SYSTEM = """You are CARDINAL, chief of staff on an XAUUSD (gold) desk that trades the London and
New York opening-range breakouts. You are talking out loud to the operator, {name}.

Style: calm, concise, precise, a little dry British wit. Two or three short spoken sentences,
no markdown, no lists, no emoji. Say numbers the way a trader would say them.

Hard rules:
- Use only the DESK STATE below. Never invent prices, ranges, volume, whale or news data.
  If something is not in the state, say you don't have it.
- You never place orders, size positions or approve plans. RISK (the Python gate) has the
  final say; if RISK says REJECT, say so plainly and do not argue it away.
- Data marked sample is synthetic. Say so if you quote it.
- If the operator asks for something the desk can do, pick one action:
  london_brief, ny_brief, status, waiting, run_analyst (ask ANALYST for a new plan),
  check_gate (re-run the Python RISK gate), focus (toggle focus mode). Otherwise none.

Reply as JSON only: {{"reply": "<what you say out loud>", "action": "<one action>"}}

DESK STATE (UTC):
{state}"""


def pick_provider(preferred: str | None = None) -> str:
    order = [preferred or os.environ.get("CHAT_PROVIDER", "").strip().lower()] + ["gemini", "groq", "grok"]
    st = keys.status()
    for p in order:
        if p in ("gemini", "groq", "grok") and st.get(p):
            return p
    raise RuntimeError("No chat key set. Add GEMINI_API_KEY (or GROQ_API_KEY / XAI_API_KEY) to .env.")


def _clean_history(history) -> list[dict]:
    out = []
    for h in (history or [])[-8:]:
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and isinstance(h.get("text"), str):
            out.append({"role": h["role"], "text": h["text"][:1000]})
    while out and out[0]["role"] == "assistant":  # Gemini wants the user to speak first
        out.pop(0)
    return out


def chat(provider: str, system: str, turns: list[dict]) -> str:
    """turns: [{"role": "user"|"assistant", "text": str}], last one is the user's question."""
    if provider == "gemini":
        model = keys.get_model("gemini")
        out = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    {"x-goog-api-key": keys.get_key("gemini")}, {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "model" if t["role"] == "assistant" else "user",
                          "parts": [{"text": t["text"]}]} for t in turns],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
        })
        return out["candidates"][0]["content"]["parts"][0]["text"]
    url = {"groq": "https://api.groq.com/openai/v1/chat/completions",
           "grok": "https://api.x.ai/v1/chat/completions"}[provider]
    out = _post(url, {"Authorization": "Bearer " + keys.get_key(provider)}, {
        "model": keys.get_model(provider),
        "messages": [{"role": "system", "content": system}]
                    + [{"role": t["role"], "content": t["text"]} for t in turns],
        "response_format": {"type": "json_object"},
        "temperature": 0.4,
    })
    return out["choices"][0]["message"]["content"]


def parse_reply(text: str) -> dict:
    try:
        m = re.search(r"\{.*\}", text, re.S)
        j = json.loads(m.group(0) if m else text)
        reply, action = str(j.get("reply", "")).strip(), str(j.get("action", "none")).strip()
    except (ValueError, AttributeError):
        reply, action = text.strip(), "none"
    if action not in ACTIONS:
        action = "none"
    return {"reply": reply[:1200] or "I didn't get an answer back. Try again.", "action": action}


def ask(question: str, state: dict, history=None, provider: str | None = None, name: str = "Mazhar") -> dict:
    question = (question or "").strip()[:1000]
    if not question:
        raise ValueError("empty question")
    prov = pick_provider(provider)
    system = SYSTEM.format(name=str(name)[:40] or "Mazhar",
                           state=json.dumps(state or {}, default=str)[:12000])
    turns = _clean_history(history) + [{"role": "user", "text": question}]
    out = parse_reply(chat(prov, system, turns))
    out.update(provider=prov, model=keys.get_model(prov))
    return out
