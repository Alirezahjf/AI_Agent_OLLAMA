"""GitHub integration (OAuth redirect flow + Personal Access Token).

Exposes a synchronous client that the agent loop / actions layer use:
REST API calls (``requests``) plus local ``git`` operations.  The token is
stored in a separate JSON file (never in ``config.json``) and is fed to
``git`` through a process-local env var so it never lands in ``.git/config``.
"""

from .client import GitHubClient, GitHubError, GitHubUser, PendingOAuth

__all__ = ["GitHubClient", "GitHubError", "GitHubUser", "PendingOAuth"]
