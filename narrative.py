"""
narrative.py — local LLM narrative generation via Ollama, shared by
fred_pull.py and fdic_pull.py.

Turns computed numbers into a couple of plain-English sentences so the
dashboard reads like an analyst's note instead of a spreadsheet. Runs
against a local Ollama server (free, no API key, no per-run cost) — the
right tool for something that fires on a schedule, not interactively.

Hard requirement: this must NEVER break a scheduled pull. If Ollama isn't
running, times out, or the model isn't pulled, every function here returns
None and the caller falls back to a deterministic rule-based sentence.
Narrative text is a nice-to-have; the numbers are the product.
"""

import os

import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
TIMEOUT_SECONDS = 90  # these scripts fire daily/weekly/monthly, so Ollama is
                       # almost always cold-loading the model (~15-20s alone)
                       # on top of generation — give it real room before we
                       # give up and fall back to the rule-based sentence.


def generate(prompt: str, model: str = DEFAULT_MODEL) -> str | None:
    """One-shot generation. Returns plain text, or None on any failure."""
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": model, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.3}},  # low temp: this is analysis, not fiction
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
        return text or None
    except (requests.RequestException, ValueError, KeyError) as exc:
        print(f"narrative.py: Ollama unavailable ({exc}) — using rule-based fallback")
        return None
