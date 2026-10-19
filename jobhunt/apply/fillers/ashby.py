"""Ashby filler: div-based React form on jobs.ashbyhq.com.

Selectors live in config/ats/ashby.yaml. System fields carry `_systemfield_*` names; custom
questions are keyed by UUID. Yes/No questions are two buttons over a hidden checkbox and are
modelled as a RADIO field with options Yes/No.
"""

from __future__ import annotations

from jobhunt.apply.fillers.common import ConfigFiller

KEY = "ashby"

FILLER = ConfigFiller.from_config(KEY)
