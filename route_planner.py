"""Lab route planner.

Give a START and END material. Claude (the "expert") proposes candidate
process sequences; each one is checked in Neo4j against what the lab
actually has (processes, equipment availability, consumable stock).
Only the valid sequences are printed and saved back to Neo4j.

    python route_planner.py "Alumina powder" "Coated disc"
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
# 1. LLM expert: propose candidate sequences
# --------------------------------------------------------------------------

class Candidate(BaseModel):
    steps: list[str] = Field(description="Ordered process names, first step consumes START, last produces END")
    rationale: str = Field(description="One or two sentences on why this route makes sense")


class Proposal(BaseModel):
    candidates: list[Candidate]


SYSTEM_PROMPT = """You are a materials-processing expert helping a lab plan how to turn one \
material into another. You propose several distinct, technically sound process routes.

Use the exact process names from the lab's catalog wherever one fits. If the best route \
needs a process the catalog does not list, you may still propose it with a short, plain \
name; it will be flagged as missing from the lab, which is useful for the lab to know.
Order steps so each step's output is the next step's input. Propose different routes \
(different processes or orderings), not near-duplicates."""


def propose_sequences(client: anthropic.Anthropic, start: str, end: str,
                      catalog: list[dict], n: int) -> list[Candidate]:
    catalog_text = "\n".join(
        f"- {p['name']}: {', '.join(p['inputs']) or '?'} -> {', '.join(p['outputs']) or '?'}"
        + (f"  ({p['description']})" if p.get("description") else "")
        for p in catalog
    )
    user = (
        f"Lab process catalog (name: input -> output):\n{catalog_text}\n\n"
        f"START material: {start}\nEND material: {end}\n\n"
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
# 2. Neo4j: lab catalog + validation
# --------------------------------------------------------------------------

CATALOG_QUERY = """
MATCH (p:Process)
OPTIONAL MATCH (p)-[:INPUT]->(i:Material)
OPTIONAL MATCH (p)-[:OUTPUT]->(o:Material)
RETURN p.name AS name, p.description AS description,
       collect(DISTINCT i.name) AS inputs, collect(DISTINCT o.name) AS outputs
ORDER BY name
"""

# Facts about every step of one sequence, in order. Steps not in the lab come back with found=false.
STEP_FACTS_QUERY = """
UNWIND range(0, size($steps) - 1) AS idx
WITH idx, $steps[idx] AS stepName
OPTIONAL MATCH (p:Process) WHERE toLower(p.name) = toLower(stepName)
OPTIONAL MATCH (p)-[:INPUT]->(i:Material)
OPTIONAL MATCH (p)-[:OUTPUT]->(o:Material)
OPTIONAL MATCH (p)-[:NEEDS_EQUIPMENT]->(e:Equipment)
OPTIONAL MATCH (p)-[n:NEEDS_CONSUMABLE]->(c:Consumable)
RETURN idx, stepName, p IS NOT NULL AS found, p.name AS name,
       collect(DISTINCT i.name) AS inputs,
       collect(DISTINCT o.name) AS outputs,
       collect(DISTINCT CASE WHEN e IS NULL THEN NULL
               ELSE {name: e.name, available: coalesce(e.available, false)} END) AS equipment,
       collect(DISTINCT CASE WHEN c IS NULL THEN NULL
               ELSE {name: c.name, qty: coalesce(n.qty, 1), stock: coalesce(c.stock, 0),
                     unit: coalesce(c.unit, '')} END) AS consumables
