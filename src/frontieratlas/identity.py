"""Deterministic entity aliases backed by directory and domain evidence."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .model import clean, domain, public_url, text_key

LEGAL = re.compile(r"(?:[, ]+)(?:inc(?:orporated)?\.?|llc|ltd\.?|limited|corp(?:oration)?\.?|pbc|plc|gmbh|pte\.? ltd\.?|b\.?v\.?|s\.?a\.?s\.?|pty\.? ltd\.?|ag)[., ]*$", re.I)


def org_key(name: str) -> str:
    return text_key(LEGAL.sub("", clean(name)))


@dataclass(frozen=True)
class Entity:
    key: str
    name: str
    evidence: str


@dataclass(frozen=True)
class Match:
    canonical: str
    evidence: str
    rule: str


class Resolver:
    def __init__(self, startups: list[dict]):
        self.name_index: dict[str, dict[str, Entity]] = defaultdict(dict)
        self.domain_index: dict[str, dict[str, Entity]] = defaultdict(dict)
        for startup in startups:
            name, url = clean(startup.get("name")), public_url(startup.get("url"))
            if not name or not url or startup.get("status") != "Active":
                continue
            entity = Entity(str(startup["id"]), name, url)
            aliases = startup.get("former_names") or []
            self.register(entity, [name, *aliases], public_url(startup.get("website")))
        self.register(
            Entity("openai", "OpenAI", "https://openai.com/our-structure"),
            ["OpenAI", "Open AI", "OpenAI, Inc."],
            "https://openai.com",
        )

    def register(self, entity: Entity, names: list[str], website: str) -> None:
        for name in names:
            if normalized := org_key(name):
                self.name_index[normalized][entity.key] = entity
        if host := domain(website):
            self.domain_index[host][entity.key] = entity

    def resolve(self, name: str = "", website: str = "", evidence: str = "") -> Match | None:
        domain_matches = self.domain_index.get(domain(website), {})
        name_matches = self.name_index.get(org_key(name), {})
        if len(domain_matches) == 1:
            choice = next(iter(domain_matches.values()))
            if name_matches and choice.key not in name_matches:
                return Match(clean(name), evidence, "Conflicting evidence; source name retained") if name and evidence else None
            return Match(choice.name, choice.evidence, "Exact website domain")
        if len(name_matches) == 1:
            choice = next(iter(name_matches.values()))
            return Match(choice.name, choice.evidence, "Exact normalized name or alias")
        if name:
            return Match(clean(name), evidence, "Source name retained")
        return None
