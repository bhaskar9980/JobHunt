from pathlib import Path

import pytest
import yaml

from jobhunt.config import Profile
from jobhunt.match import (
    apply_prefilter,
    location_ok,
    parse_min_years,
    prefilter,
    skill_hits,
    skill_overlap,
    title_matches,
    title_predicate,
)
from jobhunt.models import Status
from jobhunt.repo.jobs import current_status, get_job, upsert_job


def test_profile_change_rechecks_existing_machine_shortlist(conn, norm, priority, make_posting):
    from jobhunt.repo.jobs import apply_score, set_job_status

    before = Profile.from_dict(
        {"targets": {"target_titles": ["software engineer"], "locations": ["bengaluru"]}}
    )
    after = Profile.from_dict(
        {"targets": {"target_titles": ["software engineer"], "locations": ["chennai"]}}
    )
    job = upsert_job(conn, make_posting(), norm, priority).job_id
    apply_prefilter(conn, before, {})
    apply_score(conn, job, 90, {"reasons": ["old preference"]})
    set_job_status(conn, job, Status.SHORTLISTED, force=True)
    apply_prefilter(conn, after, {})
    row = get_job(conn, job)
    assert row["pipeline_status"] == Status.FILTERED
    assert row["score"] is None


PROFILE = Profile.from_dict(
    {
        "targets": {
            "target_titles": [
                "software engineer",
                "software engineer ii",
                "sde 2",
                ".net developer",
                "member of technical staff",
            ],
            "exclude_titles": ["senior", "staff", "frontend", "intern", "test"],
            "seniority_max_years_required": 5,
            "locations": ["chennai", "bengaluru", "remote"],
            "relocatable_locations": ["mumbai", "india"],
            "remote_regions_excluded": ["us", "usa", "eu", "united states"],
            "foreign_markers": ["romania", "mauritius", "south korea"],
            "remote_ok": True,
            "relocate_ok": True,
        },
        "skills": {
            "must_have_any": ["c#", ".net", "python"],
            "core": ["c#", ".net", "mongodb", "kafka"],
            "nice_to_have": ["angular", "terraform"],
            "deal_breakers": ["security clearance", "us citizens only"],
        },
        "scoring": {"shortlist_min_score": 70},
    }
)
ALIASES = {
    "c#": ["c#", "csharp"],
    ".net": [".net", "dotnet", "dot net", "asp.net core"],
    "python": ["python"],
    "mongodb": ["mongodb", "mongo"],
    "kafka": ["kafka"],
    "angular": ["angular"],
    "terraform": ["terraform"],
}


@pytest.mark.parametrize(
    ("title_norm", "expected", "reason_prefix"),
    [
        ("software engineer 2", True, "title matched"),
        ("senior software engineer", False, "title excluded"),
        ("software engineer 2 frontend", False, "title excluded"),
        (".net developer", True, "title matched"),
        ("member technical staff", True, "title matched"),
        ("member technical staff 2 backend", True, "title matched"),
        ("staff software engineer", False, "title excluded"),
        ("senior member technical staff", False, "title excluded"),
        ("product manager", False, "title not in targets"),
        ("software test engineer", False, "title excluded"),
    ],
)
def test_title_matches(title_norm, expected, reason_prefix):
    ok, reason = title_matches(
        title_norm, PROFILE.targets.target_titles, PROFILE.targets.exclude_titles
    )
    assert ok is expected
    assert reason.startswith(reason_prefix)


@pytest.mark.parametrize(
    ("bucket", "is_remote", "expected"),
    [
        ("chennai", False, True),
        ("remote", False, True),
        ("london", True, True),
        ("london", False, False),
        ("unknown", False, True),
        ("mumbai", False, True),
        ("sf", False, False),
    ],
)
def test_location_ok(bucket, is_remote, expected):
    ok, _ = location_ok(bucket, is_remote, PROFILE)
    assert ok is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3-5 years of experience with C#", 3),
        ("5+ years experience building services", 5),
        ("At least 2 years of professional experience", 2),
        ("We have been in business for 20 years and love coffee", None),
        ("Requires 8 years of experience; nice to have 2 years with Kafka", 2),
        ("", None),
        (None, None),
    ],
)
def test_parse_min_years(text, expected):
    assert parse_min_years(text) == expected


def test_skill_overlap_weights():
    text = "we use c# and asp.net core with mongodb; angular on the frontend"
    # core hits: c#, .net, mongodb = 3.0 ; nice hits: angular = 0.5 ; total = 4 + 1 = 5
    assert skill_overlap(text, PROFILE.skills.core, PROFILE.skills.nice_to_have, ALIASES) == 0.7
    assert (
        skill_overlap("nothing relevant", PROFILE.skills.core, PROFILE.skills.nice_to_have, ALIASES)
        == 0.0
    )


