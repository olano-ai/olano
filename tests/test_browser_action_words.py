"""The approval gate reads buttons in the languages people browse in
(olano/browser/action_words.py, olano/browser/consequence.py).

An English-only list let a JavaScript "Enviar" or "送信" button go without
asking, and filed "Pagar" on a POST form as a plain submit that an
auto-approval rule could let through. These pin the fix: the words, the
language-free signals, and the fail-closed ``unknown`` kind.
"""

from __future__ import annotations

import pytest

from olano.browser import action_words
from olano.browser.auto_approve import AUTO_APPROVABLE_KINDS
from olano.browser.consequence import action_kind, judge

# (label, kind) across scripts: Latin, Cyrillic, Greek, Arabic, Hebrew,
# Devanagari, Thai, Chinese (both scripts), Japanese, Korean.
CASES = [
    ("Enviar", "send"),
    ("Enviar mensaje", "send"),
    ("Publicar", "send"),
    ("Pagar ahora", "protected"),
    ("Eliminar cuenta", "protected"),
    ("Realizar pedido", "protected"),
    ("Suscribirse", "signup"),
    ("Confirmar", "submit"),
    ("Envoyer", "send"),
    ("Passer la commande", "protected"),
    ("S'inscrire", "signup"),
    ("Absenden", "send"),
    ("Zahlungspflichtig bestellen", "protected"),
    ("Löschen", "protected"),
    ("Invia", "send"),
    ("Acquista ora", "protected"),
    ("Enviar pagamento", "protected"),
    ("Wyślij", "send"),
    ("Zapłać", "protected"),
    ("Отправить", "send"),
    ("Оформить заказ", "protected"),
    ("Удалить", "protected"),
    ("Надіслати", "send"),
    ("Αποστολή", "send"),
    ("Πληρωμή", "protected"),
    ("Gönder", "send"),
    ("Satın al", "protected"),
    ("إرسال", "send"),
    ("ادفع الآن", "protected"),
    ("שלח", "send"),
    ("תשלום", "protected"),
    ("भेजें", "send"),
    ("भुगतान करें", "protected"),
    ("পাঠান", "send"),
    ("ส่ง", "send"),
    ("ชำระเงิน", "protected"),
    ("Gửi", "send"),
    ("Thanh toán", "protected"),
    ("Kirim", "send"),
    ("Bayar sekarang", "protected"),
    ("发送", "send"),
    ("立即购买", "protected"),
    ("删除", "protected"),
    ("注册", "signup"),
    ("提交", "submit"),
    ("傳送", "send"),
    ("結帳", "protected"),
    ("送信", "send"),
    ("送信する", "send"),
    ("購入する", "protected"),
    ("注文を確定する", "protected"),
    ("会員登録", "signup"),
    ("보내기", "send"),
    ("결제하기", "protected"),
    ("삭제", "protected"),
    ("회원가입", "signup"),
    # Money or deletion anywhere wins over everything else, in any language.
    ("Send payment", "protected"),
    ("Enviar pago", "protected"),
    ("送金する", "protected"),
]


@pytest.mark.parametrize(("label", "kind"), CASES)
def test_buttons_are_read_in_their_own_language(label, kind):
    assert action_kind(label) == kind
    # A JavaScript button (no form) asks on these words alone.
    verdict = judge({"tag": "button", "label": label, "submits": False}, what="click")
    if kind != "submit" or label in ("Confirmar", "提交"):
        assert verdict.consequential, label
        assert verdict.kind == kind


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("ＳＥＮＤ", "send"),  # full-width forms fold (NFKC)
        ("ENVIAR", "send"),  # case folds
        ("  Enviar\n mensaje ", "send"),
        ("ｿｳｼﾝ", "unknown"),  # half-width katakana is normalized, still not a listed word
    ],
)
def test_labels_are_normalized_before_matching(label, kind):
    assert action_kind(label) == kind


