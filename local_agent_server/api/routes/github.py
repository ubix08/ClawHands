"""GitHub API routes for integration."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from local_agent_server.integrations import (
    get_github_service,
    GitHubService,
    GitHubServiceError,
    PYGithub_INSTALLED,
)

router = APIRouter(prefix="/api/github", tags=["github"])


def require_github():
    """Require GitHub integration to be available."""
    if not PYGithub_INSTALLED:
        raise HTTPException(
            status_code=501,
            detail="PyGithub not installed"
        )
    return get_github_service()


# Repository endpoints
@router.get("/repos")
async def list_repositories():
    """List repositories for the authenticated user."""
    service = require_github()
    try:
        return {"repositories": service.list_repositories()}
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/repos/{repo_name}")
async def get_repository(repo_name: str):
    """Get a repository."""
    service = require_github()
    repo = service.get_repository(repo_name)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repo


@router.post("/repos")
async def create_repository(
    name: str = Query(...),
    description: str = "",
    private: bool = False,
):
    """Create a new repository."""
    service = require_github()
    try:
        return service.create_repository(
            name=name,
            description=description,
            private=private,
        )
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/repos/{repo_name}")
async def delete_repository(repo_name: str):
    """Delete a repository."""
    service = require_github()
    success = service.delete_repository(repo_name)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to delete repository")
    return {"status": "deleted"}


# Branch endpoints
@router.get("/repos/{repo_name}/branches")
async def list_branches(repo_name: str):
    """List branches in a repository."""
    service = require_github()
    try:
        return {"branches": service.list_branches(repo_name)}
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/repos/{repo_name}/branches/{branch}")
async def get_branch(repo_name: str, branch: str):
    """Get a branch."""
    service = require_github()
    branch_data = service.get_branch(repo_name, branch)
    if not branch_data:
        raise HTTPException(status_code=404, detail="Branch not found")
    return branch_data


@router.post("/repos/{repo_name}/branches")
async def create_branch(
    repo_name: str,
    branch_name: str = Query(...),
    from_sha: str = Query(...),
):
    """Create a new branch."""
    service = require_github()
    try:
        return service.create_branch(repo_name, branch_name, from_sha)
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Pull request endpoints
@router.get("/repos/{repo_name}/pulls")
async def list_pull_requests(
    repo_name: str,
    state: str = "open",
    head: Optional[str] = None,
):
    """List pull requests."""
    service = require_github()
    try:
        return {"pulls": service.list_pull_requests(repo_name, state, head)}
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/repos/{repo_name}/pulls/{pr_number}")
async def get_pull_request(repo_name: str, pr_number: int):
    """Get a pull request."""
    service = require_github()
    pr = service.get_pull_request(repo_name, pr_number)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")
    return pr


@router.post("/repos/{repo_name}/pulls")
async def create_pull_request(
    repo_name: str,
    title: str = Query(...),
    body: str = "",
    head: str = Query(...),
    base: str = "main",
):
    """Create a pull request."""
    service = require_github()
    try:
        return service.create_pull_request(repo_name, title, body, head, base)
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/repos/{repo_name}/pulls/{pr_number}")
async def update_pull_request(
    repo_name: str,
    pr_number: int,
    title: Optional[str] = None,
    body: Optional[str] = None,
    state: Optional[str] = None,
):
    """Update a pull request."""
    service = require_github()
    try:
        return service.update_pull_request(repo_name, pr_number, title, body, state)
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/repos/{repo_name}/pulls/{pr_number}/merge")
async def merge_pull_request(
    repo_name: str,
    pr_number: int,
    commit_title: Optional[str] = None,
    commit_message: Optional[str] = None,
    merge_method: str = "squash",
):
    """Merge a pull request."""
    service = require_github()
    try:
        return service.merge_pull_request(
            repo_name, pr_number, commit_title, commit_message, merge_method
        )
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Issue endpoints
@router.get("/repos/{repo_name}/issues")
async def list_issues(repo_name: str, state: str = "open"):
    """List issues."""
    service = require_github()
    try:
        return {"issues": service.list_issues(repo_name, state)}
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/repos/{repo_name}/issues/{issue_number}")
async def get_issue(repo_name: str, issue_number: int):
    """Get an issue."""
    service = require_github()
    issue = service.get_issue(repo_name, issue_number)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    return issue


@router.post("/repos/{repo_name}/issues")
async def create_issue(
    repo_name: str,
    title: str = Query(...),
    body: str = "",
    labels: Optional[list] = None,
):
    """Create an issue."""
    service = require_github()
    try:
        return service.create_issue(repo_name, title, body, labels)
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Commit endpoints
@router.get("/repos/{repo_name}/commits")
async def list_commits(
    repo_name: str,
    path: Optional[str] = None,
    sha: Optional[str] = None,
    limit: int = 10,
):
    """List commits."""
    service = require_github()
    try:
        return {"commits": service.list_commits(repo_name, path, sha, limit)}
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/repos/{repo_name}/commits/{sha}")
async def get_commit(repo_name: str, sha: str):
    """Get a commit."""
    service = require_github()
    commit = service.get_commit(repo_name, sha)
    if not commit:
        raise HTTPException(status_code=404, detail="Commit not found")
    return commit


# File contents endpoints
@router.get("/repos/{repo_name}/contents/{path:path}")
async def get_file_contents(repo_name: str, path: str, ref: Optional[str] = None):
    """Get file contents."""
    service = require_github()
    content = service.get_file_contents(repo_name, path, ref)
    if not content:
        raise HTTPException(status_code=404, detail="File not found")
    return content


@router.put("/repos/{repo_name}/contents/{path:path}")
async def create_or_update_file(
    repo_name: str,
    path: str,
    content: str = Query(...),
    message: str = Query(...),
    branch: str = "main",
    sha: Optional[str] = None,
):
    """Create or update a file."""
    service = require_github()
    try:
        return service.create_or_update_file(
            repo_name, path, content, message, branch, sha
        )
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/repos/{repo_name}/contents/{path:path}")
async def delete_file(
    repo_name: str,
    path: str,
    message: str = Query(...),
    branch: str = "main",
    sha: str = Query(...),
):
    """Delete a file."""
    service = require_github()
    try:
        return service.delete_file(repo_name, path, message, branch, sha)
    except GitHubServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Status endpoint
@router.get("/status")
async def github_status():
    """Check GitHub integration status."""
    return {
        "installed": PYGithub_INSTALLED,
        "configured": PYGithub_INSTALLED and bool(
            get_github_service().token if PYGithub_INSTALLED else None
        ),
    }