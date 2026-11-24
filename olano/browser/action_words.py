"""What a button's words commit to, in the languages people browse in.

The approval gate (:mod:`olano.browser.consequence`) reads the label of the
control the agent is about to activate. An English-only word list let a
JavaScript "Enviar" or "送信" button go without asking, and filed "Pagar" or
"支払う" on a POST form as a plain form submit that a per-site auto-approval
rule could let through. This module is the multilingual half of that reading:

- **Four kinds**, checked in this order: ``protected`` (money, deletion,
  approval: never auto-approvable), ``send`` (messages, posts, replies,
  invites), ``signup`` (subscribe, register, book) and ``submit`` (confirm,
  save and the like). The order matters: "Enviar pago" is a payment.
- **Commit words** (:func:`commit_word`) decide whether a click asks at all
  when no form says so. They are the first three kinds plus the core "submit /
  confirm" words, and deliberately leave out the everyday "OK", "Save" and
  "Next" buttons, as the English list always did: a gate that asks on every
  dialog trains people to approve without reading.
- **Matching** is on NFKC-normalized, case-folded text. Scripts written without
  spaces between words (Chinese, Japanese, Korean, Thai) and scripts whose
  vowel signs Python does not count as word characters (Devanagari, Bengali)
  match as substrings; everything else matches on word boundaries, so "post"
  never fires inside "posts" and "pay" never inside "paypal". A false match can
  only make the agent ask (or keep a rule from applying): it never lets an
  action through, because an action no list recognizes is ``unknown`` and asks.

Code hints (:func:`hint_kind`): localized sites usually keep English names in
their markup (``msg-form__send-button``, ``data-testid="place-order"``), so the
control's id, name, test ids and class are read as a second, language-free
signal, split into words (camelCase and separators).

The lists are hand-reviewed and deliberately conservative; extend a language by
adding words, and add the case to tests/test_browser_action_words.py.
"""

from __future__ import annotations

import re
import unicodedata
from functools import cache
from itertools import pairwise

KINDS = ("protected", "send", "signup", "submit")

