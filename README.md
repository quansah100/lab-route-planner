# Lab Route Planner

Give a **start** and an **end** (each with a description). The tool finds process sequences in
the scholarly literature, compares them, checks them against your lab, and Neo4j presents the
valid ones. **Everything runs free on your own computer.** Claude is an optional, paid extra.

```
1. harvest_literature.py   ~1000 papers on manufacturing processing & characterization
                           from OpenAlex (free) ──► Neo4j

2. extract_knowledge.py    a free AI model on your Mac (Ollama) reads each abstract once and
                           records the steps it reports: input ─[process / equipment]─► output
                           ──► literature graph in Neo4j, every step linked to its paper

3. map_names.py            one time: match the literature's names to your lab's names
                           ("laser powder bed fusion machine" = your "LPBF printer")

4. route_planner.py        for one start/end:
     a. Neo4j finds chains in the literature graph where each step's output is the next
        step's input, every step backed by a paper (free, instant)
        (optional: --llm local or --llm claude adds AI suggestions)
     b. check every candidate:
          • each step is backed by at least one paper in the library
          • each step's equipment is available AND accessible in the lab
          • what each step produces is accepted by the next step's equipment
            (first accepts START, last produces END)
     c. print a comparison of all candidates; save the valid ones in Neo4j
```

## One-time setup

1. Python packages:

   ```bash
   pip install -r requirements.txt
   ```

2. Ollama, the free app that runs AI models on your Mac: download it from https://ollama.com,
   open it, then download a model (once, about 5 GB):

   ```bash
   ollama pull qwen2.5:7b
   ```

   | Your Mac's memory | Model | Setting |
   |---|---|---|
   | 16 GB | `qwen2.5:7b` (default) | nothing to set |
   | 32 GB or more | `qwen2.5:14b`, more accurate | `export OLLAMA_MODEL=qwen2.5:14b` |

3. Settings (an OpenAlex key is free from https://openalex.org):

   ```bash
   export OPENALEX_API_KEY=your-free-openalex-key
   export NEO4J_URI=bolt://localhost:7688 NEO4J_USER=neo4j NEO4J_PASSWORD=labpassword
   ```

4. Your lab (edit `seed.cypher` to match your lab first; it never deletes anything):

   ```bash
   docker exec -i neo4j-lab cypher-shell -u neo4j -p labpassword < seed.cypher
   ```

## Build the literature library (once)

```bash
python harvest_literature.py --target 1000
```

About 30 built-in topics (additive manufacturing, powder metallurgy, casting, forging, machining,
heat treatment, welding, coatings, powder characterization, SEM, XRD, EBSD, CT, tensile, fatigue,
hardness, ...). Add your own with `--topic "cold spray coating"` (repeatable).

```bash
python extract_knowledge.py
```

Takes a few hours for 1000 papers on a Mac; run it overnight. Stop any time with Ctrl+C;
re-running continues where it left off.

```bash
python map_names.py
```

Exact matches are linked automatically. For the rest you see the closest literature names and
type the numbers that mean the same thing as your lab's item (e.g. `1,3`), or Enter to skip.
Re-run it when you add equipment or papers. `python map_names.py --list` shows the matches.

## Ask for routes

```bash
python route_planner.py "print powder" "tensile test specimen" --start-desc "gas-atomized Ti-6Al-4V powder" --end-desc "ASTM E8 round specimen" --show-rejected
```

### You don't have to type your lab's exact names

The tool works out which of your lab's names you mean by its **role in the process**, not by
shared words. Every lab noun has an operational profile built from Neo4j: what makes it, what
takes it in, what comes before and after it, and what the papers call it. The free local model
checks every word you typed and says what it tells about the object: material, form, stage or
purpose. Then it picks the lab noun that is the same kind of object at the same stage.

Specifics never decide the match: "Ti64 powder", "316L powder" and "gas-atomized powder 15-45 um"
are all simply *powder*. As a start, that is your lab's raw powder ("Print powder"). The form
(powder, bar, part, specimen, data) and the stage (as-received, printed, heat treated, machined,
tested) decide.

```
Print powder  ->  Tensile test specimen
  (start 'Ti64 powder', word by word: ti64 = material: Ti-6Al-4V (a detail); powder = form: powder)
  (start: using your lab's 'Print powder' for 'Ti64 powder' (high confidence). ...)
  (end 'dogbone', word by word: dogbone = form: dog-bone tensile specimen)
  (end: using your lab's 'Tensile test specimen' for 'dogbone' (high confidence). ...)
```

When the model isn't sure, it asks you to choose. When nothing in your lab plays that role, it
says so and lists your lab's names. If Ollama isn't running, it falls back to matching words and
tells you.

| flag | what it does |
|---|---|
| `--start-desc`, `--end-desc` | describe the start and end objects (also used for matching) |
| `--match local` | match by operational role with the free local model (default) |
| `--match claude` | the same with Claude (paid, more accurate) |
| `--match words` | match by shared words only (fast, no AI) |
| `--exact` | use start/end exactly as typed (no translation into lab names) |
| `--yes` | never ask; take the best lab name automatically |
| `--llm local` | also ask the free local model for suggestions (slower) |
| `--llm claude` | also ask Claude for suggestions (paid; needs `ANTHROPIC_API_KEY`) |
| `--show-rejected` | print rejected sequences in full, with the reasons |
| `--valid-only` | list only valid candidates in the comparison |
| `--max-steps 12` | allow longer literature chains (default 10) |
| `--candidates file.json` | also check sequences from a file |
| `--no-literature-check` | don't require every step to cite a paper |
| `--json` | machine-readable output |
| `--no-save` | don't save the valid routes in Neo4j |

Test the lab check alone with the example routes:

```bash
python route_planner.py "print powder" "tensile test specimen" --candidates example_candidates.json --no-literature-check --show-rejected
```

## What's in Neo4j

```
Your lab:
(:Equipment {name, available, accessible})-[:ACCEPTS|PRODUCES]->(:Noun {name})

The literature:
(:Paper {id, doi, title, year, abstract})-[:REPORTS]->(:Transition {process, material, conditions})
(:LitNoun)-[:INPUT_OF]->(:Transition)-[:OUTPUTS]->(:LitNoun)
(:Transition)-[:USES]->(:LitEquipment)

Name matches:
(:Equipment)-[:SAME_AS]->(:LitEquipment)     (:Noun)-[:SAME_AS]->(:LitNoun)

Valid routes:
(:RouteQuery {start, startDesc, end, endDesc})-[:FOUND {rank}]->(:Route {source})-[:STEP {order}]->(:Step)
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

## Optional: Claude (paid)

Claude is more accurate than a local model at reading papers and suggesting routes. It is billed
to an Anthropic API account (console.anthropic.com), separate from a Claude.ai subscription.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python extract_knowledge.py --engine claude      # roughly $20-40 per 1000 papers; asks first
python route_planner.py "start" "end" --llm claude   # a few cents to ~30 cents per request
```

## Notes

- Only abstracts are used (full texts are mostly not freely available), so the library knows
  the steps papers summarize, not every detail of their methods sections.
- Free local models make more mistakes than Claude when reading papers. Every literature step
  links to its paper, so you can check any step the tool relies on.