ORDER BY idx
"""

SAVE_QUERY = """
CREATE (q:RouteQuery {start: $start, end: $end, createdAt: datetime()})
WITH q
UNWIND range(0, size($routes) - 1) AS r
WITH q, r, $routes[r] AS route
CREATE (q)-[:FOUND {rank: r + 1}]->(rt:Route {steps: route.steps, rationale: route.rationale})
WITH rt, route
UNWIND range(0, size(route.steps) - 1) AS s
MATCH (p:Process {name: route.steps[s]})
CREATE (rt)-[:STEP {order: s + 1}]->(p)
"""


def material_exists(session, name: str) -> bool:
    rec = session.run("MATCH (m:Material) WHERE toLower(m.name) = toLower($n) RETURN m.name AS n", n=name).single()
    return rec is not None


def validate(session, start: str, end: str, steps: list[str]) -> list[str]:
    """Return a list of problems. An empty list means the lab can run this sequence."""
    if not steps:
        return ["empty sequence"]
    facts = [r.data() for r in session.run(STEP_FACTS_QUERY, steps=steps)]
    problems: list[str] = []
    needed: dict[str, dict] = defaultdict(lambda: {"qty": 0})
    current = {start.lower()}  # material(s) available going into the next step

    for f in facts:
        label = f"step {f['idx'] + 1} '{f['stepName']}'"
        if not f["found"]:
            problems.append(f"{label}: the lab has no such process")
            current = set()  # unknown output; can't check continuity after this
            continue
        inputs = {m.lower() for m in f["inputs"]}
        if current and inputs and not (inputs & current):
            problems.append(f"{label}: needs {', '.join(f['inputs'])} but previous step gives "
                            f"{', '.join(sorted(current))}")
        for e in f["equipment"]:
            if not e["available"]:
                problems.append(f"{label}: equipment '{e['name']}' is not available")
        for c in f["consumables"]:
            agg = needed[c["name"]]
            agg["qty"] += c["qty"]
            agg["stock"], agg["unit"] = c["stock"], c["unit"]
        current = {m.lower() for m in f["outputs"]}

    if current and end.lower() not in current:
        problems.append(f"route ends with {', '.join(sorted(current))}, not '{end}'")
    for name, c in needed.items():
        if c["qty"] > c["stock"]:
            problems.append(f"consumable '{name}': need {c['qty']} {c['unit']}, stock is {c['stock']}")
    return problems


# --------------------------------------------------------------------------
# 3. Glue
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("start", help="start material, e.g. 'Alumina powder'")
    ap.add_argument("end", help="end material, e.g. 'Coated disc'")
    ap.add_argument("-n", "--num", type=int, default=5, help="how many sequences to ask the LLM for")
    ap.add_argument("--candidates", help="JSON file of candidate sequences to check instead of asking the LLM")
    ap.add_argument("--show-rejected", action="store_true", help="also print rejected sequences and why")
    ap.add_argument("--no-save", action="store_true", help="don't write valid routes back to Neo4j")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args()

    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "labpassword")),
    )
    with driver, driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as session:
        for m in (args.start, args.end):
            if not material_exists(session, m):
                print(f"warning: material '{m}' is not in the graph", file=sys.stderr)

        # 1. candidates
        if args.candidates:
            with open(args.candidates) as fh:
                candidates = [Candidate(**c) for c in json.load(fh)]
        else:
            catalog = [r.data() for r in session.run(CATALOG_QUERY)]
            candidates = propose_sequences(anthropic.Anthropic(), args.start, args.end, catalog, args.num)

        # 2. validate each against the lab
        valid, rejected = [], []
        for c in candidates:
            problems = validate(session, args.start, args.end, c.steps)
            (rejected if problems else valid).append({**c.model_dump(), "problems": problems})

        # 3. save valid routes in Neo4j
        if valid and not args.no_save:
            session.run(SAVE_QUERY, start=args.start, end=args.end,
                        routes=[{"steps": v["steps"], "rationale": v["rationale"]} for v in valid]).consume()

    if args.json:
        print(json.dumps({"start": args.start, "end": args.end, "valid": valid,
                          "rejected": rejected}, indent=2))
        return 0 if valid else 1

    print(f"\n{args.start}  ->  {args.end}")
    print(f"{len(candidates)} proposed, {len(valid)} valid with current lab resources\n")
    for i, v in enumerate(valid, 1):
        print(f"  [{i}] " + "  ->  ".join(v["steps"]))
        print(f"      {v['rationale']}")
    if not valid:
        print("  No proposed sequence can be run with what the lab has right now.")
    if args.show_rejected and rejected:
        print("\nRejected:")
        for r in rejected:
            print("  x " + "  ->  ".join(r["steps"]))
            for p in r["problems"]:
                print(f"      - {p}")
    print()
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
