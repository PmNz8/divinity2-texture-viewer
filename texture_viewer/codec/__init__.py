# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Dependency-free BC/PNG texture codec and NIF texture-resource parser.

The package deliberately contains no knowledge of DV2 archives, game paths, or
any GUI.  It is the small read-only foundation shared by future front-ends.
"""

from .bc import (
    BCCodecError,
    FORMAT_BC1,
    FORMAT_BC2,
    FORMAT_BC3,
    block_size,
    decode_mip,
    encode_bc1_binary_alpha,
    encode_bc1_opaque,
    encode_bc3,
    encode_mip,
    mip_size,
)
from .nif_texture import (
    EXPECTED_NIF_USER_VERSION,
    EXPECTED_NIF_VERSION,
    NIFTextureError,
    SUPPORTED_TEXTURE_USER_VERSIONS,
    TextureMip,
    TextureResource,
    parse_texture_resource,
    serialize_texture_resource,
)
from .png import (
    PNGError,
    PNGImage,
    decode_png,
    encode_png_gray,
    encode_png_rgb,
    encode_png_rgba,
)
from .roundtrip import (
    ChannelStatistics,
    SIDECAR_NAME,
    SCHEMA,
    SCHEMA_VERSION,
    TextureMipViews,
    TextureRoundTripError,
    build_export_set,
    decode_mip_views,
    export_texture_set,
    import_export_set,
    import_texture_set,
)

__all__ = [
    "BCCodecError",
    "ChannelStatistics",
    "EXPECTED_NIF_USER_VERSION",
    "EXPECTED_NIF_VERSION",
    "FORMAT_BC1",
    "FORMAT_BC2",
    "FORMAT_BC3",
    "NIFTextureError",
    "PNGError",
    "PNGImage",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SIDECAR_NAME",
    "SUPPORTED_TEXTURE_USER_VERSIONS",
    "TextureMip",
    "TextureResource",
    "TextureMipViews",
    "TextureRoundTripError",
    "block_size",
    "decode_mip",
    "decode_png",
    "decode_mip_views",
    "encode_bc1_binary_alpha",
    "encode_bc1_opaque",
    "encode_bc3",
    "encode_mip",
    "encode_png_gray",
    "encode_png_rgb",
    "encode_png_rgba",
    "build_export_set",
    "export_texture_set",
    "import_export_set",
    "import_texture_set",
    "mip_size",
    "parse_texture_resource",
    "serialize_texture_resource",
]
