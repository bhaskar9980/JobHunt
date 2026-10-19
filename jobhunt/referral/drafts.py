"""Outreach templates: YAML frontmatter plus a body with {placeholders}. Rendered text is checked
against the same tone rules as cover letters before it is written to data/outreach/."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from jobhunt.config import ConfigError

TEMPLATE_DIR = Path("templates") / "outreach"
TEMPLATE_SUFFIX = ".md"
MAX_EXCLAMATIONS = 2
DEFAULT_CHANNEL = "linkedin"
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_SPELL_WORD = re.compile(r"[A-Za-z]+")
_SLUG_JUNK = re.compile(r"[^a-z0-9]+")
MAX_SLUG_LENGTH = 40


@dataclass(slots=True)
class Template:
    name: str
    kind: str
    channel: str
    max_chars: int
    body: str

    @property
    def placeholders(self) -> set[str]:
        return set(_PLACEHOLDER.findall(self.body))


def template_dir(config_dir: Path) -> Path:
    return config_dir / TEMPLATE_DIR


def list_templates(config_dir: Path) -> list[str]:
    folder = template_dir(config_dir)
    if not folder.exists():
        return []
    return sorted(p.stem for p in folder.glob(f"*{TEMPLATE_SUFFIX}"))


def load_template(config_dir: Path, name: str) -> Template:
    path = template_dir(config_dir) / f"{name}{TEMPLATE_SUFFIX}"
    if not path.exists():
        raise ConfigError(f"no outreach template {name!r} in {path.parent}")
    return parse_template(name, path.read_text(encoding="utf-8"))


def parse_template(name: str, text: str) -> Template:
    match = _FRONTMATTER.match(text)
    if match is None:
        raise ConfigError(f"template {name!r} needs a YAML frontmatter block")
    meta: dict[str, Any] = yaml.safe_load(match.group(1)) or {}
    if "max_chars" not in meta or "kind" not in meta:
        raise ConfigError(f"template {name!r} frontmatter needs max_chars and kind")
    return Template(
        name=name,
        kind=str(meta["kind"]),
        channel=str(meta.get("channel", DEFAULT_CHANNEL)),
        max_chars=int(meta["max_chars"]),
        body=match.group(2).strip() + "\n",
    )


def render(template: Template, context: Mapping[str, Any]) -> str:
    """Fill placeholders; unknown ones are left as-is so check_draft can flag them."""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        value = context.get(key)
        return match.group(0) if value is None or value == "" else str(value).strip()

    text = _PLACEHOLDER.sub(_sub, template.body)
    return re.sub(r"[ \t]+\n", "\n", text).strip() + "\n"


def check_draft(text: str, template: Template, tailor_cfg: Mapping[str, Any]) -> list[str]:
    rules = tailor_cfg.get("cover_letter") or {}
    issues: list[str] = []
    length = len(text.strip())
    if length > template.max_chars:
        issues.append(f"{length} chars; {template.name} allows {template.max_chars}")
    for key in sorted(set(_PLACEHOLDER.findall(text))):
        issues.append(f"unfilled placeholder {{{key}}}")
    for char in rules.get("banned_characters") or []:
        if str(char) in text:
            issues.append(f"contains banned character {char!r}")
    issues.extend(_spelling_issues(text, rules.get("british_spellings") or {}))
    if text.count("!") > MAX_EXCLAMATIONS:
        issues.append(f"{text.count('!')} exclamation marks; keep it to {MAX_EXCLAMATIONS}")
    return issues


def _spelling_issues(text: str, mapping: Mapping[str, str]) -> list[str]:
    found = {w.lower() for w in _SPELL_WORD.findall(text)}
    return [
        f"american spelling {us!r}; use {uk!r}"
        for us, uk in mapping.items()
        if str(us).lower() in found
    ]


def slugify(value: str) -> str:
    slug = _SLUG_JUNK.sub("-", value.lower()).strip("-")
    return slug[:MAX_SLUG_LENGTH].rstrip("-") or "contact"


def write_draft(outreach_dir: Path, job_id: int, contact_name: str, text: str) -> Path:
    folder = outreach_dir / str(job_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slugify(contact_name)}{TEMPLATE_SUFFIX}"
    path.write_text(text, encoding="utf-8")
    return path
