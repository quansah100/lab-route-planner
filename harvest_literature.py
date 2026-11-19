"""Step 1: pull scholarly papers on manufacturing processing and characterization
from OpenAlex (free scholarly database) into Neo4j as (:Paper) nodes.

    python harvest_literature.py --target 1000

Set OPENALEX_API_KEY (free from https://openalex.org) to avoid rate limits.
Already-stored papers are kept; re-running only adds new ones.
Add your own topics with --topic "cold spray coating" (repeatable).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from common import database, get_driver, setup_schema

TOPICS = [
    # processing
    "laser powder bed fusion process parameters",
    "directed energy deposition metal",
    "binder jetting sintering",
    "powder metallurgy compaction sintering",
    "spark plasma sintering",
    "hot isostatic pressing additive manufacturing",
    "metal casting solidification microstructure",
    "forging hot deformation",
    "rolling and annealing microstructure",
    "extrusion of alloys",
    "CNC machining surface integrity",
    "heat treatment precipitation hardening",
    "stress relief heat treatment residual stress",
    "fusion welding microstructure mechanical properties",
    "friction stir welding",
    "thermal spray coating",
    "physical vapor deposition thin film",
    "ceramic processing slip casting sintering",
    "polymer injection molding",
    "fiber reinforced composite manufacturing",
    "gas atomization metal powder production",
    # characterization and testing
    "metal powder characterization particle size flowability",
    "scanning electron microscopy microstructure characterization",
    "X-ray diffraction phase analysis alloy",
    "electron backscatter diffraction texture",
    "X-ray computed tomography porosity",
    "metallographic specimen preparation",
    "tensile testing specimen additive manufacturing",
    "fatigue testing specimen preparation",
    "hardness testing microhardness",
]

API = "https://api.openalex.org/works"
FIELDS = "id,doi,title,publication_year,abstract_inverted_index,cited_by_count,primary_location"

UPSERT = """
UNWIND $papers AS p
MERGE (x:Paper {id: p.id})
ON CREATE SET x.extracted = false
SET x.doi = p.doi, x.title = p.title, x.year = p.year, x.abstract = p.abstract,
    x.citedBy = p.cited_by, x.venue = p.venue, x.topic = coalesce(x.topic, p.topic)
"""


def fetch(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "lab-route-planner"}
    if os.getenv("OPENALEX_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['OPENALEX_API_KEY']}"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429 and attempt < 4:
                try:
                    wait = int(json.loads(body).get("retryAfter", 30))
                except (ValueError, AttributeError):
                    wait = 30
                print(f"  rate limited, waiting {wait}s ...", file=sys.stderr)
                time.sleep(wait + 1)
                continue
            raise RuntimeError(f"OpenAlex error {e.code}: {body[:300]}") from None
        except urllib.error.URLError as e:
            if attempt < 4:
                time.sleep(5)
                continue
            raise RuntimeError(f"Could not reach OpenAlex: {e}") from None
    raise RuntimeError("OpenAlex kept rate-limiting; set OPENALEX_API_KEY and try again.")


def abstract_text(inverted: dict | None) -> str:
    if not inverted:
        return ""
    positions = sorted((pos, word) for word, poss in inverted.items() for pos in poss)
    return " ".join(word for _, word in positions)


def to_paper(work: dict, topic: str) -> dict | None:
    abstract = abstract_text(work.get("abstract_inverted_index"))
    if len(abstract.split()) < 40 or not work.get("title"):
        return None
    venue = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
    return {
        "id": work["id"].rsplit("/", 1)[-1],  # e.g. W2741809807
        "doi": work.get("doi"),
        "title": work["title"],
        "year": work.get("publication_year"),
        "abstract": abstract,
        "cited_by": work.get("cited_by_count", 0),
        "venue": venue,
        "topic": topic,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=int, default=1000, help="how many new papers to add (default 1000)")
    ap.add_argument("--topic", action="append", default=[], help="extra search topic (repeatable)")
    ap.add_argument("--only-my-topics", action="store_true", help="use only the --topic values, not the built-in list")
    ap.add_argument("--since", type=int, default=2005, help="earliest publication year (default 2005)")
    args = ap.parse_args()

    topics = args.topic if args.only_my_topics else TOPICS + args.topic
    if not topics:
        print("No topics given.", file=sys.stderr)
        return 2
    per_topic = math.ceil(args.target / len(topics))

    driver = get_driver()
    with driver, driver.session(database=database()) as session:
        setup_schema(session)
        known = {r["id"] for r in session.run("MATCH (p:Paper) RETURN p.id AS id")}
        added = 0
        for topic in topics:
            if added >= args.target:
                break
            want = min(per_topic, args.target - added)
            got, page = [], 1
            while len(got) < want and page <= 5:
                data = fetch({
                    "search": topic,
                    "filter": f"has_abstract:true,type:article,publication_year:>{args.since - 1}",
                    "sort": "relevance_score:desc",
                    "per-page": 50,
                    "page": page,
                    "select": FIELDS,
                })
                results = data.get("results", [])
                for w in results:
                    p = to_paper(w, topic)
                    if p and p["id"] not in known:
                        known.add(p["id"])
                        got.append(p)
                        if len(got) >= want:
                            break
                if len(results) < 50:
                    break
                page += 1
            if got:
                session.run(UPSERT, papers=got).consume()
            added += len(got)
            print(f"  {len(got):3d} new  ({added}/{args.target})  {topic}")

        total = session.run("MATCH (p:Paper) RETURN count(p) AS n").single()["n"]
    print(f"\nAdded {added} papers. The library now holds {total}.")
    print("Next: python extract_knowledge.py   (Claude reads them and builds the literature graph)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
