"""Tailored text guards: cover-letter validation against the resume rulebook, resume facts export,
and the answer cheat-sheet used in assisted mode."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pypdf import PdfReader

from jobhunt.apply.fillers.base import AnswerSet
from jobhunt.config import ConfigError

log = logging.getLogger(__name__)

TAILOR_FILE = "tailor.yaml"
DEFAULT_MIN_WORDS = 120
DEFAULT_MAX_WORDS = 250
DEFAULT_MAX_ACCOMPLISHMENT_LINES = 300
COVER_PDF_SECTION = "cover_letter_pdf"
ENV_PDFLATEX = "JOBHUNT_PDFLATEX"
COVER_BUILD_DIR = "_cover_build"
COVER_TEX_NAME = "cover_letter.tex"
COVER_PDF_NAME = "cover_letter.pdf"
DEFAULT_FONT_SIZE = "11pt"
DEFAULT_MARGIN = "1in"
PDFLATEX_TIMEOUT_SECONDS = 120
LOG_TAIL_CHARS = 1500
REQUIRED_PAGES = 1

_NUMBER = re.compile(r"(?<![\w/])\d[\d,]*(?:\.\d+)?\+?%?")
_WORD = re.compile(r"[A-Za-z']+")
_SPELL_WORD = re.compile(r"[A-Za-z]+")
_RESUME_ITEM = re.compile(r"\\resumeItem\{")
_SUBHEADING = re.compile(
    r"\\resumeSubheading\s*\{([^}]*)\}\s*\{([^}]*)\}\s*\{([^}]*)\}\s*\{([^}]*)\}"
)
_SUBSUB = re.compile(r"\\resumeSubSubheading\s*\{([^}]*)\}\s*\{([^}]*)\}")
_LATEX_CMD_WITH_ARG = re.compile(
    r"\\(?:textbf|emph|textit|scshape|small|footnotesize|textcolor\{[^}]*\})\{"
)
_HREF = re.compile(r"\\href\{[^}]*\}\{([^}]*)\}")
_FAICON = re.compile(r"\\faIcon\{[^}]*\}")
_SIMPLE_CMDS = re.compile(r"\\(?:vspace\{[^}]*\}|\\|,|;|newline|par)")
_MATH_SIM = re.compile(r"\$\\sim\$")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADING = re.compile(r"^#+\s*", re.M)
_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_TEX_TEMPLATE = (
    "\\documentclass[{font_size},a4paper]{{article}}\n"
    "\\usepackage[margin={margin}]{{geometry}}\n"
    "\\usepackage[T1]{{fontenc}}\n"
    "\\usepackage{{lmodern}}\n"
    "\\usepackage{{parskip}}\n"
    "\\usepackage[hidelinks]{{hyperref}}\n"
    "\\pagestyle{{empty}}\n"
    "\\begin{{document}}\n"
    "\\noindent\\textbf{{\\large {name}}}\\\\\n"
    "{contact}\\\\[1em]\n"
    "{today}\\\\[1em]\n"
    "{body}\n"
    "\\end{{document}}\n"
)
RunFn = Callable[..., subprocess.CompletedProcess[str]]


class CoverLetterError(RuntimeError):
    """The cover letter could not be rendered to a valid one-page PDF."""


_BARE_CMDS = re.compile(
    r"\\(?:footnotesize|scriptsize|small|normalsize|large|scshape|bfseries|itshape|"
    r"raggedright|centering)\b"
)


def load_tailor_config(config_dir: Path) -> dict[str, Any]:
    path = config_dir / TAILOR_FILE
    if not path.exists():
        raise ConfigError(f"missing {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------- cover letter


def validate_cover_letter(
    text: str,
    facts: str,
    cfg: Mapping[str, Any],
    *,
    job_description: str | None = None,
) -> list[str]:
    """Return a list of problems; an empty list means the letter passes the rulebook."""
    rules = cfg.get("cover_letter") or {}
    issues: list[str] = []
    words = len(_WORD.findall(text))
    low, high = (
        int(rules.get("min_words", DEFAULT_MIN_WORDS)),
        int(rules.get("max_words", DEFAULT_MAX_WORDS)),
    )
    if words < low or words > high:
        issues.append(f"{words} words; expected {low}-{high}")
    for char in rules.get("banned_characters") or []:
        if char in text:
            issues.append(
                f"contains banned character {char!r} (use a comma, colon or new sentence)"
            )
    lowered = text.lower()
    for phrase in rules.get("banned_phrases") or []:
        if str(phrase).lower() in lowered:
            issues.append(f"contains banned phrase: {phrase!r}")
    issues.extend(_spelling_issues(text, rules.get("british_spellings") or {}))
    if rules.get("numbers_must_come_from_facts", True):
        issues.extend(_number_issues(text, facts, job_description))
    return issues


def _spelling_issues(text: str, mapping: Mapping[str, str]) -> list[str]:
    found = {w.lower() for w in _SPELL_WORD.findall(text)}
    return [
        f"american spelling {us!r}; use {uk!r}"
        for us, uk in mapping.items()
        if str(us).lower() in found
    ]


def _number_issues(text: str, facts: str, job_description: str | None) -> list[str]:
    allowed = numbers_in(facts) | numbers_in(job_description or "")
    allowed.add(str(date.today().year))
    return [
        f"number {token!r} does not appear in resume facts or the job description"
        for token in sorted(numbers_in(text) - allowed)
    ]


def numbers_in(text: str) -> set[str]:
    return {_norm_number(m.group(0)) for m in _NUMBER.finditer(text or "")}


def _norm_number(token: str) -> str:
    return token.replace(",", "").rstrip("+").rstrip("%")


# --------------------------------------------------------------------------- cover letter pdf


def latex_escape(text: str) -> str:
    return "".join(_LATEX_SPECIALS.get(char, char) for char in text)


def letter_to_tex(markdown: str, profile: Mapping[str, Any], cfg: Mapping[str, Any]) -> str:
    """Plain paragraphs (blank-line separated) with a name/contact header; **bold** survives."""
    pdf_cfg = cfg.get(COVER_PDF_SECTION) or {}
    body = _MD_HEADING.sub("", markdown)
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", body) if p.strip()]
    escaped = [_MD_BOLD.sub(r"\\textbf{\1}", latex_escape(p)) for p in paragraphs]
    phone = profile.get("phone") or {}
    links = profile.get("links") or {}
    contact_parts = [
        str(profile.get("email") or ""),
        f"{phone.get('country_code', '')} {phone.get('number', '')}".strip(),
        str(links.get("linkedin") or ""),
    ]
    contact = r" \textbullet{} ".join(latex_escape(part) for part in contact_parts if part)
    return _TEX_TEMPLATE.format(
        font_size=str(pdf_cfg.get("font_size", DEFAULT_FONT_SIZE)),
        margin=str(pdf_cfg.get("margin", DEFAULT_MARGIN)),
        name=latex_escape(
            f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
        ),
        contact=contact,
        today=date.today().strftime("%d %B %Y"),
        body="\n\n".join(escaped),
    )


def resolve_pdflatex(cfg: Mapping[str, Any]) -> str:
    """JOBHUNT_PDFLATEX, then tailor.yaml, then whatever is on PATH."""
    from_env = os.getenv(ENV_PDFLATEX)
    if from_env:
        return from_env
    configured = (cfg.get(COVER_PDF_SECTION) or {}).get("pdflatex")
    if configured and Path(str(configured)).exists():
        return str(configured)
    found = shutil.which("pdflatex")
    if found:
        return found
    raise CoverLetterError(
        f"pdflatex not found; set {ENV_PDFLATEX} in .env or {COVER_PDF_SECTION}.pdflatex"
    )


def render_cover_letter_pdf(
    letter_path: Path,
    out_pdf: Path,
    profile: Mapping[str, Any],
    cfg: Mapping[str, Any],
    *,
    pdflatex: str,
    run: RunFn = subprocess.run,
) -> Path:
    """Compile cover_letter.md to a one-page PDF next to it. Raises CoverLetterError otherwise."""
    build_dir = out_pdf.parent / COVER_BUILD_DIR
    build_dir.mkdir(parents=True, exist_ok=True)
    tex_path = build_dir / COVER_TEX_NAME
    tex_path.write_text(
        letter_to_tex(letter_path.read_text(encoding="utf-8"), profile, cfg), encoding="utf-8"
    )
    command = [
        pdflatex,
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-output-directory={build_dir}",
        str(tex_path),
    ]
    result = run(
        command, capture_output=True, text=True, timeout=PDFLATEX_TIMEOUT_SECONDS, check=False
    )
    produced = build_dir / COVER_PDF_NAME
    if result.returncode != 0 or not produced.exists():
        tail = (result.stdout or "")[-LOG_TAIL_CHARS:]
        raise CoverLetterError(f"pdflatex failed (exit {result.returncode}): {tail}")
    pages = len(PdfReader(str(produced)).pages)
    if pages != REQUIRED_PAGES:
        raise CoverLetterError(f"cover letter is {pages} pages; must be {REQUIRED_PAGES}")
    shutil.copyfile(produced, out_pdf)
    log.info("cover letter PDF written to %s", out_pdf)
    return out_pdf


# --------------------------------------------------------------------------- resume facts


def extract_resume_facts(
    tex_path: Path, accomplishments_path: Path | None, cfg: Mapping[str, Any]
) -> str:
    """Bullets, roles and every number from the .tex, plus numbered Accomplishments.md lines."""
    tex = tex_path.read_text(encoding="utf-8")
    bullets = [clean_latex(b) for b in resume_items(tex)]
    roles = [
        f"{clean_latex(a)} | {clean_latex(b)} | {clean_latex(c)} | {clean_latex(d)}"
        for a, b, c, d in _SUBHEADING.findall(tex)
    ] + [f"{clean_latex(a)} | {clean_latex(b)}" for a, b in _SUBSUB.findall(tex)]
    numbers = sorted(numbers_in("\n".join(bullets + roles)), key=_sort_key)
    lines = [
        f"# Resume facts (generated {date.today().isoformat()} from {tex_path.name})",
        "",
        "Every number in a cover letter or answer must appear below. Do not round up or invent.",
        "",
        "## Roles",
        *[f"- {r}" for r in roles],
        "",
        "## Bullets",
        *[f"- {b}" for b in bullets],
        "",
        "## Numbers",
        ", ".join(numbers),
    ]
    extra = _accomplishment_lines(accomplishments_path, cfg)
    if extra:
        lines += ["", "## Accomplishments.md (lines with numbers)", *extra]
    return "\n".join(lines) + "\n"


def _sort_key(token: str) -> tuple[float, str]:
    try:
        return float(token), token
    except ValueError:
        return float("inf"), token


def _accomplishment_lines(path: Path | None, cfg: Mapping[str, Any]) -> list[str]:
    if path is None or not path.exists():
        return []
    limit = int(
        (cfg.get("resume_facts") or {}).get(
            "max_accomplishment_lines", DEFAULT_MAX_ACCOMPLISHMENT_LINES
        )
    )
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and _NUMBER.search(stripped):
            out.append(f"- {stripped[:300]}")
        if len(out) >= limit:
            break
    return out


def resume_items(tex: str) -> list[str]:
    """Contents of every \\resumeItem{...}, with balanced-brace matching."""
    items: list[str] = []
    for match in _RESUME_ITEM.finditer(tex):
        start = match.end()
        depth, index = 1, start
        while index < len(tex) and depth:
            char = tex[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        items.append(tex[start : index - 1])
    return items


def clean_latex(fragment: str) -> str:
    text = _HREF.sub(r"\1", fragment)
    text = _FAICON.sub("", text)
    text = _MATH_SIM.sub("~", text)
    for _ in range(4):
        text = _LATEX_CMD_WITH_ARG.sub("", text)
    text = _SIMPLE_CMDS.sub(" ", text)
    text = _BARE_CMDS.sub(" ", text)
    text = text.replace("\\%", "%").replace("\\&", "&").replace("\\#", "#").replace("\\$", "$")
    text = text.replace("{", "").replace("}", "").replace("--", "-")
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- answer sheet


def render_answer_sheet(
    job: Mapping[str, Any], answers: AnswerSet, profile: Mapping[str, Any]
) -> str:
    links = profile.get("links") or {}
    phone = profile.get("phone") or {}
    lines = [
        f"# Answer sheet: {job.get('company')} / {job.get('title')} (job {job.get('id')})",
        "",
        f"Apply URL: {job.get('apply_url') or job.get('url')}",
        "",
        "## Profile",
        f"- Name: {profile.get('first_name', '')} {profile.get('last_name', '')}",
        f"- Email: {profile.get('email', '')}",
        f"- Phone: {phone.get('country_code', '')} {phone.get('number', '')}",
        f"- LinkedIn: {links.get('linkedin', '')}",
        f"- GitHub: {links.get('github', '')}",
        f"- Portfolio: {links.get('portfolio', '')}",
        f"- Current: {profile.get('current_title', '')} at {profile.get('current_company', '')}",
        "",
        "## Answers",
    ]
    for answer in answers.answers.values():
        if answer.skipped:
            continue
        shown = answer.option if answer.option is not None else answer.value
        lines.append(f"- {answer.label}: {shown}")
    if answers.unmatched:
        lines += ["", "## Needs your answer", *[f"- {f.label}" for f in answers.unmatched]]
    return "\n".join(lines) + "\n"
