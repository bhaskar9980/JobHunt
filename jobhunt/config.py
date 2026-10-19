"""Configuration loading. YAML files under config/, env overrides from .env / the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from jobhunt.normalize import Normalizer

ENV_ROOT = "JOBHUNT_ROOT"
ENV_DB_PATH = "JOBHUNT_DB_PATH"
ENV_DATA_DIR = "JOBHUNT_DATA_DIR"
ENV_LOG_LEVEL = "JOBHUNT_LOG_LEVEL"

CONFIG_DIR_NAME = "config"
SETTINGS_FILE = "settings.yaml"
PROFILE_FILE = "profile.yaml"
COMPANIES_FILE = "companies.yaml"
SOURCES_FILE = "sources.yaml"
LOCATIONS_FILE = "locations.yaml"
SKILL_ALIASES_FILE = "skill_aliases.yaml"
ANSWERS_FILE = "answers.yaml"
RUBRIC_FILE = "scoring_rubric.md"
MAIL_RULES_FILE = "mail_rules.yaml"
REFERRALS_FILE = "referrals.yaml"


class ConfigError(RuntimeError):
    """Raised when a config file is missing or malformed."""


def default_root() -> Path:
    env_root = os.getenv(ENV_ROOT)
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parents[1]


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ConfigError(f"{path.name}: invalid YAML{location}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


@dataclass(slots=True)
class Settings:
    root: Path
    config_dir: Path
    data_dir: Path
    logs_dir: Path
    db_path: Path
    raw: dict[str, Any] = field(default_factory=dict)

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError(f"settings.yaml section '{name}' must be a mapping")
        return value

    def path(self, key: str) -> Path:
        if key == "db_path":
            return self.db_path
        if key == "data_dir":
            return self.data_dir
        override = os.getenv(f"JOBHUNT_{key.upper()}")
        if override:
            return Path(override)
        override = os.getenv(f"JOBHUNT_{key.upper()}")
        if override:
            return Path(override)
        value = self.section("paths").get(key)
        if value is None:
            raise ConfigError(f"settings.yaml paths.{key} is not set")
        return self.resolve(value)

    def resolve(self, value: str | Path) -> Path:
        if Path(value).is_absolute():
            return Path(value)
        path = _absolute(Path(value), self.root)
        configured_data = _absolute(Path(self.section("paths").get("data_dir", "data")), self.root)
        return _rebase(path, configured_data, self.data_dir)

    def config_file(self, name: str) -> Path:
        return self.config_dir / name

    def ensure_dirs(self) -> None:
        for key in ("inbox_dir", "scoring_dir", "applications_dir", "outreach_dir"):
            self.path(key).mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


def load_settings(root: Path | None = None) -> Settings:
    root = (root or default_root()).resolve()
    load_dotenv(root / ".env", override=False)
    config_dir = root / CONFIG_DIR_NAME
    raw = load_yaml(config_dir / SETTINGS_FILE)
    paths = raw.get("paths", {})
    if not isinstance(paths, dict):
        raise ConfigError("settings.yaml paths must be a mapping")
    configured_data = _absolute(Path(paths.get("data_dir", "data")), root)
    data_dir = _absolute(Path(os.getenv(ENV_DATA_DIR) or configured_data), root)
    db_setting = Path(paths.get("db_path", "data/jobs.db"))
    configured_db = _absolute(db_setting, root)
    if not db_setting.is_absolute():
        configured_db = _rebase(configured_db, configured_data, data_dir)
    db_path = Path(os.getenv(ENV_DB_PATH) or configured_db)
    logs_dir = root / paths.get("logs_dir", "logs")
    return Settings(
        root=root,
        config_dir=config_dir,
        data_dir=_absolute(data_dir, root),
        logs_dir=_absolute(logs_dir, root),
        db_path=_absolute(db_path, root),
        raw=raw,
    )


def _rebase(path: Path, old_base: Path, new_base: Path) -> Path:
    try:
        return new_base / path.relative_to(old_base)
    except ValueError:
        return path


def _absolute(path: Path, root: Path) -> Path:
    return Path(os.path.normpath(str(path if path.is_absolute() else root / path)))


@dataclass(slots=True)
class Targets:
    target_titles: list[str]
    exclude_titles: list[str]
    seniority_max_years_required: int
    locations: list[str]
    remote_ok: bool
    relocate_ok: bool
    relocatable_locations: list[str] = field(default_factory=list)
    remote_regions_excluded: list[str] = field(default_factory=list)
    foreign_markers: list[str] = field(default_factory=list)
    exclude_title_languages: list[str] = field(default_factory=list)
    international_remote_or_relocation: bool = False
    international_rules: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class Skills:
    must_have_any: list[str]
    core: list[str]
    nice_to_have: list[str]
    deal_breakers: list[str]


@dataclass(slots=True)
class ScoringConfig:
    shortlist_min_score: int
    batch_size: int
    description_max_chars: int


@dataclass(slots=True)
class Profile:
    candidate: dict[str, Any]
    targets: Targets
    skills: Skills
    comp: dict[str, Any]
    scoring: ScoringConfig

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        targets = data.get("targets") or {}
        skills = data.get("skills") or {}
        scoring = data.get("scoring") or {}
        return cls(
            candidate=data.get("candidate") or {},
            targets=Targets(
                target_titles=[str(t).lower() for t in targets.get("target_titles", [])],
                exclude_titles=[str(t).lower() for t in targets.get("exclude_titles", [])],
                seniority_max_years_required=int(targets.get("seniority_max_years_required", 99)),
                locations=[str(t).lower() for t in targets.get("locations", [])],
                remote_ok=bool(targets.get("remote_ok", True)),
                relocate_ok=bool(targets.get("relocate_ok", False)),
                relocatable_locations=[
                    str(t).lower() for t in targets.get("relocatable_locations", [])
                ],
                remote_regions_excluded=[
                    str(t).lower() for t in targets.get("remote_regions_excluded", [])
                ],
                foreign_markers=[str(t).lower() for t in targets.get("foreign_markers", [])],
                exclude_title_languages=[
                    str(t).lower() for t in targets.get("exclude_title_languages", [])
                ],
                international_remote_or_relocation=bool(
                    targets.get("international_remote_or_relocation", False)
                ),
                international_rules=targets.get("international_rules") or {},
            ),
            skills=Skills(
                must_have_any=[str(s).lower() for s in skills.get("must_have_any", [])],
                core=[str(s).lower() for s in skills.get("core", [])],
                nice_to_have=[str(s).lower() for s in skills.get("nice_to_have", [])],
                deal_breakers=[str(s).lower() for s in skills.get("deal_breakers", [])],
            ),
            comp=data.get("comp") or {},
            scoring=ScoringConfig(
                shortlist_min_score=int(scoring.get("shortlist_min_score", 70)),
                batch_size=int(scoring.get("batch_size", 25)),
                description_max_chars=int(scoring.get("description_max_chars", 6000)),
            ),
        )


def load_profile(settings: Settings) -> Profile:
    path = settings.config_file(PROFILE_FILE)
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy config/profile.example.yaml to config/profile.yaml "
            "and fill it in."
        )
    data = load_yaml(path)
    targets = data.get("targets") or {}
    if targets.get("international_remote_or_relocation"):
        targets["international_rules"] = load_yaml(settings.config_file(LOCATIONS_FILE)).get(
            "international_eligibility", {}
        )
    return Profile.from_dict(data)


@dataclass(slots=True)
class CompanyConfig:
    name: str
    ats: str
    token: str | None = None
    tenant: str | None = None
    wd: str | None = None
    site: str | None = None
    host: str | None = None
    location: str | None = None
    feed: str | None = None
    search_text: str | None = None
    search_terms: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    enabled: bool = True


def load_companies(settings: Settings) -> list[CompanyConfig]:
    data = load_yaml(settings.config_file(COMPANIES_FILE))
    companies: list[CompanyConfig] = []
    for entry in data.get("companies") or []:
        if not isinstance(entry, dict) or "name" not in entry or "ats" not in entry:
            raise ConfigError(f"companies.yaml entry needs name and ats: {entry!r}")
        companies.append(
            CompanyConfig(
                name=str(entry["name"]),
                ats=str(entry["ats"]).lower(),
                token=entry.get("token"),
                tenant=entry.get("tenant"),
                wd=entry.get("wd"),
                site=entry.get("site"),
                host=entry.get("host"),
                location=entry.get("location"),
                feed=entry.get("feed"),
                search_text=entry.get("search_text"),
                search_terms=[str(term) for term in entry.get("search_terms", [])],
                aliases=[str(a).lower() for a in entry.get("aliases", [])],
                domains=[str(d).lower() for d in entry.get("domains", [])],
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return companies


def load_company_aliases(settings: Settings) -> dict[str, list[str]]:
    data = load_yaml(settings.config_file(COMPANIES_FILE))
    aliases = data.get("aliases") or {}
    return {str(k).lower(): [str(v).lower() for v in vals] for k, vals in aliases.items()}


def load_sources(settings: Settings) -> dict[str, Any]:
    return load_yaml(settings.config_file(SOURCES_FILE))


def load_location_aliases(settings: Settings, section: str = "cities") -> dict[str, str]:
    """Flatten one section of locations.yaml into alias -> canonical bucket."""
    data = load_yaml(settings.config_file(LOCATIONS_FILE))
    flat: dict[str, str] = {}
    for canonical, aliases in (data.get(section) or {}).items():
        for alias in aliases or []:
            flat[str(alias).lower()] = str(canonical).lower()
    return flat


def load_skill_aliases(settings: Settings) -> dict[str, list[str]]:
    data = load_yaml(settings.config_file(SKILL_ALIASES_FILE))
    return {str(k).lower(): [str(v).lower() for v in vals] for k, vals in data.items()}


def build_normalizer(settings: Settings) -> Normalizer:
    norm_cfg = settings.section("normalize")
    return Normalizer(
        company_suffixes=frozenset(str(s).lower() for s in norm_cfg.get("company_suffixes", [])),
        company_drop_anywhere=frozenset(
            str(s).lower() for s in norm_cfg.get("company_drop_anywhere", [])
        ),
        keep_query=frozenset(str(s).lower() for s in norm_cfg.get("keep_query", [])),
        location_aliases=load_location_aliases(settings),
        region_aliases=load_location_aliases(settings, "regions"),
    )


def source_priority(settings: Settings) -> dict[str, int]:
    raw = settings.section("source_priority")
    return {str(k).lower(): int(v) for k, v in raw.items()}
