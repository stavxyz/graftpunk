"""The name gate (graftpunk.har.paths.printable_name, and its copy in the graft
skill's har-facts.py) over a list of common posted body field names, so the claim
that a generated stub still sends them is checkable and pinned (#359).

Since 1.42.2 a body field name prints in the digest, --json and --endpoints-json,
and a generated stub sends it, only when the gate passes it. The list covers form
logins, CSRF and page-state fields, OAuth and SAML fields, login framework and bot
check fields, and common API body keys.
"""

from __future__ import annotations

from graftpunk.har.paths import printable_name
from tests.unit.test_har_paths import _load_har_facts

_LOGIN = (  # noqa: SIM905 - one compact string on purpose: the list is data
    "username password email login user pass passwd pwd remember rememberMe remember_me "
    "user[email] user[password] user[login] user[remember_me] session[email] "
    "session[password] j_username j_password loginfmt identifier Email Password UserName "
    "Username Login LoginName loginName emailAddress email_address phone phoneNumber mobile"
).split()
_CSRF_AND_PAGE_STATE = (  # noqa: SIM905
    "__RequestVerificationToken authenticity_token _token csrfmiddlewaretoken csrf_token "
    "_csrf csrf __VIEWSTATE __EVENTVALIDATION __VIEWSTATEGENERATOR __EVENTTARGET "
    "__EVENTARGUMENT __VIEWSTATEENCRYPTED __LASTFOCUS __PREVIOUSPAGE form_key _wpnonce "
    "nonce _csrf_token YII_CSRF_TOKEN"
).split()
_OAUTH_AND_SAML = (  # noqa: SIM905
    "grant_type client_id client_secret redirect_uri code code_verifier code_challenge "
    "code_challenge_method state scope response_type response_mode refresh_token "
    "SAMLResponse SAMLRequest RelayState id_token assertion audience prompt login_hint"
).split()
_FRAMEWORK_AND_BOT_CHECK = (  # noqa: SIM905
    "utf8 commit _method submit action return_url returnUrl ReturnUrl next redirect "
    "redirect_to lt execution _eventId g-recaptcha-response cf-turnstile-response "
    "h-captcha-response captcha otp mfa_code totp verificationCode verification_code token"
).split()
_API = (  # noqa: SIM905
    "query variables operationName extensions page per_page pageSize limit offset cursor "
    "after before first last sort sortBy order orderBy filter filters q search id ids name "
    "firstName lastName first_name last_name fullName address address1 address2 city "
    "region province zip zipCode postalCode postal_code country countryCode quantity qty "
    "productId product_id sku variantId variant_id cartId cart_id items lineItems "
    "line_items amount currency couponCode coupon_code promoCode discountCode deviceId "
    "device_id timezone locale language lang clientMutationId input data type status "
    "message description title accept_terms acceptTerms terms newsletter subscribe "
    "marketingOptIn birthday dob gender company storeId store_id locationId shippingMethod "
    "paymentMethod payment_method_id cardNumber cvc expMonth expYear note comment rating "
    "review_text"
).split()

COMMON_BODY_FIELD_NAMES = tuple(
    dict.fromkeys(_LOGIN + _CSRF_AND_PAGE_STATE + _OAUTH_AND_SAML + _FRAMEWORK_AND_BOT_CHECK + _API)
)

# Held back by the rules until #359's second round, now fixed names: Microsoft's
# login form field and the vowel-less quantity abbreviation.
_FIXED_SINCE_359 = ("loginfmt", "qty")


def test_the_list_is_the_one_measured() -> None:
    assert len(COMMON_BODY_FIELD_NAMES) == 196


def test_the_gate_holds_back_none_of_the_common_body_field_names() -> None:
    assert [name for name in COMMON_BODY_FIELD_NAMES if not printable_name(name)] == []


def test_har_facts_reads_each_name_as_the_digest_does() -> None:
    script = _load_har_facts()
    assert [
        n for n in COMMON_BODY_FIELD_NAMES if script.printable_name(n) != printable_name(n)
    ] == []


def test_the_two_fixed_names_pass_only_whole() -> None:
    for name in _FIXED_SINCE_359:
        assert printable_name(name)
        assert not printable_name(f"{name}Zq9vKx")
