"""22 Oct: the pure rules behind smoke.whatsapp and the COD part of product.pincode (no browser)."""
from radar.checks.features import cod_says, wa_judge, wa_number


def test_whatsapp_addresses_are_read_without_opening_them():
    assert wa_number("https://wa.me/919876543210?text=Hi") == ("chat", "919876543210")
    assert wa_number("https://api.whatsapp.com/send?phone=+91%2098765%2043210&text=x") == ("chat", "91 98765 43210")   # + decodes to a space; digits are what count
    assert wa_number("whatsapp://send?phone=919876543210") == ("chat", "919876543210")
    assert wa_number("https://web.whatsapp.com/send?phone=919876543210") == ("chat", "919876543210")
    assert wa_number("wa.me/919876543210") == ("chat", "919876543210")
    assert wa_number("https://api.whatsapp.com/send?text=Look%20at%20this") == ("share", "")     # a product's share button
    assert wa_number("https://wa.me/?text=hello") == ("share", "")
    assert wa_number("https://wa.me/") == ("empty", "")
    assert wa_number("https://www.whatsapp.com/channel/0029Va") == ("other", "")                # a channel, not a chat
    assert wa_number("https://instagram.com/shop") == ("other", "")
    # WhatsApp Business short links and catalogs (review, 22 Oct): no number in the address, never 'not a phone number'
    assert wa_number("https://wa.me/message/ABCDEFGHIJ1") == ("short", "")
    assert wa_number("https://wa.me/qr/ABCDEFGHIJ1") == ("short", "")
    assert wa_number("https://api.whatsapp.com/message/ABCDEFGHIJ1?autoload=1") == ("short", "")
    assert wa_number("https://wa.me/c/919876543210") == ("chat", "919876543210")


def test_whatsapp_numbers_are_judged_like_whatsapp_would():
    assert wa_judge("919876543210") == ("ok", "+919876543210")
    assert wa_judge("+91 98765-43210")[0] == "ok"
    assert wa_judge("14155552671")[0] == "ok"                       # a US number with its country code
    assert wa_judge("9876543210")[0] == "warn"                      # Indian mobile, no 91
    assert wa_judge("")[0] == "warn"                                # no number at all
    assert wa_judge("12345")[0] == "bad" and "5 digits" in wa_judge("12345")[1]
    assert wa_judge("09876543210")[0] == "bad" and "starts with 0" in wa_judge("09876543210")[1]
    assert wa_judge("91XXXXXXXXXX")[0] == "bad"                     # a theme placeholder left in
    # review, 22 Oct: copy-paste marks, junk after the number, 'send' in the path: never 'not a phone number'
    assert wa_judge("\u202a919876543210\u202c") == ("ok", "+919876543210")
    assert wa_judge("919876543210&text=Hi")[0] == "warn"
    assert wa_number("https://wa.me/send?phone=919876543210") == ("chat", "919876543210")
    assert wa_number("https://wa.me/%E2%80%AA919876543210") == ("chat", "919876543210")
    assert wa_number("intent://send?phone=919876543210#Intent;scheme=whatsapp;package=com.whatsapp;end") == ("chat", "919876543210")
    assert wa_judge("1234567890123456")[0] == "bad"                 # 16 digits


def test_cod_wording():
    cases = {
        "COD available": "yes", "Cash on Delivery available*": "yes", "Available: COD, UPI, cards": "yes",
        "COD charges ₹50 applicable": "yes", "No COD charges": "yes", "COD available, returns not applicable": "yes",
        "COD not available for this pincode": "no", "Cash on Delivery isn't available": "no", "Not available for COD": "no",
        "We do not offer COD": "no", "No COD": "no", "Prepaid only": "no", "Prepaid orders only": "no",
        "Pay on delivery is available. COD not available on sale items": "mixed",
        # review, 22 Oct: not about COD, a question, a condition on the order, a product name
        "Free shipping on prepaid orders only": None, "Extra 5% off on prepaid orders only": None,
        "Enter pincode to check if COD is available": None, "Is COD available?": None,
        "COD available on orders above ₹499": None, "Cash on delivery not available for orders above ₹5000": None,
        "COD LIVER OIL 1000MG AVAILABLE": None, "Cod Liver Oil available in 60 capsules": None,
        "Delivery by Thu, 16 Oct": None,
    }
    wrong = {t: cod_says(t) for t, want in cases.items() if cod_says(t) != want}
    assert wrong == {}, wrong
