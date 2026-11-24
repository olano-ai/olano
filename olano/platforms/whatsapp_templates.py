"""WhatsApp message templates, whoever relays them: the model and WhatsApp's rules.

WhatsApp lets a business open a conversation, or write to someone more than 24
hours after they last wrote, only with a template Meta has approved. Two
connectors send them, and they store the same thing differently: Twilio keeps
a template as a Content resource (``twilio_content``) and relays it to Meta,
while Meta's Cloud API (``whatsapp_meta_graph``) serves the WhatsApp Business
Account's own templates. Both are flattened into one :class:`Template`, so the
agent's WhatsApp tools list, fill and send a template the same way on either,
and WhatsApp's rules for a new one (checked by :func:`template_problems`
before a review is spent on it) live here once.

## Meta's rules, checked before a review is spent on them

Twilio documents these under "Using variables with Content Templates", and a
template that breaks one is rejected days later with a one-line reason:

- placeholders are numbered ``{{1}}``, ``{{2}}``... with no gaps, or named in
  lowercase (``{{first_name}}``), never side by side, never the first or last
  thing in the body, and every ``x`` of them need at least ``2x + 1`` other
  words around them;
- every placeholder has a sample value (Meta reviews the template with them);
- the body is at most 1024 characters, and template names are lowercase
  letters, digits and underscores.

**A variable's value may not contain a line break** (WhatsApp also refuses tabs
and long runs of spaces), so :func:`clean_variable` collapses whitespace before
any send.

## One set of placeholder keys per template

A template's placeholders are keyed as the agent fills them. Body placeholders
keep the key they are written with (``{{1}}`` takes ``"1"``, ``{{first_name}}``
takes ``"first_name"``). Meta keeps a header's and each website button's
variable separate from the body's (a header ``{{1}}`` is not the body's
``{{1}}``), so the Cloud API side rewrites them as ``{{header}}`` and
``{{button_N}}`` when it reads a template: one flat set of keys, no collisions,
and :attr:`Template.placeholders` reads them all off the text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

#: Meta's limit on a template body. Twilio accepts 1600 characters for
#: ``twilio/text``, but WhatsApp refuses a template body over this.
MAX_TEMPLATE_BODY = 1024

#: WhatsApp's limits for template buttons, as Twilio documents them
#: (twilio.com/docs/content, "twilio/quick-reply" and "twilio/call-to-action").
#: Meta allows more combinations; these are the ones both connectors create.
MAX_BUTTON_TITLE = 20
MAX_REPLY_BUTTONS = 10
MAX_URL_BUTTONS = 2
MAX_PHONE_BUTTONS = 1
MAX_CALL_TO_ACTION_BODY = 640
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


#: The business categories WhatsApp accepts in a sender's profile, as the
#: agent names them (twilio.com/docs/whatsapp/api/senders, ``profile.vertical``).
#: The Cloud API spells the same list as enums; ``whatsapp_meta_graph`` maps them.
PROFILE_VERTICALS = (
    "Automotive",
    "Beauty/Spa/Salon",
    "Clothing/Apparel",
    "Education",
    "Entertainment",
    "Event Planning",
    "Finance/Banking",
    "Food/Grocery",
    "Hotel/Lodging",
    "Medical/Health",
    "Non-profit",
    "Professional Services",
    "Public Service",
    "Restaurant",
    "Shopping/Retail",
    "Travel/Transportation",
    "Other",
)
#: WhatsApp's length limits on the profile fields the tool changes.
PROFILE_LIMITS: dict[str, int] = {"about": 139, "description": 512, "address": 256}


#: WhatsApp's template categories. ``AUTHENTICATION`` templates are a
#: separate content type (one-time codes with a copy button), so the create
#: tool refuses it with a pointer instead of submitting a text template Meta
#: would reject.
CATEGORIES = ("UTILITY", "MARKETING", "AUTHENTICATION")
CREATABLE_CATEGORIES = ("UTILITY", "MARKETING")

#: Approval states Twilio and the Cloud API report, lowercased. Only
#: ``approved`` can be sent. ``unsubmitted`` and ``received`` are Twilio's own
#: (the Cloud API submits a template when it is created); the last four are
#: the Cloud API's.
STATUS_HELP: dict[str, str] = {
    "unsubmitted": "created but never submitted to WhatsApp for approval",
    "received": "received by Twilio, not yet in review at WhatsApp",
    "pending": "under review by WhatsApp (usually within 24 hours)",
    "approved": "approved by WhatsApp and ready to send",
    "rejected": "rejected by WhatsApp",
    "paused": "paused by WhatsApp after negative feedback from recipients; it cannot be sent",
    "disabled": "disabled by WhatsApp after negative feedback or a policy violation; it cannot be sent",
    "in_appeal": "rejected, and an appeal of the rejection is under review",
    "pending_deletion": "being deleted; it cannot be sent",
    "limit_exceeded": "over the Business Account's template limit; it cannot be sent",
    "archived": "archived in WhatsApp Manager; it cannot be sent until it is unarchived",
}

TEMPLATE_NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:_[A-Za-z]{2,4})?$")
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
#: A named placeholder WhatsApp accepts: lowercase letters, digits and
#: underscores, starting with a letter ({{first_name}}).
_NAMED_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
#: Twilio's limit on a placeholder's name, on every channel ("A variable key
#: has a maximum length of 16 characters", Using Variables with Content
#: Templates). A numbered placeholder never comes near it.
MAX_VARIABLE_KEY = 16
_ADJACENT = re.compile(r"\}\}[^A-Za-z0-9\{\}]*\{\{")
_ENDS_WITH_PLACEHOLDER = re.compile(r"\}\}[\s.,!?;:)\]]*$")
_STARTS_WITH_PLACEHOLDER = re.compile(r"^[\s(\[]*\{\{")


def clean_variable(value: Any) -> str:
    """A template variable value WhatsApp will accept.

    WhatsApp refuses a variable containing a line break, a tab or a long run
    of spaces, after the provider has already accepted the message, so every run of
    whitespace collapses to one space.
    """
    return " ".join(str(value if value is not None else "").split())


def placeholders_in(body: str) -> list[str]:
    """The placeholder keys a template body uses, in order, each once."""
    seen: list[str] = []
    for key in _PLACEHOLDER.findall(body or ""):
        if key not in seen:
            seen.append(key)
    return seen


def render(body: str, values: Mapping[str, str]) -> str:
    """The body with each placeholder replaced by its value (unknown ones kept)."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), body or "")


