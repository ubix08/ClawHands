"""GitHub integration service for Local Agent Server.

This module provides GitHub API integration using PyGithub.
It mirrors the original agent-server GitHub integration.
"""

import os
import logging
from typing import Optional, List, Dict, Any

from pydantic import SecretStr

try:
    import github
    from github import Github as PyGithub
    from github import Repository as PyGithubRepository
    from github import PullRequest as PyGithubPR
    from github import Branch as PyGithubBranch
    from github import Commit as PyGithubCommit
    from github import Issue as PyGithubIssue
    PYGithub_INSTALLED = True
except ImportError:
    PYGithub_INSTALLED = False

logger = logging.getLogger(__name__)


class GitHubServiceError(Exception):
    """Base exception for GitHub service errors."""
    pass


class GitHubAuthenticationError(GitHubServiceError):
    """Raised when GitHub authentication fails."""
    pass


class GitHubNotFoundError(GitHubServiceError):
    """Raised when a GitHub resource is not found."""
    pass


class GitHubBranchesMixin:
    """Mixin for branch operations."""
    
    def list_branches(self, repo_name: str) -> List[Dict[str, Any]]:
        """List branches in a repository."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        repo = self._get_repo(repo_name)
        return [{"name": b.name, "sha": b.commit.sha} for b in repo.get_branches()]
    
    def get_branch(self, repo_name: str, branch: str) -> Optional[Dict[str, Any]]:
        """Get a branch."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        try:
            b = self._get_repo(repo_name).get_branch(branch)
            return {"name": b.name, "sha": b.commit.sha}
        except Exception:
            return None
    
    def create_branch(self, repo_name: str, branch_name: str, from_sha: str) -> Dict[str, Any]:
        """Create a new branch."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        repo = self._get_repo(repo_name)
        ref = repo.get_git_ref(f"refs/heads/{branch_name}")
        ref.create(sha=from_sha, force=True)
        return {"name": branch_name, "from_sha": from_sha}


class GitHubPRsMixin:
    """Mixin for pull request operations."""
    
    def list_pull_requests(
        self, 
        repo_name: str, 
        state: str = "open",
        head: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """List pull requests."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        repo = self._get_repo(repo_name)
        prs = repo.get_pulls(state=state, head=head)
        return [
            {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state,
                "head_sha": pr.head.sha,
                "base": pr.base.ref,
                "head": pr.head.ref,
                "url": pr.html_url,
            }
            for pr in prs
        ]
    
    def get_pull_request(self, repo_name: str, pr_number: int) -> Optional[Dict[str, Any]]:
        """Get a pull request."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        try:
            pr = self._get_repo(repo_name).get_pull(pr_number)
            return {
                "number": pr.number,
                "title": pr.title,
                "state": pr.state,
                "body": pr.body,
                "head_sha": pr.head.sha,
                "base": pr.base.ref,
                "head": pr.head.ref,
                "url": pr.html_url,
                "diff_url": pr.diff_url,
                "commits_url": pr.commits_url,
            }
        except Exception:
            return None
    
    def create_pull_request(
        self,
        repo_name: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> Dict[str, Any]:
        """Create a pull request."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        repo = self._get_repo(repo_name)
        pr = repo.create_pull(
            title=title,
            body=body,
            head=head,
            base=base,
        )
        return {
            "number": pr.number,
            "title": pr.title,
            "url": pr.html_url,
        }
    
    def update_pull_request(
        self,
        repo_name: str,
        pr_number: int,
        title: Optional[str] = None,
        body: Optional[str] = None,
        state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update a pull request."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        repo = self._get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        
        if title is not None:
            pr.edit(title=title)
        if body is not None:
            pr.edit(body=body)
        if state is not None:
            pr.edit(state=state)
        
        return {"number": pr.number, "state": pr.state}
    
    def merge_pull_request(
        self,
        repo_name: str,
        pr_number: int,
        commit_title: Optional[str] = None,
        commit_message: Optional[str] = None,
        merge_method: str = "squash",
    ) -> Dict[str, Any]:
        """Merge a pull request."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        repo = self._get_repo(repo_name)
        pr = repo.get_pull(pr_number)
        
        if pr.is_mergeable():
            merge_commit = pr.merge(commit_title, commit_message, merge_method)
            return {
                "merged": True,
                "sha": merge_commit.sha,
                "message": merge_commit.message,
            }
        return {"merged": False}


class GitHubReposMixin:
    """Mixin for repository operations."""
    
    def list_repositories(self) -> List[Dict[str, Any]]:
        """List repositories for the authenticated user."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        g = self._get_client()
        user = g.get_user()
        return [
            {
                "name": repo.name,
                "full_name": repo.full_name,
                "private": repo.private,
                "html_url": repo.html_url,
                "default_branch": repo.default_branch,
            }
            for repo in user.get_repos()
        ]
    
    def get_repository(self, repo_name: str) -> Optional[Dict[str, Any]]:
        """Get a repository."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        try:
            repo = self._get_client().get_repo(repo_name)
            return {
                "name": repo.name,
                "full_name": repo.full_name,
                "private": repo.private,
                "html_url": repo.html_url,
                "default_branch": repo.default_branch,
                "description": repo.description,
            }
        except Exception:
            return None
    
    def create_repository(
        self,
        name: str,
        description: str = "",
        private: bool = False,
        auto_init: bool = True,
        gitignore_template: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new repository."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        g = self._get_client()
        repo = g.get_user().create_repo(
            name=name,
            description=description,
            private=private,
            auto_init=auto_init,
            gitignore_template=gitignore_template,
        )
        return {
            "name": repo.name,
            "full_name": repo.full_name,
            "html_url": repo.html_url,
        }
    
    def delete_repository(self, repo_name: str) -> bool:
        """Delete a repository."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        try:
            repo = self._get_client().get_repo(repo_name)
            repo.delete()
            return True
        except Exception:
            return False


class GitHubIssuesMixin:
    """Mixin for issue operations."""
    
    def list_issues(
        self,
        repo_name: str,
        state: str = "open",
    ) -> List[Dict[str, Any]]:
        """List issues."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        repo = self._get_repo(repo_name)
        issues = repo.get_issues(state=state)
        return [
            {
                "number": issue.number,
                "title": issue.title,
                "state": issue.state,
                "body": issue.body,
                "html_url": issue.html_url,
            }
            for issue in issues
        ]
    
    def get_issue(self, repo_name: str, issue_number: int) -> Optional[Dict[str, Any]]:
        """Get an issue."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        try:
            issue = self._get_repo(repo_name).get_issue(issue_number)
            return {
                "number": issue.number,
                "title": issue.title,
                "state": issue.state,
                "body": issue.body,
                "html_url": issue.html_url,
            }
        except Exception:
            return None
    
    def create_issue(
        self,
        repo_name: str,
        title: str,
        body: str = "",
        labels: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create an issue."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        repo = self._get_repo(repo_name)
        issue = repo.create_issue(title=title, body=body, labels=labels or [])
        return {
            "number": issue.number,
            "title": issue.title,
            "html_url": issue.html_url,
        }
    
    def update_issue(
        self,
        repo_name: str,
        issue_number: int,
        title: Optional[str] = None,
        body: Optional[str] = None,
        state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update an issue."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        repo = self._get_repo(repo_name)
        issue = repo.get_issue(issue_number)
        
        if title is not None:
            issue.edit(title=title)
        if body is not None:
            issue.edit(body=body)
        if state is not None:
            issue.edit(state=state)
        
        return {"number": issue.number, "state": issue.state}


class GitHubCommitsMixin:
    """Mixin for commit operations."""
    
    def list_commits(
        self,
        repo_name: str,
        path: Optional[str] = None,
        sha: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """List commits."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        repo = self._get_repo(repo_name)
        commits = repo.get_commits(sha=sha, path=path)
        
        result = []
        for commit in commits[:limit]:
            result.append({
                "sha": commit.sha,
                "message": commit.commit.message,
                "author": commit.commit.author.name,
                "date": commit.commit.author.date.isoformat(),
            })
        return result
    
    def get_commit(self, repo_name: str, sha: str) -> Optional[Dict[str, Any]]:
        """Get a commit."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        try:
            commit = self._get_repo(repo_name).get_commit(sha)
            return {
                "sha": commit.sha,
                "message": commit.commit.message,
                "author": commit.commit.author.name,
                "date": commit.commit.author.date.isoformat(),
            }
        except Exception:
            return None


class GitHubContentsMixin:
    """Mixin for file contents operations."""
    
    def get_file_contents(
        self,
        repo_name: str,
        path: str,
        ref: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get file contents."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        try:
            repo = self._get_repo(repo_name)
            content = repo.get_contents(path, ref=ref)
            return {
                "name": content.name,
                "path": content.path,
                "type": content.type,
                "content": content.content,
                "encoding": content.encoding,
                "sha": content.sha,
            }
        except Exception:
            return None
    
    def create_or_update_file(
        self,
        repo_name: str,
        path: str,
        content: str,
        message: str,
        branch: str = "main",
        sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or update a file."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        import base64
        repo = self._get_repo(repo_name)
        
        encoded_content = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        
        if sha:
            result = repo.update_file(
                path=path,
                content=encoded_content,
                message=message,
                branch=branch,
                sha=sha,
            )
        else:
            result = repo.create_file(
                path=path,
                content=encoded_content,
                message=message,
                branch=branch,
            )
        
        return {"path": path, "sha": result["commit"].sha}
    
    def delete_file(
        self,
        repo_name: str,
        path: str,
        message: str,
        branch: str = "main",
        sha: str,
    ) -> Dict[str, Any]:
        """Delete a file."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        repo = self._get_repo(repo_name)
        result = repo.delete_file(
            path=path,
            message=message,
            branch=branch,
            sha=sha,
        )
        return {"path": path, "sha": result["commit"].sha}


# Main GitHub Service class combining all mixins
class GitHubService(
    GitHubBranchesMixin,
    GitHubPRsMixin,
    GitHubReposMixin,
    GitHubIssuesMixin,
    GitHubCommitsMixin,
    GitHubContentsMixin,
):
    """
    Full GitHub service for Local Agent Server.
    
    Provides GitHub API operations mirroring the original agent-server.
    """
    
    BASE_URL = 'https://api.github.com'
    GRAPHQL_URL = 'https://api.github.com/graphql'
    
    def __init__(
        self,
        token: SecretStr | None = None,
        base_domain: str | None = None,
    ) -> None:
        if not PYGithub_INSTALLED:
            logger.warning("PyGithub not installed - GitHub integration disabled")
        
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            self.token = os.getenv("GITHUB_TOKEN")
        
        if base_domain and base_domain != 'github.com':
            self.BASE_URL = f'https://{base_domain}/api/v3'
            self.GRAPHQL_URL = f'https://{base_domain}/api/graphql'
        
        self._client = None
    
    def _get_client(self) -> PyGithub:
        """Get authenticated PyGithub client."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        if self._client is None:
            if not self.token:
                raise GitHubAuthenticationError("No GitHub token configured")
            self._client = PyGithub(self.token.get_secret_value())
        return self._client
    
    def _get_repo(self, repo_name: str) -> PyGithubRepository:
        """Get repository by name."""
        if not PYGithub_INSTALLED:
            raise GitHubServiceError("PyGithub not installed")
        
        return self._get_client().get_repo(repo_name)
    
    @property
    def provider(self) -> str:
        return "github"
    
    def is_configured(self) -> bool:
        """Check if GitHub integration is configured."""
        return PYGithub_INSTALLED and bool(self.token)


# Singleton instance
_github_service: Optional[GitHubService] = None


def get_github_service() -> GitHubService:
    """Get the global GitHub service instance."""
    global _github_service
    if _github_service is None:
        _github_service = GitHubService()
    return _github_service


def set_github_service(service: GitHubService) -> None:
    """Set the global GitHub service instance."""
    global _github_service
    _github_service = service