import subprocess
from pathlib import Path

import pytest
from pypdf import PdfWriter

from jobhunt.apply.fillers.base import Answer, AnswerSet
from jobhunt.apply.schema import FieldKind, FormField
from jobhunt.apply.tailor import (
    CoverLetterError,
    clean_latex,
    extract_resume_facts,
    latex_escape,
    letter_to_tex,
    load_tailor_config,
    numbers_in,
    render_answer_sheet,
    render_cover_letter_pdf,
    resolve_pdflatex,
    resume_items,
    validate_cover_letter,
)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
CFG = load_tailor_config(CONFIG_DIR)

FACTS = (
    "Developed 20 salary rules. Payroll runs of 1,000+ employees. 12 microservices. 80% coverage."
)

GOOD = (
    "I am writing to apply for the Software Engineer II role. At Platform Science I developed "
    "the core of a salary calculation engine on .NET and MongoDB that computes driver pay through "
    "20 salary rules, and built the calculation management service that splits payroll runs of "
    "1,000+ employees into scheduled batches tracked through Kafka events. I also built the "
    "activity data platform behind the product, part of 12 microservices created from scratch, "
    "and raised test coverage above 80% on five services. Your organisation's focus on reliable "
    "backend systems for logistics matches the problems I have spent the last three years "
    "solving, and I would bring the same care for correctness, observability and clear ownership "
    "to your team. I would welcome the chance to talk about how my experience with event-driven "
    "services and payroll-grade data integrity could help you. Thank you for considering my "
    "application, and I look forward to hearing from you soon about next steps in the process."
)


def test_good_letter_passes():
    assert validate_cover_letter(GOOD, FACTS, CFG) == []


def test_word_count_bounds():
    short = "Too short."
    issues = validate_cover_letter(short, FACTS, CFG)
    assert any("words" in i for i in issues)


def test_em_dash_and_banned_phrase_and_spelling():
    bad = GOOD.replace("role. At", "role — at").replace("organisation", "organization")
    bad += " We shipped FleetHours to customers."
    issues = validate_cover_letter(bad, FACTS, CFG)
    assert any("banned character" in i for i in issues)
    assert any("organization" in i for i in issues)
    assert any("shipped fleethours" in i for i in issues)


def test_numbers_must_come_from_facts_or_job_description():
    bad = GOOD.replace("20 salary rules", "25 salary rules")
    issues = validate_cover_letter(bad, FACTS, CFG)
    assert any("'25'" in i for i in issues)
    # a number quoted from the job description is fine
    ok = GOOD + " You ask for 3-5 years of experience, which matches my background."
    assert validate_cover_letter(ok, FACTS, CFG, job_description="3-5 years of experience") == []
    assert validate_cover_letter(ok, FACTS, CFG) != []


def test_numbers_in_normalises_commas_and_suffixes():
    assert numbers_in("1,000+ employees, 80% coverage, 3.5 years") == {"1000", "80", "3.5"}


TEX = "\n".join(
    [
        r"\resumeSubheading",
        r"  {Platform Science (previously Trimble)}{Jun 2023 -- Present}"
        r"{\footnotesize Trimble's transport division}{Chennai, India}",
        r"\resumeSubSubheading",
        r"  {Software Engineer II}{Apr 2025 -- Present}",
        r"\resumeItemListStart",
        r"  \resumeItem{Developed the core of the \textbf{salary calculation engine}: "
        r"\textbf{20 salary rules} covering leave and overtime.}",
        r"  \resumeItem{Cleared \textbf{16 Snyk vulnerabilities} (1 critical, 7 high) "
        r"and raised coverage above \textbf{80\%}.}",
        r"  \resumeItem{Published a paper at \href{https://ieeexplore.ieee.org/document/10538542}"
        r"{\textcolor{accent}{\textbf{IEEE TrustCom 2023}}} with $\sim$30\% faster mining.}",
        r"\resumeItemListEnd",
    ]
)


def test_resume_items_and_clean_latex():
    items = resume_items(TEX)
    assert len(items) == 3
    assert clean_latex(items[0]) == (
        "Developed the core of the salary calculation engine: 20 salary rules covering leave "
        "and overtime."
    )
    assert clean_latex(items[1]) == (
        "Cleared 16 Snyk vulnerabilities (1 critical, 7 high) and raised coverage above 80%."
    )
    assert "IEEE TrustCom 2023 with ~30% faster mining" in clean_latex(items[2])


def test_extract_resume_facts(tmp_path):
    tex = tmp_path / "resume.tex"
    tex.write_text(TEX, encoding="utf-8")
    acc = tmp_path / "Accomplishments.md"
    acc.write_text(
        "# Accomplishments\n355 merged PRs\nno numbers here\n600K activities\n", encoding="utf-8"
    )
    facts = extract_resume_facts(tex, acc, CFG)
    assert (
        "## Roles" in facts
        and "Platform Science (previously Trimble) | Jun 2023 - Present" in facts
    )
    assert "- Software Engineer II | Apr 2025 - Present" in facts
    assert "## Numbers" in facts
    for token in ("1", "7", "16", "20", "30", "80", "2023", "2025"):
        assert token in facts.split("## Numbers")[1].split("##")[0]
    assert "- 355 merged PRs" in facts
    assert "no numbers here" not in facts
    assert extract_resume_facts(tex, None, CFG).count("Accomplishments.md") == 0


