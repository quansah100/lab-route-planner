"""Shared settings: Neo4j connection, model name, and the database schema."""

from __future__ import annotations

import os
import re

from neo4j import GraphDatabase

MODEL = "claude-opus-5-5"

SCHEMA = [
    "CREATE CONSTRAINT paper_id IF NOT EXISTS FOR (p:Paper) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT lit_noun IF NOT EXISTS FOR (n:LitNoun) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT lit_equipment IF NOT EXISTS FOR (e:LitEquipment) REQUIRE e.name IS UNIQUE",
    "CREATE FULLTEXT INDEX paper_text IF NOT EXISTS FOR (p:Paper) ON EACH [p.title, p.abstract]",
    "CREATE FULLTEXT INDEX transition_text IF NOT EXISTS FOR (t:Transition) ON EACH [t.text]",
    "CREATE FULLTEXT INDEX lit_noun_name IF NOT EXISTS FOR (n:LitNoun) ON EACH [n.name]",
]

STOPWORDS = set("""a an and are as at be by for from in into is it of on or the to with without
using use used via per than that this these those its their our we which""".split())


def get_driver():
    return GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "labpassword")),
    )


def database() -> str:
    return os.getenv("NEO4J_DATABASE", "neo4j")


def setup_schema(session) -> None:
    for stmt in SCHEMA:
        session.run(stmt).consume()


def lucene_query(*texts: str) -> str:
    """Turn free text into a safe full-text search query (any of the words)."""
    words = []
    for t in texts:
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", t or ""):
            w = w.lower().strip("-")
            if len(w) > 2 and w not in STOPWORDS and w not in words:
                words.append(w)
    # escape the characters Lucene treats specially
    return " OR ".join(re.sub(r'([+\-!(){}\[\]^"~*?:\\/&|])', r"\\\1", w) for w in words)
