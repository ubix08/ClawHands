"""Core configuration for Local Agent Server."""

import os
from typing import Optional

# Server configuration
SERVER_NAME = "local-agent-server"
SERVER_VERSION = "1.0.0"

# API configuration
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))

# LLM configuration
DEFAULT_MODEL = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4-5-20250929")
LLM_BASE_URL = os.getenv("LLM_BASE_URL")  # Optional custom endpoint

# Agent configuration
DEFAULT_AGENT_TYPE = os.getenv("DEFAULT_AGENT_TYPE", "default")
ENABLE_BROWSER = os.getenv("ENABLE_BROWSER", "true").lower() == "true"

# Workspace configuration
WORKSPACE_BASE_DIR = os.getenv("WORKSPACE_BASE_DIR", os.path.expanduser("~/agent-workspaces"))

# Security configuration
API_KEY_HEADER = "Authorization"
API_KEY_PREFIX = "Bearer"

# CORS configuration
CORS_ORIGINS = ["*"]
CORS_CREDENTIALS = True
CORS_METHODS = ["*"]
CORS_HEADERS = ["*"]


class Settings:
    """Server settings class."""
    
    def __init__(self):
        self.name = SERVER_NAME
        self.version = SERVER_VERSION
        self.host = API_HOST
        self.port = API_PORT
        self.default_model = DEFAULT_MODEL
        self.llm_base_url = LLM_BASE_URL
        self.default_agent_type = DEFAULT_AGENT_TYPE
        self.enable_browser = ENABLE_BROWSER
        self.workspace_base_dir = WORKSPACE_BASE_DIR
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "host": self.host,
            "port": self.port,
            "default_model": self.default_model,
            "llm_base_url": self.llm_base_url,
            "default_agent_type": self.default_agent_type,
            "enable_browser": self.enable_browser,
            "workspace_base_dir": self.workspace_base_dir,
        }


# Global settings instance
settings = Settings()


def get_settings() -> Settings:
    """Get the global settings instance."""
    return settings