@pytest.mark.parametrize(
    "label",
    [
        "Posts",  # word boundaries: "post" is not in "posts"
        "PayPal account",  # nor "pay" in "paypal"
        "Unsubscribe",  # nor "subscribe" in "unsubscribe"
        "Ver más",
        "Me gusta",
        "Accept cookies",
        "Apply filters",
        "Save",
        "OK",
        "확인",  # Korean "OK": names a submit but never asks on its own
        "Siguiente",
    ],
)
def test_everyday_buttons_do_not_ask_on_their_own(label):
    assert not judge({"tag": "button", "label": label, "submits": False}, what="c").consequential


def test_a_label_no_list_recognizes_is_unknown_and_no_rule_can_cover_it():
    verdict = judge(
        {"tag": "button", "label": "Lipa sasa", "submits": True, "method": "post"}, what="c"
    )
    assert verdict.consequential and verdict.kind == "unknown"
    assert "unknown" not in AUTO_APPROVABLE_KINDS


def test_pay_on_a_form_is_never_filed_as_a_plain_submit():
    """The bug: "Pagar" on a POST form came back ``submit``, auto-approvable."""
    for label in ("Pagar", "支払う", "결제", "Оплатить", "Payer"):
        verdict = judge(
            {"tag": "button", "label": label, "submits": True, "method": "post"}, what="c"
        )
        assert verdict.kind == "protected", label
        assert verdict.kind not in AUTO_APPROVABLE_KINDS


def test_a_plain_link_never_asks_on_its_words():
    for label in ("Order history", "注文履歴", "Historial de pagos", "Delete account settings"):
        info = {"tag": "a", "label": label, "navigates": True, "submits": False}
        assert not judge(info, what="c").consequential, label
    # A link that is really a button (href="#", role=button) still does.
    assert judge({"tag": "a", "label": "Eliminar", "navigates": False}, what="c").consequential


def test_english_code_names_catch_what_the_words_miss():
    info = {"tag": "button", "label": "", "hints": "msg-form__send-button artdeco-button"}
    verdict = judge(info, what="c")
    assert verdict.consequential and verdict.kind == "send"
    assert action_words.hint_kind("placeOrderButton") == ("protected", "placeorder")
    assert action_words.hint_kind("btn sort-order") is None  # "order" alone is not a hint
    assert action_words.hint_kind("unsubscribeLink") is None


@pytest.mark.parametrize(
    "info",
    [
        {"secret": True},  # a password, card or one-time-code field
        {"payframe": True},  # a Stripe / PayPal / Adyen… frame
        {"page": "https://shop.example/es/checkout/paso-2"},
        {"page": "https://shop.example/", "action": "https://pay.example/payment/confirm"},
    ],
)
def test_payment_or_password_pages_make_any_button_protected(info):
    base = {"tag": "button", "label": "Lipa", "submits": False}
    verdict = judge({**base, **info}, what="c")
    assert verdict.consequential and verdict.kind == "protected"
    # The words do not matter: "Send" on such a page is still protected.
    named = judge({**base, **info, "label": "Enviar"}, what="c")
    assert named.kind == "protected"


def test_enter_in_a_field_is_named_by_the_forms_submit_button():
    info = {
        "tag": "input",
        "label": "Mensaje",
        "submits": True,
        "method": "post",
        "submitLabel": "Enviar",
    }
    assert judge(info, what="Press Enter").kind == "send"


def test_the_word_lists_are_well_formed():
    for kind, langs in action_words.WORDS.items():
        assert kind in action_words.KINDS
        for lang, words in langs.items():
            for word in words:
                assert action_words.normalize(word), (kind, lang, word)
                assert "\u2014" not in word
    # A submit-only word must never be a protected one: that would downgrade money.
    protected = {
        action_words.normalize(w) for ws in action_words.WORDS["protected"].values() for w in ws
    }
    submit_only = {
        action_words.normalize(w) for ws in action_words.SUBMIT_ONLY.values() for w in ws
    }
    assert not protected & submit_only
    # At least thirty languages, and the four the product is asked about.
    langs = set(action_words.WORDS["send"])
    assert len(langs) >= 30
    assert {"es", "zh", "ja", "ko"} <= langs