# fmt: off
# kind → language → words. Phrases are fine ("place order"); matching is on
# the normalized label, see the module docstring.
WORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "protected": {
        "en": ("buy", "buy now", "purchase", "pay", "pay now", "payment", "order", "place order",
               "order now", "checkout", "check out", "transfer", "withdraw", "donate", "donation",
               "delete", "remove account", "close account", "approve", "merge", "wire", "refund"),
        "es": ("comprar", "compra", "pagar", "pago", "realizar pedido", "hacer pedido", "pedir",
               "tramitar pedido", "finalizar compra", "transferir", "transferencia", "retirar",
               "donar", "eliminar", "borrar", "aprobar", "reembolso"),
        "pt": ("comprar", "pagar", "pagamento", "fazer pedido", "finalizar compra",
               "finalizar pedido", "transferir", "sacar", "doar", "excluir", "apagar", "eliminar",
               "aprovar", "reembolso"),
        "fr": ("acheter", "achat", "payer", "paiement", "commander", "passer la commande",
               "valider la commande", "transférer", "virement", "retirer", "faire un don",
               "supprimer", "approuver", "rembourser"),
        "de": ("kaufen", "jetzt kaufen", "bezahlen", "zahlen", "zahlung", "bestellen",
               "zahlungspflichtig bestellen", "kasse", "überweisen", "überweisung", "abheben",
               "spenden", "löschen", "genehmigen", "erstatten"),
        "it": ("acquista", "compra", "paga", "pagamento", "ordina", "procedi all'ordine",
               "trasferisci", "bonifico", "preleva", "dona", "elimina", "cancella", "approva",
               "rimborso"),
        "nl": ("kopen", "koop", "betalen", "betaling", "bestellen", "afrekenen", "overboeken",
               "opnemen", "doneren", "verwijderen", "goedkeuren"),
        "sv": ("köp", "köpa", "betala", "betalning", "beställ", "till kassan", "överför",
               "ta ut", "donera", "radera", "ta bort", "godkänn"),
        "da": ("køb", "betal", "betaling", "bestil", "til kassen", "overfør", "doner", "slet",
               "godkend"),
        "no": ("kjøp", "betal", "betaling", "bestill", "til kassen", "overfør", "doner", "slett",
               "godkjenn"),
        "fi": ("osta", "maksa", "maksu", "tilaa", "siirrä", "lahjoita", "poista", "hyväksy"),
        "pl": ("kup", "kup teraz", "zapłać", "płatność", "zamów", "zamawiam", "przelew",
               "przelej", "wypłać", "przekaż darowiznę", "usuń", "zatwierdź"),
        "cs": ("koupit", "zaplatit", "platba", "objednat", "převést", "vybrat", "darovat",
               "smazat", "odstranit", "schválit"),
        "ro": ("cumpără", "cumpara", "plătește", "plăteşte", "plata", "plată", "comandă",
               "plasează comanda", "transferă", "retrage", "donează", "șterge", "şterge",
               "aprobă"),
        "hu": ("vásárlás", "megveszem", "fizetés", "fizetek", "megrendelés", "rendelés leadása",
               "utalás", "átutalás", "kivét", "adományoz", "törlés", "töröl", "jóváhagy"),
        "el": ("αγορά", "αγόρασε", "πληρωμή", "πλήρωσε", "παραγγελία", "μεταφορά", "ανάληψη",
               "δωρεά", "διαγραφή", "έγκριση"),
        "ru": ("купить", "оплатить", "оплата", "заказать", "оформить заказ", "перевести",
               "перевод", "вывести", "пожертвовать", "удалить", "одобрить", "утвердить"),
        "uk": ("купити", "оплатити", "оплата", "замовити", "оформити замовлення", "переказати",
               "переказ", "вивести", "пожертвувати", "видалити", "схвалити"),
        "tr": ("satın al", "öde", "ödeme", "ödeme yap", "sipariş ver", "siparişi tamamla",
               "transfer", "havale", "para çek", "bağış yap", "sil", "onayla"),
        "ar": ("شراء", "اشتر", "اشتري", "دفع", "ادفع", "الدفع", "اطلب", "اطلب الآن", "إتمام الطلب",
               "تحويل", "سحب", "تبرع", "حذف", "احذف", "موافقة"),
        "he": ("קנה", "קנייה", "רכישה", "שלם", "תשלום", "הזמן", "בצע הזמנה", "העבר", "העברה",
               "משוך", "תרום", "מחק", "מחיקה", "אשר"),
        "fa": ("خرید", "پرداخت", "سفارش", "ثبت سفارش", "انتقال", "برداشت", "اهدا", "حذف", "تایید پرداخت"),
        "hi": ("खरीदें", "ख़रीदें", "भुगतान", "भुगतान करें", "ऑर्डर करें", "ऑर्डर दें", "ट्रांसफर",
               "स्थानांतरण", "निकालें", "दान करें", "हटाएं", "हटाएँ", "डिलीट"),
        "bn": ("কিনুন", "পেমেন্ট", "অর্থ প্রদান", "অর্ডার করুন", "স্থানান্তর", "দান করুন", "মুছুন",
               "মুছে ফেলুন"),
        "id": ("beli", "beli sekarang", "bayar", "pembayaran", "buat pesanan", "pesan sekarang",
               "checkout", "transfer", "tarik", "donasi", "hapus", "setujui"),
        "ms": ("beli", "bayar", "pembayaran", "buat pesanan", "pindah", "keluarkan", "derma",
               "padam", "luluskan"),
        "vi": ("mua", "mua ngay", "thanh toán", "đặt hàng", "chuyển tiền", "chuyển khoản",
               "rút tiền", "quyên góp", "ủng hộ", "xóa", "xoá", "phê duyệt"),
        "th": ("ซื้อ", "ชำระเงิน", "จ่ายเงิน", "สั่งซื้อ", "โอนเงิน", "ถอนเงิน", "บริจาค", "ลบ", "อนุมัติ"),
        "zh": ("购买", "立即购买", "付款", "支付", "结账", "结算", "下单", "提交订单", "转账",
               "提现", "捐款", "捐赠", "删除", "批准", "合并", "購買", "立即購買", "結帳", "結算",
               "提交訂單", "轉帳", "提領", "捐贈", "刪除", "退款"),
        "ja": ("購入", "買う", "支払", "お支払い", "決済", "注文", "注文する", "注文を確定",
               "レジに進む", "送金", "振込", "振り込", "出金", "寄付", "削除", "承認", "マージ",
               "返金"),
        "ko": ("구매", "구입", "결제", "주문", "주문하기", "송금", "이체", "출금", "기부", "삭제",
               "승인", "병합", "환불"),
    },
    "send": {
        "en": ("send", "post", "publish", "reply", "tweet", "invite", "share post", "connect"),
        "es": ("enviar", "envía", "envia", "publicar", "responder", "contestar", "invitar",
               "conectar"),
        "pt": ("enviar", "publicar", "postar", "responder", "convidar", "conectar"),
        "fr": ("envoyer", "publier", "répondre", "inviter", "se connecter avec"),
        "de": ("senden", "absenden", "abschicken", "versenden", "veröffentlichen", "posten",
               "antworten", "einladen", "vernetzen"),
        "it": ("invia", "pubblica", "rispondi", "invita", "collegati"),
        "nl": ("verzenden", "versturen", "verstuur", "plaatsen", "publiceren", "reageren",
               "beantwoorden", "uitnodigen", "connectie maken"),
        "sv": ("skicka", "publicera", "svara", "bjud in"),
        "da": ("send", "udgiv", "slå op", "svar", "inviter"),
        "no": ("send", "publiser", "svar", "inviter"),
        "fi": ("lähetä", "julkaise", "vastaa", "kutsu"),
        "pl": ("wyślij", "opublikuj", "odpowiedz", "zaproś", "nawiąż kontakt"),
        "cs": ("odeslat", "poslat", "zveřejnit", "publikovat", "odpovědět", "pozvat"),
        "ro": ("trimite", "publică", "răspunde", "invită"),
        "hu": ("küldés", "elküld", "közzététel", "közzétesz", "válasz", "meghív"),
        "el": ("αποστολή", "στείλε", "δημοσίευση", "απάντηση", "πρόσκληση"),
        "ru": ("отправить", "опубликовать", "ответить", "пригласить"),
        "uk": ("надіслати", "відправити", "опублікувати", "відповісти", "запросити"),
        "tr": ("gönder", "paylaş", "yayınla", "yanıtla", "cevapla", "davet et", "bağlantı kur"),
        "ar": ("إرسال", "ارسال", "أرسل", "نشر", "انشر", "رد", "الرد", "دعوة", "ادع"),
        "he": ("שלח", "שליחה", "פרסם", "פרסום", "השב", "הזמן לחבר"),
        "fa": ("ارسال", "بفرست", "انتشار", "منتشر کن", "پاسخ", "دعوت"),
        "hi": ("भेजें", "भेजो", "पोस्ट करें", "प्रकाशित करें", "जवाब दें", "उत्तर दें",
               "आमंत्रित करें"),
        "bn": ("পাঠান", "পোস্ট করুন", "প্রকাশ করুন", "উত্তর দিন", "আমন্ত্রণ"),
        "id": ("kirim", "kirimkan", "posting", "terbitkan", "balas", "undang", "terhubung"),
        "ms": ("hantar", "siarkan", "terbitkan", "balas", "jemput"),
        "vi": ("gửi", "đăng bài", "xuất bản", "trả lời", "phản hồi", "mời", "kết nối"),
        "th": ("ส่ง", "โพสต์", "เผยแพร่", "ตอบกลับ", "เชิญ"),
        "zh": ("发送", "发布", "发表", "回复", "邀请", "发帖", "發送", "傳送", "送出訊息", "發布",
               "發佈", "發表", "回覆", "邀請"),
        "ja": ("送信", "送る", "投稿", "返信", "公開する", "招待", "つながる"),
        "ko": ("보내기", "전송", "게시하기", "발행", "답장", "답글", "초대", "1촌 신청"),
    },
    "signup": {
        "en": ("subscribe", "sign up", "create account", "register", "book", "book now",
               "reserve"),
        "es": ("suscribirse", "suscribir", "suscríbete", "regístrate", "registrarse",
               "crear cuenta", "reservar"),
        "pt": ("assinar", "inscrever-se", "inscreva-se", "cadastre-se", "cadastrar",
               "criar conta", "reservar"),
        "fr": ("s'abonner", "abonnez-vous", "s'inscrire", "inscrivez-vous", "créer un compte",
               "réserver"),
        "de": ("abonnieren", "registrieren", "konto erstellen", "anmelden", "buchen",
               "reservieren"),
        "it": ("iscriviti", "abbonati", "registrati", "crea account", "prenota"),
        "nl": ("abonneren", "registreren", "account aanmaken", "aanmelden", "boeken",
               "reserveren"),
        "sv": ("prenumerera", "registrera", "skapa konto", "boka"),
        "da": ("abonner", "tilmeld", "opret konto", "book", "reserver"),
        "no": ("abonner", "registrer", "opprett konto", "bestill time", "reserver"),
        "fi": ("rekisteröidy", "luo tili", "varaa"),
        "pl": ("subskrybuj", "zarejestruj się", "załóż konto", "zarezerwuj"),
        "cs": ("odebírat", "zaregistrovat", "registrovat", "vytvořit účet", "rezervovat"),
        "ro": ("abonează-te", "abonează", "înregistrează-te", "creează cont", "rezervă"),
        "hu": ("feliratkozás", "regisztráció", "regisztrálok", "fiók létrehozása", "foglalás"),
        "el": ("εγγραφή", "δημιουργία λογαριασμού", "κράτηση"),
        "ru": ("подписаться", "зарегистрироваться", "регистрация", "создать аккаунт",
               "забронировать"),
        "uk": ("підписатися", "зареєструватися", "реєстрація", "створити акаунт",
               "забронювати"),
        "tr": ("abone ol", "kaydol", "kayıt ol", "üye ol", "hesap oluştur", "rezervasyon yap"),
        "ar": ("اشترك", "اشتراك", "سجل", "التسجيل", "إنشاء حساب", "احجز", "حجز"),
        "he": ("הירשם", "הרשמה", "צור חשבון", "הזמן מקום"),
        "fa": ("اشتراک", "ثبت نام", "ثبت‌نام", "ایجاد حساب", "رزرو"),
        "hi": ("सदस्यता लें", "साइन अप करें", "रजिस्टर करें", "खाता बनाएं", "बुक करें"),
        "bn": ("সাবস্ক্রাইব", "সাইন আপ", "নিবন্ধন", "অ্যাকাউন্ট তৈরি", "বুক করুন"),
        "id": ("berlangganan", "daftar", "buat akun", "pesan tiket", "reservasi"),
        "ms": ("langgan", "daftar", "cipta akaun", "tempah"),
        "vi": ("đăng ký", "tạo tài khoản", "đặt chỗ", "đặt phòng", "đặt lịch"),
        "th": ("สมัครสมาชิก", "สมัคร", "ลงทะเบียน", "สร้างบัญชี", "จอง"),
        "zh": ("订阅", "注册", "创建账户", "预订", "预约", "訂閱", "註冊", "建立帳戶", "預訂",
               "預約"),
        "ja": ("購読", "登録", "新規登録", "会員登録", "アカウント作成", "予約", "申し込", "申込"),
        "ko": ("구독", "가입", "회원가입", "계정 만들기", "예약", "신청"),
    },
    "submit": {
        "en": ("submit", "confirm"),
        "es": ("confirmar", "enviar formulario"),
        "pt": ("confirmar", "submeter"),
        "fr": ("confirmer", "valider", "soumettre"),
        "de": ("bestätigen", "einreichen"),
        "it": ("conferma", "invia modulo"),
        "nl": ("bevestigen", "indienen"),
        "sv": ("bekräfta",),
        "da": ("bekræft",),
        "no": ("bekreft",),
        "fi": ("vahvista",),
        "pl": ("potwierdź", "zatwierdź formularz"),
        "cs": ("potvrdit",),
        "ro": ("confirmă",),
        "hu": ("megerősítés", "megerősít"),
        "el": ("επιβεβαίωση", "υποβολή"),
        "ru": ("подтвердить",),
        "uk": ("підтвердити",),
        "tr": ("gönder formu", "doğrula"),
        "ar": ("تأكيد", "أكد"),
        "he": ("אישור",),
        "fa": ("تایید", "تأیید"),
        "hi": ("पुष्टि करें", "जमा करें"),
        "bn": ("নিশ্চিত করুন", "জমা দিন"),
        "id": ("konfirmasi",),
        "ms": ("sahkan",),
        "vi": ("xác nhận",),
        "th": ("ยืนยัน",),
        "zh": ("提交", "确认", "確認提交", "送出"),
        "ja": ("確定", "提出"),
        "ko": ("제출", "확정"),
    },
}

