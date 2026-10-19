"""Workday filler: the multi-page wizard on *.myworkdayjobs.com.

Everything selector-shaped lives in config/ats/workday.yaml, including the `pages:` block that
tells the runner how to step from Contact Information through to the review page. The user signs
in himself; the runner waits for that and the session then persists in the browser profile.
"""

from __future__ import annotations

from jobhunt.apply.fillers.common import ConfigFiller

KEY = "workday"

FILLER = ConfigFiller.from_config(KEY)
