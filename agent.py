#!/usr/bin/env python3
"""
Hantavirus tracker — dual-LLM autonomous data updater.

Pipeline:
  1. OpenAI GPT-4o (with built-in web search) — scours the internet for the
     latest hantavirus / M/V Hondius news and returns a structured research brief.
  2. Claude Sonnet — receives the research brief + current dataset and produces
     the validated, schema-compliant data.json update.

Why two models?
  • OpenAI's Responses API has native real-time web search built in, giving
    broad, high-recall coverage across news sources.
  • Claude is significantly more reliable at strict JSON schema adherence and
    long-structured-output tasks, so it handles the write step.
  • Cost: the expensive web-search I/O goes to OpenAI; Claude does one focused
    synthesis call with no search overhead.

Required env:
  ANTHROPIC_API_KEY
  OPENAI_API_KEY
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import openai

# ── Config ───────────────────────────────────────────────────────────
DATA_PATH = Path("data.json")

OPENAI_MODEL    = "gpt-4o"          # supports web_search_preview tool
ANTHROPIC_MODEL = "claude-sonnet-4-6"
MAX_TOKENS      = 8000

ALLOWED_STATUSES = {"confirmed", "suspected", "monitor", "origin"}

# ── Search queries sent to OpenAI ────────────────────────────────────
# Running multiple focused queries gives better recall than one broad one.
SEARCH_QUERIES = [
    "M/V Hondius hantavirus outbreak latest news 2026",
    "Hondius cruise ship hantavirus cases deaths update",
    "WHO hantavirus 2026-E000227 situation report",
    "hantavirus Andes strain outbreak repatriation passengers 2026",
]

# ── Prompts ───────────────────────────────────────────────────────────

OPENAI_SYSTEM = """You are an epidemiological intelligence analyst.
Your task: search the web for the very latest information (last 24-72 hours)
about the M/V Hondius hantavirus outbreak (WHO event 2026-E000227) and return
a structured research brief.

Your output must be a JSON object with this exact shape — no markdown, no prose:
{
  "searched_at": "<ISO-8601 UTC timestamp>",
  "headlines": [
    {
      "source": "<publication name>",
      "date":   "<date string as reported>",
      "url":    "<url if available, else null>",
      "summary": "<2-3 sentence factual summary of what is NEW in this article>"
    }
  ],
  "key_updates": [
    "<one concrete factual update per item, e.g. 'France: 1 new suspected case, hospitalised Bichat Paris 10 May'>"
  ],
  "site_deltas": [
    {
      "site_id":   "<matches existing site id or new id>",
      "field":     "<confirmed|suspected|deaths|monitored|status|desc>",
      "old_value": "<previous value or null if new site>",
      "new_value": "<updated value>",
      "source":    "<publication>"
    }
  ]
}

Be exhaustive — search broadly, cross-reference multiple sources, and capture
every new case, death, repatriation, or status change you can find.
Do NOT fabricate. If you find nothing new, return empty arrays."""

OPENAI_USER_TEMPLATE = """Search for the latest news on these topics:

{queries}

Current dataset snapshot (for context — do NOT echo this back, only report CHANGES):
Last updated: {updated_at}
Sites: {site_count} across {country_count} countries
Confirmed: {confirmed} | Suspected: {suspected} | Deaths: {deaths}

Return the research brief JSON now."""

# ─────────────────────────────────────────────────────────────────────

CLAUDE_SYSTEM = """You are a public-health data engineer maintaining a structured
JSON dataset for the M/V Hondius hantavirus outbreak (WHO event 2026-E000227).

You will receive:
  A) A research brief produced by a web-search agent (OpenAI GPT-4o)
  B) The current data.json

Your job: merge A into B and output the COMPLETE updated dataset.

OUTPUT FORMAT — output ONLY valid JSON, no preamble, no markdown fences:

