"""LKML Feed Service — SDK + REST API for lore.kernel.org mailing lists via NNTP."""

from .models import FetchResult, MailEntry
from .sdk import LKMLFeedClient

__all__ = ["FetchResult", "LKMLFeedClient", "MailEntry"]
