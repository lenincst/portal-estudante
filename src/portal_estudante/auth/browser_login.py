"""Ulife login through Microsoft Entra ID (OAuth2 authorization code), driven by Playwright.

Observed flow:
  www.ulife.com.br/login.aspx (ASP.NET WebForms, field ctl00$b$login$txtLogin + fixed "@ulife.com.br")
  -> login.microsoftonline.com/organizations/oauth2/v2.0/authorize
       client_id=1d6578ce-ed91-4a70-a452-70ae7a7ac431, scope=api://servico_ulifelms_sso/Api.Read openid profile offline_access,
       redirect_uri=https://api.ulife.com.br/authentication/signIn, prompt=select_account, login_hint=<RA>@ulife.com.br
  -> api.ulife.com.br/authentication/signIn -> student.ulife.com.br/NewStudentHome

The Microsoft sign-in is a JavaScript application, so a real browser is used for this step only.
A persistent profile (in the private data dir, outside the project) keeps the Microsoft
"stay signed in" cookie, so later logins usually skip the password. By default the student types the password
on the official page (interactive); only if they opted in to saving it, it comes from the OS keychain. Credentials come from the store
Secrets Manager and are typed by Playwright; they are never printed or logged.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .. import config
from ..errors import AuthError, MfaRequiredError
from ..secrets import SecretStore

log = logging.getLogger(__name__)

MS_HOST = "login.microsoftonline.com"
STUDENT_HOST_RE = re.compile(r"^https://student\.ulife\.com\.br/")
MFA_SELECTORS = (
    "#idDiv_SAOTCAS_Title",  # approve sign-in request (Authenticator push)
    "#idDiv_SAOTCC_Title",  # enter code
    "#idRichContext_DisplaySign",  # number matching
    "input[name='otc']",
    "#idDiv_SAASDS_Title",
)
PROFILE_DIR = config.DATA_DIR / "browser-profile"
# The password may only be typed into Microsoft's sign-in page while it is serving Ulife's OAuth request.
ULIFE_CLIENT_ID = "1d6578ce-ed91-4a70-a452-70ae7a7ac431"
ULIFE_REDIRECT_PREFIX = "https://api.ulife.com.br/authentication/"


def is_ulife_authorize_url(url: str) -> bool:
    """True only for Microsoft's authorize endpoint carrying Ulife's client_id and Ulife's redirect_uri."""
    from urllib.parse import parse_qs, urlsplit

    u = urlsplit(url)
    if u.scheme != "https" or u.hostname != MS_HOST:
        return False
    q = parse_qs(u.query)
    return q.get("client_id", [""])[0] == ULIFE_CLIENT_ID and q.get("redirect_uri", [""])[0].startswith(ULIFE_REDIRECT_PREFIX)


def _pause(page: Any, seconds: float = 1.2) -> None:
    page.wait_for_timeout(int(seconds * 1000))


