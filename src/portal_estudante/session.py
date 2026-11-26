"""Authenticated HTTP session for Ulife (pure HTTP after a one-time browser login).

How the portal authenticates:
- Cookie-authenticated APIs (apistudent, apiadmin, apisocial /Feed, www handlers) use the `.ulife.com.br`
  cookies set at login (`frmLogin` forms ticket, `AuthenticationData`, `UserRoles`, CloudFront-*).
- Bearer-authenticated APIs (apisocial /users/*, cloudapp.animaeducacao.com.br microservices) use per-resource
  tokens minted by `POST https://api.ulife.com.br/authentication/token` (form: accessToken, refreshToken, scope).
  The initial pair lives in the portal's localStorage key `MSENTRA_ANIMA_KEY_FRONTEND_ULIFE_COMUNICACAO`.

`SessionData` (cookies + token pair) is persisted in the credential store as `ULIFE_SESSION` (encrypted), so the
app never writes session secrets to disk. When the session expires, a new browser login is performed.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from . import __version__, config
from .errors import EndpointGoneError, RateLimitedError, SessionExpiredError, PortalError
from .secrets import SecretStore

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.ulife.com.br/authentication/token"
SESSION_SECRET_KEY = "ULIFE_SESSION"
LOCALSTORAGE_TOKEN_KEY = "MSENTRA_ANIMA_KEY_FRONTEND_ULIFE_COMUNICACAO"
COOKIE_DOMAINS = ("ulife.com.br", "animaeducacao.com.br")
USER_AGENT = f"portal-estudante/{__version__} (cliente nao oficial; python-httpx)"
# Pacing is fixed in code on purpose (protects the user's account and the portal): every request waits at least
# PACE_MIN seconds plus random jitter; requests are serialized (one at a time). 429/5xx back off exponentially.
PACE_MIN = 1.5
PACE_JITTER = 0.6
BACKOFF_STEPS = (2.0, 4.0, 8.0, 16.0)


class Scopes:
    """OAuth resource scopes accepted by /authentication/token (observed in portal traffic)."""

    LMS = "api://servico_ulifelms/Api.Read"  # apisocial /users/* (notifications, commitments)
    CLASS_SCHEDULE = "api://servico_quadro_aulas/Api.Read"  # cloudapp servico-quadro-aulas
    CLASS_SCHEDULE_LINKS = "api://servico_quadro_aulas_links/Api.Read"  # cloudapp servico-quadro-aulas-links
    DOCUMENTS = "api://servico_app_servicos_academicos_servico_documentacao_pessoa/Api.Read"  # documentacaopessoa


@dataclass
class SessionData:
    cookies: list[dict[str, Any]]
    access_token: str
    refresh_token: str
    user: dict[str, Any] = field(default_factory=dict)  # non-secret ids discovered at login
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> SessionData:
        return cls(**json.loads(raw))

    def __repr__(self) -> str:  # never print secrets
        return f"SessionData(cookies={len(self.cookies)}, created_at={self.created_at:.0f}, user={self.user})"


def capture_session_from_browser(headless: bool = True, secrets: SecretStore | None = None) -> SessionData:
    """Sign in and extract the session. Without a saved password the browser is shown (interactive login)."""
    secrets = secrets or SecretStore()
    if secrets.get_optional("ULIFE_PASSWORD") is None:
        headless = False  # the student types the password on the official page
    """Log in with Playwright and extract cookies + the token pair (values never logged)."""
    from playwright.sync_api import sync_playwright

    from .auth.browser_login import open_logged_in_context

    with sync_playwright() as pw:
        context, page = open_logged_in_context(pw, headless=headless, secrets=secrets)
        try:
            # The token pair is written by the portal's microfrontends shortly after the home loads.
            raw = None
            for _ in range(20):
                raw = page.evaluate(f"() => localStorage.getItem('{LOCALSTORAGE_TOKEN_KEY}')")
                if raw:
                    break
                page.wait_for_timeout(1000)
            if not raw:
                raise SessionExpiredError("Logged in, but the portal did not publish its token pair in localStorage")
            tokens = json.loads(raw)
            user = page.evaluate("() => (window.ILangSettings ? {UserID: ILangSettings.UserID, UserOrganizationID: ILangSettings.UserOrganizationID, OrganizationID: ILangSettings.OrganizationID, UserName: ILangSettings.UserName} : {})")
            cookies = [c for c in context.cookies() if any(c["domain"].endswith(d) for d in COOKIE_DOMAINS)]
            return SessionData(cookies=cookies, access_token=tokens["accessToken"], refresh_token=tokens["refreshToken"], user=user or {})
        finally:
            context.close()


class PortalHttp:
    """httpx wrapper with human pacing, specific errors, bearer minting and automatic re-login."""

    def __init__(self, secrets: SecretStore | None = None, session: SessionData | None = None,
                 headless_login: bool = True, request_budget: int | None = None) -> None:
        self.secrets = secrets or SecretStore()
        self.headless_login = headless_login
        self._last = 0.0
        self._lock = threading.Lock()
        self._bearers: dict[str, tuple[str, float]] = {}
        self._session = session
        self._client: httpx.Client | None = None
        self.request_budget = request_budget
        self.request_count = 0

    # -- session lifecycle -----------------------------------------------------------------------
    @property
    def session(self) -> SessionData:
        if self._session is None:
            raw = self.secrets.get_optional(SESSION_SECRET_KEY)
            self._session = SessionData.from_json(raw) if raw else self._relogin()
        return self._session

    def relogin(self) -> SessionData:
        return self._relogin()

    def _relogin(self) -> SessionData:
        log.info("Performing browser login to obtain a fresh Ulife session")
        data = capture_session_from_browser(headless=self.headless_login, secrets=self.secrets)
        self.secrets.put(SESSION_SECRET_KEY, data.to_json(), "Ulife session (cookies + token pair) created by portal_estudante; rotated automatically")
        self._session = data
        self._bearers.clear()
        if self._client:
            self._client.close()
            self._client = None
        return data

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            jar = httpx.Cookies()
            for c in self.session.cookies:
                jar.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
            self._client = httpx.Client(
                cookies=jar, follow_redirects=False, timeout=30.0,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*",
                         "Origin": "https://student.ulife.com.br", "Referer": "https://student.ulife.com.br/"},
            )
        return self._client

    def _pace(self) -> None:
        with self._lock:
            self.request_count += 1
            if self.request_budget is not None and self.request_count > self.request_budget:
                raise RateLimitedError(f"Local safety budget of {self.request_budget} requests per process exceeded "
                                       "(protects the account from runaway loops)")
            wait = PACE_MIN + random.uniform(0, PACE_JITTER) - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    # -- bearer tokens ------------------------------------------------------------------------------
    def bearer(self, scope: str) -> str:
        cached = self._bearers.get(scope)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        self._pace()
        s = self.session
        form = {"accessToken": s.access_token, "refreshToken": s.refresh_token, "scope": scope}
        resp = self.client.post(TOKEN_URL, data=form,
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
        if resp.status_code in (400, 401, 403):
            raise SessionExpiredError(f"Token refresh rejected (HTTP {resp.status_code})")
        body = resp.json() if resp.content else {}
        result = (body or {}).get("Result") or {}
        if not body.get("Success") or not result.get("AccessToken"):
            raise SessionExpiredError(f"Token refresh failed: {body.get('ErrorMessage') or 'empty response'}")
        if result.get("RefreshToken") and result["RefreshToken"] != s.refresh_token:
            s.refresh_token = result["RefreshToken"]
            s.access_token = result["AccessToken"] if scope == Scopes.LMS else s.access_token
        self._bearers[scope] = (result["AccessToken"], time.time() + int(result.get("ExpiresIn") or 300))
        return result["AccessToken"]

    def persist(self) -> None:
        """Save the (possibly rotated) session back to Secrets Manager."""
        if self._session is not None:
            self.secrets.put(SESSION_SECRET_KEY, self._session.to_json(), "Ulife session (cookies + token pair) created by portal_estudante; rotated automatically")

    # -- requests ------------------------------------------------------------------------------------
    def request(self, method: str, url: str, *, scope: str | None = None, retry_login: bool = True, **kw: Any) -> httpx.Response:
        """Paced request with session renewal and exponential backoff on 429/5xx (then a specific error)."""
        for attempt, delay in enumerate((*BACKOFF_STEPS, None)):
            resp = self._request_once(method, url, scope=scope, retry_login=retry_login, **kw)
            if resp.status_code != 429 and resp.status_code < 500:
                return resp
            if delay is None:
                break
            log.warning("HTTP %s at %s; backing off %.0fs (attempt %d)", resp.status_code, url.split("?")[0], delay, attempt + 1)
            time.sleep(delay + random.uniform(0, 1))
        if resp.status_code == 429:
            raise RateLimitedError(f"Portal throttled {url.split('?')[0]} after {len(BACKOFF_STEPS)} retries")
        raise PortalError(f"Portal server error HTTP {resp.status_code} at {url.split('?')[0]} after {len(BACKOFF_STEPS)} retries")

    def _request_once(self, method: str, url: str, *, scope: str | None = None, retry_login: bool = True, **kw: Any) -> httpx.Response:
        self._pace()
        log.info("%s %s", method, url.split("?")[0])
        headers = dict(kw.pop("headers", {}) or {})
        try:
            if scope:
                headers["Authorization"] = f"Bearer {self.bearer(scope)}"
            resp = self.client.request(method, url, headers=headers, **kw)
            self._raise_for_session(resp)
        except SessionExpiredError:
            if not retry_login:
                raise
            log.info("Session expired; logging in again")
            self._relogin()
            return self._request_once(method, url, scope=scope, retry_login=False, headers=headers, **kw)
        if resp.status_code in (404, 410):
            raise EndpointGoneError(f"{method} {url.split('?')[0]}", [], hint=f"HTTP {resp.status_code}: endpoint moved or removed; the library needs an update")
        return resp

    @staticmethod
    def _raise_for_session(resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise SessionExpiredError(f"HTTP {resp.status_code} at {resp.request.url.host}")
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "").lower()
            if "login" in loc or "microsoftonline" in loc or "error" in loc:
                raise SessionExpiredError(f"Redirected to {loc.split('?')[0][:80]}")

    def get_json(self, url: str, params: dict[str, Any] | None = None, *, scope: str | None = None) -> Any:
        resp = self.request("GET", url, params=params, scope=scope)
        if resp.status_code in (301, 302, 303, 307, 308):
            raise EndpointGoneError(f"GET {url.split('?')[0]}", [], hint=f"redirect to {resp.headers.get('location', '')[:120]}")
        try:
            return resp.json()
        except ValueError as exc:
            # Some legacy handlers (.ashx) answer JSON with text/html content-type.
            try:
                return json.loads(resp.text)
            except ValueError:
                raise PortalError(f"Expected JSON from {url.split('?')[0]} but got {resp.headers.get('content-type')}") from exc

    def close(self) -> None:
        if self._client:
            self._client.close()
