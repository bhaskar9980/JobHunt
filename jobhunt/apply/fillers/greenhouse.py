"""Greenhouse filler: legacy boards.greenhouse.io forms and the React job-boards host.

Everything selector-shaped lives in config/ats/greenhouse.yaml. Known quirks handled by the
shared ConfigFiller: react-select comboboxes (typed then picked), the phone country combobox
filled before the national number, and the resume file input hidden behind an "Attach" button.
"""

from __future__ import annotations

from jobhunt.apply.fillers.common import ConfigFiller

KEY = "greenhouse"

FILLER = ConfigFiller.from_config(KEY)
