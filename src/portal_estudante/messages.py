"""Direct messages (legacy "Comunidade" message center, ASP.NET WebForms, server-rendered HTML).

How it works:
- Send: `POST api.ulife.com.br/Message/SendMessage` (form, jQuery-style): Recipients[i][EntityID]=<UserOrganizationId>,
  Recipients[i][Type]=1 (user; 2 = user list), Subject, Body (HTML), UserRecipientCount → `PortalClient.send_direct_message`.
- Sent box: `www/Community/Outbox.aspx`; received box: `www/Community/Inbox.aspx` (same row markup).
  Row: `div.msg-list__item#divMessage{id}`, `aria-label="Mensagem de {name}: {subject}"`, checkbox attrs IsRead/MessageId/UserId,
  link `MessageDetail.aspx?MsgId=&BoxType=`, relative time, thread size "(n)".
  Read state (Inbox): UNREAD rows have class `inboxmessageactive`
  and IsRead="1"; READ rows have class `inboxmessage` and IsRead="2" (the attribute name is misleading).
  In the Outbox a just-sent message shows IsRead="0" (meaning not established; kept raw in `read_flag`).
- Thread: `www/Community/MessageDetail.aspx?MsgId=&BoxType=`; each message `.messagecollapse` (sender, full date in title,
  Para/Assunto/Data, body in an iframe served by staticcdns3 `/PAT/TinyMCEPages/CommunityMessage/{id}_*.html`).
- Opening a thread is a server-side GET that may mark a RECEIVED message as read, so reading an unread inbox thread
  requires `allow_mark_read=True`. Mark read/unread/delete are WebForms postbacks (writes) and are not implemented.
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from .contracts import ContractIssue
from .errors import PageStructureChangedError, WriteBlockedError

if TYPE_CHECKING:
    from .client import PortalClient

WWW = "https://www.ulife.com.br"
ROW_RE = re.compile(r'<div class="msg-list__item(?P<cls>[^"]*)" id="divMessage(?P<id>\d+)"[^>]*aria-label="(?P<label>[^"]*)"(?P<body>.*?)(?=<div class="msg-list__item|<div class="msg-list-footer|</form>)', re.S)
DETAIL_LINK_RE = re.compile(r'href="(?P<href>[^"]*MessageDetail\.aspx\?MsgId=[^"]+)"')
TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(TAG_RE.sub(" ", fragment))).strip()


@dataclass
class MessageSummary:
    message_id: int
    box: str  # "inbox" | "outbox"
    counterpart: str  # sender (inbox) or recipient (outbox)
    subject: str
    is_read: bool
    read_flag: str  # raw IsRead attribute ("1" unread, "2" read in the Inbox; "0" seen in the Outbox)
    when_text: str
    thread_size: int
    detail_url: str


@dataclass
class ThreadMessage:
    sender: str
    date: str
    to: list[str] = field(default_factory=list)
    subject: str = ""
    body_html: str = ""
    body_url: str = ""

    @property
    def body_text(self) -> str:
        return _text(self.body_html)


def parse_box(page_html: str, box: str) -> list[MessageSummary]:
    """Parse Inbox.aspx / Outbox.aspx HTML into summaries (raises PageStructureChangedError if markers vanish)."""
    if 'class="msg-list"' not in page_html:
        raise PageStructureChangedError(f"GET www/Community/{box.title()}.aspx",
                                        [ContractIssue("div.msg-list", "message list container", "absent")], hint="messages.parse_box")
    out: list[MessageSummary] = []
    for m in ROW_RE.finditer(page_html):
        body = m.group("body")
        label = htmllib.unescape(m.group("label"))
        name, _, subject = label.removeprefix("Mensagem de ").partition(": ")
        read = re.search(r'IsRead="(\d)"', body)
        link = DETAIL_LINK_RE.search(body)
        when = re.search(r"(Há [^<]{1,40})", body)
        size = re.search(r"\((\d+)\)", _text(body))
        if not (read and link):
            raise PageStructureChangedError(f"GET www/Community/{box.title()}.aspx",
                                            [ContractIssue(f"divMessage{m.group('id')}", "IsRead + MessageDetail link", "absent")],
                                            hint="messages.parse_box")
        unread = "inboxmessageactive" in m.group("cls") or read.group(1) == "1"
        out.append(MessageSummary(int(m.group("id")), box, name.strip(), subject.strip(), not unread, read.group(1),
                                  when.group(1).strip() if when else "", int(size.group(1)) if size else 1,
                                  urljoin(f"{WWW}/Community/", htmllib.unescape(link.group("href")))))
    return out


def extract_body(page_html: str) -> str:
    """Inner <body> of the static TinyMCE page, without scripts/noscript/styles (the CDN page embeds analytics)."""
    m = re.search(r"<body[^>]*>(.*)</body>", page_html, re.S | re.I)
    inner = m.group(1) if m else page_html
    return re.sub(r"<(script|noscript|style)\b.*?</\1\s*>", "", inner, flags=re.S | re.I).strip()


def parse_thread(page_html: str) -> list[ThreadMessage]:
    blocks = page_html.split('<div class="messagecollapse">')[1:]
    if not blocks:
        raise PageStructureChangedError("GET www/Community/MessageDetail.aspx",
                                        [ContractIssue(".messagecollapse", "message block", "absent")], hint="messages.parse_thread")
    out = []
    for b in blocks:
        sender = re.search(r"<h1>\s*<a[^>]*>(.*?)</a>", b, re.S)
        date = re.search(r"<span title='([^']+)'", b)
        to = re.findall(r'rptTargetUsers_ctl\d+_uphTarget">(.*?)</a>', b)
        subject = re.search(r"<label>Assunto:</label>\s*<span>(.*?)</span>", b, re.S)
        iframe = re.search(r'<iframe src="([^"]+CommunityMessage[^"]+)"', b)
        out.append(ThreadMessage(_text(sender.group(1)) if sender else "", htmllib.unescape(date.group(1)) if date else "",
                                 [_text(x) for x in to], _text(subject.group(1)) if subject else "", "", iframe.group(1) if iframe else ""))
    return out


class Messages:
    """Read access to the message center. Writes: see PortalClient.send_direct_message."""

    def __init__(self, client: PortalClient) -> None:
        self.client = client

    def _box(self, box: str) -> list[MessageSummary]:
        page = "Inbox.aspx" if box == "inbox" else "Outbox.aspx"
        resp = self.client.http.request("GET", f"{WWW}/Community/{page}")
        return parse_box(resp.text, box)

    def inbox(self) -> list[MessageSummary]:
        return self._box("inbox")

    def outbox(self) -> list[MessageSummary]:
        return self._box("outbox")

    def thread(self, summary: MessageSummary, *, allow_mark_read: bool = False, with_bodies: bool = True) -> list[ThreadMessage]:
        """Open a conversation. An UNREAD inbox thread is blocked unless allow_mark_read=True (opening may mark it read)."""
        if summary.box == "inbox" and not summary.is_read and not allow_mark_read:
            raise WriteBlockedError("open_unread_message", {"message_id": summary.message_id, "reason": "opening may mark it as read"})
        resp = self.client.http.request("GET", summary.detail_url)
        msgs = parse_thread(resp.text)
        if with_bodies:
            for m in msgs:
                if m.body_url:
                    m.body_html = extract_body(self.client.http.request("GET", m.body_url).text)
        return msgs
