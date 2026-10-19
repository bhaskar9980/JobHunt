"""Shared filler plumbing: selector config, DOM introspection, fills, submit and confirmation.

Every ATS filler is a `ConfigFiller` driven by `config/ats/{key}.yaml`. The Python knows the
config keys and the browser mechanics; every selector and text pattern lives in the yaml.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from rapidfuzz import fuzz, process

from jobhunt.apply.captcha import looks_like_login
from jobhunt.apply.detect import AtsRegistry
from jobhunt.apply.fillers.base import (
    Answer,
    Confirmation,
    FileInputs,
    FillerError,
    FillReport,
    LoginRequired,
    SubmissionNotStarted,
)
from jobhunt.apply.schema import FieldKind, FormField, FormSchema
from jobhunt.config import CONFIG_DIR_NAME, default_root, load_yaml

log = logging.getLogger(__name__)

# A next-page control whose label contains any of these is really a send button.
SUBMIT_WORDS = ("submit", "send application", "apply now", "finish")
DEFAULT_SETTLE_MS = 6000
POLL_MS = 250

ATS_CONFIG_DIR = "ats"
RESUME_MARKER = "resume"
COVER_LETTER_MARKER = "cover_letter"
PROFILE_RESUME_KEY = "resume"
DEFAULT_TIMEOUT_MS = 15_000
QUICK_TIMEOUT_MS = 1_500
DEFAULT_TYPE_DELAY_MS = 40
DEFAULT_OPTIONS_TIMEOUT_MS = 1_500
DEFAULT_SUGGESTION_TIMEOUT_MS = 2_500
CONFIRM_POLL_SECONDS = 0.5
OPTION_MATCH_THRESHOLD = 70
YES_NO = ("Yes", "No")
PLACEHOLDER_OPTIONS = frozenset({"", "select...", "select", "please select", "choose...", "choose"})
WEAK_LABELS = frozenset({"attach", "enter manually", "upload file", "browse", "choose file"})
_REQUIRED_MARKS = "*✱＊"
_SLUG = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")
_TRUE_WORDS = frozenset({"yes", "true", "1", "y", "on"})
_FALSE_WORDS = frozenset({"no", "false", "0", "n", "off"})

_KIND_BY_TYPE: dict[str, FieldKind] = {
    "email": FieldKind.EMAIL,
    "tel": FieldKind.PHONE,
    "url": FieldKind.URL,
    "number": FieldKind.NUMBER,
    "file": FieldKind.FILE,
    "date": FieldKind.DATE,
    "checkbox": FieldKind.CHECKBOX,
    "radio": FieldKind.RADIO,
}
_GROUPED_KINDS = frozenset({FieldKind.RADIO, FieldKind.CHECKBOX})
_CHOICE_KINDS = frozenset({FieldKind.SELECT, FieldKind.RADIO, FieldKind.MULTISELECT})

# One round trip: every heading and fillable element inside the form root, in DOM order.
DESCRIBE_JS = """
(root, opts) => {
  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const textOf = (el) => (el ? clean(el.innerText || el.textContent) : '');
  // Label text without the controls (and <option>s) a wrapping <label> may contain.
  const dropFromLabels = 'input, select, textarea, option, button, script, style, [hidden]'
    + (opts.labelExclude ? ', ' + opts.labelExclude : '');
  const labelText = (labelEl) => {
    if (!labelEl) return '';
    const clone = labelEl.cloneNode(true);
    clone.querySelectorAll(dropFromLabels).forEach((n) => n.remove());
    return clean(clone.textContent);
  };
  const byIds = (ids) => clean((ids || '').split(/\\s+/).filter(Boolean)
    .map((id) => textOf(document.getElementById(id))).join(' '));
  const nodes = Array.from(root.querySelectorAll(opts.selector));
  let section = null;
  const out = [];
  nodes.forEach((el, index) => {
    const tag = el.tagName.toLowerCase();
    if (/^h[1-6]$/.test(tag) || tag === 'legend') {
      const heading = clean(el.textContent);
      if (heading) section = heading;
      return;
    }
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) return;
    if (el.getAttribute('aria-hidden') === 'true') return;
    // File inputs and yes/no proxies are hidden on purpose; anything else hidden is not a question.
    const yesno = Boolean(opts.yesno && el.closest(opts.yesno));
    if (type !== 'file' && !yesno && window.getComputedStyle(el).display === 'none') return;
    const labelEl = el.closest('label');
    const group = el.closest('[role="group"]');
    const container = opts.container ? el.closest(opts.container) : null;
    const containerLabelEl = container && opts.containerLabel
      ? container.querySelector(opts.containerLabel) : null;
    out.push({
      index, tag, type,
      role: el.getAttribute('role') || '',
      name: el.getAttribute('name') || '',
      id: el.id || '',
      required: Boolean(el.required) || el.getAttribute('aria-required') === 'true'
        || Boolean(group && group.getAttribute('aria-required') === 'true'),
      labels: el.labels ? Array.from(el.labels).map(labelText).filter(Boolean) : [],
      labelledBy: byIds(el.getAttribute('aria-labelledby')),
      ariaLabel: clean(el.getAttribute('aria-label')),
      closestLabel: labelText(labelEl),
      containerLabel: textOf(containerLabelEl),
      labelClasses: [labelEl, containerLabelEl].filter(Boolean)
        .map((n) => n.getAttribute('class') || '').join(' '),
      groupLabel: group ? byIds(group.getAttribute('aria-labelledby')) : '',
      placeholder: clean(el.getAttribute('placeholder')),
      multiple: Boolean(el.multiple),
      options: tag === 'select'
        ? Array.from(el.options).map((o) => ({ value: o.value, text: textOf(o) })) : [],
      value: typeof el.value === 'string' ? el.value : '',
      checked: Boolean(el.checked),
      yesno,
      fieldPath: container ? (container.getAttribute('data-field-path') || '') : '',
      section,
    });
  });
  return out;
}
"""

MEMBER_LABELS_JS = (
    "els => els.map(e => ((e.closest('label') && e.closest('label').innerText) "
    "|| e.value || '').replace(/\\s+/g, ' ').trim())"
)
TAG_JS = "e => e.tagName.toLowerCase()"
FILE_COUNT_JS = "e => e.files ? e.files.length : 0"


# --------------------------------------------------------------------------- config


def cfg_get(cfg: Mapping[str, Any], *path: str, default: Any = None) -> Any:
    node: Any = cfg
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def config_dir_or_default(config_dir: Path | None) -> Path:
    return config_dir or default_root() / CONFIG_DIR_NAME


def load_ats_config(key: str, config_dir: Path | None = None) -> dict[str, Any]:
    return load_yaml(config_dir_or_default(config_dir) / ATS_CONFIG_DIR / f"{key}.yaml")


@lru_cache(maxsize=4)
def _registry(config_dir: str) -> AtsRegistry:
    return AtsRegistry.load(Path(config_dir))


def url_matches(key: str, url: str, config_dir: Path | None = None) -> bool:
    return _registry(str(config_dir_or_default(config_dir))).detect(url) == key


# --------------------------------------------------------------------------- pure helpers


def clean_label(text: str | None) -> str:
    cleaned = _WS.sub(" ", (text or "").replace(" ", " ")).strip()
    return cleaned.rstrip(_REQUIRED_MARKS + " ").strip()


def has_required_mark(text: str | None) -> bool:
    stripped = (text or "").replace(" ", " ").rstrip()
    return bool(stripped) and stripped[-1] in _REQUIRED_MARKS


def slug_key(label: str) -> str:
    return _SLUG.sub("-", label.lower()).strip("-")[:60]


def css_attr(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def kind_for(tag: str, input_type: str, role: str = "", *, multiple: bool = False) -> FieldKind:
    if tag == "select":
        return FieldKind.MULTISELECT if multiple else FieldKind.SELECT
    if tag == "textarea":
        return FieldKind.TEXTAREA
    if role == "combobox":
        return FieldKind.SELECT
    if tag != "input":
        return FieldKind.UNKNOWN
    return _KIND_BY_TYPE.get(input_type, FieldKind.TEXT)


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_WORDS


def yes_no_index(value: Any) -> int | None:
    """0 for a yes-like value, 1 for a no-like value, None otherwise (never fuzzy)."""
    word = choice_text(value).lower()
    if word in _TRUE_WORDS:
        return 0
    return 1 if word in _FALSE_WORDS else None


def choice_text(value: Any) -> str:
    if isinstance(value, bool):
        return YES_NO[0] if value else YES_NO[1]
    return str(value).strip()


def best_option(wanted: str, options: Sequence[str], threshold: int = OPTION_MATCH_THRESHOLD):
    """Index of the option matching `wanted`, exact (case-insensitive) first, then fuzzy."""
    lowered = [o.strip().lower() for o in options]
    target = wanted.strip().lower()
    if target in lowered:
        return lowered.index(target)
    hit = process.extractOne(target, lowered, scorer=fuzz.WRatio, score_cutoff=threshold)
    return None if hit is None else hit[2]


def _links(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    return profile.get("links") or {}


def _location(profile: Mapping[str, Any]) -> Any:
    return profile.get("location") or {}


def _location_part(profile: Mapping[str, Any], part: str) -> Any:
    location = _location(profile)
    return location.get(part) if isinstance(location, Mapping) else None


def _location_text(profile: Mapping[str, Any]) -> str | None:
    location = _location(profile)
    if isinstance(location, str):
        return location
    parts = [location.get(k) for k in ("city", "state", "country")]
    return ", ".join(str(p) for p in parts if p) or None


def _phone_full(profile: Mapping[str, Any]) -> str | None:
    phone = profile.get("phone") or {}
    if isinstance(phone, str):
        return phone
    return " ".join(str(phone.get(k)) for k in ("country_code", "number") if phone.get(k)) or None


def _phone_national(profile: Mapping[str, Any]) -> Any:
    phone = profile.get("phone") or {}
    return phone if isinstance(phone, str) else phone.get("number")


def _full_name(profile: Mapping[str, Any]) -> str | None:
    parts = [profile.get("first_name"), profile.get("last_name")]
    return " ".join(str(p) for p in parts if p) or None


_PROFILE_GETTERS: dict[str, Callable[[Mapping[str, Any]], Any]] = {
    "first_name": lambda p: p.get("first_name"),
    "last_name": lambda p: p.get("last_name"),
    "full_name": _full_name,
    "email": lambda p: p.get("email"),
    "phone": _phone_full,
    "phone_national": _phone_national,
    "phone_country": lambda p: _location_part(p, "country"),
    "location": _location_text,
    "linkedin": lambda p: _links(p).get("linkedin"),
    "github": lambda p: _links(p).get("github"),
    "portfolio": lambda p: _links(p).get("portfolio"),
    "current_company": lambda p: p.get("current_company"),
    "current_title": lambda p: p.get("current_title"),
}


def profile_value(profile_key: str, profile: Mapping[str, Any]) -> str | None:
    getter = _PROFILE_GETTERS.get(profile_key)
    value = getter(profile) if getter else None
    text = str(value).strip() if value is not None else ""
    return text or None


def system_profile_key(key: str, label: str, cfg: Mapping[str, Any]) -> str | None:
    mapping = cfg_get(cfg, "system_fields", default={}) or {}
    if key in mapping:
        return str(mapping[key])
    for rule in cfg_get(cfg, "system_label_patterns", default=[]) or []:
        if re.search(str(rule.get("regex", "")), label, re.I):
            return str(rule["profile"])
    return None


# --------------------------------------------------------------------------- DOM -> schema


@dataclass(slots=True)
class RawField:
    tag: str
    type: str
    role: str
    name: str
    id: str
    required: bool
    labels: list[str]
    labelled_by: str
    aria_label: str
    closest_label: str
    container_label: str
    label_classes: str
    group_label: str
    placeholder: str
    multiple: bool
    options: list[dict[str, str]]
    value: str
    checked: bool
    yesno: bool
    field_path: str
    section: str | None

    @classmethod
    def from_js(cls, data: Mapping[str, Any]) -> RawField:
        return cls(
            tag=str(data.get("tag", "")),
            type=str(data.get("type", "")),
            role=str(data.get("role", "")),
            name=str(data.get("name", "")),
            id=str(data.get("id", "")),
            required=bool(data.get("required")),
            labels=[str(t) for t in data.get("labels", [])],
            labelled_by=str(data.get("labelledBy", "")),
            aria_label=str(data.get("ariaLabel", "")),
            closest_label=str(data.get("closestLabel", "")),
            container_label=str(data.get("containerLabel", "")),
            label_classes=str(data.get("labelClasses", "")),
            group_label=str(data.get("groupLabel", "")),
            placeholder=str(data.get("placeholder", "")),
            multiple=bool(data.get("multiple")),
            options=list(data.get("options", [])),
            value=str(data.get("value", "")),
            checked=bool(data.get("checked")),
            yesno=bool(data.get("yesno")),
            field_path=str(data.get("fieldPath", "")),
            section=data.get("section"),
        )

    def label_candidates(self, kind: FieldKind) -> list[str]:
        own = [*self.labels, self.labelled_by, self.container_label, self.aria_label]
        rest = [self.closest_label, self.group_label, self.placeholder]
        if kind == FieldKind.FILE:
            return [self.group_label, self.container_label, *own, *rest]
        return [*own, *rest]

    def group_label_candidates(self) -> list[str]:
        return [self.container_label, self.group_label, self.labelled_by, self.closest_label]

    def option_label(self) -> str:
        return clean_label(self.closest_label) or self.value

    def kind(self) -> FieldKind:
        if self.yesno:
            return FieldKind.RADIO
        return kind_for(self.tag, self.type, self.role, multiple=self.multiple)

    def selector(self) -> str | None:
        if self.yesno and self.field_path:
            return f'[data-field-path="{css_attr(self.field_path)}"]'
        if self.name:
            return f'[name="{css_attr(self.name)}"]:not([type="hidden"])'
        if self.id:
            return f'[id="{css_attr(self.id)}"]'
        return None


def describe_fields(root: Any, extract_cfg: Mapping[str, Any]) -> list[RawField]:
    heading = str(extract_cfg.get("heading_selector") or "h2, h3, legend")
    fields = str(extract_cfg.get("field_selector") or "input, textarea, select")
    opts = {
        "selector": f"{heading}, {fields}",
        "container": extract_cfg.get("question_container") or "",
        "containerLabel": extract_cfg.get("question_label") or "",
        "labelExclude": extract_cfg.get("label_exclude") or "",
        "yesno": extract_cfg.get("yesno_container") or "",
    }
    data = root.evaluate(DESCRIBE_JS, opts)
    return [RawField.from_js(d) for d in data]


def first_label(candidates: Sequence[str]) -> str:
    for text in candidates:
        cleaned = clean_label(text)
        if cleaned and cleaned.lower() not in WEAK_LABELS:
            return cleaned
    return ""


def required_by_class(raw: RawField, cfg: Mapping[str, Any]) -> bool:
    """Some ATSs (Ashby) flag required questions only through a label class."""
    pattern = str(cfg_get(cfg, "extract", "required_class_regex", default="") or "")
    return bool(pattern) and re.search(pattern, raw.label_classes) is not None


def build_schema(
    ats: str, url: str, raws: Sequence[RawField], cfg: Mapping[str, Any]
) -> FormSchema:
    fields: list[FormField] = []
    groups: dict[str, FormField] = {}
    for raw in raws:
        raw.required = raw.required or required_by_class(raw, cfg)
        kind = raw.kind()
        if kind in _GROUPED_KINDS and raw.name and not raw.yesno:
            _add_group_member(groups, fields, raw, kind, cfg)
            continue
        single = _single_field(raw, kind, cfg)
        if single is not None:
            fields.append(single)
    for grouped in groups.values():
        if grouped.kind == FieldKind.CHECKBOX and len(grouped.options) > 1:
            grouped.kind = FieldKind.MULTISELECT
    return FormSchema(ats=ats, url=url, fields=fields)


def _add_group_member(
    groups: dict[str, FormField],
    fields: list[FormField],
    raw: RawField,
    kind: FieldKind,
    cfg: Mapping[str, Any],
) -> None:
    group = groups.get(raw.name)
    if group is None:
        label = first_label(raw.group_label_candidates())
        group = FormField(
            key=raw.name,
            label=label,
            kind=kind,
            required=False,
            selector=raw.selector(),
            section=raw.section,
            system=system_profile_key(raw.name, label, cfg) is not None,
        )
        groups[raw.name] = group
        fields.append(group)
    group.required = group.required or raw.required or has_required_mark(raw.container_label)
    option = raw.option_label()
    if option and option not in group.options:
        group.options.append(option)


def _single_field(raw: RawField, kind: FieldKind, cfg: Mapping[str, Any]) -> FormField | None:
    label = first_label(raw.label_candidates(kind))
    key = raw.name or raw.id or slug_key(label)
    if not key:
        return None
    options = [YES_NO[0], YES_NO[1]] if raw.yesno else _select_options(raw)
    marked = any(has_required_mark(t) for t in (*raw.labels, raw.container_label, raw.group_label))
    return FormField(
        key=key,
        label=label,
        kind=kind,
        required=raw.required or marked,
        options=options,
        selector=raw.selector(),
        section=raw.section,
        system=system_profile_key(key, label, cfg) is not None,
    )


def _select_options(raw: RawField) -> list[str]:
    return [
        str(o.get("text", "")).strip()
        for o in raw.options
        if str(o.get("text", "")).strip().lower() not in PLACEHOLDER_OPTIONS or o.get("value")
    ]


# --------------------------------------------------------------------------- filling


@dataclass(slots=True)
class FillContext:
    page: Any
    root: Any
    cfg: Mapping[str, Any]
    files: FileInputs


def locate(ctx: FillContext, form_field: FormField) -> Any:
    if form_field.selector:
        return ctx.root.locator(form_field.selector)
    return ctx.root.get_by_label(form_field.label, exact=False)


def member_labels(members: Any) -> list[str]:
    return [str(t) for t in members.evaluate_all(MEMBER_LABELS_JS)]


def wait_for_options(ctx: FillContext, timeout_ms: int | None = None) -> Any:
    selector = str(cfg_get(ctx.cfg, "combobox", "option_selector", default="[role='option']"))
    budget = timeout_ms or int(
        cfg_get(ctx.cfg, "combobox", "options_timeout_ms", default=DEFAULT_OPTIONS_TIMEOUT_MS)
    )
    options = ctx.page.locator(selector)
    try:
        options.first.wait_for(state="visible", timeout=budget)
    except PlaywrightError:
        return None
    return options


def pick_combobox(ctx: FillContext, target: Any, wanted: str) -> None:
    """Type into a custom combobox and click the best matching option, else press Enter."""
    delay = int(cfg_get(ctx.cfg, "combobox", "type_delay_ms", default=DEFAULT_TYPE_DELAY_MS))
    target.click()
    target.fill("")
    target.press_sequentially(wanted, delay=delay)
    options = wait_for_options(ctx)
    if options is None:
        target.press("Enter")
        return
    texts = options.all_inner_texts()
    index = best_option(wanted, texts)
    if index is None:
        target.press("Enter")
        return
    options.nth(index).click()


def read_combobox_options(ctx: FillContext, target: Any) -> list[str]:
    """Open a custom combobox menu, read its options, close it. Empty when nothing renders."""
    container_sel = str(cfg_get(ctx.cfg, "combobox", "container", default="") or "")
    toggle_sel = str(cfg_get(ctx.cfg, "combobox", "toggle_selector", default="") or "")
    opener = target
    if container_sel and toggle_sel:
        container = ctx.root.locator(container_sel).filter(has=target)
        toggle = container.locator(toggle_sel)
        if toggle.count():
            opener = toggle.first
    try:
        opener.click(timeout=QUICK_TIMEOUT_MS)
    except PlaywrightError:
        return []
    options = wait_for_options(ctx, QUICK_TIMEOUT_MS)
    texts = [] if options is None else [t.strip() for t in options.all_inner_texts()]
    target.press("Escape")
    return [t for t in texts if t]


def _slow_type_cfg(ctx: FillContext, form_field: FormField) -> Mapping[str, Any] | None:
    slow = cfg_get(ctx.cfg, "slow_type", default={}) or {}
    selectors = {str(s) for s in slow.get("selectors", [])}
    if form_field.selector in selectors or form_field.key in selectors:
        return slow
    return None


def slow_type(ctx: FillContext, target: Any, text: str, slow: Mapping[str, Any]) -> None:
    target.click()
    target.fill("")
    target.press_sequentially(text, delay=int(slow.get("delay_ms", DEFAULT_TYPE_DELAY_MS)))
    suggestion = str(slow.get("suggestion_selector") or "")
    if not suggestion:
        return
    hits = ctx.page.locator(suggestion)
    try:
        hits.first.wait_for(
            state="visible",
            timeout=int(slow.get("suggestion_timeout_ms", DEFAULT_SUGGESTION_TIMEOUT_MS)),
        )
        hits.first.click()
    except PlaywrightError:
        log.debug("no autocomplete suggestion for %r; keeping typed text", text)


def _fill_text(ctx: FillContext, form_field: FormField, value: Any) -> None:
    target = locate(ctx, form_field).first
    slow = _slow_type_cfg(ctx, form_field)
    if slow is not None:
        slow_type(ctx, target, str(value), slow)
        return
    target.fill(str(value))


def _fill_file(ctx: FillContext, form_field: FormField, value: Any) -> None:
    path = Path(str(value))
    if not path.exists():
        raise FillerError(f"file not found: {path}")
    locate(ctx, form_field).first.set_input_files(str(path))


def _select_native(target: Any, wanted: str) -> None:
    texts = [t.strip() for t in target.locator("option").all_inner_texts()]
    index = best_option(wanted, texts)
    if index is None:
        raise FillerError(f"option {wanted!r} not in {texts[:8]!r}")
    target.select_option(index=index)


def _fill_select(ctx: FillContext, form_field: FormField, value: Any) -> None:
    target = locate(ctx, form_field).first
    wanted = choice_text(value)
    if target.evaluate(TAG_JS) == "select":
        _select_native(target, wanted)
        return
    pick_combobox(ctx, target, wanted)


def _fill_multi(ctx: FillContext, form_field: FormField, value: Any) -> None:
    wanted = [choice_text(v) for v in (value if isinstance(value, list | tuple) else [value])]
    members = locate(ctx, form_field)
    if members.first.evaluate(TAG_JS) == "select":
        texts = [t.strip() for t in members.first.locator("option").all_inner_texts()]
        indexes = [best_option(w, texts) for w in wanted]
        members.first.select_option(index=[i for i in indexes if i is not None])
        return
    labels = member_labels(members)
    for text in wanted:
        index = best_option(text, labels)
        if index is None:
            raise FillerError(f"option {text!r} not in {labels[:8]!r}")
        members.nth(index).check()


def _fill_radio(ctx: FillContext, form_field: FormField, value: Any) -> None:
    wanted = choice_text(value)
    members = locate(ctx, form_field)
    yesno_sel = str(cfg_get(ctx.cfg, "yesno", "option_selector", default="") or "")
    if yesno_sel and form_field.selector and form_field.selector.startswith("[data-field-path"):
        index = yes_no_index(wanted)
        if index is None:
            raise FillerError(f"yes/no question cannot take {wanted!r}")
        members.first.locator(yesno_sel.format(option=YES_NO[index].lower())).click()
        return
    labels = member_labels(members)
    index = best_option(wanted, labels)
    if index is None:
        raise FillerError(f"option {wanted!r} not in {labels[:8]!r}")
    members.nth(index).check()


def _fill_checkbox(ctx: FillContext, form_field: FormField, value: Any) -> None:
    locate(ctx, form_field).first.set_checked(to_bool(value))


_FILL_BY_KIND: dict[FieldKind, Callable[[FillContext, FormField, Any], None]] = {
    FieldKind.FILE: _fill_file,
    FieldKind.SELECT: _fill_select,
    FieldKind.MULTISELECT: _fill_multi,
    FieldKind.RADIO: _fill_radio,
    FieldKind.CHECKBOX: _fill_checkbox,
}


def fill_value(ctx: FillContext, form_field: FormField, value: Any) -> None:
    _FILL_BY_KIND.get(form_field.kind, _fill_text)(ctx, form_field, value)


# --------------------------------------------------------------------------- page-level


def body_text(page: Any) -> str:
    try:
        return page.inner_text("body") or ""
    except PlaywrightError:
        return ""


def confirmation_now(
    page: Any, url_res: Sequence[re.Pattern[str]], text_res: Sequence[re.Pattern[str]]
) -> Confirmation | None:
    try:
        url = page.url
    except PlaywrightError:
        return None
    text = body_text(page)
    if any(r.search(url) for r in url_res):
        return Confirmation(url=url, text=text[:200])
    for pattern in text_res:
        match = pattern.search(text)
        if match:
            return Confirmation(url=url, text=match.group(0))
    return None


def confirm_page(
    page: Any,
    url_patterns: Sequence[str],
    text_patterns: Sequence[str],
    timeout_seconds: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Confirmation | None:
    url_res = [re.compile(re.escape(p), re.I) for p in url_patterns]
    text_res = [re.compile(re.escape(p), re.I) for p in text_patterns]
    deadline = time.monotonic() + timeout_seconds
    while True:
        found = confirmation_now(page, url_res, text_res)
        if found is not None or time.monotonic() >= deadline:
            return found
        sleep(CONFIRM_POLL_SECONDS)


# --------------------------------------------------------------------------- the filler


@dataclass(slots=True)
class ConfigFiller:
    """An AtsFiller whose selectors and patterns come from config/ats/{KEY}.yaml."""

    KEY: str
    cfg: Mapping[str, Any]
    config_dir: Path | None = None
    _confirm_sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    @classmethod
    def from_config(cls, key: str, config_dir: Path | None = None) -> ConfigFiller:
        return cls(KEY=key, cfg=load_ats_config(key, config_dir), config_dir=config_dir)

    # -- protocol ----------------------------------------------------------------

    def matches(self, url: str) -> bool:
        return url_matches(self.KEY, url, self.config_dir)

    def open(self, page: Any, url: str) -> None:
        page.goto(url, wait_until="domcontentloaded")
        if self._root_attached(page, QUICK_TIMEOUT_MS):
            return
        self._reveal(page)
        append = str(cfg_get(self.cfg, "open", "append_path", default="") or "")
        if append and not url.rstrip("/").endswith(append) and not self._root_attached(page, 0):
            page.goto(url.rstrip("/") + append, wait_until="domcontentloaded")
        timeout = int(cfg_get(self.cfg, "open", "timeout_ms", default=DEFAULT_TIMEOUT_MS))
        if self._root_attached(page, timeout):
            return
        if self._follow_embedded_form(page, timeout):
            return
        if looks_like_login(page):
            raise LoginRequired(f"{self.KEY}: login wall at {url}")
        raise FillerError(f"{self.KEY}: application form not found at {url}")

    def _follow_embedded_form(self, page: Any, timeout_ms: int) -> bool:
        """Open the real form when a company page only embeds it in an iframe.

        Plenty of employers host the job on their own careers site and drop the board's
        application form into an iframe, so the form's own markup is not in this document.
        Navigating straight to the frame's source gives the plain form the selectors expect,
        and takes the address from the page itself rather than guessing a board name.
        """
        for selector in cfg_get(self.cfg, "open", "embed_iframe", default=[]) or []:
            try:
                frame = page.locator(str(selector))
                if frame.count() == 0:
                    continue
                # The `src` property is resolved against the page; the attribute may be
                # relative, which is not navigable on its own.
                src = frame.first.evaluate("el => el.src")
            except PlaywrightError:
                continue
            if not src:
                continue
            log.info("%s: following the embedded application form", self.KEY)
            page.goto(src, wait_until="domcontentloaded")
            if self._root_attached(page, timeout_ms):
                return True
        return False

    def already_applied(self, page: Any) -> bool:
        text = body_text(page).lower()
        return any(str(p).lower() in text for p in cfg_get(self.cfg, "already_applied", default=[]))

    def extract_schema(self, page: Any) -> FormSchema:
        root = self.root(page)
        raws = describe_fields(root, cfg_get(self.cfg, "extract", default={}) or {})
        schema = build_schema(self.KEY, page.url, raws, self.cfg)
        ctx = FillContext(page, root, self.cfg, FileInputs(resume_pdf=Path()))
        for form_field in schema.fields:
            if form_field.kind == FieldKind.SELECT and not form_field.options:
                self._read_options(ctx, form_field)
        return schema

    def fill(
        self,
        page: Any,
        schema: FormSchema,
        answers: Mapping[str, Answer],
        profile: Mapping[str, Any],
        files: FileInputs,
    ) -> FillReport:
        report = FillReport()
        ctx = FillContext(page, self.root(page), self.cfg, files)
        for form_field in schema.fields:
            value, reason = self.value_for(form_field, answers, profile, files)
            if reason is not None:
                report.skipped.append(f"{form_field.key}: {reason}")
                continue
            self._fill_one(ctx, form_field, value, report)
        return report

    def advance(self, page: Any) -> bool:
        """Click through to the next page of a wizard such as Workday.

        Single-page forms declare no `pages:` block and always answer False, so the runner
        fills them exactly once.

        Workday reuses one `data-automation-id` for both Next and Submit, so matching the next
        selector is not enough: on the final page the very same element sends the application.
        Submitting here would skip the CAPTCHA check, the safety reservation and the
        confirmation step, and the run would then record that nothing was sent. So a control
        that reads as a submit is never clicked; the runner's own submit step owns that.
        """
        selector = str(cfg_get(self.cfg, "pages", "next_selector", default="") or "")
        if not selector:
            return False
        button = page.locator(selector)
        if button.count() == 0:
            return False
        target = button.first
        if not target.is_visible() or target.is_disabled():
            return False
        if self._is_submit_control(page, target):
            return False
        before = self.page_marker(page)
        target.click()
        self._settle(page, before)
        return True

    def _is_submit_control(self, page: Any, target: Any) -> bool:
        """True when this button would submit rather than move to the next page."""
        try:
            label = (target.inner_text() or "").strip().lower()
        except PlaywrightError:
            return True  # cannot read it, so refuse to click it
        if any(word in label for word in SUBMIT_WORDS):
            return True
        submit_selector = str(cfg_get(self.cfg, "submit", "selector", default="") or "")
        if not submit_selector:
            return False
        try:
            # The same element matching the submit selector is the final page's send button.
            return bool(target.evaluate("(el, sel) => el.matches(sel)", submit_selector))
        except PlaywrightError:
            return False

    def page_marker(self, page: Any) -> str:
        """Which wizard page is showing, used to tell a real move from a failed one."""
        for marker in cfg_get(self.cfg, "pages", "markers", default=[]) or []:
            try:
                # Wizards keep every page in the DOM and hide all but one, so presence is not
                # enough; the marker has to be the page the candidate can actually see.
                located = page.locator(str(marker))
                if located.count() > 0 and located.first.is_visible():
                    return str(marker)
            except PlaywrightError:
                continue
        return ""

    def _settle(self, page: Any, before: str) -> None:
        timeout = int(cfg_get(self.cfg, "pages", "settle_ms", default=DEFAULT_SETTLE_MS))
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            page.wait_for_timeout(POLL_MS)
            if self.page_marker(page) != before:
                return

    def submit(self, page: Any) -> None:
        selector = str(cfg_get(self.cfg, "submit", "selector", default="button[type=submit]"))
        button = page.locator(selector)
        if button.count() == 0:
            raise SubmissionNotStarted(f"{self.KEY}: submit button not found ({selector})")
        target = button.first
        if target.is_disabled() or target.get_attribute("aria-disabled") == "true":
            raise SubmissionNotStarted(f"{self.KEY}: submit button is disabled")
        target.click()

    def confirm(self, page: Any, timeout_seconds: float) -> Confirmation | None:
        return confirm_page(
            page,
            [str(p) for p in cfg_get(self.cfg, "confirm", "url_patterns", default=[])],
            [str(p) for p in cfg_get(self.cfg, "confirm", "text_patterns", default=[])],
            timeout_seconds,
            sleep=self._confirm_sleep,
        )

    # -- helpers -----------------------------------------------------------------

    def root_selector(self) -> str:
        roots = cfg_get(self.cfg, "open", "form_root", default=["form"])
        return ", ".join(str(r) for r in roots)

    def root(self, page: Any) -> Any:
        return page.locator(self.root_selector()).first

    def submit_locator(self, page: Any) -> Any:
        return page.locator(str(cfg_get(self.cfg, "submit", "selector", default="")))

    def _root_attached(self, page: Any, timeout_ms: int) -> bool:
        root = page.locator(self.root_selector())
        if root.count() > 0:
            return True
        if timeout_ms <= 0:
            return False
        try:
            root.first.wait_for(state="attached", timeout=timeout_ms)
        except PlaywrightError:
            return False
        return True

    def _reveal(self, page: Any) -> None:
        for selector in cfg_get(self.cfg, "open", "reveal", default=[]) or []:
            button = page.locator(str(selector))
            try:
                if button.count() and button.first.is_visible():
                    button.first.click(timeout=QUICK_TIMEOUT_MS)
                    return
            except PlaywrightError as exc:
                log.debug("%s: reveal %s skipped: %s", self.KEY, selector, exc)

    def _read_options(self, ctx: FillContext, form_field: FormField) -> None:
        target = locate(ctx, form_field).first
        try:
            if target.evaluate(TAG_JS) == "select":
                return
            form_field.options = read_combobox_options(ctx, target)
        except PlaywrightError as exc:
            log.debug("%s: could not read options for %s: %s", self.KEY, form_field.key, exc)

    def value_for(
        self,
        form_field: FormField,
        answers: Mapping[str, Answer],
        profile: Mapping[str, Any],
        files: FileInputs,
    ) -> tuple[Any, str | None]:
        """(value, None) when there is something to fill, else (None, reason)."""
        if form_field.system:
            return self._system_value(form_field, profile, files)
        answer = answers.get(form_field.key)
        if answer is None or answer.skipped:
            return None, "no answer" if answer is None else (answer.reason or "skipped")
        if form_field.kind == FieldKind.FILE:
            return _file_value(answer, files)
        if form_field.kind in _CHOICE_KINDS and answer.option is not None:
            return answer.option, None
        return (None, "empty answer") if answer.value is None else (answer.value, None)

    def _system_value(
        self, form_field: FormField, profile: Mapping[str, Any], files: FileInputs
    ) -> tuple[Any, str | None]:
        profile_key = system_profile_key(form_field.key, form_field.label, self.cfg)
        if profile_key == PROFILE_RESUME_KEY:
            return files.resume_pdf, None
        value = profile_value(profile_key or "", profile)
        if value is None:
            return None, f"no profile value for {profile_key}"
        return value, None

    def _fill_one(
        self, ctx: FillContext, form_field: FormField, value: Any, report: FillReport
    ) -> None:
        try:
            fill_value(ctx, form_field, value)
        except (PlaywrightError, FillerError) as exc:
            message = str(exc).splitlines()[0][:200]
            if form_field.required:
                report.errors.append(f"{form_field.key}: {message}")
            else:
                report.skipped.append(f"{form_field.key}: fill failed: {message}")
            return
        report.filled.append(form_field.key)


def _file_value(answer: Answer, files: FileInputs) -> tuple[Any, str | None]:
    marker = str(answer.value or "")
    if marker == RESUME_MARKER:
        return files.resume_pdf, None
    if marker == COVER_LETTER_MARKER:
        if files.cover_letter_pdf is None:
            return None, "no cover letter file"
        return files.cover_letter_pdf, None
    return None, f"unknown file marker {marker!r}"
