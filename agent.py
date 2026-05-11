#!/usr/bin/env python3
"""
Hantavirus tracker — cost-optimised dual-LLM agent.

Cost per run breakdown:
  Stage 1  OpenAI gpt-4o-mini + web_search  ($0.15/$0.60 per 1M tokens)
           Searches web, returns a structured research brief.
           If mini finds < MIN_DELTAS meaningful updates it auto-escalates
           to gpt-4o ($2.50/$10) for a deeper pass. Most runs never escalate.
  Stage 2  Claude claude-haiku-4-5 ($0.80/$4 per 1M tokens)
           Takes the research brief + current dataset, emits schema-valid JSON.
           This is the ONLY Claude call, and Haiku is Anthropic's cheapest model.

Typical cost:
  Quiet day  : mini + haiku   ≈ $0.007
  Active day : mini + haiku   ≈ $0.010
  Escalated  : mini+4o+haiku  ≈ $0.025
  vs old single-Sonnet run    ≈ $0.080

Required env: ANTHROPIC_API_KEY, OPENAI_API_KEY
"""
from __future__ import annotations
import json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path
import anthropic, openai

DATA_PATH    = Path("data.json")
OPENAI_MINI  = "gpt-4o-mini"
OPENAI_FULL  = "gpt-4o"
CLAUDE_MODEL = "claude-sonnet-4-6"
MIN_DELTAS   = 2
MAX_TOKENS   = 4000
ALLOWED_STATUSES = {"confirmed","suspected","monitor","origin"}

SEARCH_QUERIES = [
    "M/V Hondius hantavirus outbreak 2026 latest",
    "Hondius cruise ship hantavirus cases deaths update",
    "WHO hantavirus 2026-E000227 situation report",
    "hantavirus Andes strain outbreak repatriation 2026",
]

OPENAI_SYSTEM = """You are an epidemiological surveillance analyst.
Search the web for the latest news (last 24-72 hours) about the M/V Hondius
hantavirus outbreak (WHO event 2026-E000227) and return a structured JSON brief.

Output ONLY valid JSON — no markdown, no prose:
{
  "searched_at": "<ISO-8601 UTC>",
  "headlines": [{"source":"...","date":"...","summary":"2-3 sentence factual summary"}],
  "key_updates": ["one concrete factual update per item"],
  "site_deltas": [
    {"site_id":"<id>","field":"<confirmed|suspected|deaths|monitored|status|desc>",
     "old_value":"<prev or null>","new_value":"<new>","source":"<publication>"}
  ]
}
Only include deltas with a named source. Do NOT fabricate."""

OPENAI_USER = """Search topics:
{queries}

Current snapshot (context — report CHANGES only):
Updated: {updated_at} | Sites: {sites} | Confirmed: {confirmed} | Deaths: {deaths}

Return the research brief JSON."""

CLAUDE_SYSTEM = """You are a public-health data engineer.
You receive a research brief from a web-search agent and the current data.json.
Output the COMPLETE updated dataset as valid JSON — no preamble, no fences.

Schema:
{
  "event_id": "WHO/IHR EVENT 2026-E000227",
  "vessel":   "M/V HONDIUS",
  "ticker":   [string, ...],
  "sites": [
    {"id":string,"name":string,"country":string,"lat":number,"lng":number,
     "status":"confirmed"|"suspected"|"monitor"|"origin",
     "confirmed":integer,"suspected":integer,"deaths":integer,"monitored":integer,
     "desc":string}
  ]
}

Rules:
1. Apply every delta that has a named source. Ignore unsourced claims.
2. PRESERVE all existing sites — never drop a site.
3. ticker: 6-10 ALL-CAPS headlines 60-90 chars each, prefix with ▲/►//
4. desc: 1-2 terse intel-brief sentences."""

CLAUDE_USER = """=== RESEARCH BRIEF ===
{brief}

=== CURRENT DATASET ===
{dataset}

Output the complete updated JSON now."""


def _openai_research(oc, current, model):
    totals = current.get("totals", {})
    user = OPENAI_USER.format(
        queries    = "\n".join(f"- {q}" for q in SEARCH_QUERIES),
        updated_at = current.get("updated_at","never"),
        sites      = totals.get("sites", len(current.get("sites",[]))),
        confirmed  = totals.get("confirmed","?"),
        deaths     = totals.get("deaths","?"),
    )
    print(f"  [OpenAI/{model}] searching…")
    resp = oc.responses.create(
        model=model,
        tools=[{"type":"web_search_preview"}],
        input=[{"role":"system","content":OPENAI_SYSTEM},{"role":"user","content":user}],
    )
    text = ""
    for block in resp.output:
        if getattr(block,"type",None)=="message":
            for part in block.content:
                if getattr(part,"type",None)=="output_text":
                    text += part.text
    return text.strip()