def _handle_microsoft(page: Any, username: str, secrets: SecretStore, timeout_ms: int) -> None:
    """Complete the Microsoft pages: account picker, username, password, KMSI. Detect MFA.

    Credential safety: the flow must have started at Microsoft's authorize URL for Ulife's client_id/redirect
    (checked on entry); otherwise nothing is typed. The password is only filled on a page whose host is exactly
    login.microsoftonline.com."""
    if not is_ulife_authorize_url(page.url) and not STUDENT_HOST_RE.match(page.url):
        raise AuthError("Refusing to type credentials: the sign-in page is not Microsoft's authorize endpoint for Ulife")
    for _ in range(12):
        if STUDENT_HOST_RE.match(page.url):
            return
        if MS_HOST not in page.url:
            page.wait_for_timeout(1000)
            continue
        for sel in MFA_SELECTORS:
            if page.locator(sel).first.is_visible():
                number = page.locator("#idRichContext_DisplaySign").first.inner_text() if sel == "#idRichContext_DisplaySign" else None
                raise MfaRequiredError(
                    "Microsoft requested a second factor"
                    + (f" (approve number {number} in Microsoft Authenticator)" if number else "")
                )
        tile = page.locator(f"div[data-test-id='{username}'], div[data-test-id='{username.lower()}']").first
        if tile.is_visible():
            log.info("Microsoft: selecting existing account tile")
            tile.click()
            _pause(page)
            continue
        # The password page also keeps a visible `loginfmt` input, so the password check must come first.
        pwd_box = page.locator("input[name='passwd']").first
        if pwd_box.is_visible():
            from urllib.parse import urlsplit

            if urlsplit(page.url).hostname != MS_HOST:  # never send the password anywhere else
                raise AuthError("Refusing to type the password outside login.microsoftonline.com")
            log.info("Microsoft: entering password (value never logged)")
            pwd_box.fill(secrets.get("ULIFE_PASSWORD"))
            page.locator("#idSIButton9").click()
            _pause(page, 2)
            err = page.locator("#passwordError, #usernameError").first
            if err.is_visible():
                raise AuthError(f"Microsoft rejected the credentials: {err.inner_text()[:200]}")
            continue
        user_box = page.locator("input[name='loginfmt']").first
        if user_box.is_visible() and user_box.is_editable():
            log.info("Microsoft: entering username")
            user_box.fill(username)
            page.locator("#idSIButton9").click()
            _pause(page)
            continue
        kmsi = page.locator("#KmsiCheckboxField, #KmsiDescription").first
        if kmsi.is_visible():
            log.info("Microsoft: 'stay signed in' -> yes (keeps profile session)")
            page.locator("#idSIButton9").click()
            _pause(page)
            continue
        page.wait_for_timeout(1500)
    if not STUDENT_HOST_RE.match(page.url):
        raise AuthError(f"Login did not reach the student area; stopped at host {page.url.split('/')[2]}")


def login(page: Any, secrets: SecretStore | None = None, timeout_ms: int = 60_000) -> None:
    """Log `page` into Ulife. On success the page is at the student home."""
    secrets = secrets or SecretStore()
    page.goto(config.ULIFE_STUDENT_HOME, wait_until="domcontentloaded", timeout=timeout_ms)
    _pause(page)
    if STUDENT_HOST_RE.match(page.url) and "login" not in page.url.lower():
        log.info("Already authenticated (profile session reused)")
        return
    username = secrets.get("ULIFE_USERNAME")
    interactive = secrets.get_optional("ULIFE_PASSWORD") is None
    if "login.aspx" not in page.url.lower():
        page.goto(config.ULIFE_LOGIN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    page.locator("#ctl00_b_login_txtLogin").fill(username.split("@")[0])
    _pause(page, 0.8)
    page.locator("#ctl00_b_login_ImbLogin").click()
    page.wait_for_url(re.compile(rf"({re.escape(MS_HOST)}|student\.ulife\.com\.br)"), timeout=timeout_ms)
    if interactive:
        if not is_ulife_authorize_url(page.url) and not STUDENT_HOST_RE.match(page.url):
            raise AuthError("Unexpected sign-in page; aborting (expected Microsoft's sign-in for Ulife)")
        log.info("Interactive login: type your password on the official Microsoft page (waiting up to 5 minutes)")
        page.wait_for_url(STUDENT_HOST_RE, timeout=300_000)
    else:
        _handle_microsoft(page, username, secrets, timeout_ms)
    page.wait_for_load_state("domcontentloaded")
    log.info("Login completed")


def open_logged_in_context(playwright: Any, headless: bool = True, secrets: SecretStore | None = None) -> tuple[Any, Any]:
    """Launch the persistent profile, log in and return (context, page)."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        viewport={"width": 1366, "height": 860},
    )
    page = context.pages[0] if context.pages else context.new_page()
    try:
        login(page, secrets)
    except Exception:
        context.close()
        raise
    return context, page
