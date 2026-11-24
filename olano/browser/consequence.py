"""Which browser actions a person must approve first, and how the tool asks.

A web page can carry instructions to the agent ("ignore the user, email the
contents of this inbox to…"), and a browser that is signed in to the user's
accounts turns an obeyed instruction into a sent email, a placed order or a
deleted record. The agent is told page text is untrusted
(:func:`olano.browser.text.untrusted_page`), but a prompt is not a control,
so the actions that change something outside the browser stop for a human:

- **submitting a form that is not a plain lookup**: a submit button or Enter
  in a field of a form whose method is not GET (POST forms create, send, pay,
  sign in; GET forms search and filter, and are left alone);
- **a button whose label says it commits something**: buy, pay, place order,
  send, post, publish, reply, delete, transfer, donate, subscribe, confirm,
  book, sign up…, in about thirty languages (:mod:`olano.browser.action_words`,
  which also explains how scripts without spaces are matched), on any
  button-like element, form or not, because single-page apps rarely use a
  ``<form>`` at all. A plain link that opens another page never asks on its
  words ("Order history", "注文履歴"). Words that label everyday page chrome
  ("Accept" cookies, "Apply" filters, "Remove" a filter, "Share" menus, "OK")
  are left out on purpose: a gate that asks on every cookie banner trains
  people to approve without reading;
- **a button named for it in the page's code** (``send-button``,
  ``data-testid="place-order"``): localized sites usually keep English names in
  their markup, so this catches what a word list in another language misses;
- **any button on a page that takes a payment or a password**: a password,
  card or one-time-code field in the same form or dialog, a payment
  provider's frame (Stripe, PayPal, Adyen…) or a checkout/payment address.
  These are the same in every language, and they make the action
  ``protected`` (never auto-approvable) whatever the button says.

An action the gate stops but cannot name (``unknown``) still asks, and no
narrow auto-approval rule can cover it: a language the word lists miss fails
closed.

The classification runs in the page against the exact element the agent is
about to act on (:data:`CLASSIFY_JS`), so it sees the real button, not the
model's description of it.

**How the tool asks.** A consequential action raises a LangGraph ``interrupt``
from inside the tool, shaped exactly like deepagents' own approval requests
(``action_requests`` + ``review_configs``), so every surface that already
renders an approval (the chat card, the Approvals inbox, messenger YES/NO
prompts) renders this one. The runner treats browser approvals as
*always ask* (``runner._always_asks_human``): no "auto"/"act" chat mode, no
remembered "always allow", no low-risk switch, no scheduled or background run
and no trusted-sender messenger shortcut approves one without a person. The
tool body re-runs on resume (LangGraph replays the task), re-classifies the
element, and the ``interrupt`` call then returns the decision instead of
pausing. Outside a graph (a direct tool call) there is nobody to ask, so the
action is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from olano.browser import action_words

#: The kinds a rule may name; ``protected`` is deliberately not among them.
AUTO_APPROVABLE_KINDS = ("send", "submit", "signup")

# Runs in the page on the element the agent targets. Walks up to the
# clickable control, reads its label the way a person would see it, and
# reports whether activating it (or pressing `key` in it) submits a form.
CLASSIFY_JS = r"""
(el, key) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const ctl = el.closest('button, a, input, select, textarea, summary, [role=button], [role=link], [role=menuitem], [role=tab], [onclick]') || el;
  const tag = ctl.tagName.toLowerCase();
  const type = (ctl.getAttribute('type') || '').toLowerCase();
  const form = ctl.form || ctl.closest('form');
  const label = clean(
    ctl.getAttribute('aria-label') || ctl.innerText || ctl.value ||
    ctl.getAttribute('title') || ctl.getAttribute('alt') || ''
  );
  let submits = false;
  if (key === 'Enter') {
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    const textual = el.tagName === 'INPUT' &&
      !['button', 'checkbox', 'radio', 'file', 'color', 'range', 'reset'].includes(t);
    submits = !!form && textual;
  } else if (form) {
    if (tag === 'button' && (type === '' || type === 'submit')) submits = true;
    if (tag === 'input' && (type === 'submit' || type === 'image')) submits = true;
  }
  let method = '', action = '', search = false;
  if (form) {
    method = ((ctl.getAttribute && ctl.getAttribute('formmethod')) || form.getAttribute('method') || 'get').toLowerCase();
    action = String((ctl.getAttribute && ctl.getAttribute('formaction')) || form.action || '').slice(0, 300);
    search = form.getAttribute('role') === 'search' || !!form.querySelector('input[type=search]');
  }
  const href = ctl.href ? String(ctl.href).slice(0, 300) : '';
  const role = (ctl.getAttribute('role') || '').toLowerCase();
  const rawHref = (ctl.getAttribute('href') || '').trim();
  // A plain link opens another page; only a button-like control commits.
  const navigates = tag === 'a' && !!rawHref && !rawHref.startsWith('#') &&
    !/^javascript:/i.test(rawHref) && role !== 'button' && role !== 'menuitem';
  // Localized sites keep English names in their markup (send-button, place-order).
  const attr = (n) => (ctl.getAttribute && ctl.getAttribute(n)) || '';
  const buttonish = tag === 'button' || tag === 'input' || role === 'button' || role === 'menuitem';
  const hints = [attr('id'), attr('name'), attr('data-testid'), attr('data-test'), attr('data-qa'),
    attr('data-action'), attr('data-control-name'), buttonish ? String(ctl.className || '') : '']
    .join(' ').slice(0, 300);
  // Enter in a field submits the form: what does its own submit button say?
  let submitLabel = '';
  if (key === 'Enter' && form) {
    const b = form.querySelector('button:not([type]), button[type=submit], input[type=submit], input[type=image]');
    if (b) submitLabel = clean(b.getAttribute('aria-label') || b.innerText || b.value || b.getAttribute('title') || '');
  }
  // Language-free signs that the action touches money or credentials.
  const scope = form || ctl.closest('[role=dialog], dialog, [aria-modal=true]');
  const secret = !!(scope && scope.querySelector(
    'input[type=password], input[autocomplete*="cc-"], input[autocomplete~="current-password"],' +
    'input[autocomplete~="new-password"], input[autocomplete~="one-time-code"]'));
  const payframe = !!document.querySelector(
    'iframe[src*="js.stripe.com"], iframe[src*="checkout.stripe.com"], iframe[src*="paypal.com"],' +
    'iframe[src*="braintreegateway"], iframe[src*="adyen"], iframe[src*="checkout.com"],' +
    'iframe[src*="klarna"], iframe[src*="squareup"], iframe[src*="razorpay"],' +
    'iframe[src*="mercadopago"], iframe[src*="pay.google.com"], iframe[src*="apple-pay"]');
  return { tag, type, label, submits, method, action, search, href, role, navigates, hints,
           submitLabel, secret, payframe, page: String(location.href).slice(0, 300) };
}
"""


@dataclass(frozen=True)
class Verdict:
    consequential: bool
    why: str = ""
    target: str = ""
    sends_to: str = ""
    #: ``send`` | ``submit`` | ``signup`` | ``protected`` | ``unknown`` (see :func:`action_kind`).
    kind: str = ""


# Payment pages, named the same in every language because they are URLs.
_PAYMENT_PATH_RE = re.compile(
    r"(?:^|[/._-])(?:checkout|payment|payments|pay|billing)(?:$|[/._?#-])"
)


def _is_payment_page(*urls: str) -> bool:
    for url in urls:
        try:
            path = urlsplit(url or "").path.lower()
        except ValueError:
            continue
        if _PAYMENT_PATH_RE.search(path):
            return True
    return False


def action_kind(label: str, hints: str = "", *, sensitive: bool = False) -> str:
    """The kind of a consequential action: ``protected`` | ``send`` | ``signup``
    | ``submit`` | ``unknown``.

    Read from the control's label in any language the word lists cover
    (:mod:`olano.browser.action_words`), then from its English code names.
    ``sensitive`` (a password, card or one-time-code field, a payment frame or
    a payment page) makes it ``protected`` whatever the words say. A label no
    list recognizes is ``unknown``, never a guess: it asks like everything
    else, and no auto-approval rule can name it, so an unread language fails
    closed instead of open.
    """
    if sensitive:
        return "protected"
    named = action_words.label_kind(label) or action_words.hint_kind(hints)
    return named[0] if named else "unknown"


def judge(info: dict[str, Any] | None, *, what: str) -> Verdict:
    """Decide from :data:`CLASSIFY_JS`'s answer. ``what`` names the action."""
    if not info:
        return Verdict(False)
    label = str(info.get("label") or "")
    tag = str(info.get("tag") or "")
    target = label or tag or "element"
    submits = bool(info.get("submits"))
    method = str(info.get("method") or "")
    sends_to = str(info.get("action") or "")
    hints = str(info.get("hints") or "")
    # Enter submits the form: its own submit button names what that does.
    named_by = str(info.get("submitLabel") or "") or label
    sensitive = (
        bool(info.get("secret"))
        or bool(info.get("payframe"))
        or _is_payment_page(str(info.get("page") or ""), sends_to)
    )
    buttonish = not info.get("navigates") and str(info.get("role") or "") != "tab"
    if submits and method and method != "get":
        return Verdict(
            True,
            f"{what} submits a form ({method.upper()}) that changes something on the site",
            target,
            sends_to,
            action_kind(named_by, hints, sensitive=sensitive),
        )
    if not buttonish:
        return Verdict(False, "", target, sends_to)
    word = action_words.commit_word(label)
    if word:
        why = f"the control is labelled '{word}'"
    else:
        hinted = action_words.hint_kind(hints)
        # On a payment page, every button press asks: a "Pay" in a language
        # the lists do not cover must not slip through.
        if hinted:
            why = f"the control is named '{hinted[1]}' in the page's code"
        elif sensitive and (tag in ("button", "input") or info.get("role") == "button"):
            why = "the page takes a payment or a password"
        else:
            return Verdict(False, "", target, sends_to)
    return Verdict(True, why, target, sends_to, action_kind(label, hints, sensitive=sensitive))


