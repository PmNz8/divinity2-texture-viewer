# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only Divinity II texture archive viewer.

The package is a thin front-end over the audited DV2 and texture codec
modules.  The runtime needs only Python and Tcl/Tk, not WebView or .NET.
"""

from .controller import TextureViewerController, ViewerController
from .model import (
    EntrySnapshot,
    MAX_TEXTURE_PAYLOAD_SIZE,
    TextureSelection,
    TextureViewerError,
    TextureViewerModel,
    VALID_VIEW_NAMES,
)

__all__ = [
    "EntrySnapshot",
    "MAX_TEXTURE_PAYLOAD_SIZE",
    "TextureSelection",
    "TextureViewerController",
    "TextureViewerError",
    "TextureViewerModel",
    "VALID_VIEW_NAMES",
    "ViewerController",
]
