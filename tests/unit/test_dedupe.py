import pytest

from jobhunt.models import UpsertOutcome
from jobhunt.repo.jobs import get_job, job_to_dict, sources_for_job, upsert_job


def test_email_then_ats_collapse_to_one_job_and_ats_wins(conn, norm, priority, make_posting):
    email = make_posting(
        source="linkedin_email",
        source_job_id="4012345678",
        url="https://www.linkedin.com/jobs/view/4012345678/?trackingId=x",
        apply_url=None,
        ats_type=None,
        description_md="short snippet",
        company="Acme",
        location="Bangalore, India",
    )
    first = upsert_job(conn, email, norm, priority)
    assert first.outcome == UpsertOutcome.CREATED

    ats = make_posting(description_md="Full description from Greenhouse.")
    second = upsert_job(conn, ats, norm, priority)
    assert second.outcome == UpsertOutcome.MERGED
    assert second.job_id == first.job_id

    row = get_job(conn, first.job_id)
    assert row["primary_source"] == "greenhouse"
    assert row["description_md"] == "Full description from Greenhouse."
    assert row["apply_url"] == ats.apply_url
    assert row["url_canonical"] == "https://boards.greenhouse.io/acme/jobs/1001"
    assert [s["source"] for s in sources_for_job(conn, first.job_id)] == [
        "linkedin_email",
        "greenhouse",
    ]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_ats_then_email_keeps_ats_content(conn, norm, priority, make_posting):
    ats_id = upsert_job(conn, make_posting(), norm, priority).job_id
    email = make_posting(
        source="linkedin_email",
        source_job_id="4012345678",
        url="https://www.linkedin.com/jobs/view/4012345678/",
        apply_url=None,
        description_md="snippet",
        company="Acme Inc",
        location="Bengaluru",
    )
    result = upsert_job(conn, email, norm, priority)
    assert result.outcome == UpsertOutcome.MERGED
    assert result.job_id == ats_id
    row = get_job(conn, ats_id)
    assert row["primary_source"] == "greenhouse"
    assert row["description_md"] == "Build .NET microservices with MongoDB and Kafka."


def test_same_source_id_twice_is_refreshed(conn, norm, priority, make_posting):
    first = upsert_job(conn, make_posting(), norm, priority)
    second = upsert_job(conn, make_posting(description_md="updated text"), norm, priority)
    assert second.outcome == UpsertOutcome.REFRESHED
    assert second.job_id == first.job_id
    assert get_job(conn, first.job_id)["description_md"] == "updated text"
    assert conn.execute("SELECT COUNT(*) FROM job_sources").fetchone()[0] == 1


def test_two_distinct_openings_same_source_do_not_merge(conn, norm, priority, make_posting):
    a = upsert_job(conn, make_posting(source_job_id="1"), norm, priority)
    b = upsert_job(
        conn,
        make_posting(source_job_id="2", url="https://boards.greenhouse.io/acme/jobs/2"),
        norm,
        priority,
    )
    assert a.outcome == UpsertOutcome.CREATED
    assert b.outcome == UpsertOutcome.CREATED
    assert a.job_id != b.job_id


def test_url_match_beats_fingerprint(conn, norm, priority, make_posting):
    first = upsert_job(conn, make_posting(), norm, priority)
    other = make_posting(
        source="hn", source_job_id="c1", title="Backend Developer", location="Chennai"
    )
    result = upsert_job(conn, other, norm, priority)
    assert result.outcome == UpsertOutcome.MERGED
    assert result.job_id == first.job_id


@pytest.mark.parametrize("source", ["workday", "eightfold", "successfactors"])
def test_tenant_local_requisition_ids_do_not_merge_employers(conn, norm, priority, source):
    from xml.etree import ElementTree

    from jobhunt.clock import now
    from jobhunt.sources import eightfold, successfactors, workday

    postings = []
    for company in ("Alpha", "Beta"):
        host = company.lower() + ".invalid"
        if source == "workday":
            posting = workday._parse_item(
                {"externalPath": "/job/Engineer_R1", "title": "Engineer"},
                company,
                workday.Site(company.lower(), "wd5", "careers"),
                now(),
            )
        elif source == "eightfold":
            posting = eightfold._parse_position(
                {"id": "1", "name": "Engineer"}, company, host, host
            )
        else:
            item = ElementTree.fromstring(
                f"<item><guid>1</guid><link>https://{host}/job/1</link>"
                "<title>Engineer</title></item>"
            )
            posting = successfactors._to_posting(item, company)
        assert posting is not None
        postings.append(posting)
    assert postings[0].source_job_id != postings[1].source_job_id
    identifiers = [upsert_job(conn, p, norm, priority).job_id for p in postings]
    assert identifiers[0] != identifiers[1]
    assert get_job(conn, identifiers[0])["company"] == "Alpha"


