import pytest

from jobhunt.normalize import (
    canonical_url,
    fingerprint,
    location_bucket,
    normalize_company,
    normalize_title,
)

KEEP = {"gh_jid", "jobid", "id"}


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/jobs?currentJobId=4012345678",
        "https://notlinkedin.com/jobs/view/4012345678",
    ],
)
def test_unrelated_hosts_are_not_rewritten_to_linkedin(url):
    assert not canonical_url(url).startswith("https://www.linkedin.com/")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://Boards.Greenhouse.io/acme/jobs/123?gh_src=abc&utm_source=x#app",
            "https://boards.greenhouse.io/acme/jobs/123",
        ),
        (
            "https://acme.com/careers?gh_jid=123&utm_campaign=z",
            "https://acme.com/careers?gh_jid=123",
        ),
        (
            "https://www.linkedin.com/jobs/view/software-engineer-at-acme-4012345678/?trackingId=abc",
            "https://www.linkedin.com/jobs/view/4012345678",
        ),
        (
            "https://in.linkedin.com/jobs/view/4012345678/?refId=x",
            "https://www.linkedin.com/jobs/view/4012345678",
        ),
        (
            "https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=dotnet",
            "https://www.linkedin.com/jobs/view/4012345678",
        ),
        ("jobs.lever.co/acme/abc-def/", "https://jobs.lever.co/acme/abc-def"),
        ("https://jobs.ashbyhq.com/acme/uuid", "https://jobs.ashbyhq.com/acme/uuid"),
        (
            "https://in.indeed.com/rc/clk?jk=abc123def456&from=ja&tk=xyz",
            "https://in.indeed.com/viewjob?jk=abc123def456",
        ),
        (
            "https://www.indeed.com/viewjob?jk=ABC123DEF456&vjs=3",
            "https://www.indeed.com/viewjob?jk=abc123def456",
        ),
        (
            "https://in.indeed.com/jobs?q=dotnet&l=Chennai",
            "https://in.indeed.com/jobs",
        ),
    ],
)
def test_canonical_url(url, expected):
    assert canonical_url(url, KEEP) == expected


SUFFIXES = {"inc", "ltd", "pvt", "private", "limited", "technologies", "india", "co"}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme, Inc.", "acme"),
        ("Platform Science Inc", "platform science"),
        ("Zeta Technologies Pvt. Ltd.", "zeta"),
        ("Acme Careers", "acme"),
        ("Tata Consultancy Services", "tata consultancy services"),
        ("Inc", "inc"),
        ("Procter & Gamble", "procter and gamble"),
    ],
)
def test_normalize_company(name, expected):
    assert normalize_company(name, SUFFIXES, {"careers", "jobs"}) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Software Engineer II", "software engineer 2"),
        ("Software Engineer 2 (Remote)", "software engineer 2"),
        ("SDE-II - Backend", "software engineer 2 backend"),
        ("Sr. Software Engineer", "senior software engineer"),
        ("Software Engineer II [Req 123456]", "software engineer 2"),
        (".NET Developer - Hyderabad", ".net developer hyderabad"),
        ("C# Developer", "c# developer"),
        ("Backend Engineer, Python (f/m/d)", "backend engineer python"),
        ("Software Engineer - JR0012345", "software engineer"),
    ],
)
def test_normalize_title(title, expected):
    assert normalize_title(title) == expected


ALIASES = {
    "bengaluru": "bengaluru",
    "bangalore": "bengaluru",
    "chennai": "chennai",
    "india": "india",
}
CITIES = {"pune": "pune", "delhi": "delhi", "hyd": "hyderabad"}
REGIONS = {"india": "india", "united states": "usa"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bengaluru, Karnataka, India", "bengaluru"),
        ("Bangalore", "bengaluru"),
        ("Remote - India", "remote"),
        ("Chennai (Hybrid)", "chennai"),
        ("India", "india"),
        ("Berlin", "unknown"),
        (None, "unknown"),
        ("Work from home", "remote"),
    ],
)
def test_location_bucket(text, expected):
    assert location_bucket(text, ALIASES) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Pune, India", "pune"),
        ("India - Pune", "pune"),
        ("India", "india"),
        ("Bharat", "unknown"),
        ("HYD, India", "hyderabad"),
        ("Austin, United States", "usa"),
        ("Reykjavik", "unknown"),
    ],
)
def test_a_city_outranks_its_country(text, expected):
    """Without the split, "India" is the longer alias and wins over "Pune"."""
    assert location_bucket(text, CITIES, REGIONS) == expected


def test_fingerprint_is_stable_and_short():
    a = fingerprint("acme", "software engineer 2", "bengaluru")
    b = fingerprint("acme", "software engineer 2", "bengaluru")
    c = fingerprint("acme", "software engineer 2", "chennai")
    assert a == b
    assert a != c
    assert len(a) == 16


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dot Net Developer", ".net developer"),
        ("Dot-Net Developer", ".net developer"),
        ("DOT NET DEVELOPER", ".net developer"),
        ("Dot Net Full Stack Developer", ".net full stack developer"),
        ("DotNet Developer", ".net developer"),
        (".Net Developer", ".net developer"),
        # A word that merely begins with "dotnet" is left alone.
        ("Dotnetwork Engineer", "dotnetwork engineer"),
    ],
)
def test_dot_net_is_spelled_many_ways_and_normalises_to_one(raw, expected):
    """Indian boards write "Dot Net" as often as ".NET"; both must reach the same target title."""
    assert normalize_title(raw) == expected