{
  "event_id": "WHO/IHR EVENT 2026-E000227",
  "vessel": "M/V HONDIUS",
  "ticker": [string, ...],
  "sites": [
    {
      "id":        string,   // stable lowercase-hyphen id (e.g. "us-ca")
      "name":      string,   // display name ALL-CAPS
      "country":   string,   // region/country ALL-CAPS
      "lat":       number,   // -90 to 90
      "lng":       number,   // -180 to 180
      "status":    string,   // "confirmed"|"suspected"|"monitor"|"origin"
      "confirmed": integer,  // >= 0
      "suspected": integer,  // >= 0
      "deaths":    integer,  // >= 0
      "monitored": integer,  // >= 0
      "desc":      string    // 1-2 sentence intelligence brief
    }
  ]
}

RULES:
1. Apply every delta in the research brief that is supported by a named source.
2. Discard any delta that looks fabricated or unsourced.
3. PRESERVE all existing sites — never drop a site just because it's not in
   today's news. Outbreak sites stay for the duration.
4. status:
     confirmed = PCR-confirmed cases at this site
     suspected = under investigation, not yet confirmed
     monitor   = contact tracing / asymptomatic monitoring only
     origin    = source location, no cases here
5. desc: terse, factual, intelligence-brief style. 1-2 sentences max.
6. ticker: 6-10 ALL-CAPS headlines, 60-90 chars each.
   Prefix: ▲ alert  ► status update  // info
7. If no new information exists for a site, copy its existing data unchanged."""

CLAUDE_USER_TEMPLATE = """=== RESEARCH BRIEF (from OpenAI web search) ===
{research_brief}

=== CURRENT DATASET ===
{current_dataset}

