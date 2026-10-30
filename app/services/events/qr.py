"""Decode ticket QR codes locally; never navigate arbitrary scanned URLs."""

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

EVENT_ID = re.compile(r"evt-[A-Za-z0-9]{1,80}\Z")
KEY = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


@dataclass(frozen=True)
class TicketCode:
    event_id: str
    key: str = field(repr=False)

    @property
    def url(self):
        return f"https://luma.com/check-in/{self.event_id}?{urlencode({'pk': self.key})}"


def parse_ticket(value: str, expected_event: str) -> TicketCode:
    if len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid ticket QR code")
    try:
        url = urlsplit(value)
        params = parse_qs(url.query, keep_blank_values=True, max_num_fields=8)
        valid_host = url.hostname in {"luma.com", "lu.ma"} and url.port is None
    except ValueError:
        raise ValueError("Invalid ticket QR code") from None
    if url.scheme != "https" or not valid_host or url.username or url.password or url.fragment:
        raise ValueError("Show a Luma ticket QR code")
    event_id = url.path.removeprefix("/check-in/")
    if not url.path.startswith("/check-in/") or not EVENT_ID.fullmatch(event_id):
        raise ValueError("This is not a Luma check-in code")
    if event_id != expected_event:
        raise ValueError("This ticket is for a different event")
    keys = params.get("pk", [])
    if len(keys) != 1 or not KEY.fullmatch(keys[0]):
        raise ValueError("Invalid ticket key")
    return TicketCode(event_id, keys[0])


class QRDecoder:
    def __init__(self):
        self.detector = None

    def decode(self, frame) -> list[str]:
        import cv2

        if frame is None:
            return []
        if self.detector is None:
            self.detector = cv2.QRCodeDetector()
        # One decoder worker owns this detector. Keep the original resolution for phone screens.
        ok, values, _, _ = self.detector.detectAndDecodeMulti(frame)
        if ok:
            return list(dict.fromkeys(v for v in values if v))
        value, _, _ = self.detector.detectAndDecode(frame)
        return [value] if value else []
