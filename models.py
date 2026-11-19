"""The shape of a route, shared by the planner and its fallbacks."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Step(BaseModel):
    process: str = Field(description="Short description of what this step does")
    equipment: str = Field(description="The single piece of equipment that performs this step")
    output: str = Field(description="The noun this step produces, which the next step takes in")
    citations: list[str] = Field(default_factory=list,
                                 description="Ids of the papers (e.g. W2741809807) that support this step")


class Candidate(BaseModel):
    steps: list[Step] = Field(description="Ordered steps; the first takes START, the last produces END")
    rationale: str = Field(description="One or two sentences on why this route makes sense")
    basis: str = Field(description="Standards and literature this route is based on")


class Proposal(BaseModel):
    candidates: list[Candidate]


def route_key(cand: Candidate) -> tuple:
    """Two candidates with the same equipment and outputs are the same route."""
    return tuple((s.equipment.lower(), s.output.lower()) for s in cand.steps)
