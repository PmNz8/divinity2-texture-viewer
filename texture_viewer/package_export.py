# SPDX-FileCopyrightText: 2026 PmNz8
# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic texture-package construction for read-only asset viewers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Mapping

from texture_viewer import dv2lib
from texture_viewer.codec.nif_texture import (
    NIFTextureError,
    TextureResource,
    serialize_texture_resource,
)
from texture_viewer.codec.roundtrip import TextureRoundTripError, build_export_set

# DKS asset-package v1 constants; no writable Builder dependency.
ASSET_TYPE_TEXTURE_NIF = "texture_nif"
TEMPLATE_FILE_NAME = "template.nif"
TEXTURE_DIRECTORY_NAME = "texture"
ASSET_JSON_NAME = "asset.json"
ASSET_SCHEMA = "divinity2.dks_asset_package"
ASSET_SCHEMA_VERSION = 1


class PackageExportError(ValueError):
    """Raised when an authoring package cannot be constructed safely."""


@dataclass(frozen=True, slots=True)
class TexturePackageFiles:
    """Immutable, filesystem-independent texture package payload set."""

    template_logical_path: str
    target_logical_path: str
    template_sha256: str
    template_size: int
    files: Mapping[str, bytes]


def _canonical_json(document: object) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _logical_path(value: str, label: str) -> str:
    try:
        normalized = dv2lib.normalize_archive_path(value)
    except (dv2lib.DV2Error, TypeError) as error:
        raise PackageExportError(f"{label} is not a safe logical archive path") from error
    if not normalized.casefold().endswith(".nif"):
        raise PackageExportError(f"{label} must end in .nif")
    return normalized


def build_texture_asset_package_files(
    resource: TextureResource,
    template_logical_path: str,
    *,
    target_logical_path: str | None = None,
    mip_mode: str = "all",
    mip_profile: str = "raw",
) -> TexturePackageFiles:
    """Build a complete v1 package in memory without touching any archive."""

    if not isinstance(resource, TextureResource):
        raise PackageExportError("resource must be a parsed TextureResource")
    template_path = _logical_path(template_logical_path, "template logical path")
    target_path = _logical_path(
        template_path if target_logical_path is None else target_logical_path,
        "target logical path",
    )
    try:
        template_payload = serialize_texture_resource(resource)
        if mip_mode == "all":
            texture_files = build_export_set(resource)
        elif mip_mode == "base":
            from texture_viewer.codec.mip_generation import build_base_export_set
            texture_files = build_base_export_set(resource, profile=mip_profile)
        else:
            raise PackageExportError("mip mode must be all or base")
    except (NIFTextureError, TextureRoundTripError, ValueError) as error:
        raise PackageExportError(f"cannot build texture package: {error}") from error
    template_sha256 = hashlib.sha256(template_payload).hexdigest()
    if template_sha256 != resource.payload_sha256 or len(template_payload) != resource.payload_size:
        raise PackageExportError("serialized template differs from the parsed source identity")

    asset_document = {
        "schema": ASSET_SCHEMA,
        "schema_version": ASSET_SCHEMA_VERSION,
        "asset_type": ASSET_TYPE_TEXTURE_NIF,
        "template": {
            "file": TEMPLATE_FILE_NAME,
            "logical_path": template_path,
            "payload_sha256": template_sha256,
            "payload_size": len(template_payload),
        },
        "target_logical_path": target_path,
    }
    files: dict[str, bytes] = {
        ASSET_JSON_NAME: _canonical_json(asset_document),
        TEMPLATE_FILE_NAME: template_payload,
    }
    for name, payload in texture_files.items():
        files[f"{TEXTURE_DIRECTORY_NAME}/{name}"] = payload
    return TexturePackageFiles(
        template_logical_path=template_path,
        target_logical_path=target_path,
        template_sha256=template_sha256,
        template_size=len(template_payload),
        files=MappingProxyType(dict(sorted(files.items()))),
    )


__all__ = [
    "PackageExportError",
    "TexturePackageFiles",
    "build_texture_asset_package_files",
]
