"""Lab route planner.

Give a START and END. Claude, as the domain expert, proposes candidate
process sequences from its own knowledge, including the equipment and
consumables every step needs. Each sequence is then compared with the
lab inventory in Neo4j (equipment availability, consumable stock).
Only the sequences the lab can run are printed and saved in Neo4j.

    python route_planner.py "print powder" "tensile test specimen"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

import anthropic
from neo4j import GraphDatabase
from pydantic import BaseModel, Field

MODEL = "claude-opus-5-5"


# --------------------------------------------------------------------------
# 1. LLM expert: propose candidate sequences from its own knowledge
# --------------------------------------------------------------------------

class ConsumableNeed(BaseModel):
    name: str
    qty: float = Field(description="Amount used by this step, in the lab's unit when known")
    unit: str


class Step(BaseModel):
    process: str = Field(description="Name of the process step")
    equipment: list[str] = Field(description="Every piece of equipment this step needs")
    consumables: list[ConsumableNeed] = Field(description="Consumables this step uses up")


class Candidate(BaseModel):
    steps: list[Step] = Field(description="Ordered steps from START to END")
    rationale: str = Field(description="One or two sentences on why this route makes sense")
    basis: str = Field(description="Standards, common practice or literature this route is based on")


class Proposal(BaseModel):
    candidates: list[Candidate]


SYSTEM_PROMPT = """You are an expert in materials science, processing and testing. Given a \
START and an END, propose several distinct, technically sound process sequences that get \
from START to END, drawing on your own knowledge of standard practice, standards (e.g. ASTM, \
ISO) and the literature.

Propose routes on technical merit. Do not limit yourself to what the lab owns: a separate \
check compares your routes with the lab inventory afterwards.

For every step, list all equipment it needs and the consumables it uses up, with quantities \
for a single run. You are also given the lab's inventory list, but only for naming: when the \
lab has an item that does the job, use the lab's exact name for it; otherwise give a clear \
generic name. Make the routes genuinely different (different processes or orderings), not \
near-duplicates."""


def propose_sequences(client: anthropic.Anthropic, start: str, end: str,
                      inventory: dict, n: int) -> list[Candidate]:
    inv_text = (
        "Equipment: " + ", ".join(e["name"] for e in inventory["equipment"]) + "\n"
        "Consumables: " + ", ".join(f"{c['name']} ({c['unit']})" for c in inventory["consumables"])
    )
    user = (
        f"Lab inventory names (for naming only):\n{inv_text}\n\n"
        f"START: {start}\nEND: {end}\n\n"
        f"Propose up to {n} candidate process sequences from START to END."
    )
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if the request is declined, the API retries on a fallback model
        output_config={"effort": "high"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_format=Proposal,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("The model declined to propose routes for this request.")
    if response.parsed_output is None:
        raise RuntimeError(f"Could not parse the model's answer (stop_reason={response.stop_reason}).")
    return response.parsed_output.candidates


# --------------------------------------------------------------------------
# 2. Neo4j: lab inventory and the comparison
# --------------------------------------------------------------------------

INVENTORY_QUERY = """
CALL () { MATCH (e:Equipment) RETURN collect({name: e.name, available: coalesce(e.available, false)}) AS equipment }
CALL () { MATCH (c:Consumable) RETURN collect({name: c.name, stock: coalesce(c.stock, 0),
                                               unit: coalesce(c.unit, '')}) AS consumables }
RETURN equipment, consumables
"""

# Look up every item a candidate needs. Items the lab doesn't have come back with found = false.
LOOKUP_QUERY = """
CALL () {
  UNWIND $equipment AS want
  OPTIONAL MATCH (e:Equipment) WHERE toLower(e.name) = toLower(want)
  RETURN collect({want: want, found: e IS NOT NULL, name: e.name,
                  available: coalesce(e.available, false)}) AS equipment
}
CALL () {
  UNWIND $consumables AS want
  OPTIONAL MATCH (c:Consumable) WHERE toLower(c.name) = toLower(want)
  RETURN collect({want: want, found: c IS NOT NULL, name: c.name,
                  stock: coalesce(c.stock, 0), unit: coalesce(c.unit, '')}) AS consumables
}
RETURN equipment, consumables
"""

SAVE_QUERY = """
CREATE (q:RouteQuery {start: $start, end: $end, createdAt: datetime()})
WITH q
UNWIND range(0, size($routes) - 1) AS r
WITH q, r, $routes[r] AS route
CREATE (q)-[:FOUND {rank: r + 1}]->(rt:Route {summary: route.summary, rationale: route.rationale,
                                              basis: route.basis})
