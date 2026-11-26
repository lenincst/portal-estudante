"""Offline tests (no network, no secrets): contracts, drift baseline, errors, write guard, SignalR parsing,
notification de-duplication and HTTP error mapping."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from portal_estudante import config, contracts
from portal_estudante import client as client_mod
from portal_estudante.client import PortalClient
from portal_estudante.contracts import Contract, Field, Shape
from portal_estudante.errors import EndpointGoneError, RateLimitedError, SchemaChangedError, SessionExpiredError, PortalError, WriteBlockedError
from portal_estudante.notifications import NotificationWatcher, SeenStore, parse_signalr_frame
from portal_estudante.session import SessionData, PortalHttp


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(contracts, "_baseline", None)
    # Pacing is fixed in code; tests only skip the actual waiting.
    import portal_estudante.session as session_mod
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: None)
    return tmp_path


class FakeSecrets:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get_optional(self, key: str) -> str | None:
        return self.store.get(key)

    def get(self, key: str) -> str:
        return self.store[key]

    def put(self, key: str, value: str, note: str = "") -> None:
        self.store[key] = value


def make_http(**kw: Any) -> PortalHttp:
    session = SessionData(cookies=[{"name": "frmLogin", "value": "x", "domain": ".ulife.com.br", "path": "/"}],
                          access_token="a", refresh_token="r", user={"UserID": 1, "UserOrganizationID": 2, "OrganizationID": 3})
    return PortalHttp(secrets=FakeSecrets(), session=session, **kw)  # type: ignore[arg-type]


CONTRACT = Contract("GET test", Shape({"Result": Field("list", item=Shape({"Name": Field("str"), "Id": Field("int")})), "Success": Field("bool")}),
                    hint="client.test")


# --- contracts ----------------------------------------------------------------------------------------
def test_contract_ok() -> None:
    data = {"Result": [{"Name": "a", "Id": 1}], "Success": True}
    assert contracts.check(CONTRACT, data) is data


def test_contract_missing_field_is_breaking() -> None:
    with pytest.raises(SchemaChangedError) as e:
        contracts.check(CONTRACT, {"Result": [{"Id": 1}], "Success": True})
    assert "$.Result[0].Name" in str(e.value) and "client.test" in str(e.value)


def test_contract_type_change_is_breaking() -> None:
    with pytest.raises(SchemaChangedError) as e:
        contracts.check(CONTRACT, {"Result": [{"Name": "a", "Id": "1"}], "Success": True})
    assert e.value.issues[0].observed == "str"


def test_bool_is_not_int() -> None:
    with pytest.raises(SchemaChangedError):
        contracts.check(CONTRACT, {"Result": [{"Name": "a", "Id": True}], "Success": True})


def test_baseline_first_run_silent_then_reports_new_and_removed(isolated_data_dir: Path) -> None:
    contracts.check(CONTRACT, {"Result": [{"Name": "a", "Id": 1, "Extra": 1}], "Success": True})
    assert not (isolated_data_dir / "contracts" / "drift.jsonl").exists()
    contracts.check(CONTRACT, {"Result": [{"Name": "a", "Id": 1, "Brand": "x"}], "Success": True})
    lines = [json.loads(x)["message"] for x in (isolated_data_dir / "contracts" / "drift.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "new field: $.Result[].Brand" in lines
    assert "field no longer present: $.Result[].Extra" in lines


def test_empty_list_does_not_report_removed_children(isolated_data_dir: Path) -> None:
    contracts.check(CONTRACT, {"Result": [{"Name": "a", "Id": 1}], "Success": True})
    contracts.check(CONTRACT, {"Result": [], "Success": True})
    assert not (isolated_data_dir / "contracts" / "drift.jsonl").exists()


def test_envelope_failure() -> None:
    with pytest.raises(PortalError, match="boom"):
        contracts.check_envelope("GET x", {"Success": False, "ErrorMessage": "boom"})


# --- HTTP error mapping ---------------------------------------------------------------------------------
@respx.mock
def test_404_maps_to_endpoint_gone() -> None:
    respx.get("https://apistudent.ulife.com.br/x").mock(return_value=httpx.Response(404))
    with pytest.raises(EndpointGoneError):
        make_http().get_json("https://apistudent.ulife.com.br/x")


@respx.mock
def test_429_maps_to_rate_limited() -> None:
    respx.get("https://apistudent.ulife.com.br/x").mock(return_value=httpx.Response(429))  # persists through all retries
    with pytest.raises(RateLimitedError):
        make_http().get_json("https://apistudent.ulife.com.br/x")


@respx.mock
def test_redirect_to_login_is_session_expired_without_retry() -> None:
    respx.get("https://apistudent.ulife.com.br/x").mock(return_value=httpx.Response(302, headers={"location": "https://www.ulife.com.br/login.aspx"}))
    with pytest.raises(SessionExpiredError):
        make_http().request("GET", "https://apistudent.ulife.com.br/x", retry_login=False)


def test_request_budget_blocks_runaway_loops() -> None:
    http = make_http(request_budget=2)
    http._pace()
    http._pace()
    with pytest.raises(RateLimitedError, match="budget"):
        http._pace()


@respx.mock
def test_bearer_minting_and_cache() -> None:
    route = respx.post("https://api.ulife.com.br/authentication/token").mock(
        return_value=httpx.Response(200, json={"Success": True, "Result": {"AccessToken": "T", "RefreshToken": "R2", "ExpiresIn": 3600}}))
    http = make_http()
    assert http.bearer("scope") == "T"
    assert http.bearer("scope") == "T"
    assert route.call_count == 1
    assert http.session.refresh_token == "R2"


def test_session_repr_hides_secrets() -> None:
    s = SessionData(cookies=[{"name": "a", "value": "SECRET"}], access_token="SECRET", refresh_token="SECRET")
    assert "SECRET" not in repr(s)


# --- client ---------------------------------------------------------------------------------------------
def test_writes_blocked_by_default() -> None:
    c = PortalClient(http=make_http())
    with pytest.raises(WriteBlockedError) as e:
        c.send_teacher_message({"ClassDisciplineID": 9}, "Bom dia professor, tenho uma dúvida", 1, confirm=True)
    assert e.value.details["ClassDisciplineId"] == 9
    with pytest.raises(WriteBlockedError):
        PortalClient(http=make_http(), allow_writes=True).mark_notifications_read(["1"])  # confirm missing


def test_support_ticket_never_sends_cookies() -> None:
    c = PortalClient(http=make_http())
    with pytest.raises(WriteBlockedError) as e:
        c.create_support_ticket("t", "<p>d</p>", 5, confirm=True)
    assert e.value.details["cookies"] == ""


def test_message_validation_mirrors_portal_rules() -> None:
    with pytest.raises(ValueError):
        PortalClient(http=make_http(), allow_writes=True).send_teacher_message({"ClassDisciplineID": 1}, "oi", 1, confirm=True)


def test_crypt_extracts_cd_parameter() -> None:
    d = {"ContentUrl": "https://student.ulife.com.br/Lesson/Index?cd=ab%2bc%3d%3d"}
    assert PortalClient._crypt(d) == "ab+c=="


@respx.mock
def test_members_pagination_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        {"Success": True, "Result": [{"UserID": 1, "Name": "a", "UserIsTeacher": True, "UserIsOnlyStudent": False, "RoleTypeNames": []}],
         "TotalCount": 2, "HasMorePages": True, "PageIndex": 1, "NextPageIndex": 2},
        {"Success": True, "Result": [{"UserID": 2, "Name": "b", "UserIsTeacher": False, "UserIsOnlyStudent": True, "RoleTypeNames": []}],
         "TotalCount": 2, "HasMorePages": False, "PageIndex": 2, "NextPageIndex": 3},
    ]
    respx.get(url__startswith=f"{client_mod.APIADMIN}/ClassDisciplineMembers/getList").mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    d = {"ClassDisciplineID": 1, "StudentClassDisciplineID": 2, "OrganizationCourseID": 3, "DisciplineName": "x"}
    roster = PortalClient(http=make_http(), classmates=True).members(d)
    assert [m["name"] for m in roster] == ["a", "b"] and roster[0]["role"] == "professor"
    assert set(roster[0]) == {"name", "role", "user_organization_id"}  # minimal fields only


def test_members_disabled_by_default() -> None:
    from portal_estudante.errors import ClassmatesDisabledError
    with pytest.raises(ClassmatesDisabledError):
        PortalClient(http=make_http()).members({"ClassDisciplineID": 1})


# --- notifications --------------------------------------------------------------------------------------
def test_parse_signalr_frame() -> None:
    frame = '{"C":"d-1","M":[{"H":"iLangHub","M":"notificationCountChanged","A":[38]}]}'
    assert parse_signalr_frame(frame) == [("iLangHub", "notificationCountChanged", [38])]
    assert parse_signalr_frame("{}") == []
    assert parse_signalr_frame("not json") == []


class FakeClient:
    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self.batches = batches
        self.http = make_http()

    def notifications_summary(self) -> dict[str, Any]:
        items = self.batches.pop(0)
        return {"Notifications": items, "UnReadCount": len(items)}


def test_watcher_emits_only_new_notifications() -> None:
    got: list[str] = []
    fake = FakeClient([[{"Id": "1"}], [{"Id": "2"}, {"Id": "1"}], [{"Id": "2"}]])
    w = NotificationWatcher(fake, lambda n: got.append(n["Id"]), use_push=False, poll_interval=1)  # type: ignore[arg-type]
    assert w.poll_interval >= 300  # fixed floor
    w.check_now()  # first run: existing ones are marked seen, not emitted
    w.check_now()
    w.check_now()
    assert got == ["2"]
    assert SeenStore().ids == {"1", "2"}


# --- messages (synthetic HTML mirroring the real markup; no real data) ------------------------------
from portal_estudante.client import jquery_param  # noqa: E402
from portal_estudante.messages import MessageSummary, Messages, extract_body, parse_box, parse_thread  # noqa: E402

ROW = ('<div class="msg-list" id="x"><div class="msg-list__item {cls}" id="divMessage{mid}" role="listitem" aria-label="Mensagem de Fulano: Assunto X">'
       '<input type="checkbox" IsRead="{flag}" MessageId="{mid}" UserId="1" /><a href="MessageDetail.aspx?MsgId=AB%2b&amp;BoxType=CD">Assunto X</a>'
       ' Há 1 ano (3)</div></form>')


def test_parse_box_unread_and_read_states() -> None:
    unread = parse_box(ROW.format(cls="inboxmessageactive", flag="1", mid=10), "inbox")[0]
    read = parse_box(ROW.format(cls="inboxmessage", flag="2", mid=11), "inbox")[0]
    assert (unread.is_read, read.is_read) == (False, True)
    assert unread.counterpart == "Fulano" and unread.subject == "Assunto X" and unread.thread_size == 3
    assert unread.detail_url == "https://www.ulife.com.br/Community/MessageDetail.aspx?MsgId=AB%2b&BoxType=CD"


def test_opening_unread_inbox_thread_is_blocked() -> None:
    s = MessageSummary(1, "inbox", "Fulano", "X", False, "1", "", 1, "https://www.ulife.com.br/Community/MessageDetail.aspx?MsgId=1")
    with pytest.raises(WriteBlockedError):
        Messages(PortalClient(http=make_http())).thread(s)


def test_parse_box_structure_change() -> None:
    from portal_estudante.errors import PageStructureChangedError
    with pytest.raises(PageStructureChangedError):
        parse_box("<html>nova página</html>", "inbox")


def test_parse_thread_and_body() -> None:
    page = ("<div class=\"messagecollapse\"> <h1> <a id=\"u\">Remetente</a> <span title='1 de janeiro de 2026 às 10:00'>x</span></h1>"
            "<a id=\"ctl00_b_rptMessages_ctl00_collapseMessage_MessageTargetUser1_rptTargetUsers_ctl00_uphTarget\">Destino</a>"
            "<label>Assunto:</label> <span>Oi</span><iframe src=\"https://staticcdns3.ulife.com.br/PAT/TinyMCEPages/CommunityMessage/1_a.html\"")
    m = parse_thread(page)[0]
    assert (m.sender, m.date, m.to, m.subject) == ("Remetente", "1 de janeiro de 2026 às 10:00", ["Destino"], "Oi")
    assert extract_body("<html><body><p>oi</p><script>x()</script></body></html>") == "<p>oi</p>"


def test_jquery_param_matches_portal_encoding() -> None:
    assert jquery_param({"Recipients": [{"EntityID": 7, "Type": 1}], "S": "a", "N": None, "B": True}) == [
        ("Recipients[0][EntityID]", "7"), ("Recipients[0][Type]", "1"), ("S", "a"), ("N", ""), ("B", "true")]


def test_direct_message_blocked_without_confirmation() -> None:
    with pytest.raises(WriteBlockedError) as e:
        PortalClient(http=make_http(), allow_writes=True).send_direct_message(7, "teste", "<p>teste</p>")
    assert e.value.details["Recipients"] == [{"EntityID": 7, "Type": 1}]


def test_direct_message_single_recipient_only() -> None:
    with pytest.raises(ValueError):
        PortalClient(http=make_http(), allow_writes=True).send_direct_message([7, 8], "a", "<p>b</p>", confirm=True)  # type: ignore[arg-type]


def test_dry_run_returns_preview_and_sends_nothing() -> None:
    out = PortalClient(http=make_http(), dry_run=True).send_direct_message(7, "Assunto", "<p>oi</p>", confirm=True)
    assert out["dry_run"] is True and out["action"] == "send_direct_message" and out["would_send"]["UserRecipientCount"] == 1


@respx.mock
def test_backoff_retries_429_then_succeeds() -> None:
    route = respx.get("https://apistudent.ulife.com.br/y").mock(side_effect=[httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"ok": 1})])
    assert make_http().get_json("https://apistudent.ulife.com.br/y") == {"ok": 1}
    assert route.call_count == 3


def test_pacing_is_fixed_in_code() -> None:
    import portal_estudante.session as session_mod
    assert session_mod.PACE_MIN >= 1.5 and "min_interval" not in PortalHttp.__init__.__code__.co_varnames


def test_user_agent_is_neutral() -> None:
    import portal_estudante.session as session_mod
    assert session_mod.USER_AGENT.startswith("portal-estudante/") and "Mozilla" not in session_mod.USER_AGENT


def test_check_includes_roster_only_with_opt_in() -> None:
    names_default = [n for n, _ in PortalClient(http=make_http()).read_checks()]
    names_opt_in = [n for n, _ in PortalClient(http=make_http(), classmates=True).read_checks()]
    assert "members" not in names_default and "members" in names_opt_in
