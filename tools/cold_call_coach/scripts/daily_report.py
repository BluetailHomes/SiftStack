"""Daily cold-call report for one caller: today plus week to date.

Reads the graded per-call reports (reports/cold_call/*_{caller}.md, each with a
SCORES JSON footer), buckets them by Central-time day, and writes the numeric
tables. The narrative sections (what improved, fixes, record flags) are left
as markers for the grader to fill in.

    python daily_report.py                       # today (Central), week from 2026-10-26
    python daily_report.py --day 2026-10-27 --week-start 2026-10-26
"""
import argparse
import datetime as dt
import json
import re
from pathlib import Path

from pull_calls_datasift import ct_now, to_ct

ROOT = Path.cwd()
REPORTS = ROOT / "output" / "call_coaching" / "reports"
NARRATIVE = "_(grader: fill in)_"


def load(caller_slug):
    calls = []
    for f in sorted((REPORTS / "cold_call").glob(f"*_{caller_slug}.md")):
        t = f.read_text(encoding="utf-8")
        if "```json" not in t:
            continue
        j = json.loads(t.rsplit("```json", 1)[1].split("```")[0])
        m = re.search(r"Date/Time: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", t)
        if not m:
            continue
        j["ct"] = to_ct(m.group(1))
        calls.append(j)
    return calls


def _avg(xs):
    return f"{sum(xs) / len(xs):.2f}" if xs else "-"


def short_stats(calls):
    s = [c for c in calls if c["call_type"] == "short"]
    op = [c["opener"] for c in s if c.get("opener") is not None]
    cv = [c["conversion"] for c in s if c.get("conversion") is not None]
    att = sum(1 for c in s if c.get("conversion_attempted") and c.get("conversion") is not None)
    return {
        "graded": str(len(s)),
        "opener": _avg(op),
        "conv": f"{_avg(cv)} (n={len(cv)})",
        "att": f"{att} of {len(cv)}",
    }


def criteria(calls, prefix):
    crit = {}
    for c in calls:
        for k, v in (c.get("criteria") or {}).items():
            if v is not None and k.startswith(prefix):
                crit.setdefault(k, []).append(v)
    return crit


OPENER_NAMES = {
    "1.1": "Decision-maker confirmed first",
    "1.2": "Soft plans frame, specific property",
    "1.3": "Awkwardness named",
    "1.4": "Escalation discipline",
}


def render(name, day, week_start, today, wtd):
    full_t = [c for c in today if c["call_type"] == "full"]
    full_w = [c for c in wtd if c["call_type"] == "full"]
    out = [
        f"# Daily Call Report: {name}, {day:%a %Y-%m-%d}",
        "",
        f"Today: {len(today)} graded conversations ({len(full_t)} full, {len(today) - len(full_t)} short). "
        f"Week to date ({week_start:%m/%d}-{day:%m/%d}): {len(wtd)} ({len(full_w)} full, {len(wtd) - len(full_w)} short). "
        "Full and short calls use different scales; they are not ranked together.",
        "",
        "## Full calls (/100)",
        "",
    ]
    if full_w:
        out += ["| Call | Date | Total | Band | Note |", "|---|---|---|---|---|"]
        for c in sorted(full_w, key=lambda c: c["ct"]):
            mark = " (today)" if c["ct"].date() == day else ""
            out.append(f"| {c['call_id']} | {c['ct']:%m/%d}{mark} | {c['total']:.2f} | {c.get('band', '')} | {NARRATIVE} |")
    else:
        out.append("No full calls this week yet.")

    st, sw = short_stats(today), short_stats(wtd)
    out += [
        "", "## Short calls", "",
        "| Metric | Today | Week to date |", "|---|---|---|",
        f"| Short calls graded | {st['graded']} | {sw['graded']} |",
        f"| Opener average (/5) | {st['opener']} | {sw['opener']} |",
        f"| Conversion average (3.3, /5) | {st['conv']} | {sw['conv']} |",
        f"| Conversion attempted | {st['att']} | {sw['att']} |",
        "", "## Opener criteria (all graded calls)", "",
        "| Criterion | Today | Week to date |", "|---|---|---|",
    ]
    ct_, cw = criteria(today, "1."), criteria(wtd, "1.")
    for k, label in OPENER_NAMES.items():
        out.append(f"| {k} {label} | {_avg(ct_.get(k, []))} | {_avg(cw.get(k, []))} |")

    out += [
        "", "## Script adoption today", "",
        "- 1.3 softener (\"out of the blue\"): " + NARRATIVE,
        "- 1.2 plans frame (\"any plans for...\"): " + NARRATIVE,
        "- 3.3 one more question after the no: " + NARRATIVE,
        "- No \"thanks for calling\" on outbound: " + NARRATIVE,
        "- Callbacks pinned to a day and time: " + NARRATIVE,
        "", "## What improved", "", NARRATIVE,
        "", "## Top 3 fixes for tomorrow", "", NARRATIVE,
        "", "## Record flags", "", NARRATIVE, "",
    ]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="YYYY-MM-DD, Central time (default today)")
    ap.add_argument("--week-start", default="2026-10-26")
    ap.add_argument("--caller-slug", default="rahaf_gamea")
    ap.add_argument("--caller-name", default='Rahaf Gamea ("Britney")')
    a = ap.parse_args()
    day = dt.date.fromisoformat(a.day) if a.day else ct_now().date()
    week_start = dt.date.fromisoformat(a.week_start)

    calls = load(a.caller_slug)
    today = [c for c in calls if c["ct"].date() == day]
    wtd = [c for c in calls if week_start <= c["ct"].date() <= day]
    dest = REPORTS / f"daily_{a.caller_slug.split('_')[0]}_{day}.md"
    dest.write_text(render(a.caller_name, day, week_start, today, wtd), encoding="utf-8")
    print(f"{dest}: today {len(today)} graded, week to date {len(wtd)}")


if __name__ == "__main__":
    main()