def _job(**overrides):
    base = {
        "title": "Software Engineer II",
        "title_norm": "software engineer 2",
        "location_bucket": "bengaluru",
        "is_remote": 0,
        "description_md": "Build C# services on .NET with MongoDB. 3-5 years of experience.",
    }
    base.update(overrides)
    return base


def test_prefilter_pass_collects_reasons():
    result = prefilter(_job(), PROFILE, ALIASES)
    assert result.passed
    assert result.min_years == 3
    assert result.overlap > 0
    assert any(r.startswith("skill overlap") for r in result.reasons)


def test_prefilter_deal_breaker():
    result = prefilter(_job(description_md="Must be US citizens only. C# role."), PROFILE, ALIASES)
    assert not result.passed
    assert result.reasons[-1].startswith("deal breaker")


def test_prefilter_too_senior():
    result = prefilter(_job(description_md="8+ years of experience in C#"), PROFILE, ALIASES)
    assert not result.passed
    assert "8+ years" in result.reasons[-1]


def test_prefilter_missing_must_have():
    result = prefilter(
        _job(description_md="Java and Spring Boot, 3 years experience"), PROFILE, ALIASES
    )
    assert not result.passed
    assert "none of" in result.reasons[-1]


def test_prefilter_without_description_passes_provisionally():
    result = prefilter(_job(description_md=None), PROFILE, ALIASES)
    assert result.passed
    assert result.reasons[-1] == "no description yet"


def test_apply_prefilter_moves_statuses_and_rescan_restores(conn, norm, priority, make_posting):
    good = upsert_job(conn, make_posting(source_job_id="1"), norm, priority).job_id
    bad = upsert_job(
        conn,
        make_posting(
            source_job_id="2",
            title="Senior Software Engineer",
            url="https://boards.greenhouse.io/acme/jobs/2",
        ),
        norm,
        priority,
    ).job_id
    summary = apply_prefilter(conn, PROFILE, ALIASES)
    assert summary.evaluated == 2
    assert summary.passed == 1
    assert summary.filtered == 1
    assert current_status(conn, good) == Status.DISCOVERED
    assert current_status(conn, bad) == Status.FILTERED
    assert get_job(conn, good)["prefilter_pass"] == 1

    # relax the profile: senior is fine now -> rescan restores the job
    relaxed = Profile.from_dict(
        {
            "targets": {
                "target_titles": ["software engineer"],
                "exclude_titles": [],
                "locations": ["bengaluru"],
            },
            "skills": {
                "must_have_any": [".net"],
                "core": [".net"],
                "nice_to_have": [],
                "deal_breakers": [],
            },
        }
    )
    summary = apply_prefilter(conn, relaxed, ALIASES, rescan=True)
    assert summary.restored == 1
    assert current_status(conn, bad) == Status.DISCOVERED


def test_title_predicate_uses_title_and_location(conn, norm, priority, make_posting):
    job_id = upsert_job(conn, make_posting(), norm, priority).job_id
    row = get_job(conn, job_id)
    assert title_predicate(PROFILE)(row) is True
    far = upsert_job(
        conn,
        make_posting(source_job_id="9", url="https://x.io/9", location="San Francisco, CA"),
        norm,
        priority,
    ).job_id
    assert title_predicate(PROFILE)(get_job(conn, far)) is False


@pytest.mark.parametrize(
    ("bucket", "is_remote", "text", "expected", "reason_prefix"),
    [
        ("remote", True, "Remote - US", False, "remote restricted to us"),
        ("remote", True, "Remote (USA)", False, "remote restricted to usa"),
        ("remote", True, "REMOTE (EU)", False, "remote restricted to eu"),
        ("remote", True, "Anywhere in the World", True, "remote"),
        ("remote", True, "Remote - India", True, "remote"),
        ("remote", True, "Remote (Austin)", True, "remote"),
        ("remote", True, "New York, NY (HQ)", False, "remote restricted to us state NY"),
        ("unknown", False, "Chicago, IL", False, "location us state IL"),
        ("unknown", False, "Bucharest, Romania", False, "location romania"),
        ("unknown", False, "Seoul, South Korea", False, "location south korea"),
        ("unknown", False, "Mauritius", False, "location mauritius"),
        ("unknown", False, "Coimbatore", True, "location unknown"),
        ("unknown", False, None, True, "location unknown"),
    ],
)
def test_location_ok_uses_location_text(bucket, is_remote, text, expected, reason_prefix):
    ok, reason = location_ok(bucket, is_remote, PROFILE, text)
    assert ok is expected
    assert reason.startswith(reason_prefix)


