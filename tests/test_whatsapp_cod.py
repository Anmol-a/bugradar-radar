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


def test_whatsapp_numbers_are_judged_like_whatsapp_would():
    assert wa_judge("919876543210") == ("ok", "opens a chat with +919876543210")
    assert wa_judge("+91 98765-43210")[0] == "ok"
    assert wa_judge("14155552671")[0] == "ok"                       # a US number with its country code
    assert wa_judge("9876543210")[0] == "warn"                      # Indian mobile, no 91
    assert wa_judge("")[0] == "warn"                                # no number at all
    assert wa_judge("12345")[0] == "bad" and "5 digits" in wa_judge("12345")[1]
    assert wa_judge("09876543210")[0] == "bad" and "starts with 0" in wa_judge("09876543210")[1]
    assert wa_judge("91XXXXXXXXXX")[0] == "bad"                     # a theme placeholder left in
    assert wa_judge("1234567890123456")[0] == "bad"                 # 16 digits


def test_cod_wording():
    assert cod_says("COD available") == "yes"
    assert cod_says("Cash on Delivery available*") == "yes"
    assert cod_says("Available: COD, UPI, cards") == "yes"
    assert cod_says("COD not available for this pincode") == "no"
    assert cod_says("No COD") == "no" and cod_says("Prepaid orders only") == "no"
    assert cod_says("Pay on delivery is available. COD not available on sale items") == "mixed"
    assert cod_says("Cod Liver Oil available in 60 capsules") is None        # a product, not a payment promise
    assert cod_says("Delivery by Thu, 16 Oct") is None
