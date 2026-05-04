"""Integrations package for Local Agent Server.

Provides integration services for GitHub, GitLab, etc.
"""

from local_agent_server.integrations.github.github_service import (
    GitHubService,
    GitHubServiceError,
    GitHubAuthenticationError,
    GitHubNotFoundError,
    get_github_service,
    set_github_service,
    PYGithub_INSTALLED,
)

__all__ = [
    # GitHub service
    "GitHubService",
    "GitHubServiceError", 
    "GitHubAuthenticationError",
    "GitHubNotFoundError",
    "get_github_service",
    "set_github_service",
    "PYGithub_INSTALLED",
]