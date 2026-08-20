"""Secure GitHub App integration.

The package deliberately separates OAuth, credential storage, API operations,
local Git, and LLM action registration.  Access/refresh tokens are opaque to
all callers outside this package and are never serialized into app settings.
"""

from .service import GitHubService

__all__ = ["GitHubService"]
