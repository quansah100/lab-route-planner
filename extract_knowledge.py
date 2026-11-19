"""Step 2: an AI model reads every stored paper once and records the process
steps it reports (input -> process / equipment -> output). These become the
literature graph in Neo4j, each step linked to the paper that reports it:

    (:LitNoun)-[:INPUT_OF]->(:Transition)-[:OUTPUTS]->(:LitNoun)
    (:Transition)-[:USES]->(:LitEquipment)
    (:Paper)-[:REPORTS]->(:Transition)

    python extract_knowledge.py                  # free: local model via Ollama
    python extract_knowledge.py --engine claude  # optional, paid: Claude API

Papers already read are skipped, so you can stop (Ctrl+C) and re-run any time.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from pydantic import BaseModel, Field

from common import database, get_driver, setup_schema
from llm import CLAUDE_MODEL, OLLAMA_MODEL, EngineError, ask, check_local

# Rough per-paper cost when using Claude, for the estimate shown before starting (USD).
EST_CLAUDE_COST_PER_PAPER = 0.02


class Transition(BaseModel):
    input: str = Field(description="Generic name of what goes in, e.g. 'metal powder', 'as-built part'")
    process: str = Field(description="What is done, e.g. 'stress relief heat treatment'")
    equipment: str = Field(description="Generic equipment type, e.g. 'vacuum furnace'; 'unspecified' if not stated")
    output: str = Field(description="Generic name of what comes out, e.g. 'stress-relieved part'")
    material: str = Field(description="The specific material, e.g. 'Ti-6Al-4V', or '' if not stated")
    conditions: str = Field(description="Key parameters reported, e.g. '650 C, 2 h, argon', or ''")


class Extraction(BaseModel):
    transitions: list[Transition]


SYSTEM_PROMPT = """You extract manufacturing, processing, specimen-preparation and \
characterization/testing steps from a scientific paper's title and abstract.

Record each step the paper actually reports performing, in order, as input -> process \
(on some equipment) -> output. Each step's output is normally the next step's input; keep \
those names identical so the steps chain.

Use short, generic, lower-case names that other papers would also use, so steps from \
different papers connect: e.g. 'metal powder', 'sieved powder', 'as-built part', \
'stress-relieved part', 'tensile specimen', 'tensile test results', 'microstructure \
images'. Put alloy or material names in 'material' and parameters in 'conditions', not \
in the noun names. For characterization or testing steps, the output is the result \
(e.g. 'particle size distribution', 'tensile test results').

Do not invent steps the abstract does not support. Return an empty list if the paper \
reports no such steps (e.g. a pure review or simulation).

Answer only with JSON of the form {"transitions": [{"input": "...", "process": "...", \
"equipment": "...", "output": "...", "material": "...", "conditions": "..."}]}."""

WRITE = """
MATCH (p:Paper {id: $id})
OPTIONAL MATCH (p)-[:REPORTS]->(old:Transition)
DETACH DELETE old
WITH DISTINCT p
SET p.extracted = true, p.extractedAt = datetime(), p.extractedBy = $by,
    p.transitionCount = size($transitions)
WITH p
UNWIND range(0, size($transitions) - 1) AS k
WITH p, k, $transitions[k] AS t
MERGE (i:LitNoun {name: toLower(trim(t.input))})
MERGE (o:LitNoun {name: toLower(trim(t.output))})
MERGE (e:LitEquipment {name: toLower(trim(t.equipment))})
CREATE (tr:Transition {process: t.process, material: t.material, conditions: t.conditions,
                       order: k + 1,
                       text: t.input + ' ' + t.process + ' ' + t.equipment + ' ' + t.output + ' ' + t.material})