Merge the research brief into the current dataset and output the complete
updated JSON now."""


# ── Step 1: OpenAI web research ───────────────────────────────────────

def run_openai_research(client: openai.OpenAI, current: dict) -> str:
    """
    Fire GPT-4o with the web_search_preview tool.
    Returns the raw research brief string (JSON from the model).
    """
    query_block = "\n".join(f"- {q}" for q in SEARCH_QUERIES)
    totals = current.get("totals", {})

    user_msg = OPENAI_USER_TEMPLATE.format(
        queries       = query_block,
        updated_at    = current.get("updated_at", "never"),
        site_count    = totals.get("sites", len(current.get("sites", []))),
        country_count = totals.get("countries_affected", "?"),
        confirmed     = totals.get("confirmed", "?"),
        suspected     = totals.get("suspected", "?"),
        deaths        = totals.get("deaths", "?"),
    )

    print("  [OpenAI] Running web research...")
    response = client.responses.create(
        model    = OPENAI_MODEL,
        tools    = [{"type": "web_search_preview"}],
        input    = [
            {"role": "system", "content": OPENAI_SYSTEM},
            {"role": "user",   "content": user_msg},
        ],
    )

    # Extract text output from the response
    text = ""
    for block in response.output:
        if getattr(block, "type", None) == "message":
            for part in block.content:
                if getattr(part, "type", None) == "output_text":
                    text += part.text

    text = text.strip()
    if not text:
        raise RuntimeError("OpenAI returned no text content.")

    print(f"  [OpenAI] Research brief received ({len(text)} chars)")
    return text


# ── Step 2: Claude synthesis ──────────────────────────────────────────

def run_claude_synthesis(client: anthropic.Anthropic,
                         research_brief: str,
                         current: dict) -> dict:
    """
    Send the research brief + current dataset to Claude.
    Returns the parsed updated dataset dict.
    """
    user_msg = CLAUDE_USER_TEMPLATE.format(
        research_brief  = research_brief,
        current_dataset = json.dumps(current, indent=2, ensure_ascii=False),
    )

    print("  [Claude] Synthesising dataset update...")
    response = client.messages.create(
        model      = ANTHROPIC_MODEL,
        max_tokens = MAX_TOKENS,
        system     = CLAUDE_SYSTEM,
        messages   = [{"role": "user", "content": user_msg}],
    )

    text = "".join(
        block.text for block in response.content
        if getattr(block, "type", None) == "text"
    ).strip()

    if not text:
        raise RuntimeError("Claude returned no text content.")

    # Strip markdown fences if present despite instructions
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1)

    # Safety net: find outermost {...} if preamble crept in
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)

    parsed = json.loads(text)
    print("  [Claude] Synthesis complete.")
    return parsed


# ── Validation ────────────────────────────────────────────────────────

def validate_and_clean(data: dict) -> dict:
    """Strict schema validation + computed totals. Raises AssertionError on bad data."""
    assert isinstance(data, dict), "top-level must be object"
    assert isinstance(data.get("sites"), list) and data["sites"], \
        "sites must be non-empty array"

    seen_ids: set[str] = set()
    cleaned: list[dict] = []

    for i, s in enumerate(data["sites"]):
        ctx = f"site #{i} ({s.get('id', '?')})"
        for k in ("id", "name", "country", "lat", "lng", "status",
                  "confirmed", "suspected", "deaths", "monitored", "desc"):
            assert k in s, f"{ctx}: missing field {k!r}"

        # Coerce types (models sometimes return floats for int fields)
        s["confirmed"] = int(s["confirmed"])
        s["suspected"] = int(s["suspected"])
        s["deaths"]    = int(s["deaths"])
        s["monitored"] = int(s["monitored"])
        s["lat"]       = float(s["lat"])
        s["lng"]       = float(s["lng"])

        assert -90  <= s["lat"] <=  90,  f"{ctx}: lat out of range"
        assert -180 <= s["lng"] <= 180,  f"{ctx}: lng out of range"
        assert s["status"] in ALLOWED_STATUSES, \
            f"{ctx}: invalid status {s['status']!r}"
        assert s["id"] not in seen_ids, f"{ctx}: duplicate id"
        seen_ids.add(s["id"])
        cleaned.append(s)

    data["sites"] = cleaned

    if not isinstance(data.get("ticker"), list):
        data["ticker"] = []

    # Compute totals ourselves — never trust model arithmetic
    data["totals"] = {
        "confirmed":          sum(s["confirmed"] for s in cleaned),
        "suspected":          sum(s["suspected"] for s in cleaned),
        "deaths":             sum(s["deaths"]    for s in cleaned),
        "monitored":          sum(s["monitored"] for s in cleaned),
        "countries_affected": len({s["country"]  for s in cleaned}),
        "sites":              len(cleaned),
    }

    data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["event_id"]   = data.get("event_id", "WHO/IHR EVENT 2026-E000227")
    data["vessel"]     = data.get("vessel",   "M/V HONDIUS")

    return data


# ── Entry point ───────────────────────────────────────────────────────

def main() -> int:
    missing = [k for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
               if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 2

    anthropic_client = anthropic.Anthropic()
    openai_client    = openai.OpenAI()

    # Load current state
    if DATA_PATH.exists():
        current = json.loads(DATA_PATH.read_text())
    else:
        current = {"sites": [], "ticker": [], "updated_at": "never"}

    print(f"Loaded: {len(current.get('sites', []))} sites, "
          f"last updated {current.get('updated_at', 'never')}")

    # ── Step 1: OpenAI web research ──
    try:
        research_brief = run_openai_research(openai_client, current)
    except Exception as e:
        print(f"ERROR: OpenAI research step failed: {e}", file=sys.stderr)
        return 1

    # ── Step 2: Claude synthesis ──
    try:
        new_data = run_claude_synthesis(anthropic_client, research_brief, current)
    except (json.JSONDecodeError, RuntimeError) as e:
        print(f"ERROR: Claude synthesis failed: {e}", file=sys.stderr)
        return 1

    # ── Validate ──
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
        f"✓ {DATA_PATH} written — "
        f"{t['sites']} sites | "
        f"{t['confirmed']} confirmed | "
        f"{t['suspected']} suspected | "
        f"{t['deaths']} deaths | "
        f"{t['countries_affected']} countries"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
