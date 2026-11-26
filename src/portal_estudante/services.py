"""Academic services beyond the course pages (from the portal's side menu and its microfrontends).

Identity for these services is derived from the user's own Entra ID token claims (never hard-coded):
RA = `sAMAccountName` (= `upn` local part) and CPF = `extatr3` (validated by checksum). Values are never logged.

Reads are validated (contracts) where the shape was observed live. Writes are blocked unless
`PortalClient(allow_writes=True)` AND `confirm=True`; each validates the portal's own rules first
(formats, sizes, required fields) so a WriteBlockedError preview shows exactly what would be sent.
None of the writes here has been executed against the real account.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import PortalError

if TYPE_CHECKING:
    from .client import PortalClient

CLOUD = "https://cloudapp.animaeducacao.com.br"
APISTUDENT = "https://apistudent.ulife.com.br"
ASYNC = f"{CLOUD}/atendimento-assincrono/services/api/v1"
PERSONAL = f"{CLOUD}/atendimento/services/personalizado/api/v1"
PROGRESS = "https://prd.ulife.com.br/progresso-estudante/api/v1"
WWW = "https://www.ulife.com.br"


class Scope:
    ASYNC_SUPPORT = "api://servico_atendimento_assincrono/Api.Read"
    PERSONAL_SUPPORT = "api://servico_atendimento_personalizado/Api.Read"
    SIAF = "api://servico_academico_siaf/.default"
    ACADEMIC_DOCS = "api://servico_documento_academico/Api.Read"
    PROGRESS = "api://servico_progresso_estudante_api/Api.Read"
    DOCUMENTS = "api://servico_app_servicos_academicos_servico_documentacao_pessoa/Api.Read"
    ACADEMIC_RECORD = "api://servico_historico_escolar_digital/Api.Read"
    LIBRARIES = "api://servico_gestao_bibliotecas/Api.Read"
    SCHOLARSHIPS = "api://fdd7df71-3fc2-401f-9465-56993c264ef4/Api.Read"  # observed in portal traffic


# Portal rules (as enforced by the portal's own pages)
REQUEST_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
REQUEST_MAX_TOTAL_BYTES = 10 * 1024 * 1024  # "A soma dos arquivos não pode ultrapassar 10mb"
REQUEST_MAX_FILES = 24  # "Não é possível anexar mais de 24 arquivos"
ENROLL_DOC_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
ENROLL_DOC_MAX_BYTES = 10 * 1024 * 1024  # fileSizeLimit 10485760
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".gif", ".png"}
PHOTO_MAX_BYTES = 4 * 1024 * 1024  # "Tamanho máximo do arquivo 4 Mb"
PHOTO_MIN_SIDE = 300  # "a dimensão da imagem deve ser maior que 300x300 pixels"


def image_size(path: Path) -> tuple[int, int]:
    """Width/height of a PNG, GIF or JPEG file from its header (no imaging library needed)."""
    data = path.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
            i += 2 + length
    raise ValueError("could not read image dimensions (expected PNG, GIF or JPEG)")


def _photo_flow(client: Any, image: Path, crop: dict[str, int]) -> dict[str, Any]:
    """Drive FullProfile.aspx like a person would. Only POSTs to that page are allowed; anything else is aborted."""
    from urllib.parse import urlsplit

    from playwright.sync_api import sync_playwright

    from .auth.browser_login import open_logged_in_context

    url = "https://www.ulife.com.br/Community/FullProfile.aspx"

    def guard(route: Any) -> Any:
        req = route.request
        u = urlsplit(req.url)
        if req.method in ("GET", "HEAD", "OPTIONS") or (u.hostname == "www.ulife.com.br" and u.path.lower() == "/community/fullprofile.aspx"):
            return route.continue_()
        return route.abort()

    with sync_playwright() as pw:
        ctx, page = open_logged_in_context(pw, headless=client.http.headless_login, secrets=client.http.secrets)
        try:
            ctx.route("**/*", guard)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            current = page.locator("[id$='UploadPicture1_imgCurrent']").first.get_attribute("src") or ""
            page.evaluate("() => { const h = document.getElementById('helphero-dom'); if (h) h.remove(); ShowUpload(); }")
            page.set_input_files("[id$='UploadPicture1_UploadPicture1_fuPicture']", str(image))
            page.check("[id$='UploadPicture1_UploadPicture1_chkTermsOfUseDeclaration']")
            page.locator("[id$='UploadPicture1_UploadPicture1_btnUpload']").first.evaluate("e => e.click()")
            page.wait_for_timeout(6000)
            error = page.evaluate("() => (document.querySelector('[id$=ltrErrorMessage]') || {}).innerText || ''").strip()
            crop_field = page.locator("[id$='imcImage_CropResults']").first
            if error or not crop_field.count() or not crop_field.get_attribute("value"):
                raise PortalError(f"Photo upload rejected by the portal: {error or 'crop step did not open'}")
            crop_field.evaluate("(e, v) => { e.value = v; }", json.dumps(crop, separators=(",", ":")))
            page.locator("[id$='UploadPicture1_dcc_btnSave']").first.evaluate("e => e.click()")
            page.wait_for_timeout(6000)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            after = page.locator("[id$='UploadPicture1_imgCurrent']").first.get_attribute("src") or ""
        finally:
            ctx.close()
    if after == current:
        raise PortalError("Photo save did not change the profile picture version; nothing confirmed")
    return {"saved": True, "version": after.split("version=")[-1]}


def _claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _valid_cpf(cpf: str) -> bool:
    if not re.fullmatch(r"\d{11}", cpf) or len(set(cpf)) == 1:
        return False
    d = [int(x) for x in cpf]
    return all(d[k] == ((sum(d[i] * (k + 1 - i) for i in range(k)) * 10) % 11) % 10 for k in (9, 10))


class Services:
    def __init__(self, client: PortalClient) -> None:
        self.client = client
        self._identity: dict[str, str] | None = None

    # -- identity ---------------------------------------------------------------------------------------
    def identity(self) -> dict[str, str]:
        """RA, CPF, name and e-mail from the user's own token claims (raises if the claims changed)."""
        if self._identity is None:
            from .session import Scopes

            c = _claims(self.client.http.bearer(Scopes.DOCUMENTS))
            ra, cpf = str(c.get("sAMAccountName", "")), str(c.get("extatr3", ""))
            if not ra.isdigit() or not _valid_cpf(cpf):
                from .contracts import ContractIssue
                from .errors import SchemaChangedError

                raise SchemaChangedError("Entra ID token claims", [ContractIssue("sAMAccountName/extatr3", "RA digits + valid CPF", "changed")],
                                         hint="services.Services.identity")
            self._identity = {"ra": ra, "cpf": cpf, "name": str(c.get("name", "")), "email": str(c.get("upn", ""))}
        return dict(self._identity)

    def _get(self, url: str, params: Any = None, scope: str | None = "cookie") -> Any:
        if scope == "cookie":
            return self.client.http.get_json(url, params)
        return self.client.http.get_json(url, params, scope=scope)

    # -- reads (student pages) ----------------------------------------------------------------------------
    def evaluative_activities(self, tab: int = 0) -> list[dict[str, Any]]:
        """'Atividades Avaliativas' (tab 0 = pending). Follows pagination with loop detection."""
        return self.client._paginate(f"{APISTUDENT}/ActivityList/GetActivities", {"ActivityTabs": tab, "FirstLoad": "true", "PageSize": 20, "SearchCriteria": ""})

    def certificates(self) -> list[dict[str, Any]]:
        """'Microcertificados'."""
        return self.client._paginate(f"{APISTUDENT}/Certificates/GetCertificates", {"FirstLoad": "true", "PageSize": 20, "SearchCriteria": ""})

    def academic_record(self) -> Any:
        """'Documentação Acadêmica' — digital academic record (histórico) of the current enrollment."""
        i = self.identity()
        return self._get(f"{CLOUD}/historicoescolardigital/api/v1/conferencia/alunos/{i['cpf']}/matriculas/{i['ra']}", scope=Scope.ACADEMIC_RECORD)

    def enrollment_declaration_eligibility(self) -> Any:
        """'Declaração de Regularidade Acadêmica' — enrollments eligible for the declaration."""
        i = self.identity()
        return self._get(f"{CLOUD}/servico-academico-siaf/api/v1/declaracao-academica/alunos/matriculas-elegiveis/{i['ra']},{i['cpf']}",
                         {"cpf": i["cpf"]}, scope=Scope.SIAF)

    def hybridity(self) -> Any:
        """'Percentual de Hibridez' — share of online vs in-person workload."""
        return self._get(f"{CLOUD}/servico-academico-siaf/api/v1/calculo-hibridez/alunos/{self.identity()['ra']}", scope=Scope.SIAF)

    def siaf_courses(self) -> Any:
        i = self.identity()
        return self._get(f"{CLOUD}/servico-academico-siaf/api/v1/alunos/{i['cpf']}/matricula/{i['ra']}/cursos", scope=Scope.SIAF)

    def progress(self) -> dict[str, Any]:
        """'Progresso Geral' — courses, indicators and curricular units of the current term."""
        sid = self.client.student_id
        courses = self._get(f"{PROGRESS}/cursos", {"estudanteId": sid}, scope=Scope.PROGRESS)
        out: dict[str, Any] = {"courses": courses, "terms": {}, "indicators": {}, "units": {}}
        for course in courses if isinstance(courses, list) else []:
            cid = course.get("id") or course.get("cursoId")
            if cid is None:
                continue
            terms = self._get(f"{PROGRESS}/periodos-letivos", {"estudanteId": sid, "cursoId": cid, "apenasConcluidos": "false"}, scope=Scope.PROGRESS)
            out["terms"][cid] = terms
            current = next((t for t in terms if isinstance(t, dict)), None) if isinstance(terms, list) else None
            term = (current or {}).get("periodoLetivo") or (current or {}).get("descricao")
            if term:
                p = {"estudanteId": sid, "cursoId": cid, "periodoLetivo": term}
                out["indicators"][cid] = self._get(f"{PROGRESS}/indicadores-progresso", p, scope=Scope.PROGRESS)
                out["units"][cid] = self._get(f"{PROGRESS}/unidades-curriculares", p, scope=Scope.PROGRESS)
        return out

    def online_libraries(self, brand_id: int = 13) -> Any:
        """'Bibliotecas Online' (brand 13 observed for the institution)."""
        return self._get(f"{CLOUD}/servico-gestao-bibliotecas/services/v1/libraries/brands/{brand_id}", scope=Scope.LIBRARIES)

    # -- requests ("Solicitações": e.g. justify absences with a medical certificate) -------------------------
    def request_types(self, coordination: bool = False) -> list[dict[str, Any]]:
        q = "?tipoCurso=Graduacao" + ("&coordenacao=true" if coordination else "")
        return self._get(f"{ASYNC}/tipo-solicitacoes{q}", scope=Scope.ASYNC_SUPPORT)

    def request_reasons(self, type_id: int, coordination: bool = False) -> list[dict[str, Any]]:
        return self._get(f"{ASYNC}/tipo-solicitacoes/{type_id}/motivos" + ("?coordenacao=true" if coordination else ""), scope=Scope.ASYNC_SUPPORT)

    def request_subjects(self, reason_id: int, coordination: bool = False) -> list[dict[str, Any]]:
        return self._get(f"{ASYNC}/motivos/{reason_id}/assuntos" + ("?coordenacao=true" if coordination else ""), scope=Scope.ASYNC_SUPPORT)

    @staticmethod
    def _files_payload(files: list[str | Path], allowed: set[str], max_total: int, max_files: int) -> list[dict[str, str]]:
        paths = [Path(f) for f in files]
        if len(paths) > max_files:
            raise ValueError(f"at most {max_files} files")
        bad = [p.name for p in paths if p.suffix.lower() not in allowed]
        if bad:
            raise ValueError(f"unsupported file type(s) {bad}; allowed: {sorted(allowed)}")
        missing = [str(p) for p in paths if not p.is_file()]
        if missing:
            raise FileNotFoundError(missing)
        total = sum(p.stat().st_size for p in paths)
        if total >= max_total:
            raise ValueError(f"files total {total} bytes; limit {max_total} bytes")
        return [{"nome": p.name, "arquivoBase64": base64.b64encode(p.read_bytes()).decode()} for p in paths]

    def create_request(self, type_: dict[str, Any], reason: dict[str, Any], subject: dict[str, Any], message: str,
                       files: list[str | Path] | None = None, phone: str = "", *, confirm: bool = False) -> Any:
        """Open a request ("solicitação") with optional attachments (PDF/PNG/JPG, ≤24 files, total <10 MB).

        `type_`, `reason`, `subject` are items returned by request_types/request_reasons/request_subjects (need `id`
        and `salesforceId`). For absences with a medical certificate the portal path observed is
        Acadêmico → "Disciplinas/UC, Aulas, Notas e Faltas" → "Notas e Faltas" (there is no explicit "atestado" type).
        """
        if not message.strip():
            raise ValueError("message is required")
        ident = self.identity()
        payload = {"tipoSolicitacaoId": type_["id"], "tipoSolicitacaoSalesForceId": type_.get("salesforceId"),
                   "motivoId": reason["id"], "motivoSalesForceId": reason.get("salesforceId"),
                   "assuntoId": subject["id"], "assuntoSalesForceId": subject.get("salesforceId"), "mensagem": message,
                   "cpf": ident["cpf"], "ra": ident["ra"], "nome": ident["name"], "telefone": phone, "email": f"{ident['ra']}@ulife.com.br",
                   "arquivos": self._files_payload(files or [], REQUEST_EXTENSIONS, REQUEST_MAX_TOTAL_BYTES, REQUEST_MAX_FILES)}
        preview = {**payload, "cpf": "<from token>", "arquivos": [f["nome"] for f in payload["arquivos"]]}
        if not self.client._guard("create_request", confirm, preview):
            return self.client._preview("create_request", preview)
        resp = self.client.http.request("POST", f"{ASYNC}/casos", json=payload, scope=Scope.ASYNC_SUPPORT)
        return resp.json() if resp.content else {"status": resp.status_code}

    def update_contact(self, phone: str = "", email: str = "", *, confirm: bool = False) -> Any:
        """Update phone and/or e-mail (help-service screen; the profile page itself is read-only)."""
        if not phone and not email:
            raise ValueError("phone or email is required")
        ra = self.identity()["ra"]
        q = "&".join(x for x in (f"telefone={phone}" if phone else "", f"email={email}" if email else "") if x)
        if not self.client._guard("update_contact", confirm, {"telefone": phone, "email": email}):
            return self.client._preview("update_contact", {"telefone": phone, "email": email})
        resp = self.client.http.request("PUT", f"{PERSONAL}/informacoes-aluno/{ra}?{q}", scope=Scope.PERSONAL_SUPPORT)
        return {"status": resp.status_code}

    # -- documents -----------------------------------------------------------------------------------------
    def download_enrollment_document(self, aluno_documento_id: int, dest: str) -> str:
        """Download a document listed by PortalClient.enrollment_documents() (field `alunoDocumentoId`)."""
        from .session import Scopes

        resp = self.client.http.request("GET", f"{CLOUD}/documentacaopessoa/api/v1/alunos/documentos-ulife/{aluno_documento_id}",
                                        params={"cpf": self.identity()["cpf"]}, scope=Scopes.DOCUMENTS)
        Path(dest).write_bytes(resp.content)
        return dest

    def upload_enrollment_document(self, document_type_code: int, file: str | Path, *, confirm: bool = False) -> Any:
        """Send an enrollment document (JPG/PNG/PDF ≤10 MB). `document_type_code` = `codigoTipoDocumentoSiaf`
        of the pending item in enrollment_documents()."""
        from .session import Scopes

        p = Path(file)
        if p.suffix.lower() not in ENROLL_DOC_EXTENSIONS:
            raise ValueError(f"unsupported file type {p.suffix}; allowed {sorted(ENROLL_DOC_EXTENSIONS)}")
        if not p.is_file():
            raise FileNotFoundError(p)
        if p.stat().st_size > ENROLL_DOC_MAX_BYTES:
            raise ValueError("file larger than 10 MB")
        preview = {"codigoTipoDocumentoSiaf": document_type_code, "documento": p.name}
        if not self.client._guard("upload_enrollment_document", confirm, preview):
            return self.client._preview("upload_enrollment_document", preview)
        with p.open("rb") as fh:
            resp = self.client.http.request("POST", f"{CLOUD}/documentacaopessoa/api/v1/alunos/documentos-ulife",
                                            params={"cpf": self.identity()["cpf"]}, scope=Scopes.DOCUMENTS,
                                            data={"codigoTipoDocumentoSiaf": str(document_type_code)}, files={"documento": (p.name, fh)})
        return resp.json() if resp.content else {"status": resp.status_code}

    # -- profile ---------------------------------------------------------------------------------------------
    def upload_profile_photo(self, image: str | Path, *, crop: tuple[int, int, int, int] | None = None, confirm: bool = False) -> Any:
        """Change the profile photo (www/Community/FullProfile.aspx), exactly as the page does.

        Portal rules: .jpg/.jpeg/.gif/.png, at most 4 MB, dimensions LARGER than 300x300 px, and the mandatory declaration
        "Eu declaro ter todos os direitos legais sobre a imagem e não violar o Termo de Uso" (accepted on your behalf
        only when you call this with confirm=True). Flow: ShowUpload() → multipart upload ("Salvar foto") → crop dialog
        (`CropResults` = {"left","top","width","height"} in image pixels; portal default is a 300x300 centered square,
        which would ZOOM the photo) → save. By default the crop is the largest centered square (the whole image when square),
        so nothing is cut beyond what a square avatar requires. Uses the browser (WebForms postbacks with ViewState).
        """
        p = Path(image)
        if p.suffix.lower() not in PHOTO_EXTENSIONS:
            raise ValueError(f"unsupported image type {p.suffix}; allowed {sorted(PHOTO_EXTENSIONS)}")
        if not p.is_file():
            raise FileNotFoundError(p)
        if p.stat().st_size > PHOTO_MAX_BYTES:
            raise ValueError("image larger than 4 MB")
        w, h = image_size(p)
        if w <= PHOTO_MIN_SIDE or h <= PHOTO_MIN_SIDE:
            raise ValueError(f"image is {w}x{h}; the portal requires more than {PHOTO_MIN_SIDE}x{PHOTO_MIN_SIDE} pixels")
        side = min(w, h)
        left, top, cw, ch = crop or ((w - side) // 2, (h - side) // 2, side, side)
        preview = {"image": p.name, "size": [w, h], "crop": {"left": left, "top": top, "width": cw, "height": ch}, "declaration": True}
        if not self.client._guard("upload_profile_photo", confirm, preview):
            return self.client._preview("upload_profile_photo", preview)
        return _photo_flow(self.client, p, preview["crop"])

    # -- notification preferences ("Configurar Notificações") --------------------------------------------------
    NOTIFICATION_CHANNELS = {"email": 1, "sms": 2, "mailbox": 3, "page": 4, "mobile": 5}  # NotificationMessageType (page enum)

    def notification_preferences(self) -> list[dict[str, Any]]:
        """Per-event channel settings (EmailIsOn/PageIsOn/MobileIsOn), grouped by category; all pages, loop-detected."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        index = 1
        while True:
            data = self.client.http.get_json(f"{WWW}/Handlers/Notification/GetUserNotification.ashx",
                                             {"pageSize": 20, "searchCriteria": "", "pageIndex": index})
            groups = (data or {}).get("Result") or []
            sig = json.dumps(groups, sort_keys=True)
            if not groups or sig in seen:
                return out
            seen.add(sig)
            out += groups
            index += 1

    def set_notification_preference(self, event_id: int, channel: str, enabled: bool, *, confirm: bool = False) -> Any:
        """Turn one channel (email|sms|mailbox|page|mobile) on/off for one event (`EventId` from notification_preferences)."""
        if channel not in self.NOTIFICATION_CHANNELS:
            raise ValueError(f"channel must be one of {sorted(self.NOTIFICATION_CHANNELS)}")
        payload = {"t": self.NOTIFICATION_CHANNELS[channel], "ei": event_id, "s": enabled}
        if not self.client._guard("set_notification_preference", confirm, payload):
            return self.client._preview("set_notification_preference", payload)
        return self.client._form_post(f"{WWW}/Handlers/Notification/NotificationTurnSettings.ashx", payload)

    # -- scholarship policy ------------------------------------------------------------------------------------
    def scholarship_policy_pdf(self, dest: str) -> str:
        """Download 'Política de Bolsas' PDF for the current enrollment."""
        i = self.identity()
        resp = self.client.http.request("GET", f"{CLOUD}/servicobolsas/services/api/politicas/alunos/{i['ra']}",
                                        params={"numCpf": i["cpf"]}, scope=Scope.SCHOLARSHIPS)
        Path(dest).write_bytes(resp.content)
        return dest
