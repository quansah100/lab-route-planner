"""Lab route planner.

Give a START and an END. Claude, as the domain expert, proposes candidate
sequences from its own knowledge: each step is a piece of equipment and the
noun (material, part, specimen, ...) it produces. Neo4j then checks every
sequence against the lab. A route is valid when:

  * every step's equipment is available AND accessible, and
  * the noun each step produces is accepted by the next step's equipment
    (the first equipment accepts START, the last one produces END).

Only valid routes are printed and saved in Neo4j.

    python route_planner.py "print powder" "tensile test specimen"
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import anthropic
from neo4j import GraphDatabase
from pydantic import BaseModel, Field

MODEL = "claude-opus-5-5"


# --------------------------------------------------------------------------
# 1. LLM expert: propose candidate sequences from its own knowledge
# --------------------------------------------------------------------------

class Step(BaseModel):
    process: str = Field(description="Short description of what this step does")
    equipment: str = Field(description="The single piece of equipment that performs this step")
    output: str = Field(description="The noun this step produces, which the next step takes in")


class Candidate(BaseModel):
    steps: list[Step] = Field(description="Ordered steps; the first takes START, the last produces END")
    rationale: str = Field(description="One or two sentences on why this route makes sense")
    basis: str = Field(description="Standards, common practice or literature this route is based on")


class Proposal(BaseModel):
    candidates: list[Candidate]


SYSTEM_PROMPT = """You are an expert in materials science, processing and testing. Given a \
START and an END, propose several distinct, technically sound sequences that get from START \
to END, drawing on your own knowledge of standard practice, standards (e.g. ASTM, ISO) and \
the literature.

Each step is performed by exactly one piece of equipment and produces one noun (a material, \
part, specimen or result). That noun is what the next step's equipment takes in. The first \
step takes START; the last step must produce END.

Propose routes on technical merit. Do not limit yourself to what the lab owns: a separate \
check compares your routes with the lab afterwards. You are also given the lab's equipment \
and the nouns it uses, but only for naming: when the lab has equipment that does the job, \
use its exact name, and when a noun matches one of the lab's nouns, use that exact name. \
Otherwise use a clear generic name. Make the routes genuinely different, not near-duplicates."""


def propose_sequences(client: anthropic.Anthropic, start: str, end: str,
                      lab: list[dict], n: int) -> list[Candidate]:
    lab_text = "\n".join(
        f"- {e['name']}: accepts {', '.join(e['accepts']) or '?'}; produces {', '.join(e['produces']) or '?'}"
        for e in lab
    )
    user = (
        f"Lab equipment and nouns (for naming only):\n{lab_text}\n\n"
        f"START: {start}\nEND: {end}\n\n"
        f"Propose up to {n} candidate sequences from START to END."
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
# 2. Neo4j: the lab, and the check
# --------------------------------------------------------------------------

LAB_QUERY = """
MATCH (e:Equipment)
OPTIONAL MATCH (e)-[:ACCEPTS]->(a:Noun)
OPTIONAL MATCH (e)-[:PRODUCES]->(p:Noun)
RETURN e.name AS name, collect(DISTINCT a.name) AS accepts, collect(DISTINCT p.name) AS produces
ORDER BY name
"""

# Facts about each step's equipment, in step order. Unknown equipment comes back with found = false.
STEP_QUERY = """
UNWIND range(0, size($equipment) - 1) AS idx
WITH idx, $equipment[idx] AS want
OPTIONAL MATCH (e:Equipment) WHERE toLower(e.name) = toLower(want)
OPTIONAL MATCH (e)-[:ACCEPTS]->(a:Noun)
OPTIONAL MATCH (e)-[:PRODUCES]->(p:Noun)
RETURN idx, want, e IS NOT NULL AS found, e.name AS name,
       e.available AS available, e.accessible AS accessible,
       collect(DISTINCT toLower(a.name)) AS accepts,
       collect(DISTINCT toLower(p.name)) AS produces