def normalize_template_name(raw: Any) -> str:
    """A WhatsApp template name: lowercase letters, digits and underscores."""
    name = re.sub(r"[^a-z0-9]+", "_", str(raw or "").strip().lower()).strip("_")
    return name[:512]


@dataclass(frozen=True)
class Button:
    """One button on a template: a quick reply, a website link or a phone call.

    ``value`` is the reply's id, the link's address or the phone number.
    """

    kind: str
    title: str
    value: str = ""

    def describe(self) -> dict[str, str]:
        row = {"type": self.kind, "title": self.title}
        if self.kind == "url":
            row["url"] = self.value
        elif self.kind == "phone":
            row["phone"] = self.value
        return row


@dataclass(frozen=True)
class Template:
    """One template and its WhatsApp approval, flattened, whichever connector holds it.

    ``sid`` is the provider's id: a Twilio Content SID (``HX…``) or a Cloud
    API template id (digits). ``header`` and ``footer`` are the Cloud API's
    extra text around the body; a header that is an image, a video or a
    document has no text and names its kind in ``header_format``.
    ``previous_category`` is set when Meta re-categorized the template (it is
    billed by ``category``, the current one); ``correct_category`` is the
    category Meta says it should have when it disagrees with the current one. ``provider_data`` carries what
    only the sending side needs, such as a Cloud API template's original
    parameter names.
    """

    sid: str
    friendly_name: str = ""
    language: str = ""
    content_type: str = ""
    body: str = ""
    samples: dict[str, str] = field(default_factory=dict)
    whatsapp_name: str = ""
    category: str = ""
    status: str = "unsubmitted"
    rejection_reason: str = ""
    date_updated: str = ""
    buttons: tuple[Button, ...] = ()
    header: str = ""
    header_format: str = ""
    footer: str = ""
    previous_category: str = ""
    correct_category: str = ""
    quality: str = ""
    provider_data: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    @property
    def name(self) -> str:
        return self.whatsapp_name or self.friendly_name or self.sid

    @property
    def placeholders(self) -> list[str]:
        """Every placeholder the template uses: the header's, the body's, its buttons'."""
        return placeholders_in(
            " ".join([self.header, self.body, *(f"{b.title} {b.value}" for b in self.buttons)])
        )

    def rendered(self, values: Mapping[str, str]) -> str:
        """The message as the recipient reads it, with *values* filled in."""
        parts = [render(self.header, values), render(self.body, values), self.footer]
        text = "\n".join(p for p in parts if p)
        if self.buttons:
            text += "\n" + " ".join(f"[{render(b.title, values)}]" for b in self.buttons)
        return text

    def describe(self, *, with_status: bool = True) -> dict[str, Any]:
        """What a tool shows the model about this template."""
        row: dict[str, Any] = {
            "name": self.name,
            "sid": self.sid,
            "language": self.language,
            "category": self.category or None,
        }
        if self.previous_category and self.previous_category != self.category:
            row["category_changed_from"] = self.previous_category
            row["category_note"] = (
                f"WhatsApp moved this template from {self.previous_category} to "
                f"{self.category}; it is billed as {self.category}."
            )
        elif self.correct_category and self.correct_category != self.category:
            row["category_note"] = (
                f"WhatsApp considers this template {self.correct_category}, not "
                f"{self.category}, and may re-categorize it."
            )
        if self.header:
            row["header"] = self.header
        elif self.header_format:
            row["header"] = f"[{self.header_format.lower()}]"
        row["body"] = self.body
        if self.footer:
            row["footer"] = self.footer
        row["variables"] = {key: self.samples.get(key, "") for key in self.placeholders}
        if self.buttons:
            row["buttons"] = [b.describe() for b in self.buttons]
        if self.content_type and self.content_type != "twilio/text":
            row["content_type"] = self.content_type
        if with_status:
            row["status"] = self.status
            row["status_means"] = STATUS_HELP.get(self.status, "")
            if self.rejection_reason:
                row["rejection_reason"] = self.rejection_reason
            if self.quality:
                row["quality"] = self.quality
        return row