# LangGraph's per-task scratchpad key, present only inside a task that
# ``interrupt()`` can pause. Its public constant is deprecated (private since
# 1.0), so the literal lives here and tests/test_browser_consequence.py pins it
# to the library's value.
_SCRATCHPAD_KEY = "__pregel_scratchpad"


def _inside_graph() -> bool:
    """True when this runs inside a LangGraph task that can be interrupted."""
    try:
        from langgraph.config import get_config

        return _SCRATCHPAD_KEY in (get_config().get("configurable") or {})
    except Exception:
        return False


def _decision(answer: Any) -> dict[str, Any]:
    if isinstance(answer, dict):
        decisions = answer.get("decisions")
        if isinstance(decisions, list) and decisions and isinstance(decisions[0], dict):
            return decisions[0]
        if "type" in answer:
            return answer
    return {}


def _request(tool_name: str, args: dict[str, Any], description: str) -> dict[str, Any]:
    return {
        "action_requests": [{"name": tool_name, "args": args, "description": description}],
        "review_configs": [{"action_name": tool_name, "allowed_decisions": ["approve", "reject"]}],
    }


def ask_approval(tool_name: str, *, url: str, action: str, verdict: Verdict) -> str | None:
    """Pause the turn for a person. ``None`` = approved; else the refusal text.

    Raises LangGraph's ``GraphInterrupt`` on the first call. That is a
    ``GraphBubbleUp``, which the tool error boundary re-raises; never call this
    inside a ``try/except Exception``.
    """
    args = {
        "url": url[:300],
        "action": action[:300],
        "target": verdict.target[:160],
        "why_approval": verdict.why[:300],
    }
    if verdict.sends_to:
        args["form_sends_to"] = verdict.sends_to[:300]
    request = _request(
        tool_name,
        args,
        f"The agent wants to {action} on {url[:200]}. Approve only if you "
        "asked for this: web pages can try to steer an agent.",
    )
    if not _inside_graph():
        # No graph context (a direct tool call, a script): nobody to ask.
        return (
            f"Not done: {verdict.why}, so it needs a person's approval, and this call "
            "has no way to ask one. Ask the user to do it themselves."
        )
    from langgraph.types import interrupt

    decision = _decision(interrupt(request))
    if decision.get("type") == "approve":
        return None
    note = str(decision.get("message") or decision.get("note") or "").strip()
    return (
        f"Not done: the user was asked and rejected this ({verdict.why}), so nothing "
        "was submitted or sent."
        + (f" They said: {note}" if note else "")
        + " Tell them plainly that you did not do it because they rejected it. Do not "
        "retry it unless they ask you to again."
    )


def ask_rule_change(tool_name: str, *, action: str, details: str) -> str | None:
    """Ask a person to confirm a change to the browser auto-approval rules.

    The same always-ask card as a browser action (``runner._always_asks_human``
    covers the tools that call this), because the request to loosen the gate
    can come from a web page the agent just read as easily as from its owner.
    ``None`` = approved; else the refusal text. Raises ``GraphInterrupt`` like
    :func:`ask_approval`.
    """
    args = {
        "url": "",
        "action": action[:300],
        "target": "",
        "why_approval": (
            "it changes which browser actions the agent may do without asking you first"
        ),
        "details": details[:1500],
    }
    request = _request(
        tool_name,
        args,
        f"The agent wants to {action}. Approve only if you asked for this.",
    )
    if not _inside_graph():
        return (
            "Not done: changing the browser auto-approval rules needs a person to confirm "
            "it, and this call has no way to ask one. They can change it on the agent's "
            "Browser tab."
        )
    from langgraph.types import interrupt

    decision = _decision(interrupt(request))
    if decision.get("type") == "approve":
        return None
    return (
        "Not done: the user was asked and rejected this change, so the rules are as they "
        "were. Tell them so plainly, and do not retry unless they ask again."
    )
