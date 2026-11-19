# Lab Route Planner

Give a **start** and an **end** (each with a description). The tool proposes process sequences
grounded in the scholarly literature, compares them, checks them against your lab, and Neo4j
presents the valid ones.

```
1. harvest_literature.py   ~1000 papers on manufacturing processing & characterization
                           (OpenAlex) ──► Neo4j (:Paper)

2. extract_knowledge.py    Claude reads each abstract once and records the steps it reports:
                           input ──[process / equipment]──► output, linked to the paper
                           ──► literature graph in Neo4j

3. route_planner.py        for one prompt (start + description, end + description):
     a. find the relevant papers and step chains in the literature graph
     b. Claude (the expert) proposes sequences grounded in them, citing papers per step
     c. check every candidate:
          • each step is backed by at least one real paper in the library
          • each step's equipment is available AND accessible in the lab
          • what each step produces is accepted by the next step's equipment
            (first accepts START, last produces END)
     d. print a comparison of all candidates; save the valid ones in Neo4j
```

"Learning" here means the papers are stored and searched for every prompt, and Claude is given
the relevant ones as evidence. The model itself is not retrained.

## Setup

```bash
pip install -r requirements.txt
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENALEX_API_KEY=your-free-openalex-key
export NEO4J_URI=bolt://localhost:7688 NEO4J_USER=neo4j NEO4J_PASSWORD=labpassword
```

An OpenAlex key is free from https://openalex.org. Without one, searches are often rate-limited.

Load your lab (edit `seed.cypher` to match your lab first; it never deletes anything):

```bash
docker exec -i neo4j-lab cypher-shell -u neo4j -p labpassword < seed.cypher
```

## 1. Build the literature library (once)

```bash
python harvest_literature.py --target 1000
```

About 30 built-in topics (additive manufacturing, powder metallurgy, casting, forging, machining,
heat treatment, welding, coatings, powder characterization, SEM, XRD, EBSD, CT, tensile, fatigue,
hardness, ...). Add your own with `--topic "cold spray coating"` (repeatable). Re-running only
adds new papers.

```bash
python extract_knowledge.py
```

Calls Claude once per paper. It shows a cost estimate and asks before starting (roughly
$20-40 for 1000 papers). Papers already read are skipped, so you can stop and resume.

## 2. Ask for routes

```bash
python route_planner.py "print powder" "tensile test specimen" --start-desc "gas-atomized Ti-6Al-4V powder, 15-45 um" --end-desc "ASTM E8 round tensile specimen" --show-rejected
```

| flag | what it does |
|---|---|
| `--start-desc`, `--end-desc` | describe the start and end objects |
| `-n 8` | ask for up to 8 sequences (default 5) |
| `--papers 50` | how many relevant papers to give Claude (default 30) |
| `--show-rejected` | print rejected sequences in full |
| `--show-evidence` | print the papers and literature chains given to Claude |
| `--no-literature-check` | don't require every step to cite a paper |
| `--candidates file.json` | check sequences from a file instead of asking Claude |
| `--json` | machine-readable output |
| `--no-save` | don't save the valid routes in Neo4j |

Without an API key, test the lab check with the example routes:

```bash
python route_planner.py "print powder" "tensile test specimen" --candidates example_candidates.json --no-literature-check --show-rejected
```

## What's in Neo4j

Your lab:

```
(:Equipment {name, available, accessible})
(:Equipment)-[:ACCEPTS]->(:Noun {name})
(:Equipment)-[:PRODUCES]->(:Noun {name})
```

The literature:

```
(:Paper {id, doi, title, year, abstract})-[:REPORTS]->(:Transition {process, material, conditions})
(:LitNoun)-[:INPUT_OF]->(:Transition)-[:OUTPUTS]->(:LitNoun)
(:Transition)-[:USES]->(:LitEquipment)
```

Valid routes:

```
(:RouteQuery {start, startDesc, end, endDesc})-[:FOUND {rank}]->(:Route)-[:STEP {order}]->(:Step)
(:Step)-[:TAKES]->(:Noun)  -[:USES]->(:Equipment)  -[:GIVES]->(:Noun)  -[:SUPPORTED_BY]->(:Paper)
```

Useful Neo4j Browser queries (use Chrome or Firefox; Safari blocks the local connection):

```cypher
// valid routes with their supporting papers
MATCH (q:RouteQuery)-[f:FOUND]->(r:Route)-[s:STEP]->(st:Step)-[u]->(x) RETURN q, f, r, s, st, u, x

// the literature graph around one noun
MATCH (n:LitNoun {name:'as-built part'})-[r1]-(t:Transition)-[r2]-(m) RETURN n, r1, t, r2, m LIMIT 100

// library size
MATCH (p:Paper) RETURN count(p) AS papers, sum(CASE WHEN p.extracted THEN 1 ELSE 0 END) AS read
```

Update the lab as it changes:

```cypher
MATCH (e:Equipment {name:'HIP unit'}) SET e.available = true;
MATCH (e:Equipment {name:'Scanning electron microscope'}) SET e.accessible = true;
MATCH (e:Equipment {name:'CNC lathe'}) MERGE (n:Noun {name:'As-built part'}) MERGE (e)-[:ACCEPTS]->(n);
```

## Notes

- Lab names are matched ignoring upper/lower case but otherwise must be the same. Claude is shown
  the lab's names so it uses them; `--show-rejected` reveals any mismatch.
- Only abstracts are used (full texts are mostly not freely available), so the library knows the
  steps papers summarize, not every detail in their methods sections.
- Claude calls use server-side refusal fallbacks (`fallbacks="default"`): if a request is
  declined, the API retries on a fallback model.