def test_prefilter_filters_us_remote_by_text():
    job = _job(location_bucket="remote", is_remote=1, location="Remote - US")
    result = prefilter(job, PROFILE, ALIASES)
    assert result.passed is False
    assert "remote restricted to us" in result.reasons


@pytest.mark.parametrize(
    ("text", "expected", "reason"),
    [
        # Real state codes still reject, and are still named as states.
        ("Chicago, IL", False, "location us state IL not in targets"),
        ("Austin, TX", False, "location us state TX not in targets"),
        # An ISO country code is not a US state and must not be reported as one.
        ("Lisbon, PT", False, "location country code PT not in targets"),
        ("Amsterdam, NL", False, "location country code NL not in targets"),
        # "IN" is Indiana and India. Reading it as Indiana silently dropped Indian roles.
        ("Coimbatore, IND-31, IN", True, "location unknown"),
        ("Kochi, IN", True, "location unknown"),
        ("Bengaluru, IND-29, IN, 560035", True, "location unknown"),
    ],
)
def test_a_two_letter_location_code_is_read_correctly(text, expected, reason):
    ok, got = location_ok("unknown", False, PROFILE, text)
    assert ok is expected
    assert got == reason


LANGUAGE_PROFILE = Profile.from_dict(
    {
        "targets": {
            "target_titles": [
                "software engineer",
                "full stack developer",
                "full stack engineer",
                "backend developer",
            ],
            "exclude_titles": ["senior"],
            "exclude_title_languages": ["java", "python", "go", "golang", "node.js", "c++"],
            "locations": ["bengaluru"],
        },
        "skills": {"must_have_any": ["c#", ".net"]},
    }
)


@pytest.mark.parametrize(
    ("title", "passed", "last_reason"),
    [
        ("Java Full Stack Developer with DevOps", False, "title names java, not c# or .net"),
        ("Backend Developer (Python)", False, "title names python, not c# or .net"),
        ("Software Engineer - Go", False, "title names go, not c# or .net"),
        ("C++ Software Engineer", False, "title names c++, not c# or .net"),
        ("Java/.NET Full Stack Developer", True, "skill overlap"),
        ("Full Stack Engineer (Dot Net AND Python)", True, "skill overlap"),
        ("Software Engineer, C#", True, "skill overlap"),
        ("Software Engineer", True, "skill overlap"),
    ],
)
def test_a_title_built_around_another_language_is_filtered(title, passed, last_reason):
    from jobhunt.normalize import normalize_title

    # The description names his stack, so only the title rule can reject these.
    job = _job(title=title, title_norm=normalize_title(title), description_md="C# on .NET")
    result = prefilter(job, LANGUAGE_PROFILE, ALIASES)
    assert result.passed is passed
    assert result.reasons[-1].startswith(last_reason)


def test_shipped_aliases_read_dot_net_as_dotnet():
    path = Path(__file__).parents[2] / "config" / "skill_aliases.yaml"
    aliases = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert skill_hits("experience with dot net and dot-net core", [".net"], aliases) == [".net"]


def test_the_title_language_rule_is_off_when_the_profile_lists_no_languages():
    job = _job(title="Java Developer", title_norm="java developer")
    profile = Profile.from_dict(
        {"targets": {"target_titles": ["java developer"], "locations": ["bengaluru"]}}
    )
    assert prefilter(job, profile, ALIASES).passed


@pytest.fixture
def international_profile():
    from dataclasses import asdict

    data = asdict(PROFILE)
    data["targets"]["international_remote_or_relocation"] = True
    path = Path(__file__).parents[2] / "config" / "locations.yaml"
    data["targets"]["international_rules"] = yaml.safe_load(path.read_text(encoding="utf-8")).get(
        "international_eligibility", {}
    )
    return Profile.from_dict(data)


