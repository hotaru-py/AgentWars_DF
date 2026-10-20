"""Triage accuracy on a labelled set (spec §2 U5, §13 P2).

    python -m dispatch.eval                       the configured LLM (needs GOOGLE_API_KEY)
    python -m dispatch.eval --provider jev        the optional Jev provider (needs TYPESAFE_API_KEY)
    python -m dispatch.eval --json                machine-readable report

Each report in dispatch/evals/incident_reports.jsonl goes through the real DecisionEngine (same
questions, calibration and gates as live triage, chain "<provider> -> human") on a throwaway
database. A report's expected outcome is its kind, or "rejected" for not_an_incident / not credible;
the machine's outcome is read the same way from its answers.

    kind accuracy       right kind, over the reports the provider answered
    outcome accuracy    right outcome (kind or rejected), over the reports it answered
    automation rate     share handled without a person (route act or review)
    wrong-auto rate     share handled without a person AND wrong: the number that matters most

Provider answers are cached in .eval_cache.json (git-ignored) by provider, model and text, so
re-running costs no API calls and a run cut short by quota resumes where it stopped; --no-cache asks
again. After STOP_AFTER failures in a row (quota, overload) the rest are listed as unanswered.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from dispatch.core.config import Settings
from dispatch.core.decisions import Answer, Route
from dispatch.decide import INCIDENT_TRIAGE, DecisionEngine
from dispatch.address.places import find as find_place

REPORTS = Path(__file__).parent / "evals" / "incident_reports.jsonl"
CACHE = Path(__file__).resolve().parent.parent / ".eval_cache.json"
_ANSWERS = TypeAdapter(dict[str, Answer])
STOP_AFTER = 3


def load_reports(path: Path | str = REPORTS) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


class Cached:
    """Wraps a provider: answers by (provider, model, text) from a JSON file, asking only on a miss."""

    def __init__(self, inner, path: Path | None):
        self.inner, self.path = inner, path
        self.name, self.capabilities = inner.name, getattr(inner, "capabilities", set())
        self.meta = getattr(inner, "meta", None)
        self.cache: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else {}
        self.hits = self.misses = 0

    def _key(self, state) -> str:
        model = getattr(self.meta, "model", None) or getattr(self.inner, "model", "")
        return f"{self.name}|{model}|{state['report']['text']}"

    async def decide(self, state, questions):
        key = self._key(state)
        if key in self.cache:
            self.hits += 1
            return _ANSWERS.validate_python(self.cache[key])
        answers = await self.inner.decide(state, questions)
        self.misses += 1
        self.cache[key] = {k: v.model_dump(mode="json") for k, v in answers.items()}
        if self.path:
            self.path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")
        return answers


def _outcome(kind: str | None, credible: bool | None) -> str | None:
    if kind is None:
        return None
    return "rejected" if kind == "not_an_incident" or credible is False else kind


async def evaluate(provider_name: str, provider, reports: list[dict], cache: Path | None = CACHE) -> dict:
    from dispatch.store import Store
    wrapped = Cached(provider, cache)
    with tempfile.TemporaryDirectory() as d:
        store = Store(f"sqlite:///{d}/eval.db")
        store.create_all()
        engine = DecisionEngine(store, {provider_name: wrapped},
                                Settings(deciders=f"incident_triage={provider_name},human", deciders_shadow=""))
        rows = []
        failures = 0
        for r in reports:
            if failures >= STOP_AFTER:
                rows.append({"text": r["text"], "lang": r.get("lang", "?"), "expected": _outcome(r["kind"], r["credible"]),
                             "kind": r["kind"], "got_kind": None, "got": None, "route": "skipped", "confidence": 0.0,
                             "error": f"not asked: the provider failed {STOP_AFTER} times in a row"})
                continue
            place = find_place(r["text"])
            state = {"report": {"text": r["text"], "source": "eval", "place_mentioned": place[0] if place else None,
                                "names_a_vehicle": r["kind"] == "breakdown"}}
            res = await engine.decide(INCIDENT_TRIAGE, f"eval-{len(rows)}", state)
            rec = res.record
            got_kind = rec.answers["kind"].choice if "kind" in rec.answers else None
            got_cred = rec.answers["credible"].value if "credible" in rec.answers else None
            rows.append({"text": r["text"], "lang": r.get("lang", "?"), "expected": _outcome(r["kind"], r["credible"]),
                         "kind": r["kind"], "got_kind": got_kind, "got": _outcome(got_kind, got_cred),
                         "route": rec.route.value, "confidence": round(rec.confidence, 3),
                         "error": next((t.get("detail") for t in res.trail if t.get("result") in ("ProviderUnavailable", "NoAnswer")), None)})
            failures = failures + 1 if rows[-1]["error"] and got_kind is None else 0
        store.engine.dispose()
    answered = [x for x in rows if x["got"] is not None]
    auto = [x for x in rows if x["route"] in (Route.act.value, Route.review.value)]
    asked = [x for x in rows if x["route"] != "skipped"]
    by_lang: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for x in answered:
        by_lang[x["lang"]][0] += x["got"] == x["expected"]
        by_lang[x["lang"]][1] += 1
    pct = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    return {
        "provider": provider_name, "model": getattr(getattr(provider, "meta", None), "model", None) or getattr(provider, "model", None),
        "reports": len(rows), "answered": len(answered),
        "kind_accuracy": pct(sum(x["got_kind"] == x["kind"] for x in answered), len(answered)),
        "outcome_accuracy": pct(sum(x["got"] == x["expected"] for x in answered), len(answered)),
        "automation_rate": pct(len(auto), len(asked)),
        "wrong_auto_rate": pct(sum(x["got"] != x["expected"] for x in auto), len(asked)),
        "by_language": {k: pct(a, b) for k, (a, b) in sorted(by_lang.items())},
        "cache": {"hits": wrapped.hits, "misses": wrapped.misses},
        "mistakes": [x for x in rows if x["got"] is not None and x["got"] != x["expected"]],
        "unanswered": [{"text": x["text"], "error": x["error"]} for x in rows if x["got"] is None],
    }


def _provider(name: str, settings: Settings):
    from dispatch.decide import default_providers
    providers = default_providers(settings)
    if name not in providers:
        raise SystemExit(f"provider {name!r} isn't configured (set its key in .env); available: {', '.join(providers)}")
    if name == "llm" and not settings.gemini_enabled:
        raise SystemExit("the llm provider needs GOOGLE_API_KEY in .env")
    return providers[name]


def main(argv: list[str]) -> int:
    from dispatch.core.config import load
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")          # Tamil / Hindi text on a Windows console
    ap = argparse.ArgumentParser(description="Triage accuracy on the labelled incident reports.")
    ap.add_argument("--provider", default="llm")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv[1:])
    settings = load()
    rep = asyncio.run(evaluate(args.provider, _provider(args.provider, settings), load_reports(),
                               None if args.no_cache else CACHE))
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0
    f = lambda v: "n/a" if v is None else f"{v * 100:.0f}%"  # noqa: E731
    print(f"{rep['provider']} ({rep['model']}): {rep['answered']}/{rep['reports']} answered "
          f"(cache: {rep['cache']['hits']} hits, {rep['cache']['misses']} asked)")
    print(f"  kind accuracy     {f(rep['kind_accuracy'])}")
    print(f"  outcome accuracy  {f(rep['outcome_accuracy'])}")
    print(f"  automation rate   {f(rep['automation_rate'])}   (handled without a person)")
    print(f"  wrong-auto rate   {f(rep['wrong_auto_rate'])}   (handled without a person, and wrong)")
    print("  by language       " + ", ".join(f"{k} {f(v)}" for k, v in rep["by_language"].items()))
    for m in rep["mistakes"]:
        print(f"  ✗ {m['text'][:70]!r}: expected {m['expected']}, got {m['got']} ({m['route']}, {m['confidence']})")
    for u in rep["unanswered"][:STOP_AFTER + 1]:
        print(f"  ? {u['text'][:60]!r}: {str(u['error'])[:110]}")
    if len(rep["unanswered"]) > STOP_AFTER + 1:
        print(f"  ... {len(rep['unanswered']) - STOP_AFTER - 1} more unanswered. Answers so far are cached: re-run later to resume.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