WITH rt, route
UNWIND range(0, size(route.steps) - 1) AS s
WITH rt, s, route.steps[s] AS step
CREATE (rt)-[:STEP {order: s + 1}]->(st:Step {order: s + 1, process: step.process})
WITH st, step
CALL (st, step) {
  UNWIND step.equipment AS eq
  MATCH (e:Equipment) WHERE toLower(e.name) = toLower(eq)
  CREATE (st)-[:USES]->(e)
}
CALL (st, step) {
  UNWIND step.consumables AS c
  MATCH (x:Consumable) WHERE toLower(x.name) = toLower(c.name)
  CREATE (st)-[:CONSUMES {qty: c.qty}]->(x)
}
"""


def validate(session, cand: Candidate) -> list[str]:
    """Compare a candidate with the lab. An empty list means the lab can run it."""
    if not cand.steps:
        return ["empty sequence"]
    eq_names = sorted({e for s in cand.steps for e in s.equipment}, key=str.lower)
    totals: dict[str, float] = defaultdict(float)
    for s in cand.steps:
        for c in s.consumables:
            totals[c.name.lower()] += c.qty
    cons_names = sorted({c.name for s in cand.steps for c in s.consumables}, key=str.lower)

    rec = session.run(LOOKUP_QUERY, equipment=eq_names, consumables=cons_names).single()
    problems: list[str] = []
    for e in rec["equipment"]:
        if not e["found"]:
            problems.append(f"equipment '{e['want']}': the lab doesn't have it")
        elif not e["available"]:
            problems.append(f"equipment '{e['name']}': not available right now")
    for c in rec["consumables"]:
        need = totals[c["want"].lower()]
        if not c["found"]:
            problems.append(f"consumable '{c['want']}': the lab doesn't have it")
        elif need > c["stock"]:
            problems.append(f"consumable '{c['name']}': need {need:g} {c['unit']}, stock is {c['stock']:g}")
    return problems


def summary(cand: Candidate) -> str:
    return "  ->  ".join(s.process for s in cand.steps)


# --------------------------------------------------------------------------
# 3. Glue
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("start", help="what you start from, e.g. 'print powder'")
    ap.add_argument("end", help="what you want to end with, e.g. 'tensile test specimen'")
    ap.add_argument("-n", "--num", type=int, default=5, help="how many sequences to ask the LLM for")
    ap.add_argument("--candidates", help="JSON file of candidate sequences to check instead of asking the LLM")
    ap.add_argument("--show-rejected", action="store_true", help="also print rejected sequences and why")
    ap.add_argument("--details", action="store_true", help="print equipment and consumables for each step")
    ap.add_argument("--no-save", action="store_true", help="don't save the valid routes in Neo4j")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args()

    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "labpassword")),
    )
    with driver, driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as session:
        inventory = session.run(INVENTORY_QUERY).single().data()
        if not inventory["equipment"] and not inventory["consumables"]:
            print("The lab inventory in Neo4j is empty. Load it first, e.g. with seed.cypher.", file=sys.stderr)
            return 2

        # 1. candidates from the expert (or from a file)
        if args.candidates:
            with open(args.candidates) as fh:
                candidates = [Candidate(**c) for c in json.load(fh)]
        else:
            candidates = propose_sequences(anthropic.Anthropic(), args.start, args.end, inventory, args.num)

        # 2. compare each with the lab
        valid, rejected = [], []
        for c in candidates:
            problems = validate(session, c)
            (rejected if problems else valid).append((c, problems))

        # 3. save the valid ones in Neo4j
        if valid and not args.no_save:
            session.run(SAVE_QUERY, start=args.start, end=args.end, routes=[
                {**c.model_dump(), "summary": summary(c)} for c, _ in valid
            ]).consume()

    if args.json:
        print(json.dumps({
            "start": args.start, "end": args.end,
            "valid": [c.model_dump() for c, _ in valid],
            "rejected": [{**c.model_dump(), "problems": p} for c, p in rejected],
        }, indent=2))
        return 0 if valid else 1

    def show(c: Candidate, marker: str) -> None:
        print(f"  {marker} {summary(c)}")
        print(f"      why:   {c.rationale}")
        print(f"      basis: {c.basis}")
        if args.details:
            for i, s in enumerate(c.steps, 1):
                cons = ", ".join(f"{x.name} {x.qty:g} {x.unit}" for x in s.consumables) or "-"
                print(f"      {i}. {s.process}: equipment {', '.join(s.equipment) or '-'}; consumables {cons}")

    print(f"\n{args.start}  ->  {args.end}")
    print(f"{len(candidates)} proposed, {len(valid)} the lab can run now\n")
    for i, (c, _) in enumerate(valid, 1):
        show(c, f"[{i}]")
    if not valid:
        print("  None of the proposed sequences can be run with what the lab has right now.")
    if args.show_rejected and rejected:
        print("\nRejected:")
        for c, problems in rejected:
            show(c, "x")
            for p in problems:
                print(f"      - {p}")
    if valid and not args.no_save:
        print("\nSaved in Neo4j. View them in the Neo4j Browser with:")
        print("  MATCH (q:RouteQuery)-[f:FOUND]->(r:Route)-[s:STEP]->(st:Step)-[u]->(x) RETURN q, f, r, s, st, u, x")
    print()
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