@pytest.mark.parametrize(
    ("location", "bucket", "remote", "evidence", "passed"),
    [
        ("Bengaluru, India", "bengaluru", False, "", True),
        ("Bengaluru, India", "unknown", False, "", True),
        ("Remote - India", "remote", True, "", True),
        ("Remote (Worldwide)", "remote", True, "", True),
        ("Remote", "remote", True, "Work from anywhere in the world.", True),
        ("Remote", "remote", True, "This role can be performed remotely from India.", True),
        (
            "London, UK",
            "london",
            False,
            "We offer visa sponsorship and relocation assistance.",
            True,
        ),
        (
            "London, UK",
            "london",
            False,
            "We can sponsor visas and provide a relocation package.",
            True,
        ),
        (
            "Bangkok, Thailand",
            "unknown",
            False,
            "Full Relocation and Onboarding Support. Visa sponsorship for you and your family.",
            True,
        ),
        ("Remote - US", "remote", True, "", False),
        ("Remote - EU", "remote", True, "", False),
        ("Remote", "remote", True, "We are fully remote and serve customers worldwide.", False),
        (
            "Remote",
            "remote",
            True,
            "Work from anywhere in the world. This role is only open to US residents.",
            False,
        ),
        ("Remote - US", "remote", True, "Work from anywhere in the world.", False),
        (
            "Remote",
            "remote",
            True,
            "Work from anywhere in the world for up to 30 days a year.",
            False,
        ),
        ("Amsterdam, NL", "amsterdam", False, "We offer a relocation package.", False),
        (
            "London, UK",
            "london",
            False,
            "Visa sponsorship is available. Relocation assistance is not available.",
            False,
        ),
        (
            "London, UK",
            "london",
            False,
            "We cannot sponsor visas. We provide relocation assistance.",
            False,
        ),
        ("London, UK", "london", False, "Visa sponsorship: No. Relocation assistance: Yes.", False),
        (
            "London, UK",
            "london",
            False,
            "Do you require visa sponsorship and relocation assistance?",
            False,
        ),
        (
            "London, UK",
            "london",
            False,
            "Applicants must be willing to relocate and arrange their own visa.",
            False,
        ),
        ("London, UK", "london", False, "", False),
        (None, "unknown", False, "", False),
        ("Remote - Worldwide", "remote", True, "This is not a remote position.", False),
        (
            "London, UK",
            "london",
            False,
            "We offer visa sponsorship and relocation assistance. "
            "Applicants must currently reside in the US.",
            False,
        ),
    ],
)
def test_international_requires_positive_eligibility_evidence(
    international_profile, location, bucket, remote, evidence, passed
):
    result = prefilter(
        _job(
            location=location,
            location_bucket=bucket,
            is_remote=remote,
            description_md=f"Build C# .NET services. 3 years of experience. {evidence}",
        ),
        international_profile,
        ALIASES,
    )
    assert result.passed is passed
    if not passed:
        assert "eligibility" in result.reasons[-1]


def test_international_unknown_eligibility_is_filtered_but_still_scored(
    conn, norm, priority, make_posting, international_profile
):
    """Parked is not decided. The job stays filtered but goes to the scorer for a verdict."""
    from jobhunt.repo.jobs import unscored_candidates

    job_id = upsert_job(
        conn, make_posting(location="Remote", is_remote=True), norm, priority
    ).job_id
    apply_prefilter(conn, international_profile, ALIASES)
    assert get_job(conn, job_id)["status"] == Status.FILTERED
    assert [row["id"] for row in unscored_candidates(conn, 10)] == [job_id]


def test_international_hydration_can_discover_sponsorship(
    conn, norm, priority, make_posting, international_profile
):
    from jobhunt.repo.jobs import needing_hydration, unscored_candidates

    job_id = upsert_job(
        conn,
        make_posting(location="London, UK", description_md=None, needs_hydration=True),
        norm,
        priority,
    ).job_id
    assert title_predicate(international_profile)(get_job(conn, job_id))
    apply_prefilter(conn, international_profile, ALIASES)
    assert [r["id"] for r in needing_hydration(conn, 10)] == [job_id]
    assert unscored_candidates(conn, 10) == []


def test_international_missing_description_without_hydration_is_not_eligible(international_profile):
    result = prefilter(
        _job(location="Remote", location_bucket="remote", is_remote=True, description_md=None),
        international_profile,
        ALIASES,
    )
    assert not result.passed


@pytest.mark.parametrize("description", [None, "C# .NET job summary"])
def test_international_failed_hydration_rechecks_eligibility(
    conn, norm, priority, make_posting, international_profile, description
):
    from jobhunt.repo.jobs import clear_hydration, needing_hydration, unscored_candidates

    job_id = upsert_job(
        conn,
        make_posting(location="London, UK", description_md=description, needs_hydration=True),
        norm,
        priority,
    ).job_id
    apply_prefilter(conn, international_profile, ALIASES)
    assert [r["id"] for r in needing_hydration(conn, 10)] == [job_id]
    clear_hydration(conn, job_id, "Description unavailable")
    assert unscored_candidates(conn, 10) == []
    apply_prefilter(conn, international_profile, ALIASES)
    assert get_job(conn, job_id)["status"] == Status.FILTERED