CREATE (p)-[:REPORTS]->(tr), (i)-[:INPUT_OF]->(tr), (tr)-[:OUTPUTS]->(o), (tr)-[:USES]->(e)
"""


def clean(transitions: list[Transition]) -> list[dict]:
    """Drop steps with empty names (small local models sometimes produce them)."""
    out = []
    for t in transitions:
        if t.input.strip() and t.output.strip() and t.process.strip():
            d = t.model_dump()
            d["equipment"] = d["equipment"].strip() or "unspecified"
            out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=["local", "claude"], default="local",
                    help="local = free model via Ollama (default); claude = Claude API (paid)")
    ap.add_argument("--limit", type=int, help="read at most this many papers this run")
    ap.add_argument("--workers", type=int, help="papers read in parallel (default: 1 local, 6 claude)")
    ap.add_argument("--redo", action="store_true", help="re-read papers that were already read")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation (claude engine)")
    args = ap.parse_args()
    workers = args.workers or (1 if args.engine == "local" else 6)
    model = OLLAMA_MODEL if args.engine == "local" else CLAUDE_MODEL

    if args.engine == "local":
        try:
            check_local()
        except EngineError as e:
            print(e, file=sys.stderr)
            return 2

    driver = get_driver()
    with driver:
        with driver.session(database=database()) as session:
            setup_schema(session)
            where = "" if args.redo else "WHERE coalesce(p.extracted, false) = false"
            limit = f"LIMIT {int(args.limit)}" if args.limit else ""
            papers = [r.data() for r in session.run(
                f"MATCH (p:Paper) {where} RETURN p.id AS id, p.title AS title, p.abstract AS abstract "
                f"ORDER BY p.citedBy DESC {limit}")]
        if not papers:
            print("Nothing to read: every stored paper has been read already (use --redo to re-read).")
            return 0

        if args.engine == "claude":
            est = len(papers) * EST_CLAUDE_COST_PER_PAPER
            print(f"{len(papers)} papers to read with {model} (paid). Rough cost: ${est:.0f}-{est * 2:.0f}.")
            if not args.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Stopped.")
                return 1
        else:
            print(f"{len(papers)} papers to read with {model} on this computer (free). "
                  f"Stop any time with Ctrl+C; re-running continues where it left off.")

        lock = threading.Lock()
        done = steps = failed = 0
        t0 = time.time()

        def work(paper: dict) -> int:
            result = ask(args.engine, SYSTEM_PROMPT,
                         f"Title: {paper['title']}\n\nAbstract: {paper['abstract']}", Extraction, effort="low")
            rows = clean(result.transitions)
            with driver.session(database=database()) as s:
                s.run(WRITE, id=paper["id"], transitions=rows, by=f"{args.engine}:{model}").consume()
            return len(rows)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(work, p): p for p in papers}
            try:
                for fut in as_completed(futures):
                    with lock:
                        try:
                            steps += fut.result()
                        except Exception as e:  # one bad paper shouldn't stop the run
                            failed += 1
                            print(f"  skipped {futures[fut]['id']}: {e}", file=sys.stderr)
                        done += 1
                        if done % 10 == 0 or done == len(papers):
                            rate = (time.time() - t0) / done
                            left = rate * (len(papers) - done) / 60
                            print(f"  {done}/{len(papers)} papers read, {steps} steps recorded"
                                  f"  (~{left:.0f} min left)")
            except KeyboardInterrupt:
                print("\nStopping; progress so far is saved. Re-run to continue.")
                pool.shutdown(wait=False, cancel_futures=True)
                return 130

        with driver.session(database=database()) as session:
            stats = session.run("""
                MATCH (p:Paper) WITH count(p) AS papers, sum(CASE WHEN p.extracted THEN 1 ELSE 0 END) AS read
                OPTIONAL MATCH (t:Transition) WITH papers, read, count(t) AS steps
                OPTIONAL MATCH (n:LitNoun) RETURN papers, read, steps, count(n) AS nouns""").single()

    print(f"\nLibrary: {stats['papers']} papers, {stats['read']} read, "
          f"{stats['steps']} steps, {stats['nouns']} distinct nouns.")
    if failed:
        print(f"{failed} papers were skipped; re-run to retry them.")
    print("Next: python map_names.py   (match the literature's names to your lab's names)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