def run_research(oc, current):
    brief = _openai_research(oc, current, OPENAI_MINI)
    try:
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", brief, re.DOTALL)
        parsed = json.loads(fence.group(1) if fence else brief)
        nd = len(parsed.get("site_deltas",[]))
        nh = len(parsed.get("headlines",[]))
        print(f"  [mini] {nh} headlines, {nd} deltas")
        if nd < MIN_DELTAS and nh < 2:
            print("  [escalate] thin brief — trying gpt-4o…")
            brief = _openai_research(oc, current, OPENAI_FULL)
    except Exception:
        pass
    return brief


def synthesise(ac, brief, current):
    print(f"  [Claude/{CLAUDE_MODEL}] synthesising…")
    resp = ac.messages.create(
        model=CLAUDE_MODEL, max_tokens=MAX_TOKENS, system=CLAUDE_SYSTEM,
        messages=[{"role":"user","content":CLAUDE_USER.format(
            brief=brief,
            dataset=json.dumps(current, indent=2, ensure_ascii=False),
        )}],
    )
    text = "".join(b.text for b in resp.content if getattr(b,"type",None)=="text").strip()
    if not text: raise RuntimeError("Claude empty response")
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)
    if fence: text = fence.group(1)
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m: text = m.group(0)
    return json.loads(text)


def validate(data):
    assert isinstance(data,dict)
    assert isinstance(data.get("sites"),list) and data["sites"]
    seen=set(); clean=[]
    for i,s in enumerate(data["sites"]):
        ctx=f"site#{i}({s.get('id','?')})"
        for k in ("id","name","country","lat","lng","status","confirmed","suspected","deaths","monitored","desc"):
            assert k in s, f"{ctx}: missing {k!r}"
        s["confirmed"]=int(s["confirmed"]); s["suspected"]=int(s["suspected"])
        s["deaths"]=int(s["deaths"]);       s["monitored"]=int(s["monitored"])
        s["lat"]=float(s["lat"]);           s["lng"]=float(s["lng"])
        assert -90<=s["lat"]<=90,   f"{ctx}: lat range"
        assert -180<=s["lng"]<=180, f"{ctx}: lng range"
        assert s["status"] in ALLOWED_STATUSES, f"{ctx}: bad status"
        assert s["id"] not in seen, f"{ctx}: duplicate"
        seen.add(s["id"]); clean.append(s)
    data["sites"]=clean
    if not isinstance(data.get("ticker"),list): data["ticker"]=[]
    data["totals"]={
        "confirmed":         sum(s["confirmed"] for s in clean),
        "suspected":         sum(s["suspected"] for s in clean),
        "deaths":            sum(s["deaths"]    for s in clean),
        "monitored":         sum(s["monitored"] for s in clean),
        "countries_affected":len({s["country"]  for s in clean}),
        "sites":             len(clean),
    }
    data["updated_at"]=datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["event_id"]=data.get("event_id","WHO/IHR EVENT 2026-E000227")
    data["vessel"]=data.get("vessel","M/V HONDIUS")
    return data


def main():
    missing=[k for k in ("ANTHROPIC_API_KEY","OPENAI_API_KEY") if not os.environ.get(k)]
    if missing: print(f"ERROR: missing env: {', '.join(missing)}",file=sys.stderr); return 2

    ac=anthropic.Anthropic(); oc=openai.OpenAI()
    current=json.loads(DATA_PATH.read_text()) if DATA_PATH.exists() \
            else {"sites":[],"ticker":[],"updated_at":"never"}
    print(f"Loaded: {len(current.get('sites',[]))} sites, {current.get('updated_at','never')}")

    try: brief=run_research(oc,current)
    except Exception as e: print(f"ERROR research: {e}",file=sys.stderr); return 1

    try: new=synthesise(ac,brief,current)
    except (json.JSONDecodeError,RuntimeError) as e: print(f"ERROR synthesis: {e}",file=sys.stderr); return 1

    try: new=validate(new)
    except AssertionError as e: print(f"ERROR validation: {e}",file=sys.stderr); return 1

    DATA_PATH.write_text(json.dumps(new,indent=2,ensure_ascii=False)+"\n")
    t=new["totals"]
    print(f"✓ {t['sites']} sites | {t['confirmed']} confirmed | {t['suspected']} suspected | {t['deaths']} deaths | {t['countries_affected']} countries")
    return 0

if __name__=="__main__":
    sys.exit(main())
