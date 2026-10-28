"""Official Python client for the LassoCut background-removal API."""
from ._client import (
    DEFAULT_API_URL,
    PRESETS,
    Balance,
    LassoCut,
    LassoCutError,
    LassoCutWarning,
    Result,
)
from ._version import __version__

__all__ = ["LassoCut", "LassoCutError", "LassoCutWarning", "Result", "Balance", "PRESETS", "DEFAULT_API_URL",
           "__version__"]
