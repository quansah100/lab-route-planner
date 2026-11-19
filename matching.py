"""Translate what you type into your lab's names, by operational meaning.

Every lab noun gets an operational profile from Neo4j: which equipment takes it
in, which makes it, what comes before and after it in your lab, and which
literature names and processes it is linked to. An AI model (free local model
by default) checks every word you typed, decides what it says about the
object (material, form, condition, stage, purpose), and picks the lab noun
whose role in the process fits best, with a reason and a confidence.

If no AI model is available, it falls back to matching words.
"""

from __future__ import annotations

import sys
from typing import Literal

from pydantic import BaseModel, Field, create_model

from common import lucene_query
from llm import EngineError, ask, check_local

# --------------------------------------------------------------------------
# Operational profiles of the lab's nouns
# --------------------------------------------------------------------------

PROFILES_QUERY = """
MATCH (n:Noun)
RETURN n.name AS name,
  [(e:Equipment)-[:ACCEPTS]->(n) | e.name] AS taken_in_by,
  [(e:Equipment)-[:PRODUCES]->(n) | e.name] AS made_by,
  [(n)<-[:ACCEPTS]-(:Equipment)-[:PRODUCES]->(m:Noun) WHERE m <> n | m.name] AS next,
  [(n)<-[:PRODUCES]-(:Equipment)-[:ACCEPTS]->(m:Noun) WHERE m <> n | m.name] AS before,
  [(n)-[:SAME_AS]->(l:LitNoun) | l.name] AS literature_names,
  [(n)-[:SAME_AS]->(:LitNoun)-[:INPUT_OF]->(t:Transition) | t.process][..40] AS lit_goes_into,
  [(n)-[:SAME_AS]->(:LitNoun)<-[:OUTPUTS]-(t:Transition) | t.process][..40] AS lit_comes_from
ORDER BY name
"""


def _top(items: list[str], k: int = 4) -> list[str]:
    counts: dict[str, int] = {}
    for x in items:
        if x:
            counts[x.lower()] = counts.get(x.lower(), 0) + 1
    return [x for x, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:k]]


def noun_profiles(session) -> dict[str, str]:
    profiles = {}
    for r in session.run(PROFILES_QUERY):
        parts = []
        if r["made_by"]:
            parts.append(f"made by: {', '.join(sorted(set(r['made_by'])))}")
        else:
            parts.append("not made by any lab equipment (a raw input or something received)")
        if r["taken_in_by"]:
            parts.append(f"taken in by: {', '.join(sorted(set(r['taken_in_by'])))}")
        else:
            parts.append("not taken in by any lab equipment (a final product or result)")
        if r["before"]:
            parts.append(f"comes after: {', '.join(sorted(set(r['before'])))}")
        if r["next"]:
            parts.append(f"leads to: {', '.join(sorted(set(r['next'])))}")
        if r["literature_names"]:
            parts.append(f"papers call it: {', '.join(r['literature_names'])}")
        if r["lit_goes_into"]:
            parts.append(f"in papers it goes into: {', '.join(_top(r['lit_goes_into']))}")
        if r["lit_comes_from"]:
            parts.append(f"in papers it comes out of: {', '.join(_top(r['lit_comes_from']))}")
        profiles[r["name"]] = "; ".join(parts)
    return profiles


# --------------------------------------------------------------------------
# AI matching (checks every word, judges operational fit)
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You match what a researcher typed to the names used in their lab, by \
OPERATIONAL MEANING, not by shared words.

You get the researcher's START and END (each with an optional description) and the lab's \
nouns, each with its operational profile: what makes it, what takes it in, what comes \
before and after it in the lab, and what the papers call it.

For START and END separately:
1. Check every word typed (and in the description). Say what each word tells you about the \
object: its material, its form (powder, bar, plate, wire, specimen, image, data ...), its \
condition or processing stage (as-received, printed, heat treated, machined, tested ...), \
or its purpose. Abbreviations and trade names count (e.g. Ti64 = Ti-6Al-4V).
2. Pick the lab noun whose ROLE in the process fits best: the same kind of object at the \
same stage. Specifics never decide the match: alloy or material (Ti64, 316L, alumina), \
supplier, particle size, dimensions or standard numbers are details of the same object. \
"Ti64 powder", "316L powder" and "gas-atomized powder 15-45 um" are all simply POWDER. \
The FORM (powder, bar, plate, part, specimen, data ...) and the STAGE decide. If only the \
form is given and no stage, a START is the raw, as-received form and an END is the most \
finished form of it.
   - START is what goes into the first operation, so prefer nouns the lab takes in.
   - END is what the last operation gives, so prefer nouns the lab makes, and it should come \
