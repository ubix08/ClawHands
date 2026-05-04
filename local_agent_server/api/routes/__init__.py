"""API routes package."""

from .admin import router as admin_router
from .conversations import router as conversations_router
from .workspaces import router as workspaces_router
from .skills import router as skills_router

__all__ = [
    "admin_router",
    "conversations_router", 
    "workspaces_router",
    "skills_router",
]