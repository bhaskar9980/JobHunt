"""Rule-based email classification. Weighted phrase lists per class, ATS-domain boost, negative
markers for alerts and newsletters. Confidence is clamped to 0..1."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from jobhunt.mail.models import KIND_UNCLASSIFIED, Classification, MailMessage
from jobhunt.mail.rules import (
    DEFAULT_ATS_BOOST,
    DEFAULT_SUBJECT_BONUS,
    DEFAULT_TIE_MARGIN,
    is_ats_domain,
    thresholds,
)

log = logging.getLogger(__name__)

MAX_CONFIDENCE = 1.0
MIN_CONFIDENCE = 0.0


def classify(msg: MailMessage, rules: Mapping[str, Any]) -> Classification:
    """Score every class, pick the best (precedence breaks near-ties), unclassified below review."""
    penalty, negatives = _negative_penalty(msg, rules)
    ats_sender = is_ats_domain(msg.from_domain, rules)
    raw_scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    for kind, spec in (rules.get("classes") or {}).items():
        raw, hits = _class_score(msg, spec or {}, rules, ats_sender)
        raw_scores[str(kind)] = raw - penalty
        evidence[str(kind)] = hits
    scores = {kind: _clamp(raw) for kind, raw in raw_scores.items()}
    if not scores:
        return Classification(KIND_UNCLASSIFIED, MIN_CONFIDENCE, negatives, scores)
    best = _pick_best(raw_scores, rules)  # raw, so a clamp cannot hide the margin
    _, review = thresholds(rules)
    confidence = scores[best]
    kind = best if confidence >= review else KIND_UNCLASSIFIED
    hits = evidence.get(best, []) + [f"-{n}" for n in negatives]
    log.debug("classify %s: %s %.2f %s", msg.thread_id, kind, confidence, hits)
    return Classification(kind, round(confidence, 3), hits, scores)


def _class_score(
    msg: MailMessage, spec: Mapping[str, Any], rules: Mapping[str, Any], ats_sender: bool
) -> tuple[float, list[str]]:
    text = msg.text
    subject = msg.subject.lower()
    total = 0.0
    hits: list[str] = []
    in_subject = False
    for phrase, weight in (spec.get("phrases") or {}).items():
        needle = str(phrase).lower()
        if needle in text:
            total += float(weight)
            hits.append(needle)
            in_subject = in_subject or needle in subject
    if in_subject:
        total += float(rules.get("subject_bonus", DEFAULT_SUBJECT_BONUS))
        hits.append("+subject")
    if ats_sender and bool(spec.get("ats_boost", False)) and hits:
        total += float(rules.get("ats_boost", DEFAULT_ATS_BOOST))
        hits.append("+ats_domain")
    return total, hits


def _negative_penalty(msg: MailMessage, rules: Mapping[str, Any]) -> tuple[float, list[str]]:
    text = msg.text
    penalty = 0.0
    hits: list[str] = []
    for marker, weight in (rules.get("negative_markers") or {}).items():
        if str(marker).lower() in text:
            penalty += float(weight)
            hits.append(str(marker))
    sender = (msg.sender_email or "").lower()
    domain = (msg.from_domain or "").lower()
    for address, weight in (rules.get("negative_senders") or {}).items():
        if _sender_matches(str(address).lower(), sender, domain):
            penalty += float(weight)
            hits.append(f"sender:{address}")
    return penalty, hits


def _sender_matches(rule: str, sender: str, domain: str) -> bool:
    """A rule with an @ is an exact address; a bare domain matches it and its subdomains."""
    if "@" in rule:
        return sender == rule
    return bool(domain) and (domain == rule or domain.endswith("." + rule))


def _pick_best(scores: Mapping[str, float], rules: Mapping[str, Any]) -> str:
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_kind, top_score = ranked[0]
    margin = float(rules.get("tie_margin", DEFAULT_TIE_MARGIN))
    precedence = [str(k) for k in rules.get("precedence") or []]
    contenders = [k for k, s in ranked if top_score - s < margin]
    if len(contenders) == 1 or not precedence:
        return top_kind
    contenders.sort(key=lambda k: precedence.index(k) if k in precedence else len(precedence))
    return contenders[0]


def _clamp(value: float) -> float:
    return max(MIN_CONFIDENCE, min(MAX_CONFIDENCE, value))
