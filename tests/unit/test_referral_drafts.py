"""Outreach templates: frontmatter, rendering, tone/length checks, file naming, second degree."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunt.apply.tailor import load_tailor_config
from jobhunt.config import ConfigError
from jobhunt.referral import second_degree
from jobhunt.referral.drafts import (
    check_draft,
    list_templates,
    load_template,
    parse_template,
    render,
    slugify,
    write_draft,
)

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
TAILOR = load_tailor_config(CONFIG_DIR)
TEMPLATES = ["connection_note_recruiter", "followup_1", "referral_excolleague", "referral_friend"]

# Realistic upper bounds for what the skill will feed in.
LONG_CONTEXT = {
    "first_name": "Subrahmanyam",
    "company": "Platform Science India Pvt",
    "role": "Software Engineer II, Backend Platform (.NET)",
    "job_url": "https://job-boards.greenhouse.io/platformscience/jobs/7452539003?gh_src=1a2b3c4d",
    "hook": (
        "I saw your post about moving the pay pipeline onto Kafka and it is exactly the kind of "
        "problem I have been solving this year."
    ),
    "one_liner": (
        "I am a backend engineer at Platform Science, building .NET and Python services on Kafka "
        "and MongoDB."
    ),
    "resume_link": "https://your-site.example/",
    "signature": "Subrahmanyam",
    "current_company": "Example Corp",
}


def test_list_templates_finds_the_four_shipped_templates():
    assert list_templates(CONFIG_DIR) == TEMPLATES


@pytest.mark.parametrize("name", TEMPLATES)
def test_every_template_renders_clean_with_long_values(name):
    template = load_template(CONFIG_DIR, name)
    text = render(template, LONG_CONTEXT)
    assert check_draft(text, template, TAILOR) == [], (name, len(text), text)
    assert "{" not in text
    if template.kind != "connection_note":
        assert LONG_CONTEXT["job_url"] in text


def test_missing_template_raises():
    with pytest.raises(ConfigError):
        load_template(CONFIG_DIR, "nope")


def test_parse_template_requires_frontmatter_fields():
    with pytest.raises(ConfigError):
        parse_template("x", "no frontmatter here")
    with pytest.raises(ConfigError):
        parse_template("x", "---\nkind: referral\n---\nbody\n")
    tpl = parse_template("x", "---\nkind: referral\nmax_chars: 50\n---\nHi {first_name}\n")
    assert (tpl.kind, tpl.channel, tpl.max_chars, tpl.placeholders) == (
        "referral",
        "linkedin",
        50,
        {"first_name"},
    )


def test_render_leaves_unknown_placeholders_for_the_checker():
    tpl = parse_template("x", "---\nkind: referral\nmax_chars: 500\n---\nHi {first_name}, {hook}\n")
    text = render(tpl, {"first_name": "Priya"})
    assert text == "Hi Priya, {hook}\n"
    issues = check_draft(text, tpl, TAILOR)
    assert issues == ["unfilled placeholder {hook}"]


def test_check_draft_flags_dash_spelling_length_and_exclamations():
    tpl = parse_template("x", "---\nkind: referral\nmax_chars: 40\n---\n{hook}\n")
    text = render(tpl, {"hook": "We optimize everything — really!!! Truly great organization."})
    issues = check_draft(text, tpl, TAILOR)
    assert any("chars" in i for i in issues)
    assert any("banned character" in i for i in issues)
    assert any("'optimize'" in i for i in issues)
    assert any("'organization'" in i for i in issues)
    assert any("exclamation" in i for i in issues)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Priya Natarajan", "priya-natarajan"),
        ("  J. K.  O'Neil ", "j-k-o-neil"),
        ("###", "contact"),
    ],
)
def test_slugify(name, expected):
    assert slugify(name) == expected


def test_write_draft_puts_file_under_job_folder(tmp_path):
    path = write_draft(tmp_path, 42, "Priya Natarajan", "hello\n")
    assert path == tmp_path / "42" / "priya-natarajan.md"
    assert path.read_text(encoding="utf-8") == "hello\n"


def test_people_search_url_is_a_linkedin_search_the_user_opens():
    url = second_degree.people_search_url("Platform Science", ["recruiter", "engineering manager"])
    assert url.startswith("https://www.linkedin.com/search/results/people/?keywords=")
    assert "Platform+Science+recruiter+engineering+manager" in url


def test_connection_note_enforces_300_chars():
    tpl = load_template(CONFIG_DIR, "connection_note_recruiter")
    note = second_degree.connection_note(tpl, LONG_CONTEXT)
    assert len(note) <= second_degree.CONNECTION_NOTE_LIMIT
    huge = dict(LONG_CONTEXT, role="x" * 400)
    with pytest.raises(second_degree.DraftTooLong):
        second_degree.connection_note(tpl, huge)
