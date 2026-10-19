"""Second-degree path when nobody in the contacts table works at the company.

We never scrape LinkedIn. This module only builds a people-search URL for the user to open in his
own browser and renders the short connection note he pastes by hand.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlencode

from jobhunt.referral.drafts import Template, render

LINKEDIN_PEOPLE_SEARCH = "https://www.linkedin.com/search/results/people/"
CONNECTION_NOTE_LIMIT = 300
DEFAULT_ROLE_KEYWORDS = ("recruiter", "engineering manager")


class DraftTooLong(ValueError):
    pass


def people_search_url(company: str, role_keywords: Iterable[str] = ()) -> str:
    keywords = " ".join([company.strip(), *[k.strip() for k in role_keywords if k.strip()]])
    return f"{LINKEDIN_PEOPLE_SEARCH}?{urlencode({'keywords': keywords})}"


def connection_note(template: Template, context: Mapping[str, Any]) -> str:
    """Render a connection-request note; LinkedIn hard-limits these to 300 characters."""
    text = render(template, context).strip()
    if len(text) > CONNECTION_NOTE_LIMIT:
        raise DraftTooLong(f"{len(text)} chars; LinkedIn allows {CONNECTION_NOTE_LIMIT}")
    return text
