"""Detect which ATS an apply URL belongs to, using config/ats/registry.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from jobhunt.config import ConfigError

REGISTRY_RELATIVE = Path("ats") / "registry.yaml"


@dataclass(slots=True)
class AtsRegistry:
    patterns: dict[str, list[re.Pattern[str]]]
    python_fillers: frozenset[str] = field(default_factory=frozenset)
    assisted: frozenset[str] = field(default_factory=frozenset)
    manual: frozenset[str] = field(default_factory=frozenset)
    review_hosts: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, config_dir: Path) -> AtsRegistry:
        path = config_dir / REGISTRY_RELATIVE
        if not path.exists():
            raise ConfigError(f"missing ATS registry: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        patterns = {
            str(key).lower(): [re.compile(p, re.I) for p in (values or [])]
            for key, values in (data.get("patterns") or {}).items()
        }
        return cls(
            patterns=patterns,
            python_fillers=frozenset(str(v).lower() for v in data.get("python_fillers", [])),
            assisted=frozenset(str(v).lower() for v in data.get("assisted", [])),
            manual=frozenset(str(v).lower() for v in data.get("manual", [])),
            review_hosts=data.get("review_hosts") or {},
        )

    def detect(self, url: str | None, fallback: str | None = None) -> str | None:
        if url:
            for ats, regexes in self.patterns.items():
                if any(r.search(url) for r in regexes):
                    return ats
        return fallback.lower() if fallback else None

    def review_url_allowed(self, url: str, ats: str | None) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                self.mode_for(ats) == "python"
                and parsed.scheme == "https"
                and not parsed.username
                and not parsed.password
                and parsed.port in {None, 443}
                and parsed.hostname in self.review_hosts.get(ats or "", [])
            )
        except ValueError:
            return False

    def mode_for(self, ats: str | None) -> str:
        """python | assisted | manual | unsupported."""
        if ats in self.python_fillers:
            return "python"
        if ats in self.assisted:
            return "assisted"
        if ats in self.manual:
            return "manual"
        return "unsupported"
