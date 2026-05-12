"""This module contains enums for eve_mcp."""

from enum import Enum


class EventType(Enum):
    """Event type from EVE MCP."""

    TOKEN = "token"
    SOURCE = "source"
    ERROR = "error"
    FINAL = "final"
