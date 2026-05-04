"""Workspace manager service - Manages workspace directories."""

import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

import logging

logger = logging.getLogger(__name__)


class WorkspaceManager:
    """Manages workspace directories for conversations.
    
    Creates and manages workspace directories where agents work.
    """
    
    def __init__(self, base_dir: Optional[str] = None):
        """Initialize the workspace manager.
        
        Args:
            base_dir: Base directory for workspaces. Defaults to ~/agent-workspaces
        """
        self.base_dir = Path(base_dir or os.path.expanduser("~/agent-workspaces"))
        self.base_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Workspace manager initialized: {self.base_dir}")
    
    def create_workspace(self, conversation_id: str) -> str:
        """Create a new workspace directory for a conversation.
        
        Args:
            conversation_id: ID of the conversation
            
        Returns:
            Path to the created workspace directory
        """
        workspace = self.base_dir / conversation_id
        workspace.mkdir(parents=True, exist_ok=True)
        
        # Create a .gitkeep to preserve the directory in git
        (workspace / ".gitkeep").touch()
        
        logger.info(f"Created workspace: {workspace}")
        return str(workspace)
    
    def get_workspace(self, conversation_id: str) -> Optional[str]:
        """Get the workspace directory for a conversation.
        
        Args:
            conversation_id: ID of the conversation
            
        Returns:
            Path to the workspace directory, or None if it doesn't exist
        """
        workspace = self.base_dir / conversation_id
        return str(workspace) if workspace.exists() else None
    
    def delete_workspace(self, conversation_id: str) -> None:
        """Delete a workspace directory.
        
        Args:
            conversation_id: ID of the conversation
        """
        workspace = self.base_dir / conversation_id
        if workspace.exists():
            shutil.rmtree(workspace)
            logger.info(f"Deleted workspace: {workspace}")
    
    def list_workspaces(self) -> list[str]:
        """List all workspace directories.
        
        Returns:
            List of conversation IDs with workspaces
        """
        if not self.base_dir.exists():
            return []
        return [d.name for d in self.base_dir.iterdir() if d.is_dir()]
    
    def get_workspace_size(self, conversation_id: str) -> int:
        """Get the size of a workspace directory in bytes.
        
        Args:
            conversation_id: ID of the conversation
            
        Returns:
            Size in bytes
        """
        workspace = self.base_dir / conversation_id
        if not workspace.exists():
            return 0
        
        total = 0
        for path in workspace.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total


# Factory function for dependency injection
_workspace_manager: Optional[WorkspaceManager] = None


def get_workspace_manager() -> WorkspaceManager:
    """Get the global workspace manager instance."""
    global _workspace_manager
    if _workspace_manager is None:
        _workspace_manager = WorkspaceManager()
    return _workspace_manager


def set_workspace_manager(manager: WorkspaceManager) -> None:
    """Set the global workspace manager instance."""
    global _workspace_manager
    _workspace_manager = manager