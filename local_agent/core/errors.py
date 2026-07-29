"""Typed exceptions for the local assistant.

All user-visible errors are AssistantError subclasses so the CLI can render
them cleanly without leaking stack traces.
"""

from __future__ import annotations


class AssistantError(Exception):
    """Base class for every user-facing error in the local assistant."""


class ConfigError(AssistantError):
    """Settings could not be loaded or are invalid."""


class ActionRefused(AssistantError):
    """The agent decided (or the user decided) to refuse an action.

    This is NOT a failure. It is the normal flow when a destructive
    operation is vetoed. The CLI should render it as informational.
    """


class DependencyMissing(AssistantError):
    """A required package or system component is not installed.

    The CLI will print an actionable install command.
    """

    def __init__(self, message: str, install_hint: str = "") -> None:
        super().__init__(message)
        self.install_hint = install_hint
