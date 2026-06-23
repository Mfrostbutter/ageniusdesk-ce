"""Pydantic models for the C3 constitution (baseline instruction set)."""

from __future__ import annotations

from pydantic import BaseModel


class BaselineFrontmatter(BaseModel):
    version: int
    updated: str
    overrideable_sections: list[str] = []


class PutBaselineRequest(BaseModel):
    expected_version: int
    overrideable_sections: list[str] = []
    content: str


class BaselineResponse(BaseModel):
    version: int
    updated: str
    overrideable_sections: list[str]
    content: str
    size: int
