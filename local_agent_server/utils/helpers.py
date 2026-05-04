"""Utility functions for Local Agent Server."""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict


def setup_logging(level: str = "INFO") -> None:
    """Setup logging configuration."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def get_utc_now() -> datetime:
    """Get current UTC time."""
    return datetime.now(timezone.utc)


def serialize_json(obj: Any) -> str:
    """Serialize an object to JSON string."""
    return json.dumps(obj, default=str)


def deserialize_json(data: str) -> Any:
    """Deserialize a JSON string to an object."""
    return json.loads(data)


def format_timestamp(dt: datetime) -> str:
    """Format a datetime as ISO string."""
    return dt.isoformat()


def parse_timestamp(ts: str) -> datetime:
    """Parse an ISO timestamp string."""
    return datetime.fromisoformat(ts)


def mask_sensitive(text: str, visible: int = 4) -> str:
    """Mask sensitive information in a string."""
    if len(text) <= visible:
        return "*" * len(text)
    return text[:visible] + "*" * (len(text) - visible)


def truncate(text: str, max_length: int = 1000, suffix: str = "...") -> str:
    """Truncate text to max length."""
    if len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix