"""Offline tests for loop-detecting pagination and the academic-services write validations (no network)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from portal_estudante import config, contracts
from portal_estudante.client import PortalClient
from portal_estudante.errors import WriteBlockedError
from portal_estudante.services import Services, _valid_cpf
from test_offline import make_http  # type: ignore[import-not-found]


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(contracts, "_baseline", None)
    import portal_estudante.session as session_mod
    monkeypatch.setattr(session_mod.time, "sleep", lambda s: None)  # pacing stays fixed; tests skip the wait


def _page(items: list[int], more: bool, nxt: int) -> httpx.Response:
    return httpx.Response(200, json={"Success": True, "Result": [{"Id": i} for i in items], "HasMorePages": more, "NextPageIndex": nxt})


@respx.mock
def test_paginate_stops_when_server_repeats_a_page() -> None:
    respx.get(url__startswith="https://x.test/list").mock(side_effect=[_page([1, 2], True, 2), _page([1, 2], True, 3)])
    assert [x["Id"] for x in PortalClient(http=make_http())._paginate("https://x.test/list", {})] == [1, 2]


@respx.mock
def test_paginate_stops_on_empty_page_without_fixed_cap() -> None:
    pages = [_page([i], True, i + 1) for i in range(1, 31)] + [_page([], True, 32)]
    respx.get(url__startswith="https://x.test/list").mock(side_effect=pages)
    assert len(PortalClient(http=make_http())._paginate("https://x.test/list", {})) == 30  # no 20-page cap anymore


@respx.mock
def test_paginate_stops_when_next_index_does_not_advance() -> None:
    respx.get(url__startswith="https://x.test/list").mock(side_effect=[_page([1], True, 1), _page([2], True, 1), _page([2], True, 1)])
    assert [x["Id"] for x in PortalClient(http=make_http())._paginate("https://x.test/list", {})] == [1, 2]


def test_cpf_checksum() -> None:
    assert _valid_cpf("52998224725") and not _valid_cpf("52998224724") and not _valid_cpf("11111111111")


def _services(allow: bool = True) -> Services:
    s = PortalClient(http=make_http(), allow_writes=allow).services
    s._identity = {"ra": "123", "cpf": "52998224725", "name": "Aluno Teste", "email": "123@ulife.com.br"}
    return s


def test_create_request_validates_files_and_blocks(tmp_path: Path) -> None:
    ok = tmp_path / "atestado.pdf"
    ok.write_bytes(b"%PDF-1.4 test")
    bad = tmp_path / "foto.bmp"
    bad.write_bytes(b"x")
    item = {"id": 1, "salesforceId": "sf"}
    with pytest.raises(ValueError, match="unsupported"):
        _services().create_request(item, item, item, "texto", [bad], confirm=True)
    with pytest.raises(WriteBlockedError) as e:
        _services().create_request(item, item, item, "texto", [ok])  # confirm missing
    assert e.value.details["arquivos"] == ["atestado.pdf"] and e.value.details["cpf"] == "<from token>"


def test_request_total_size_limit(tmp_path: Path) -> None:
    big = tmp_path / "grande.pdf"
    big.write_bytes(b"0" * (10 * 1024 * 1024))
    item = {"id": 1, "salesforceId": "sf"}
    with pytest.raises(ValueError, match="limit"):
        _services().create_request(item, item, item, "texto", [big], confirm=True)


def test_enrollment_document_rules(tmp_path: Path) -> None:
    doc = tmp_path / "rg.png"
    doc.write_bytes(b"png")
    with pytest.raises(WriteBlockedError) as e:
        _services(allow=False).upload_enrollment_document(7, doc, confirm=True)
    assert e.value.details == {"codigoTipoDocumentoSiaf": 7, "documento": "rg.png"}
    with pytest.raises(ValueError):
        _services().upload_enrollment_document(7, tmp_path / "x.docx", confirm=True)


def _png(path: Path, w: int, h: int) -> Path:
    import struct
    import zlib
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


def test_profile_photo_rules(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        _services().upload_profile_photo(tmp_path / "a.webp")
    with pytest.raises(ValueError, match="more than 300x300"):
        _services().upload_profile_photo(_png(tmp_path / "small.png", 300, 300), confirm=True)
    with pytest.raises(WriteBlockedError):
        _services(allow=False).upload_profile_photo(_png(tmp_path / "ok.png", 400, 400), confirm=True)


def test_profile_photo_dry_run_uses_largest_centered_square(tmp_path: Path) -> None:
    s = UlifeClient_dry()
    out = s.upload_profile_photo(_png(tmp_path / "wide.png", 800, 400), confirm=True)
    assert out["dry_run"] and out["would_send"]["crop"] == {"left": 200, "top": 0, "width": 400, "height": 400}


def UlifeClient_dry() -> Services:  # noqa: N802
    s = PortalClient(http=make_http(), dry_run=True).services
    s._identity = {"ra": "123", "cpf": "52998224725", "name": "Aluno Teste", "email": "123@ulife.com.br"}
    return s


def test_enroll_function_is_not_available() -> None:
    assert not hasattr(Services, "enroll_discipline")


def test_update_contact_requires_value_and_blocks() -> None:
    with pytest.raises(ValueError):
        _services().update_contact()
    with pytest.raises(WriteBlockedError):
        _services().update_contact(phone="48999999999")


# --- credentials for other students ----------------------------------------------------------------------
import keyring  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402

from portal_estudante.auth.browser_login import is_ulife_authorize_url  # noqa: E402
from portal_estudante.errors import SecretsError  # noqa: E402


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.data.pop((service, username), None)


@pytest.fixture
def memory_keyring():  # noqa: ANN201
    previous = keyring.get_keyring()
    kr = MemoryKeyring()
    keyring.set_keyring(kr)
    yield kr
    keyring.set_keyring(previous)


def test_password_only_for_ulife_oauth() -> None:
    good = ("https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?client_id=1d6578ce-ed91-4a70-a452-70ae7a7ac431"
            "&redirect_uri=https%3A%2F%2Fapi.ulife.com.br%2Fauthentication%2FsignIn&scope=x")
    assert is_ulife_authorize_url(good)
    assert not is_ulife_authorize_url(good.replace("login.microsoftonline.com", "login.microsoftonline.com.evil.test"))
    assert not is_ulife_authorize_url(good.replace("1d6578ce", "00000000"))
    assert not is_ulife_authorize_url(good.replace("api.ulife.com.br", "evil.test"))


def test_local_store_keychain_and_encrypted_file(memory_keyring, tmp_path: Path) -> None:  # noqa: ANN001
    from portal_estudante.secrets import LocalStore, SecretStore, save_config

    save_config({"backend": "local"})
    store = SecretStore()
    assert isinstance(store, LocalStore)
    store.put("ULIFE_PASSWORD", "segredo")
    big = "x" * 5000
    store.put("ULIFE_SESSION", big)
    assert store.get("ULIFE_SESSION") == big and store.get("ULIFE_PASSWORD") == "segredo"
    enc = (config.DATA_DIR / "vault" / "ULIFE_SESSION.enc").read_bytes()
    assert b"xxxx" not in enc  # encrypted at rest
    assert ("portal-estudante", "ULIFE_SESSION") not in memory_keyring.data


def test_unconfigured_store_explains_setup(memory_keyring) -> None:  # noqa: ANN001
    from portal_estudante.secrets import SecretStore

    with pytest.raises(SecretsError, match="setup"):
        SecretStore()
