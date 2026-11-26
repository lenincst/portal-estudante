"""High-level Ulife client: everything the student does on the portal, as Python methods.

Reads are validated against contracts (see contracts.py) so a Ulife change raises a specific error.
Writes are blocked by default: they need `PortalClient(allow_writes=True)` AND `confirm=True` per call,
and none of them has been executed against the real account without an explicit user authorization.
Endpoints with side effects that mark items as read / register accesses are never called implicitly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

import httpx
from datetime import date, timedelta
from typing import Any

from . import contracts as C
from .contracts import Contract, Field, Shape
from .errors import ClassmatesDisabledError, PageStructureChangedError, WriteBlockedError
from .session import Scopes, PortalHttp

APISTUDENT = "https://apistudent.ulife.com.br"
APIADMIN = "https://apiadmin.ulife.com.br"
APISOCIAL = "https://apisocial.ulife.com.br"
API = "https://api.ulife.com.br"
WWW = "https://www.ulife.com.br"
STUDENT = "https://student.ulife.com.br"
CLOUDAPP = "https://cloudapp.animaeducacao.com.br"

S, I, B, L, D, N = "str", "int", "bool", "list", "dict", "num"


def jquery_param(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Encode like jQuery `$.param` (used by the portal's AngularJS `base.ajax.post`): nested dicts/lists become
    `Recipients[0][EntityID]=1`; None becomes an empty string; booleans become 'true'/'false'."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            out += jquery_param(value, name)
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, (dict, list, tuple)):
                    out += jquery_param(item if isinstance(item, dict) else dict(enumerate(item)), f"{name}[{i}]")
                else:
                    out.append((f"{name}[]", "" if item is None else str(item)))
        elif isinstance(value, bool):
            out.append((name, "true" if value else "false"))
        else:
            out.append((name, "" if value is None else str(value)))
    return out


def _envelope(result: Field) -> Shape:
    return Shape({"Result": result, "Success": Field(B)})


def _paged(item: Shape) -> Shape:
    return Shape({"Result": Field(L, item=item), "Success": Field(B), "TotalCount": Field(I), "HasMorePages": Field(B), "PageIndex": Field(I)})


# --- Contracts (fields the client relies on; others are tracked as drift) -------------------------
COURSES = Contract("GET apistudent/MyCourses/courses", _envelope(Field(L, item=Shape({
    "OrganizationId": Field(I), "CourseId": Field(I), "CourseName": Field(S), "OrganizationName": Field(S),
    "StatusDescription": Field(S), "CourseStatus": Field(S), "RA": Field(S)}))), hint="client.PortalClient.courses")

DISCIPLINE_ITEM = Shape({
    "DisciplineName": Field(S), "DisciplineID": Field(I), "ClassDisciplineID": Field(I), "StudentClassDisciplineID": Field(I),
    "ClassID": Field(I), "ClassName": Field(S), "OrganizationID": Field(I), "OrganizationCourseID": Field(I),
    "ModuleStepName": Field(S, nullable=True), "TimeLoadTypeName": Field(S, nullable=True), "GradeName": Field(S, nullable=True),
    "IsMainClass": Field(B), "ClassDisciplineStartDate": Field(S, nullable=True), "ClassDisciplineEndDate": Field(S, nullable=True),
    "GradeUrl": Field(S, nullable=True), "MembersUrl": Field(S, nullable=True), "ContentUrl": Field(S, nullable=True),
    "UserMediaClassDisciplineUrl": Field(S, nullable=True), "CalendarUrl": Field(S, nullable=True)})
DISCIPLINES = Contract("GET apistudent/Student/Disciplines", Shape({"Result": Field(D, item=Shape({
    "DefaultList": Field(L, item=DISCIPLINE_ITEM), "InactiveList": Field(L, nullable=True),
    "ActivityList": Field(L, nullable=True, item=Shape({"LessonCategoryName": Field(S), "DisciplineName": Field(S),
                                                       "AvailableDate": Field(S, nullable=True), "DeliveryDate": Field(S, nullable=True),
                                                       "LessonCategoryType": Field(S, nullable=True), "LessonCategoryURL": Field(S, nullable=True)})),
    "TeacherList": Field(L, nullable=True, item=Shape({"Name": Field(S), "UserOrganizationID": Field(I), "ClassDisciplineID": Field(I)}))})),
    "Success": Field(B)}), hint="client.PortalClient.disciplines / activities / teachers")

GRADES = Contract("GET apistudent/StudentGrade/GetStudentGrades", _envelope(Field(D, item=Shape({
    "ClassDisciplineID": Field(I), "StudentLessons": Field(L)}))), hint="client.PortalClient.grades")
MEMBERS = Contract("GET apiadmin/ClassDisciplineMembers/getList", _paged(Shape({
    "UserID": Field(I), "Name": Field(S), "UserIsTeacher": Field(B), "UserIsOnlyStudent": Field(B), "RoleTypeNames": Field(L, nullable=True)})),
    hint="client.PortalClient.members")
LESSONS = Contract("GET apistudent/Lesson/GetStudentView", Shape({"Result": Field(L, item=Shape({
    "LessonID": Field(I), "LessonName": Field(S), "StudentLessonCategoryList": Field(L, nullable=True)})),
    "Success": Field(B), "TotalCount": Field(I), "HasMoreLessons": Field(B)}), hint="client.PortalClient.lessons")
MATERIALS = Contract("GET apistudent/UserMediaClassDiscipline/GetUserMediaList", Shape({"Success": Field(B), "Result": Field(L, nullable=True)}),
                     hint="client.PortalClient.materials")
FEED = Contract("GET apisocial/Feed/ListFeeds", _paged(Shape({
    "ID": Field(I), "Title": Field(S, nullable=True), "Description": Field(S, nullable=True), "CreateDate": Field(S),
    "UserName": Field(S, nullable=True), "CurrentUserViewed": Field(B), "HtmlFile": Field(S, nullable=True)})), hint="client.PortalClient.announcements")
NOTIF_SUMMARY = Contract("GET apisocial/users/{UserID}/notifications-summarized", _envelope(Field(D, item=Shape({
    "Notifications": Field(L, nullable=True), "UnReadCount": Field(I)}))), hint="client.PortalClient.notifications_summary")
NOTIF_PAGE = Contract("GET apisocial/users/{UserID}/notifications", Shape({"Result": Field(L, nullable=True), "Success": Field(B),
                                                                          "TotalCount": Field(I), "HasMorePages": Field(B)}),
                      hint="client.PortalClient.notifications")
NOTIF_CATEGORIES = Contract("GET apisocial/users/notification-categories", _envelope(Field(L, item=Shape({
    "NotificationCategoryId": Field(I), "Name": Field(S), "Key": Field(S)}))), hint="client.PortalClient.notification_categories")
COMMITMENTS = Contract("GET apisocial/users/{UOID}/commitments", _envelope(Field(L, nullable=True, item=Shape({
    "Title": Field(S), "StartDate": Field(S), "EndDate": Field(S), "Category": Field(S), "Type": Field(S)}))), hint="client.PortalClient.commitments")
CLOUD = lambda name, payload: Contract(name, Shape({"servicoProcessado": Field(B), "objetoRetorno": payload}), hint="client.PortalClient.class_schedule")  # noqa: E731
SCHEDULE = CLOUD("GET cloudapp/servico-quadro-aulas/alunos/{id}/aulas", Field(D, item=Shape({"aulas": Field(L, item=Shape({
    "disciplina": Field(S), "datas": Field(L), "codigoDiario": Field(S)}))})))
RECORDED = CLOUD("GET cloudapp/servico-quadro-aulas-links/alunos/{id}/aulas-gravadas", Field(L, item=Shape({
    "idDiarioUlife": Field(I), "dataAula": Field(S), "disciplina": Field(S), "turma": Field(S), "horaInicio": Field(S), "horaFim": Field(S)})))
TERM = CLOUD("GET cloudapp/servico-quadro-aulas/alunos/{id}/periodo-letivo-vigente", Field(D, item=Shape({"dataInicio": Field(S), "dataFim": Field(S)})))
ENROLL_DOCS = Contract("GET cloudapp/documentacaopessoa/alunos/documentos-ulife", Shape({"data": Field(D, item=Shape({
    "results": Field(L, item=Shape({"nomeTipoDocumento": Field(S), "statusDocumento": Field(S), "dataEnvio": Field(S, nullable=True)})),
    "count": Field(I)}))}), hint="client.PortalClient.enrollment_documents")
ENVELOPE_ANY = lambda name: Contract(name, Shape({"Success": Field(B), "Result": Field(L, nullable=True)}))  # noqa: E731


class PortalClient:
    """Facade over the mapped Ulife endpoints. Use as a context manager to persist rotated tokens."""

    def __init__(self, http: PortalHttp | None = None, allow_writes: bool = False, dry_run: bool = False,
                 classmates: bool = False) -> None:
        """`allow_writes`: enable write actions (each one still needs `confirm=True`).
        `dry_run`: write actions return a preview of what would be sent and never touch the portal.
        `classmates`: opt-in to read the class roster (other students' personal data; minimal fields, never saved)."""
        self.http = http or PortalHttp()
        self.allow_writes = allow_writes
        self.dry_run = dry_run
        self.classmates = classmates
        self._student_id: int | None = None

    def __enter__(self) -> PortalClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.http.persist()
        self.http.close()

    # -- pagination with loop detection (no fixed page caps) -------------------------------------------------
    def _paginate(self, url: str, params: dict[str, Any], *, contract: Contract | None = None, scope: str | None = None,
                  max_pages: int | None = None, page_param: str = "PageIndex", extra: list[tuple[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Follow pages until the server says there are no more. Stops on: empty page, HasMorePages false,
        repeated page index, or a page identical to one already seen (signature of its items).
        `max_pages` is an optional caller choice, not a safety cap."""
        out: list[dict[str, Any]] = []
        seen_pages: set[int] = set()
        signatures: set[str] = set()
        index = int(params.get(page_param, 1) or 1)
        while True:
            if index in seen_pages:
                return out
            seen_pages.add(index)
            query: Any = list({**params, page_param: index}.items()) + list(extra or [])
            data = self.http.get_json(url, query, scope=scope)
            C.check_envelope(contract.endpoint if contract else f"GET {url}", data)
            if contract:
                C.check(contract, data)
            items = (data.get("Result") if isinstance(data, dict) else data) or []
            if isinstance(items, dict):  # some endpoints wrap the list
                items = next((v for v in items.values() if isinstance(v, list)), [])
            signature = json.dumps(items, sort_keys=True, default=str)[:20000]
            if not items or signature in signatures:
                return out
            signatures.add(signature)
            out += items
            if max_pages is not None and len(seen_pages) >= max_pages:
                return out
            has_more = data.get("HasMorePages") if isinstance(data, dict) else None
            if has_more is False:
                return out
            nxt = data.get("NextPageIndex") if isinstance(data, dict) else None
            index = int(nxt) if isinstance(nxt, int) and nxt > index else index + 1

    @property
    def services(self):  # noqa: ANN201 - returns services.Services
        """Academic services: requests (solicitações), documents, progress, record, certificates, profile/contact."""
        from .services import Services

        if getattr(self, "_services", None) is None:
            self._services = Services(self)
        return self._services

    @property
    def messages(self):  # noqa: ANN201 - returns messages.Messages (imported lazily to avoid a cycle)
        """Message center: `messages.inbox()`, `messages.outbox()`, `messages.thread(summary)`."""
        from .messages import Messages

        return Messages(self)

    # -- identity -------------------------------------------------------------------------------------
    @property
    def user(self) -> dict[str, Any]:
        """Non-secret ids captured at login: UserID, UserOrganizationID, OrganizationID, UserName."""
        return dict(self.http.session.user)

    def _get(self, contract: Contract, url: str, params: dict[str, Any] | None = None, scope: str | None = None) -> Any:
        data = self.http.get_json(url, params, scope=scope)
        C.check_envelope(contract.endpoint, data)
        return C.check(contract, data)

    # -- courses & disciplines --------------------------------------------------------------------------
    def courses(self) -> list[dict[str, Any]]:
        return self._get(COURSES, f"{APISTUDENT}/MyCourses/courses")["Result"]

    def _disciplines_raw(self) -> dict[str, Any]:
        params = {"EnableOrderByFilter": "true", "EnableResumeCategory": "true", "EnableSearchDisciplines": "false", "FirstLoad": "true",
                  "GetUnenrolledDisciplineFromElasticSearch": "true", "HideCalendarLink": "false", "HideGradeLink": "false",
                  "PageIndex": 1, "PageSize": 100, "SearchCriteria": "", "ShowUnenrolledList": "false", "orderByOption": 0}
        return self._get(DISCIPLINES, f"{APISTUDENT}/Student/Disciplines", params)["Result"]

    def disciplines(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        r = self._disciplines_raw()
        return r["DefaultList"] + ((r.get("InactiveList") or []) if include_inactive else [])

    def activities(self) -> list[dict[str, Any]]:
        """Upcoming activities/assessments (deadline list shown on the home)."""
        return self._disciplines_raw().get("ActivityList") or []

    def teachers(self) -> list[dict[str, Any]]:
        return self._disciplines_raw().get("TeacherList") or []

    @staticmethod
    def _discipline_params(d: dict[str, Any], page_size: int = 20) -> dict[str, Any]:
        return {"ClassDisciplineID": d["ClassDisciplineID"], "ClassDisciplineId": d["ClassDisciplineID"],
                "StudentClassDisciplineID": d["StudentClassDisciplineID"], "OrganizationCourseID": d["OrganizationCourseID"],
                "ModuleStepName": d.get("ModuleStepName") or "", "TimeLoadTypeName": d.get("TimeLoadTypeName") or "",
                "DisciplineName": d["DisciplineName"], "PageIndex": 1, "PageSize": page_size, "SearchCriteria": "", "FirstLoad": "true"}

    @staticmethod
    def _crypt(d: dict[str, Any]) -> str:
        """Encrypted class-discipline id (`cd=` parameter of the discipline URLs)."""
        for key in ("ContentUrl", "MembersUrl", "GradeUrl", "UserMediaClassDisciplineUrl", "CalendarUrl"):
            m = re.search(r"[?&]cd=([^&]+)", d.get(key) or "")
            if m:
                from urllib.parse import unquote
                return unquote(m.group(1))
        raise PageStructureChangedError("discipline URLs", [C.ContractIssue("DefaultList[].*Url", "cd= parameter", "absent")],
                                        hint="client.PortalClient._crypt")

    # -- per-discipline reads --------------------------------------------------------------------------
    def grades(self, d: dict[str, Any]) -> list[dict[str, Any]]:
        params = self._discipline_params(d) | {"GroupHomeClassDisciplineStudent": "true", "lessonPageTab": 0}
        return self._get(GRADES, f"{APISTUDENT}/StudentGrade/GetStudentGrades", params)["Result"]["StudentLessons"]

    def members(self, d: dict[str, Any]) -> list[dict[str, Any]]:
        """Class roster — DISABLED by default (other students' personal data). Enable with `PortalClient(classmates=True)`.
        Returns only the minimum: name, role and the id needed to send a message. Nothing is cached or written to disk."""
        if not self.classmates:
            raise ClassmatesDisabledError("The class roster contains other students' personal data and is disabled by default; "
                                          "use PortalClient(classmates=True) to opt in (data is returned in memory only).")
        params = self._discipline_params(d, page_size=60) | {"ClassDisicplineMemberType": 0, "DisciplineMemberOnline": -1}
        raw = self._paginate(f"{APIADMIN}/ClassDisciplineMembers/getList", params, contract=MEMBERS)
        return [{"name": m["Name"], "role": "professor" if m.get("UserIsTeacher") else "aluno",
                 "user_organization_id": m.get("DefaultUserOrganizationID")} for m in raw]

    def lessons(self, d: dict[str, Any]) -> list[dict[str, Any]]:
        """Lessons and their activities. The API ignores PageIndex and pages by `LastLessonID`; stops on repeats."""
        out: list[dict[str, Any]] = []
        seen: set[int] = set()
        cursors: set[int] = set()
        params = self._discipline_params(d) | {"ClassDisciplineCrpt": self._crypt(d), "LessonListViewMode": 2, "lessonPageTab": 0}
        while True:
            page = self._get(LESSONS, f"{APISTUDENT}/Lesson/GetStudentView", params)
            fresh = [x for x in page["Result"] if x["LessonID"] not in seen]
            seen.update(x["LessonID"] for x in fresh)
            out += fresh
            cursor = page.get("LastLessonID")
            if not page["HasMoreLessons"] or not fresh or not cursor or cursor in cursors:
                return out
            cursors.add(cursor)
            params["LastLessonID"] = cursor
            params["PageIndex"] += 1

    def materials(self, d: dict[str, Any]) -> list[dict[str, Any]]:
        """Teacher-shared files/links for the discipline (list only; does NOT register an access)."""
        params = self._discipline_params(d) | {"ClassDisciplineCrpt": self._crypt(d)}
        return self._get(MATERIALS, f"{APISTUDENT}/UserMediaClassDiscipline/GetUserMediaList", params).get("Result") or []

    def download(self, url: str, dest: str) -> str:
        """Download a file URL (material, document) to `dest` using the authenticated session."""
        resp = self.http.request("GET", url, follow_redirects=True)
        with open(dest, "wb") as fh:
            fh.write(resp.content)
        return dest

    # -- communication: announcements, notifications, commitments ---------------------------------------
    def announcements(self, max_pages: int | None = 1, page_size: int = 30) -> list[dict[str, Any]]:
        """Announcements feed (read-only: never calls ViewFeed(s) nor /user/announcements/read).
        `max_pages=None` follows all pages (loop-detected)."""
        u = self.user
        params = {"FeedId": 0, "FeedParentId": u["UserID"], "FeedType": 0, "FirstLoad": "true", "HideFeedComment": "false",
                  "OnlyPinned": "false", "OrganizationId": u["OrganizationID"], "PageSize": page_size, "SearchCriteria": "",
                  "UserId": u["UserID"], "UserOrganizationId": u["UserOrganizationID"]}
        return self._paginate(f"{APISOCIAL}/Feed/ListFeeds", params, contract=FEED, max_pages=max_pages)

    def notifications_summary(self) -> dict[str, Any]:
        return self._get(NOTIF_SUMMARY, f"{APISOCIAL}/users/{self.user['UserID']}/notifications-summarized", scope=Scopes.LMS)["Result"]

    def notifications(self, unread_only: bool = False, search: str = "", categories: list[int] | None = None,
                      max_pages: int | None = 1) -> list[dict[str, Any]]:
        """Notifications list (read-only; listing does not mark as read). `max_pages=None` = all pages (loop-detected)."""
        extra: list[tuple[str, Any]] = []
        if search:
            extra.append(("searchTerm", search))
        if unread_only:
            extra.append(("Read", 0))
        extra += [("categories", c) for c in (categories or [])]
        return self._paginate(f"{APISOCIAL}/users/{self.user['UserID']}/notifications", {}, contract=NOTIF_PAGE, scope=Scopes.LMS,
                              max_pages=max_pages, page_param="pageIndex", extra=extra)

    def notification_categories(self) -> list[dict[str, Any]]:
        return self._get(NOTIF_CATEGORIES, f"{APISOCIAL}/users/notification-categories", scope=Scopes.LMS)["Result"]

    def commitments(self) -> list[dict[str, Any]]:
        return self._get(COMMITMENTS, f"{APISOCIAL}/users/{self.user['UserOrganizationID']}/commitments", scope=Scopes.LMS)["Result"] or []

    # -- calendar ------------------------------------------------------------------------------------------
    @property
    def student_id(self) -> int:
        """Id used by the class-schedule microservices (published as `var studentId` in the schedule page)."""
        if self._student_id is None:
            resp = self.http.request("GET", f"{STUDENT}/ClassDisciplineClassTime/Index")
            m = re.search(r"var\s+studentId\s*=\s*(\d+)", resp.text)
            if not m:
                raise PageStructureChangedError("GET student/ClassDisciplineClassTime/Index",
                                                [C.ContractIssue("script var studentId", "number", "not found")], hint="client.PortalClient.student_id")
            self._student_id = int(m.group(1))
        return self._student_id

    def class_schedule(self, start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
        """Classes between `start` and `end` (default: current week)."""
        start = start or date.today() - timedelta(days=date.today().weekday())
        end = end or start + timedelta(days=6)
        data = self._get(SCHEDULE, f"{CLOUDAPP}/servico-quadro-aulas/api/v1/alunos/{self.student_id}/aulas",
                         {"dataInicio": start.isoformat(), "dataFim": end.isoformat()}, scope=Scopes.CLASS_SCHEDULE)
        return data["objetoRetorno"]["aulas"]

    def recorded_classes(self, months_back: int = 6) -> list[dict[str, Any]]:
        return self._get(RECORDED, f"{CLOUDAPP}/servico-quadro-aulas-links/api/v1/alunos/{self.student_id}/aulas-gravadas",
                         {"mesesRetroativos": months_back}, scope=Scopes.CLASS_SCHEDULE_LINKS)["objetoRetorno"]

    def current_term(self) -> dict[str, Any]:
        return self._get(TERM, f"{CLOUDAPP}/servico-quadro-aulas/api/v1/alunos/{self.student_id}/periodo-letivo-vigente",
                         scope=Scopes.CLASS_SCHEDULE)["objetoRetorno"]

    # -- documents & services ------------------------------------------------------------------------------
    def enrollment_documents(self) -> list[dict[str, Any]]:
        """Enrollment documents sent by the student and their validation status."""
        return self._get(ENROLL_DOCS, f"{CLOUDAPP}/documentacaopessoa/api/v1/alunos/documentos-ulife", scope=Scopes.DOCUMENTS)["data"]["results"]

    def communication_categories(self, d: dict[str, Any]) -> list[dict[str, Any]]:
        """Categories available for messaging the teachers of a discipline."""
        return self._get(ENVELOPE_ANY("GET api/Message/GetCommunicationCategories"), f"{API}/Message/GetCommunicationCategories",
                         {"classDisciplineId": d["ClassDisciplineID"]}).get("Result") or []

    def support_categories(self) -> list[dict[str, Any]]:
        u = self.user
        return self._get(ENVELOPE_ANY("GET api/Support/ListSupportCategory"), f"{API}/Support/ListSupportCategory",
                         {"organizationId": u["OrganizationID"], "userOrganizationId": u["UserOrganizationID"]}).get("Result") or []

    # -- writes (blocked unless allow_writes and confirm) ----------------------------------------------------
    def _guard(self, action: str, confirm: bool, preview: dict[str, Any]) -> bool:
        """True = execute. Dry-run → False (caller returns `_preview`). Otherwise both switches are required."""
        if self.dry_run:
            return False
        if not (self.allow_writes and confirm):
            raise WriteBlockedError(action, preview)
        return True

    @staticmethod
    def _preview(action: str, preview: dict[str, Any]) -> dict[str, Any]:
        return {"dry_run": True, "action": action, "would_send": preview}

    def _form_post(self, url: str, payload: dict[str, Any]) -> Any:
        """POST exactly like the portal's `base.ajax.post`: x-www-form-urlencoded, jQuery-style nesting, cookies."""
        resp = self.http.request("POST", url, content=httpx.QueryParams(jquery_param(payload)).__str__().encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                          "Origin": STUDENT, "Referer": f"{STUDENT}/"})
        data = resp.json()
        C.check_envelope(f"POST {url.split('?')[0]}", data)
        return data

    def send_direct_message(self, recipient_user_organization_id: int, subject: str, body_html: str, *,
                            confirm: bool = False) -> Any:
        """Send a direct message ("Escrever mensagem" / "Enviar mensagem" on a member) to users.

        ONE recipient per call (no mass sending), identified by its UserOrganizationId (`members()` →
        `user_organization_id`, or a teacher id).
        Portal validation: subject and body must not be empty. The message appears in the sender's
        `outbox()` and in each recipient's `inbox()`.
        """
        if not isinstance(recipient_user_organization_id, int) or not subject.strip() or not body_html.strip():
            raise ValueError("one recipient id (int), subject and body are required")
        payload = {"Recipients": [{"EntityID": recipient_user_organization_id, "Type": 1}],
                   "Subject": subject, "Body": body_html, "UserRecipientCount": 1}
        if not self._guard("send_direct_message", confirm, payload):
            return self._preview("send_direct_message", payload)
        return self._form_post(f"{API}/Message/SendMessage", payload)

    def send_teacher_message(self, d: dict[str, Any], body: str, category_id: int, subcategory_id: int | None = None, *, confirm: bool = False) -> Any:
        """Send a message to the teachers of discipline `d` (portal rules: ≥4 words, ≥10 letters)."""
        payload = {"ClassDisciplineId": d["ClassDisciplineID"], "Body": body, "CommunicationCategoryId": category_id,
                   "CommunicationSubcategoryId": subcategory_id}
        if len(body.split()) < 4 or sum(ch.isalnum() for ch in body) < 10:
            raise ValueError("Ulife requires at least 4 words and 10 alphanumeric characters")
        if not self._guard("send_teacher_message", confirm, payload):
            return self._preview("send_teacher_message", payload)
        return self._form_post(f"{API}/Message/SendClassDisciplineMessage", payload)

    def create_support_ticket(self, title: str, description_html: str, category: Any, *, confirm: bool = False) -> Any:
        """Open a support request ("requisição"). Fields mirror the portal's supportTicketDialog.

        The portal also sends `document.cookie` in the body; this client deliberately sends it EMPTY so session
        cookies are never copied into a ticket that support staff can read.
        """
        if not title.strip() or not description_html.strip():
            raise ValueError("title and description are required")
        payload = {"title": title.strip(), "description": description_html, "category": category, "cookies": "",
                   "fullStoryURL": "", "supportUrl": f"{STUDENT}/NewStudentHome", "encryptedUserOrganizationID": None}
        if not self._guard("create_support_ticket", confirm, payload):
            return self._preview("create_support_ticket", payload)
        return self._form_post(f"{API}/Support/CreateSupportTicket", payload)

    def mark_notifications_read(self, notification_page_ids: list[str], *, confirm: bool = False) -> Any:
        if not self._guard("mark_notifications_read", confirm, {"notificationPageIds": notification_page_ids}):
            return self._preview("mark_notifications_read", {"notificationPageIds": notification_page_ids})
        return self.http.request("PATCH", f"{APISOCIAL}/users/{self.user['UserID']}/notifications/read",
                                 json={"notificationPageIds": notification_page_ids}, scope=Scopes.LMS).json()

    def read_checks(self) -> list[tuple[str, Callable[[], Any]]]:
        """Every read operation as an independent (name, thunk) pair — one failure never hides the others.
        Used by `python -m portal_estudante check` (contract smoke test / change detector)."""
        state: dict[str, Any] = {}

        def main_discipline() -> dict[str, Any]:
            if "main" not in state:
                ds = self.disciplines()
                state["main"] = next((d for d in ds if d.get("IsMainClass")), ds[0])
            return state["main"]

        s = self.services

        def reasons() -> list[dict[str, Any]]:
            if "reasons" not in state:
                types = s.request_types()
                state["reasons"] = s.request_reasons(types[0]["id"]) if types else []
            return state["reasons"]

        checks: list[tuple[str, Callable[[], Any]]] = [("courses", self.courses), ("disciplines", self.disciplines)]
        for name, fn in (("grades", self.grades), ("lessons", self.lessons), ("materials", self.materials),
                         ("communication_categories", self.communication_categories)):
            checks.append((name, lambda fn=fn: fn(main_discipline())))
        if self.classmates:  # opt-in only (other students' data)
            checks.append(("members", lambda: self.members(main_discipline())))
        checks += [("activities", self.activities), ("teachers", self.teachers), ("announcements", self.announcements),
                   ("notifications_summary", self.notifications_summary), ("notifications", self.notifications),
                   ("notification_categories", self.notification_categories), ("commitments", self.commitments),
                   ("class_schedule", self.class_schedule), ("recorded_classes", self.recorded_classes), ("current_term", self.current_term),
                   ("enrollment_documents", self.enrollment_documents), ("support_categories", self.support_categories),
                   ("messages_inbox", lambda: self.messages.inbox()), ("messages_outbox", lambda: self.messages.outbox()),
                   ("evaluative_activities", s.evaluative_activities), ("certificates", s.certificates),
                   ("academic_record", s.academic_record), ("enrollment_declaration_eligibility", s.enrollment_declaration_eligibility),
                   ("hybridity", s.hybridity), ("siaf_courses", s.siaf_courses), ("progress", s.progress),
                   ("online_libraries", s.online_libraries), ("notification_preferences", s.notification_preferences), ("request_types", s.request_types), ("request_reasons", reasons),
                   ("request_subjects", lambda: s.request_subjects(reasons()[0]["id"]) if reasons() else [])]
        return checks