# Words that only *name* a form submit: classified as ``submit`` when a form
# submits anyway, but too common on harmless dialogs and steps to make a
# non-form click ask on their own.
SUBMIT_ONLY: dict[str, tuple[str, ...]] = {
    "en": ("save", "save changes", "update", "apply", "continue", "next", "done", "ok",
           "log in", "sign in", "search"),
    "es": ("guardar", "actualizar", "aplicar", "continuar", "siguiente", "aceptar",
           "iniciar sesión", "buscar"),
    "pt": ("salvar", "guardar", "atualizar", "aplicar", "continuar", "próximo", "entrar",
           "buscar", "pesquisar"),
    "fr": ("enregistrer", "mettre à jour", "appliquer", "continuer", "suivant", "se connecter",
           "rechercher"),
    "de": ("speichern", "aktualisieren", "übernehmen", "weiter", "fortfahren", "einloggen",
           "suchen"),
    "it": ("salva", "aggiorna", "applica", "continua", "avanti", "accedi", "cerca"),
    "nl": ("opslaan", "bijwerken", "toepassen", "doorgaan", "volgende", "inloggen", "zoeken"),
    "ru": ("сохранить", "обновить", "применить", "продолжить", "далее", "войти", "найти"),
    "uk": ("зберегти", "оновити", "застосувати", "продовжити", "далі", "увійти"),
    "pl": ("zapisz", "zastosuj", "dalej", "kontynuuj", "zaloguj się"),
    "tr": ("kaydet", "güncelle", "uygula", "devam", "ileri", "giriş yap"),
    "ar": ("حفظ", "تحديث", "تطبيق", "متابعة", "التالي", "تسجيل الدخول"),
    "id": ("simpan", "perbarui", "terapkan", "lanjutkan", "berikutnya", "masuk"),
    "vi": ("lưu", "cập nhật", "áp dụng", "tiếp tục", "đăng nhập"),
    "zh": ("保存", "更新", "应用", "继续", "下一步", "确定", "登录", "儲存", "繼續", "登入"),
    "ja": ("保存", "更新", "適用", "次へ", "続ける", "ログイン", "確認", "ok"),
    "ko": ("저장", "업데이트", "적용", "계속", "다음", "확인", "로그인"),
}
# fmt: on