after START in the lab's flow.
3. Give a confidence: "high" if one lab noun clearly plays that role; "medium" if it fits \
but another is plausible; "low" if it is a guess; "no match" if no lab noun can play that \
role. Name a runner-up when one is plausible, else "none".
Keep each reason to one or two sentences, in plain language."""


class WordCheck(BaseModel):
    word: str = Field(description="One word or short term the researcher typed")
    meaning: str = Field(description="What it says about the object: material, form, stage, purpose, ...")


def _schema(names: list[str]) -> type[BaseModel]:
    noun = Literal[tuple(names)]
    runner = Literal[tuple(names + ["none"])]
    pick = create_model(
        "Pick",
        words=(list[WordCheck], ...),
        best=(noun, ...),
        runner_up=(runner, ...),
        confidence=(Literal["high", "medium", "low", "no match"], ...),
        reason=(str, ...),
    )
    return create_model("Resolution", start=(pick, ...), end=(pick, ...))


def ai_match(engine: str, start: str, start_desc: str, end: str, end_desc: str,
             profiles: dict[str, str]):
    lines = [f"- {name}: {p}" for name, p in profiles.items()]
    user = ("LAB NOUNS AND THEIR OPERATIONAL PROFILES:\n" + "\n".join(lines) + "\n\n"
            f"START typed: {start}" + (f"\nSTART description: {start_desc}" if start_desc else "") + "\n"
            f"END typed: {end}" + (f"\nEND description: {end_desc}" if end_desc else ""))
    return ask(engine, SYSTEM_PROMPT, user, _schema(list(profiles)), effort="low")


# --------------------------------------------------------------------------
# Word matching (fallback when no AI model is available)
# --------------------------------------------------------------------------

LAB_NOUN_SEARCH = """
CALL db.index.fulltext.queryNodes('lab_noun_name', $q) YIELD node, score
RETURN node.name AS name, score ORDER BY score DESC LIMIT 8
"""

LAB_NOUN_VIA_LIT = """
CALL db.index.fulltext.queryNodes('lit_noun_name', $q) YIELD node, score
MATCH (n:Noun)-[:SAME_AS]->(node)
RETURN n.name AS name, max(score) AS score
"""

NOUN_ROLE = """
MATCH (n:Noun {name: $name})
RETURN EXISTS { (:Equipment)-[:ACCEPTS]->(n) } AS accepted,
       EXISTS { (:Equipment)-[:PRODUCES]->(n) } AS produced
"""


def word_match(session, typed: str, desc: str, role: str) -> list[str]:
    """Lab nouns ranked by shared words, best first (may be empty)."""
    scores: dict[str, float] = {}
    for text in ([typed], [typed, desc]) if desc else ([typed],):
        q = lucene_query(*text)
        if not q:
            continue
        for r in session.run(LAB_NOUN_SEARCH, q=q):
            scores[r["name"]] = max(scores.get(r["name"], 0), r["score"])
        for r in session.run(LAB_NOUN_VIA_LIT, q=q):
            scores[r["name"]] = scores.get(r["name"], 0) + 0.5 * r["score"]
        if scores:
            break
    for name in scores:
        role_row = session.run(NOUN_ROLE, name=name).single()
        if role == "start" and role_row["accepted"] and not role_row["produced"]:
            scores[name] += 0.01
        if role == "end" and role_row["produced"] and not role_row["accepted"]:
            scores[name] += 0.01
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if not ranked:
        return []
    best_score = ranked[0][1]
    return [ranked[0][0]] + [n for n, s in ranked[1:4] if s >= 0.85 * best_score]


# --------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------

def _ask_choice(typed: str, role: str, options: list[str]) -> str:
    print(f"\nWhich of your lab's names is the {role} '{typed}'?")
    for i, n in enumerate(options, 1):
        print(f"  {i}. {n}")
    ans = input("Number (Enter = 1, q = quit): ").strip().lower()
    if ans == "q":
        sys.exit("Stopped.")
    if ans.isdigit() and 1 <= int(ans) <= len(options):
        return options[int(ans) - 1]
    return options[0]


def resolve_endpoints(session, start: str, start_desc: str, end: str, end_desc: str,
                      engine: str, ask_user: bool, show_words: bool = True):
    """Returns (lab start or None, lab end or None, notes to print)."""
    exact = {r["n"].lower(): r["n"] for r in session.run("MATCH (n:Noun) RETURN n.name AS n")}
    result = {"start": exact.get(start.lower()), "end": exact.get(end.lower())}
    if result["start"] and result["end"]:
        return result["start"], result["end"], []

    notes: list[str] = []
    typed = {"start": (start, start_desc), "end": (end, end_desc)}

    # 1. operational matching with an AI model
    picks = None
    if engine != "words":
        try:
            if engine == "local":
                check_local()
            picks = ai_match(engine, start, start_desc, end, end_desc, noun_profiles(session))
        except EngineError as e:
            notes.append(f"matched by words only: {e}")

    for role in ("start", "end"):
        if result[role]:
            continue
        text, desc = typed[role]
        if picks is not None:
            p = getattr(picks, role)
            if show_words and p.words:
                notes.append(f"{role} '{text}', word by word: "
                             + "; ".join(f"{w.word} = {w.meaning}" for w in p.words))
            if p.confidence == "no match":
                notes.append(f"{role} '{text}': no lab noun plays that role. {p.reason}")
                continue
            options = [p.best] + ([p.runner_up] if p.runner_up not in ("none", p.best) else [])
            choice = p.best
            if p.confidence == "low" and ask_user:
                print(f"\nNot sure which lab name '{text}' is. {p.reason}")
                choice = _ask_choice(text, role, options)
                notes.append(f"{role}: using your lab's '{choice}' for '{text}' (your choice)")
            else:
                extra = f"; runner-up: {options[1]}" if len(options) > 1 else ""
                notes.append(f"{role}: using your lab's '{choice}' for '{text}' "
                             f"({p.confidence} confidence{extra}). {p.reason}")
            result[role] = choice
        else:
            options = word_match(session, text, desc, role)
            if not options:
                continue
            choice = options[0]
            if len(options) > 1 and ask_user:
                choice = _ask_choice(text, role, options)
            note = f"{role}: using your lab's '{choice}' for '{text}' (matched by words)"
            if len(options) > 1 and not ask_user:
                note += f"; other close matches: {', '.join(o for o in options if o != choice)}"
            notes.append(note)
            result[role] = choice
    return result["start"], result["end"], notes