def parse_buttons(raw: Any) -> tuple[list[Button], list[str]]:
    """Buttons for a new template from the tool's input, and every problem with them.

    Input: ``[{"type": "reply", "title": "Yes"}, {"type": "url", "title":
    "Track order", "url": "https://shop.example/track/{{2}}"}, {"type":
    "phone", "title": "Call us", "phone": "+15551234567"}]``. Reply buttons go
    on a quick-reply template (up to ten); website and phone buttons on a
    call-to-action template (two websites, one phone). The two kinds are not
    mixed in one template here: Twilio's call-to-action type documents no
    reply id to carry, and one rule for both connectors keeps the agent's
    instructions the same.
    """
    buttons: list[Button] = []
    problems: list[str] = []
    if raw in (None, "", []):
        return buttons, problems
    if not isinstance(raw, list):
        return buttons, ["buttons must be a list of {type, title, ...} objects."]
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"Button {index} must be an object with a type and a title.")
            continue
        kind = str(item.get("type") or "reply").strip().lower()
        kind = {"quick_reply": "reply", "link": "url", "website": "url", "call": "phone"}.get(
            kind, kind
        )
        title = str(item.get("title") or "").strip()
        label = f"Button {index} ({title or kind})"
        if not title:
            problems.append(f"{label} needs a title.")
        elif len(title) > MAX_BUTTON_TITLE:
            problems.append(f"{label}: a title is at most {MAX_BUTTON_TITLE} characters.")
        elif clean_variable(title) != title:
            problems.append(f"{label}: a title cannot contain a line break or extra spaces.")
        if kind == "reply":
            slug = normalize_template_name(title) or f"reply_{index}"
            buttons.append(Button("reply", title, slug))
        elif kind == "url":
            url = str(item.get("url") or "").strip()
            keys = placeholders_in(url)
            if not url.startswith("https://"):
                problems.append(f"{label}: the website address must start with https://.")
            elif len(keys) > 1 or (keys and not re.search(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}$", url)):
                problems.append(
                    f"{label}: a placeholder can only be the very end of the address, e.g. "
                    "https://shop.example/track/{{1}}."
                )
            buttons.append(Button("url", title, url))
        elif kind == "phone":
            phone = re.sub(r"[\s()-]", "", str(item.get("phone") or ""))
            if not _E164.match(phone):
                problems.append(
                    f"{label}: the phone number must be in international format, e.g. +15551234567."
                )
            buttons.append(Button("phone", title, phone))
        else:
            problems.append(f"{label}: type must be reply, url or phone (got {kind!r}).")
    replies = [b for b in buttons if b.kind == "reply"]
    links = [b for b in buttons if b.kind == "url"]
    phones = [b for b in buttons if b.kind == "phone"]
    if replies and (links or phones):
        problems.append(
            "Reply buttons and website/phone buttons go on different templates; make one "
            "template of each kind."
        )
    if len(replies) > MAX_REPLY_BUTTONS:
        problems.append(f"At most {MAX_REPLY_BUTTONS} reply buttons fit on one template.")
    if len(links) > MAX_URL_BUTTONS:
        problems.append(f"At most {MAX_URL_BUTTONS} website buttons fit on one template.")
    if len(phones) > MAX_PHONE_BUTTONS:
        problems.append(f"At most {MAX_PHONE_BUTTONS} phone button fits on one template.")
    titles = [b.title.lower() for b in buttons if b.title]
    if len(set(titles)) != len(titles):
        problems.append("Two buttons share a title; every button needs its own.")
    return buttons, problems


def template_problems(
    body: str,
    samples: Mapping[str, str],
    *,
    category: str = "UTILITY",
    buttons: tuple[Button, ...] | list[Button] = (),
) -> list[str]:
    """Every WhatsApp rule a new template's text breaks, as sentences.

    Empty means Meta should accept the shape; it still judges the content.
    Placeholders in ``buttons`` (a website address may end with one) follow
    the numbering rule and need samples like the body's.
    """
    problems: list[str] = []
    text = (body or "").strip()
    if not text:
        return ["The template text is empty."]
    limit = (
        MAX_CALL_TO_ACTION_BODY
        if any(b.kind in ("url", "phone") for b in buttons)
        else MAX_TEMPLATE_BODY
    )
    if len(text) > limit:
        problems.append(
            f"The text is {len(text)} characters; WhatsApp allows {limit} in this template."
        )
    upper = str(category or "").strip().upper()
    if upper == "AUTHENTICATION":
        problems.append(
            "AUTHENTICATION templates are a special one-time-code format this tool does not "
            "create; build them in the Twilio Console (Messaging, Content Template Builder) "
            "or in WhatsApp Manager."
        )
    elif upper not in CREATABLE_CATEGORIES:
        problems.append(
            f"Category must be UTILITY (updates about something the customer asked for) or "
            f"MARKETING (promotions); got {category!r}."
        )

    body_keys = placeholders_in(text)
    keys = placeholders_in(" ".join([text, *(f"{b.title} {b.value}" for b in buttons)]))
    if not keys:
        return problems
    # Numbered ({{1}}, {{2}}) or named ({{first_name}}), never both: WhatsApp
    # reads a template's parameters one way or the other.
    numbered = [key for key in keys if key.isdigit()]
    named = [key for key in keys if not key.isdigit()]
    if numbered and named:
        problems.append(
            "Use either numbered placeholders ({{1}}, {{2}}, ...) or named ones "
            "({{first_name}}), not both in one template."
        )
        return problems
    if named:
        bad = [key for key in named if not _NAMED_KEY.match(key)]
        if bad:
            problems.append(
                "Placeholder names use lowercase letters, digits and underscores and start "
                "with a letter, e.g. {{first_name}} (found "
                + ", ".join("{{" + key + "}}" for key in bad)
                + ")."
            )
        long = [key for key in named if len(key) > MAX_VARIABLE_KEY]
        if long:
            problems.append(
                f"Placeholder names are at most {MAX_VARIABLE_KEY} characters (found "
                + ", ".join("{{" + key + "}}" for key in long)
                + ")."
            )
    else:
        numbers = sorted(int(key) for key in keys)
        if numbers != list(range(1, len(numbers) + 1)):
            problems.append(
                "Placeholders must be numbered from {{1}} with no gaps (found "
                + ", ".join("{{" + str(n) + "}}" for n in numbers)
                + ")."
            )
    if _ADJACENT.search(text):
        problems.append("Two placeholders sit side by side; put at least one word between them.")
    if _STARTS_WITH_PLACEHOLDER.search(text):
        problems.append("The text starts with a placeholder; start it with words.")
    if _ENDS_WITH_PLACEHOLDER.search(text):
        problems.append("The text ends with a placeholder; add words after the last one.")
    words = [w for w in _PLACEHOLDER.sub(" ", text).split() if re.search(r"[^\W_]", w)]
    needed = 2 * len(body_keys) + 1
    if body_keys and len(words) < needed:
        problems.append(
            f"Too many placeholders for the amount of text: {len(body_keys)} placeholder(s) "
            f"need at least {needed} other words, and the text has {len(words)}."
        )
    for key in keys:
        sample = str(samples.get(key, "") if samples else "").strip()
        if not sample:
            problems.append(
                "Give a sample value for {{" + key + "}}; WhatsApp reviews the template with them."
            )
        elif clean_variable(sample) != sample:
            problems.append(
                "The sample for {{" + key + "}} contains a line break or extra spaces, which "
                "WhatsApp does not allow in a variable."
            )
    return problems


def language_problem(language: str) -> Optional[str]:
    if _LANGUAGE_RE.match(str(language or "")):
        return None
    return f"Language {language!r} is not a language code; use one like en, es, pt_BR or en_US."


def _in_language(templates: list[Template], language: str) -> list[Template]:
    """The templates in *language*: an exact code, else one region of it (en -> en_US)."""
    wanted = str(language or "").strip().lower().replace("-", "_")
    if not wanted:
        return templates
    exact = [t for t in templates if t.language.lower() == wanted]
    if exact:
        return exact
    return [t for t in templates if t.language.lower().split("_")[0] == wanted]


def find_template(templates: list[Template], ref: str, language: str = "") -> Optional[Template]:
    """The template *ref* names: its id, its WhatsApp name, or its friendly name.

    Case-insensitive on names. A name shared by several templates (one per
    language) resolves only when *language* picks one, or when exactly one is
    approved, so a send never picks a language at random.
    """
    wanted = str(ref or "").strip()
    if not wanted:
        return None
    for template in templates:
        if template.sid == wanted:
            return template
    low = wanted.lower()
    matches = _in_language(
        [
            t
            for t in templates
            if low in (t.whatsapp_name.lower(), t.friendly_name.lower())
            and (t.whatsapp_name or t.friendly_name)
        ],
        language,
    )
    if len(matches) == 1:
        return matches[0]
    approved = [t for t in matches if t.approved]
    if len(approved) == 1:
        return approved[0]
    return None


def ambiguous_matches(templates: list[Template], ref: str, language: str = "") -> list[Template]:
    """Templates sharing *ref* as a name, for a "which one?" answer."""
    low = str(ref or "").strip().lower()
    return _in_language(
        [t for t in templates if low and low in (t.whatsapp_name.lower(), t.friendly_name.lower())],
        language,
    )