# English code-hint tokens (ids, test ids, classes) and the kind each names.
# Only strong tokens: "post", "order" and "book" also name everyday things
# ("post-title", "sort-order", "book-cover") and stay out.
HINT_TOKENS: dict[str, str] = {
    "checkout": "protected",
    "pay": "protected",
    "payment": "protected",
    "purchase": "protected",
    "buy": "protected",
    "delete": "protected",
    "transfer": "protected",
    "withdraw": "protected",
    "donate": "protected",
    "refund": "protected",
    "placeorder": "protected",
    "send": "send",
    "publish": "send",
    "reply": "send",
    "invite": "send",
    "subscribe": "signup",
    "signup": "signup",
    "register": "signup",
    "submit": "submit",
    "confirm": "submit",
}

# Scripts written without spaces between words, or whose vowel signs Python's
# ``\w`` does not count as letters: match these entries as substrings.
_SUBSTRING_SCRIPTS = re.compile("[ऀ-ॿঀ-৿฀-๿຀-໿က-႟ក-៿ᄀ-ᇿ぀-ヿ㄰-㆏㐀-䶿一-鿿가-힯豈-﫿ｦ-ﾟ]")


def normalize(text: str) -> str:
    """NFKC (full-width forms, compatibility characters) + case-fold, spaces collapsed."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"\s+", " ", text).strip()


def _pattern(words: set[str]) -> re.Pattern[str] | None:
    spaced: list[str] = []
    spaceless: list[str] = []
    for word in sorted(words, key=len, reverse=True):
        w = normalize(word)
        if not w:
            continue
        (spaceless if _SUBSTRING_SCRIPTS.search(w) else spaced).append(re.escape(w))
    parts = []
    if spaced:
        parts.append(r"(?<!\w)(?:" + "|".join(spaced) + r")(?!\w)")
    if spaceless:
        parts.append("(?:" + "|".join(spaceless) + ")")
    return re.compile("|".join(parts)) if parts else None


@cache
def _kind_pattern(kind: str) -> re.Pattern[str] | None:
    words = {w for lang in WORDS.get(kind, {}).values() for w in lang}
    if kind == "submit":
        words |= {w for lang in SUBMIT_ONLY.values() for w in lang}
    return _pattern(words)


@cache
def _commit_pattern() -> re.Pattern[str] | None:
    return _pattern({w for kind in WORDS.values() for lang in kind.values() for w in lang})


def label_kind(label: str) -> tuple[str, str] | None:
    """``(kind, matched word)`` for the first kind the label names, or None."""
    text = normalize(label)
    if not text:
        return None
    for kind in KINDS:
        pattern = _kind_pattern(kind)
        hit = pattern.search(text) if pattern else None
        if hit:
            return kind, hit.group(0)
    return None


def commit_word(label: str) -> str | None:
    """The word in ``label`` that makes a click ask on its own, if any."""
    pattern = _commit_pattern()
    hit = pattern.search(normalize(label)) if pattern else None
    return hit.group(0) if hit else None


def hint_tokens(hints: str) -> list[str]:
    """``"msg-form__sendButton btn"`` → ``["msg", "form", "send", "button", "btn"]``."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", hints or "")
    return [t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t]


def hint_kind(hints: str) -> tuple[str, str] | None:
    """``(kind, token)`` from the control's English code names, or None.

    ``place-order`` / ``placeOrder`` count as one token, so "order" alone (a
    sort order) never does.
    """
    tokens = hint_tokens(hints)
    joined = {a + b for a, b in pairwise(tokens)}
    found = [(HINT_TOKENS[t], t) for t in [*tokens, *joined] if t in HINT_TOKENS]
    for kind in KINDS:
        for k, token in found:
            if k == kind:
                return kind, token
    return None
