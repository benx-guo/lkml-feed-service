"""Pydantic response / entry models."""

from typing import Any, List, Optional

from pydantic import BaseModel


class MailEntry(BaseModel):
    subject: str
    author: str
    email: Optional[str] = None
    url: Optional[str] = None
    message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    received_at: str
    summary: str
    subsystem: str


class FetchResult(BaseModel):
    """Batch fetch result with catch-up indicator."""

    entries: List[MailEntry] = []
    is_caught_up: bool = True


class ApiResponse(BaseModel):
    code: int = 200
    message: str = ""
    data: Any = None