def test_render_answer_sheet():
    answers = AnswerSet(
        job_id=5,
        answers={
            "q1": Answer("q1", "Notice period", "60 days", "rule"),
            "q2": Answer("q2", "Work authorised", True, "rule", option="Yes"),
            "q3": Answer("q3", "Optional thing", None, "system", skipped=True),
        },
        unmatched=[FormField("q4", "Why us?", FieldKind.TEXTAREA, True)],
    )
    profile = {
        "first_name": "Test",
        "last_name": "Candidate",
        "email": "x@example.com",
        "phone": {"country_code": "+91", "number": "1"},
        "links": {"linkedin": "https://linkedin.com/in/x"},
    }
    sheet = render_answer_sheet(
        {"id": 5, "company": "Acme", "title": "SE II", "url": "u"}, answers, profile
    )
    assert "# Answer sheet: Acme / SE II (job 5)" in sheet
    assert "- Notice period: 60 days" in sheet
    assert "- Work authorised: Yes" in sheet
    assert "Optional thing" not in sheet
    assert "## Needs your answer" in sheet and "- Why us?" in sheet


def test_clean_latex_strips_bare_size_commands():
    raw = r"{\footnotesize Trimble's transport division, acquired in Feb 2025}"
    assert clean_latex(raw) == "Trimble's transport division, acquired in Feb 2025"
    assert clean_latex(r"\scshape Software \small Engineer") == "Software Engineer"


PROFILE = {
    "first_name": "Test",
    "last_name": "Candidate",
    "email": "b@test.invalid",
    "phone": {"country_code": "+91", "number": "98765"},
    "links": {"linkedin": "https://linkedin.com/in/x"},
}


def test_latex_escape():
    assert latex_escape("C# & .NET 100% _x_ {y}") == r"C\# \& .NET 100\% \_x\_ \{y\}"


def test_letter_to_tex_has_header_and_paragraphs():
    tex = letter_to_tex("# Dear team\n\nFirst **para** with 50%.\n\nSecond para.\n", PROFILE, CFG)
    assert r"\textbf{\large Test Candidate}" in tex
    assert "b@test.invalid" in tex and "+91 98765" in tex
    assert r"First \textbf{para} with 50\%." in tex
    assert "Second para." in tex
    assert "Dear team" in tex and "# Dear" not in tex
    assert tex.count(r"\documentclass") == 1


def _fake_pdflatex(pages: int | None):
    def run(command, **_kwargs):
        out_dir = next(a.split("=", 1)[1] for a in command if a.startswith("-output-directory="))
        if pages:
            writer = PdfWriter()
            for _ in range(pages):
                writer.add_blank_page(width=595, height=842)
            with (Path(out_dir) / "cover_letter.pdf").open("wb") as handle:
                writer.write(handle)
        return subprocess.CompletedProcess(
            command, 0 if pages else 1, stdout="! LaTeX Error: boom", stderr=""
        )

    return run


def test_render_cover_letter_pdf_one_page(tmp_path):
    letter = tmp_path / "cover_letter.md"
    letter.write_text(GOOD, encoding="utf-8")
    out = render_cover_letter_pdf(
        letter,
        tmp_path / "cover_letter.pdf",
        PROFILE,
        CFG,
        pdflatex="pdflatex",
        run=_fake_pdflatex(1),
    )
    assert out.exists()
    assert (tmp_path / "_cover_build" / "cover_letter.tex").exists()


def test_render_cover_letter_pdf_rejects_two_pages(tmp_path):
    letter = tmp_path / "cover_letter.md"
    letter.write_text(GOOD, encoding="utf-8")
    with pytest.raises(CoverLetterError, match="2 pages"):
        render_cover_letter_pdf(
            letter, tmp_path / "cover_letter.pdf", PROFILE, CFG, pdflatex="x", run=_fake_pdflatex(2)
        )
    assert not (tmp_path / "cover_letter.pdf").exists()


def test_render_cover_letter_pdf_reports_latex_failure(tmp_path):
    letter = tmp_path / "cover_letter.md"
    letter.write_text(GOOD, encoding="utf-8")
    with pytest.raises(CoverLetterError, match="LaTeX Error"):
        render_cover_letter_pdf(
            letter,
            tmp_path / "cover_letter.pdf",
            PROFILE,
            CFG,
            pdflatex="x",
            run=_fake_pdflatex(None),
        )


def test_resolve_pdflatex_prefers_env(monkeypatch):
    monkeypatch.setenv("JOBHUNT_PDFLATEX", "X:/tex/pdflatex.exe")
    assert resolve_pdflatex(CFG) == "X:/tex/pdflatex.exe"
