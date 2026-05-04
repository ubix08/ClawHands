"""Workspace API routes for direct command execution."""

import subprocess

from fastapi import APIRouter, HTTPException

from local_agent_server.models.schemas import ExecutionResponse, ExecuteCommandRequest
from local_agent_server.services import get_conversation_manager

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


@router.post("/{conversation_id}/execute", response_model=ExecutionResponse)
async def execute_command(conversation_id: str, body: ExecuteCommandRequest):
    """Execute a command in a conversation's workspace."""
    cm = get_conversation_manager()
    conv = cm.get_conversation(conversation_id)
    
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    cwd = body.cwd or conv.workspace_dir
    
    try:
        result = subprocess.run(
            body.command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )
        
        return ExecutionResponse(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Command timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{conversation_id}/files")
async def list_files(conversation_id: str):
    """List files in a conversation's workspace."""
    cm = get_conversation_manager()
    conv = cm.get_conversation(conversation_id)
    
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    import os
    from pathlib import Path
    
    workspace = Path(conv.workspace_dir)
    files = []
    
    for root, dirs, filenames in os.walk(workspace):
        for filename in filenames:
            filepath = Path(root) / filename
            files.append({
                "relative_path": str(filepath.relative_to(workspace)),
                "size": filepath.stat().st_size,
            })
    
    return {"files": files}


@router.get("/{conversation_id}/files/{path:path}")
async def read_file(conversation_id: str, path: str):
    """Read a file from a conversation's workspace."""
    cm = get_conversation_manager()
    conv = cm.get_conversation(conversation_id)
    
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    from pathlib import Path
    
    workspace = Path(conv.workspace_dir)
    filepath = workspace / path
    
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    if not filepath.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")
    
    try:
        content = filepath.read_text()
        return {"content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))