def test_scoped_source_id_reuses_legacy_url_and_preserves_decision(conn, norm, priority):
    from dataclasses import replace

    from jobhunt.clock import now
    from jobhunt.models import Status
    from jobhunt.repo.jobs import set_job_status
    from jobhunt.sources import workday

    path = "/job/Engineer_R1"
    posting = workday._parse_item(
        {"externalPath": path, "title": "Engineer"},
        "Alpha",
        workday.Site("alpha", "wd5", "careers"),
        now(),
    )
    legacy = replace(posting, source_job_id=path, title="Legacy title")
    job = upsert_job(conn, legacy, norm, priority).job_id
    set_job_status(conn, job, Status.APPROVED, force=True)
    assert upsert_job(conn, posting, norm, priority).job_id == job
    assert get_job(conn, job)["pipeline_status"] == Status.APPROVED
    assert get_job(conn, job)["title"] == "Engineer"


def test_job_to_dict_includes_sources_and_flags(conn, norm, priority, make_posting):
    job_id = upsert_job(conn, make_posting(is_remote=True), norm, priority).job_id
    data = job_to_dict(conn, get_job(conn, job_id))
    assert data["is_remote"] is True
    assert data["sources"][0]["source"] == "greenhouse"
    assert data["application"] is None
    assert data["location_bucket"] == "remote"


def test_refresh_updates_authoritative_identity_and_location(conn, norm, priority, make_posting):
    first = make_posting(is_remote=True, location="Remote")
    job_id = upsert_job(conn, first, norm, priority).job_id
    changed = make_posting(
        title="Platform Engineer",
        location="Chennai",
        is_remote=False,
        url="https://example.invalid/jobs/updated",
    )
    upsert_job(conn, changed, norm, priority)
    row = get_job(conn, job_id)
    assert row["url"] == changed.url
    assert sources_for_job(conn, job_id)[0]["url"] == changed.url
    assert row["title"] == changed.title
    assert row["title_norm"] == norm.title(changed.title)
    assert row["location_bucket"] == "chennai"
    assert row["is_remote"] == 0


def test_refresh_invalidates_machine_score_but_preserves_human_decision(
    conn, norm, priority, make_posting
):
    from jobhunt.models import PrefilterResult, Status
    from jobhunt.repo.jobs import apply_score, mark_prefilter, set_job_status

    job_id = upsert_job(conn, make_posting(), norm, priority).job_id
    mark_prefilter(conn, job_id, PrefilterResult(True, ["fit"], 0.8, 3))
    apply_score(conn, job_id, 90, {"reasons": ["fit"]})
    set_job_status(conn, job_id, Status.SHORTLISTED, force=True)
    upsert_job(conn, make_posting(description_md="Different requirements"), norm, priority)
    row = get_job(conn, job_id)
    assert row["score"] is None and row["prefilter_pass"] is None
    assert row["status"] == Status.DISCOVERED
    set_job_status(conn, job_id, Status.DISMISSED)
    upsert_job(conn, make_posting(description_md="Another revision"), norm, priority)
    assert get_job(conn, job_id)["status"] == Status.DISMISSED


def test_summary_refresh_preserves_hydrated_description(conn, norm, priority, make_posting):
    full = make_posting(description_md="Detailed verified job description")
    job_id = upsert_job(conn, full, norm, priority).job_id
    upsert_job(
        conn, make_posting(description_md="Short summary", needs_hydration=True), norm, priority
    )
    row = get_job(conn, job_id)
    assert row["description_md"] == full.description_md
    assert row["needs_hydration"] == 0


def test_hydration_can_correct_a_remote_guess(conn, norm, priority, make_posting):
    from jobhunt.repo.jobs import apply_hydration

    job_id = upsert_job(conn, make_posting(is_remote=True), norm, priority).job_id
    apply_hydration(conn, job_id, make_posting(location="Chennai", is_remote=False), norm)
    row = get_job(conn, job_id)
    assert row["is_remote"] == 0 and row["location_bucket"] == "chennai"
