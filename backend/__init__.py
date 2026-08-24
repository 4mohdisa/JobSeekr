"""JobSeekr — local job discovery, scoring and auto-application."""

from __future__ import annotations

# Kept in step with [project].version in pyproject.toml by hand. The project has
# no build-system table (uv runs it from source, it is never installed as a
# distribution), so importlib.metadata cannot see a version at runtime.
__version__ = "0.1.0"

__all__ = ["__version__"]
