"""Step 3: match the literature's names to your lab's names (one time, free).

Papers say "laser powder bed fusion machine"; your lab says "LPBF printer".
This links them in Neo4j so literature sequences can be checked against the lab:

    (:Equipment)-[:SAME_AS]->(:LitEquipment)
    (:Noun)-[:SAME_AS]->(:LitNoun)

    python map_names.py           # go through lab items that have no match yet
    python map_names.py --review  # go through every lab item again
    python map_names.py --list    # show the current matches

Exact name matches (ignoring upper/lower case) are linked automatically.
For the rest you see the closest literature names and type the numbers that
mean the same thing (e.g. 1,3), Enter to skip, or q to stop.
Re-run whenever you add equipment to the lab or papers to the library.
"""

from __future__ import annotations

import argparse
import sys

from common import database, get_driver, lucene_query, setup_schema

KINDS = {
    "equipment": ("Equipment", "LitEquipment", "lit_equipment_name",
                  "MATCH (x)<-[:USES]-(t:Transition) RETURN count(t) AS uses"),
    "nouns": ("Noun", "LitNoun", "lit_noun_name",
              "MATCH (x)-[:INPUT_OF|OUTPUTS]-(t:Transition) RETURN count(DISTINCT t) AS uses"),
}


def candidates(session, lit_label: str, index: str, name: str, k: int = 8) -> list[dict]:
    q = lucene_query(name)
    if not q:
        return []
    return [r.data() for r in session.run(f"""
        CALL db.index.fulltext.queryNodes('{index}', $q) YIELD node, score
        WITH node, score ORDER BY score DESC LIMIT $k
        CALL (node) {{ WITH node AS x {KINDS_USES[lit_label]} }}
        RETURN node.name AS name, uses ORDER BY score DESC""", q=q, k=k)]


KINDS_USES = {lit: uses for _, (_, lit, _, uses) in KINDS.items()}


def link(session, lab_label: str, lit_label: str, lab_name: str, lit_names: list[str]) -> None:
    session.run(f"""
        MATCH (a:{lab_label} {{name: $lab}})
        UNWIND $lits AS ln
        MATCH (b:{lit_label} {{name: ln}})
        MERGE (a)-[:SAME_AS]->(b)""", lab=lab_name, lits=lit_names).consume()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=list(KINDS), help="only equipment or only nouns")
    ap.add_argument("--review", action="store_true", help="also go through items that already have matches")
    ap.add_argument("--list", action="store_true", help="show the current matches and stop")
    ap.add_argument("--exact-only", action="store_true", help="link exact matches only, ask nothing")
    args = ap.parse_args()

    driver = get_driver()
    with driver, driver.session(database=database()) as session:
        setup_schema(session)
        if not session.run("MATCH (n:LitNoun) RETURN count(n) > 0 AS ok").single()["ok"]:
            print("The literature graph is empty. Run harvest_literature.py and extract_knowledge.py first.")
            return 2

        for kind in ([args.only] if args.only else list(KINDS)):
            lab_label, lit_label, index, _ = KINDS[kind]
            print(f"\n=== {kind} ===")

            # 1. exact matches, automatically
            if not args.list:
                exact = session.run(f"""
                    MATCH (a:{lab_label}), (b:{lit_label}) WHERE toLower(a.name) = b.name
                    MERGE (a)-[r:SAME_AS]->(b) RETURN count(r) AS n""").single()["n"]
                if exact:
                    print(f"  {exact} exact matches linked automatically")

            items = [r.data() for r in session.run(f"""
                MATCH (a:{lab_label})
                OPTIONAL MATCH (a)-[:SAME_AS]->(b:{lit_label})
                RETURN a.name AS name, collect(b.name) AS matched ORDER BY name""")]
            if args.list:
                for it in items:
                    print(f"  {it['name']:35} = {', '.join(it['matched']) or '(no match yet)'}")
                continue
            if args.exact_only:
                continue

            # 2. the rest, by asking
            for it in items:
                if it["matched"] and not args.review:
                    continue
                cands = [c for c in candidates(session, lit_label, index, it["name"])
                         if c["name"] not in it["matched"]]
                if not cands:
                    continue
                print(f"\n  Your lab: '{it['name']}'"
                      + (f"   (already = {', '.join(it['matched'])})" if it["matched"] else ""))
                for i, c in enumerate(cands, 1):
                    print(f"    {i}. {c['name']}   (in {c['uses']} literature steps)")
                ans = input("  Same thing? numbers like 1,3 / Enter to skip / q to stop: ").strip().lower()
                if ans == "q":
                    break
                picks = []
                for part in ans.replace(" ", "").split(","):
                    if part.isdigit() and 1 <= int(part) <= len(cands):
                        picks.append(cands[int(part) - 1]["name"])
                if picks:
                    link(session, lab_label, lit_label, it["name"], picks)
                    print(f"  linked: {', '.join(picks)}")

        if not args.list:
            print("\nDone. See all matches with: python map_names.py --list")
            print('Next: python route_planner.py "start" "end"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
