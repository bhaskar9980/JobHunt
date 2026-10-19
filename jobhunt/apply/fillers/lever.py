"""Lever filler: the legacy multipart form on jobs.lever.co.

Selectors live in config/ats/lever.yaml. Field names follow Lever's documented contract
(name, email, phone, org, urls[LinkedIn], resume, cards[...][fieldN]). The location box is an
autocomplete that trips hCaptcha when typed quickly, so it is on the slow_type list.
"""

from __future__ import annotations

from jobhunt.apply.fillers.common import ConfigFiller

KEY = "lever"

FILLER = ConfigFiller.from_config(KEY)