ORDER BY idx
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
MATCH (e:Equipment) WHERE toLower(e.name) = toLower(step.equipment)
MATCH (i:Noun) WHERE toLower(i.name) = toLower(step.input)
MATCH (o:Noun) WHERE toLower(o.name) = toLower(step.output)
CREATE (rt)-[:STEP {order: s + 1}]->(st:Step {order: s + 1, process: step.process})
CREATE (st)-[:TAKES]->(i)
CREATE (st)-[:USES]->(e)
CREATE (st)-[:GIVES]->(o)
"""


def validate(session, start: str, end: str, cand: Candidate) -> list[str]:
    """Check one route against the lab. An empty list means the route is valid."""
    if not cand.steps:
        return ["empty sequence"]
    facts = [r.data() for r in session.run(STEP_QUERY, equipment=[s.equipment for s in cand.steps])]
    problems: list[str] = []
    incoming = start  # the noun arriving at the current step

    for step, f in zip(cand.steps, facts):
        label = f"step {f['idx'] + 1} ({step.equipment})"
        if not f["found"]:
            problems.append(f"{label}: the lab doesn't have this equipment")
        else:
            if f["available"] is not True:
                problems.append(f"{label}: not available")
            if f["accessible"] is not True:
                problems.append(f"{label}: not accessible")
            if incoming.lower() not in f["accepts"]:
                problems.append(f"{label}: does not accept '{incoming}'")
            if step.output.lower() not in f["produces"]:
                problems.append(f"{label}: does not produce '{step.output}'")
        incoming = step.output

    if cand.steps[-1].output.lower() != end.lower():
        problems.append(f"route ends with '{cand.steps[-1].output}', not '{end}'")
    return problems


def summary(start: str, cand: Candidate) -> str:
    parts = [start] + [f"[{s.equipment}] -> {s.output}" for s in cand.steps]
    return " -> ".join(parts)


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
    ap.add_argument("--no-save", action="store_true", help="don't save the valid routes in Neo4j")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args()

    driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "labpassword")),
    )
    with driver, driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as session:
        lab = [r.data() for r in session.run(LAB_QUERY)]
        if not any(e["accepts"] or e["produces"] for e in lab):
            print("No equipment with ACCEPTS/PRODUCES links in Neo4j. Load it first, e.g. with seed.cypher.",
                  file=sys.stderr)
            return 2

        # 1. candidates from the expert (or from a file)
        if args.candidates:
            with open(args.candidates) as fh:
                candidates = [Candidate(**c) for c in json.load(fh)]
        else:
            candidates = propose_sequences(anthropic.Anthropic(), args.start, args.end, lab, args.num)

        # 2. check each against the lab
        valid, rejected = [], []
        for c in candidates:
            problems = validate(session, args.start, args.end, c)
            (rejected if problems else valid).append((c, problems))

        # 3. save the valid ones in Neo4j
        if valid and not args.no_save:
            routes = []
            for c, _ in valid:
                inputs = [args.start] + [s.output for s in c.steps[:-1]]
                routes.append({
                    "summary": summary(args.start, c), "rationale": c.rationale, "basis": c.basis,
                    "steps": [{**s.model_dump(), "input": i} for s, i in zip(c.steps, inputs)],
                })
            session.run(SAVE_QUERY, start=args.start, end=args.end, routes=routes).consume()

    if args.json:
        print(json.dumps({
            "start": args.start, "end": args.end,
            "valid": [c.model_dump() for c, _ in valid],
            "rejected": [{**c.model_dump(), "problems": p} for c, p in rejected],
        }, indent=2))
        return 0 if valid else 1

    def show(c: Candidate, marker: str) -> None:
        print(f"  {marker} {args.start}")
        for i, s in enumerate(c.steps, 1):
            print(f"        {i}. [{s.equipment}] {s.process}  ->  {s.output}")
        print(f"      why:   {c.rationale}")
        print(f"      basis: {c.basis}")

    print(f"\n{args.start}  ->  {args.end}")
    print(f"{len(candidates)} proposed, {len(valid)} valid in this lab\n")
    for i, (c, _) in enumerate(valid, 1):
        show(c, f"[{i}]")
        print()
    if not valid:
        print("  None of the proposed sequences can be run in this lab right now.\n")
    if args.show_rejected and rejected:
        print("Rejected:")
        for c, problems in rejected:
            show(c, "x")
            for p in problems:
                print(f"      - {p}")
            print()
    if valid and not args.no_save:
        print("Saved in Neo4j. View them in the Neo4j Browser with:")
        print("  MATCH (q:RouteQuery)-[f:FOUND]->(r:Route)-[s:STEP]->(st:Step)-[u]->(x) RETURN q, f, r, s, st, u, x\n")
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
