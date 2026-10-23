"""Plain-language briefing of the recommended plan.

The AI only writes words. Every number is computed by the simulator and planner
first, the AI is given those facts and nothing else, and its reply is rejected if
it contains any number that is not in the facts. Without an API key, without
internet, or after a rejection, the fixed template below is used instead, so the
demo never depends on the AI.

Settings (environment variables or a .env file in the project folder):
  GROQ_API_KEY   key from console.groq.com (free tier); no key = template only
  GROQ_MODEL     default openai/gpt-oss-20b
  KASKENTRA_AI   set to "off" to always use the template
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from app.verify import indian

ROOT = Path(__file__).resolve().parent.parent
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b"
TIMEOUT_S = 20
MAX_SENTENCES = 4  # a briefing is read aloud in a control room; longer than this is noise
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z₹\d])")
MARKDOWN = re.compile(r"[*_`#>]+|^\s*[-•]\s+", re.MULTILINE)


def load_env_file(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines from .env without overriding real environment variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()


def facts_from_plan(network, scenario, plan: dict) -> dict:
    """Everything the briefing may mention, all taken from the computed plan."""
    best, base = plan["best"], plan["baseline_impact"]
    v = plan["verification"]

    def facilities(impact):
        return [{"name": f["name"], "fails_at_hour": f["first_failed_hour"],
                 "hours_without_service": f["outage_hours"]}
                for f in impact["critical_facilities_down"]]

    return {
        "incident": {"cause": scenario.hazard, "severity": scenario.severity,
                     "failed": [network.assets[a].name for a in scenario.failed_assets],
                     "time_window_hours": scenario.horizon_hours,
                     "field_reports": len(scenario.reports)},
        "loss_measure": v["measure_label"],
        "no_action": {"loss": base[v["measure"]],
                      "critical_outage_hours": base["critical_facility_outage_hours"],
                      "critical_facilities": facilities(base),
                      "full_recovery_hour": base["recovery_hour"]},
        "recommended_plan": {
            "actions": [{"action": a["label"], "when": a["timing"], "cost_inr": a["cost"]}
                        for a in best["actions"]],
            "total_cost_inr": best["cost"], "budget_inr": plan["resources"]["budget"],
            "crews_used": best["crews_used"], "generators_used": best["generators_used"],
            "loss": best["impact"][v["measure"]],
            "critical_outage_hours": best["impact"]["critical_facility_outage_hours"],
            "critical_facilities": facilities(best["impact"]),
            "full_recovery_hour": best["impact"]["recovery_hour"],
            "loss_avoided_pct": best["avoided_unserved_pct"],
            "critical_outage_avoided_pct": best["avoided_critical_pct"],
            "score_out_of_100": best["score"], "combinations_checked": plan["plans_checked"],
            "equally_good_plans": [" + ".join(a["label"] for a in alt["actions"]) or "no action"
                                   for alt in plan["alternatives"] if alt["score"] == best["score"]]},
        "score_rule": "60% less loss, 30% less critical-facility outage, 10% lower cost",
        "score_independently_rechecked": v["all_match"],
    }


def template_lines(facts: dict) -> list[str]:
    """Short fixed sentences filled with the computed numbers (always available)."""
    inc, base, plan = facts["incident"], facts["no_action"], facts["recommended_plan"]

    def recovery(h):
        return f"hour {h}" if h is not None else f"after hour {inc['time_window_hours']}"

    def critical(items):
        if not items:
            return "no critical facility loses service"
        first = min(f["fails_at_hour"] for f in items)
        names = ", ".join(f["name"] for f in items)
        return f"{names} {'loses' if len(items) == 1 else 'lose'} service from hour {first}"

    lines = [f"{inc['cause'].capitalize()} ({inc['severity']}) at {', '.join(inc['failed'])}.",
             f"No action: {critical(base['critical_facilities'])}; everything back at "
             f"{recovery(base['full_recovery_hour'])}."]
    if plan["actions"]:
        acts = "; ".join(a["action"] for a in plan["actions"])
        lines.append(f"Plan (₹{indian(plan['total_cost_inr'])}): {acts}.")
        same = ({f["name"] for f in plan["critical_facilities"]}
                == {f["name"] for f in base["critical_facilities"]} and plan["critical_facilities"])
        after = (f"the same facilities lose service from hour "
                 f"{min(f['fails_at_hour'] for f in plan['critical_facilities'])}" if same
                 else critical(plan["critical_facilities"]))
        lines.append(f"With the plan: {after}; everything back at "
                     f"{recovery(plan['full_recovery_hour'])}; {plan['loss_avoided_pct']}% less "
                     f"{facts['loss_measure']}.")
        if plan["equally_good_plans"]:
            # name only the actions that differ from the chosen plan
            chosen = {a["action"] for a in plan["actions"]}
            swaps = [" + ".join(x for x in alt.split(" + ") if x not in chosen) or "no action"
                     for alt in plan["equally_good_plans"]]
            lines.append(f"Equally good by the score: {'; '.join(swaps)} instead.")
    else:
        lines.append("No affordable action improves on doing nothing.")
    lines.append("Estimates from a rule-based simulation; the duty officer approves.")
    return lines


def template_summary(facts: dict) -> str:
    return " ".join(template_lines(facts))


def tidy_ai_text(text: str) -> str:
    """Plain prose only: strip markdown, collapse whitespace, keep the first few sentences."""
    text = MARKDOWN.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    sentences = SENTENCE_END.split(text)
    return " ".join(sentences[:MAX_SENTENCES]).strip()


def allowed_numbers(facts: dict) -> set[str]:
    """Numbers of the results themselves. The score rule's weights (60/30/10) are context
    only, so "60%" in the AI text must come from a result, not from the rule."""
    results = {k: v for k, v in facts.items() if k != "score_rule"}
    return {n.replace(",", "") for n in NUMBER.findall(json.dumps(results, ensure_ascii=False))}


def unsupported_numbers(text: str, facts: dict) -> list[str]:
    """Numbers in the AI text that do not appear anywhere in the computed facts."""
    allowed = allowed_numbers(facts)
    found = [n.rstrip(".,").replace(",", "") for n in NUMBER.findall(text)]
    return sorted({n for n in found if n and n not in allowed})


SYSTEM_PROMPT = (
    "You write a spoken briefing for a city disaster control room in India. "
    "Use ONLY the facts in the JSON you are given. Do not calculate anything new, do not "
    "round, and do not add any number that is not in the JSON. Do not invent places, "
    "causes or advice. Write exactly 3 short plain sentences, at most 60 words in total, "
    "no headings, no bullet points, no markdown, no preamble. Sentence 1: what failed and "
    "what happens with no action (recovery hour). Sentence 2: what the recommended plan "
    "does, its cost, and the new recovery hour. Sentence 3: the percentage of loss the plan "
    "avoids, and that these are estimates for the duty officer to approve. Do not mention "
    "other or equally good plans; the screen lists those separately.")


def call_groq(facts: dict, api_key: str, model: str) -> str:
    """One chat-completions request to Groq. Raises on any network or API error."""
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
        "temperature": 0.2,
        "max_completion_tokens": 400,
    }
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "low"
        body["include_reasoning"] = False
    request = urllib.request.Request(
        GROQ_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "User-Agent": "kaskentra"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        data = json.loads(response.read().decode("utf-8"))
    return (data["choices"][0]["message"].get("content") or "").strip()


def summarise(facts: dict) -> dict:
    """AI briefing when possible and safe; otherwise the template.

    `lines` is the briefing split into sentences for display; `text` is the same joined.
    """
    lines = template_lines(facts)
    template = {"text": " ".join(lines), "lines": lines, "source": "template"}
    key = os.environ.get("GROQ_API_KEY", "").strip()
    model = os.environ.get("GROQ_MODEL", "").strip() or DEFAULT_MODEL
    if os.environ.get("KASKENTRA_AI", "").lower() == "off":
        return {**template, "note": "AI switched off (KASKENTRA_AI=off)."}
    if not key:
        return {**template, "note": "No GROQ_API_KEY set, so the fixed template was used."}
    try:
        text = tidy_ai_text(call_groq(facts, key, model))
    except urllib.error.HTTPError as e:
        return {**template, "note": f"Groq returned HTTP {e.code}, so the fixed template was used."}
    except Exception as e:  # no internet, timeout, unexpected reply
        return {**template, "note": f"Groq could not be reached ({type(e).__name__}), "
                                     f"so the fixed template was used."}
    if not text:
        return {**template, "note": "Groq returned an empty reply, so the fixed template was used."}
    bad = unsupported_numbers(text, facts)
    if bad:
        return {**template, "rejected_ai_text": text,
                "note": f"AI text rejected: it contained {', '.join(bad)}, which is not in the "
                        f"computed results. The fixed template was used."}
    return {"text": text, "lines": SENTENCE_END.split(text), "source": "ai", "model": model,
            "note": f"Written by {model} on Groq from the computed results; every number in it "
                    f"was checked against them."}
