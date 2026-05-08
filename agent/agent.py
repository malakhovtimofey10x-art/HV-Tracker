#!/usr/bin/env python3
"""
Hantavirus tracker — autonomous data updater.

Runs on a 2-hour cron (see .github/workflows/update-data.yml).
Uses Claude with the web_search tool to find the latest outbreak news,
validates Claude's structured response, and writes data.json.

Required env: ANTHROPIC_API_KEY
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic

# ── Config ──────────────────────────────────────────────────────────
DATA_PATH = Path("data.json")
MODEL = "claude-sonnet-4-6"  # cheap, fast, plenty smart for this task
MAX_WEB_USES = 8             # cap on web_search calls per run
MAX_TOKENS = 8000

ALLOWED_STATUSES = {"confirmed", "suspected", "monitor", "origin"}

# ── System prompt: defines the schema rigidly ──────────────────────
SYSTEM = """You are a public-health surveillance agent maintaining a structured
JSON dataset for the M/V Hondius hantavirus outbreak (WHO event 2026-E000227).

Your job: search the web for the latest news (last 24-72 hours), then output
an UPDATED data.json reflecting the current state.

OUTPUT FORMAT — output ONLY a valid JSON object, no preamble, no markdown
fences, no commentary. The object MUST match this schema exactly:

{
  "event_id": "WHO/IHR EVENT 2026-E000227",
  "vessel": "M/V HONDIUS",
  "ticker": [string, ...],
  "sites": [
    {
      "id":         string,    // stable lowercase-hyphen identifier (e.g. "us-ca")
      "name":       string,    // display name in CAPS (e.g. "CALIFORNIA")
      "country":    string,    // region/country in CAPS
      "lat":        number,    // -90 to 90
      "lng":        number,    // -180 to 180
      "status":     string,    // one of: "confirmed" | "suspected" | "monitor" | "origin"
      "confirmed":  integer,   // >= 0
      "suspected":  integer,   // >= 0
      "deaths":     integer,   // >= 0
      "monitored":  integer,   // >= 0
      "desc":       string     // 1-2 sentence intelligence brief
    }
  ]
}

RULES:
1. Update case counts and descriptions when news reports new info.
2. Add new sites for newly affected countries/regions.
3. PRESERVE all existing sites (don't drop them just because they're not in
   today's news — outbreak sites stay in the dataset for the duration).
4. status meanings:
     confirmed = laboratory-confirmed cases at this site
     suspected = cases under investigation, not yet PCR-confirmed
     monitor   = contact tracing or asymptomatic monitoring only
     origin    = source/investigation location, no cases here
5. desc tone: terse, factual, intelligence-brief style. ~1-2 sentences.
6. ticker: 6-10 short ALL-CAPS scrolling headlines, each ~60-90 chars.
   Prefix with ▲ (alert), ► (status update), or // (info).
7. If the outbreak is reported as resolved or no longer active, still keep
   all historical sites and update their status/desc accordingly.

You will receive the current dataset for context. Search for what has changed,
then output the new full dataset."""


def call_claude(client: anthropic.Anthropic, current: dict) -> dict:
    """Make the API call and parse Claude's response into a dict."""
    prompt = (
        f"Current dataset (last updated "
        f"{current.get('updated_at', 'never')}):\n\n"
        f"```json\n{json.dumps(current, indent=2, ensure_ascii=False)}\n```\n\n"
        "Search the web for the latest hantavirus / M/V Hondius outbreak news, "
        "then output the updated full dataset as JSON."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": MAX_WEB_USES,
        }],
    )

    # Concatenate text blocks from final assistant message
    text = "".join(
        block.text for block in response.content
        if getattr(block, "type", None) == "text"
    ).strip()

    if not text:
        raise RuntimeError("Claude returned no text content.")

    # Strip markdown fences if Claude wrapped output in them
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1)

    # Sometimes the JSON is preceded by chatty preamble despite instructions —
    # find the outermost {...} block.
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)

    return json.loads(text)


def validate_and_clean(data: dict) -> dict:
    """Strict validation. Raises on any malformed field."""
    assert isinstance(data, dict), "top-level must be object"
    assert isinstance(data.get("sites"), list) and data["sites"], "sites must be non-empty array"

    seen_ids: set[str] = set()
    cleaned_sites = []
    for i, s in enumerate(data["sites"]):
        ctx = f"site #{i} ({s.get('id', '?')})"
        for k in ("id", "name", "country", "lat", "lng", "status",
                  "confirmed", "suspected", "deaths", "monitored", "desc"):
            assert k in s, f"{ctx}: missing field {k!r}"

        # Type coercion (Claude sometimes returns floats for ints, etc.)
        s["confirmed"] = int(s["confirmed"])
        s["suspected"] = int(s["suspected"])
        s["deaths"]    = int(s["deaths"])
        s["monitored"] = int(s["monitored"])
        s["lat"]       = float(s["lat"])
        s["lng"]       = float(s["lng"])

        assert -90  <= s["lat"] <=  90,  f"{ctx}: lat out of range"
        assert -180 <= s["lng"] <= 180, f"{ctx}: lng out of range"
        assert s["status"] in ALLOWED_STATUSES, f"{ctx}: bad status {s['status']!r}"
        assert s["id"] not in seen_ids, f"{ctx}: duplicate id"
        seen_ids.add(s["id"])

        cleaned_sites.append(s)

    data["sites"] = cleaned_sites

    # Default ticker if missing
    if not isinstance(data.get("ticker"), list):
        data["ticker"] = []

    # Compute totals server-side (don't trust Claude's arithmetic)
    data["totals"] = {
        "confirmed":          sum(s["confirmed"] for s in cleaned_sites),
        "suspected":          sum(s["suspected"] for s in cleaned_sites),
        "deaths":             sum(s["deaths"]    for s in cleaned_sites),
        "monitored":          sum(s["monitored"] for s in cleaned_sites),
        "countries_affected": len({s["country"]  for s in cleaned_sites}),
        "sites":              len(cleaned_sites),
    }

    # Stamp it
    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["event_id"] = data.get("event_id", "WHO/IHR EVENT 2026-E000227")
    data["vessel"]   = data.get("vessel", "M/V HONDIUS")

    return data


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    client = anthropic.Anthropic()

    # Load current state (or seed empty)
    if DATA_PATH.exists():
        current = json.loads(DATA_PATH.read_text())
    else:
        current = {"sites": [], "ticker": [], "updated_at": "never"}

    print(f"Loaded current state: {len(current.get('sites', []))} sites")

    try:
        new_data = call_claude(client, current)
    except (json.JSONDecodeError, RuntimeError) as e:
        print(f"ERROR: Claude response invalid: {e}", file=sys.stderr)
        return 1

    try:
        new_data = validate_and_clean(new_data)
    except AssertionError as e:
        print(f"ERROR: Validation failed: {e}", file=sys.stderr)
        return 1

    DATA_PATH.write_text(
        json.dumps(new_data, indent=2, ensure_ascii=False) + "\n"
    )

    t = new_data["totals"]
    print(
        f"✓ {DATA_PATH}: {t['sites']} sites, "
        f"{t['confirmed']} confirmed, {t['suspected']} suspected, "
        f"{t['deaths']} deaths, {t['countries_affected']} countries"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
