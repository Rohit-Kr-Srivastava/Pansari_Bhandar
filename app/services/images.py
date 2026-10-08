"""Product image uploads.

Every upload is opened with Pillow, checked, resized and re-encoded to WebP under a random
name. The original bytes are never stored or served, so a file pretending to be an image
(HTML, SVG, scripts, polyglots) cannot reach a browser.
"""

from __future__ import annotations

import io
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_PIXELS = 40_000_000
LARGE = 1200
SMALL = 400
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageError(ValueError):
    pass


def small_path(path: str) -> str:
    return path.replace(".webp", "_sm.webp")


def _safe_target(media_root: Path, rel: str) -> Path:
    root = media_root.resolve()
    target = (root / rel).resolve()
    if root not in target.parents:
        raise ImageError("Invalid image path.")
    return target


def save_upload(data: bytes, media_root: Path, max_bytes: int, folder: str = "products") -> str:
    """Validate and store an image. Returns the stored relative path (large version)."""
    if not data:
        raise ImageError("The file is empty.")
    if len(data) > max_bytes:
        raise ImageError(f"Images must be smaller than {max_bytes // (1024 * 1024)} MB.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            width, height = probe.size  # read from the header, before any decoding
            probe.verify()
        if fmt not in ALLOWED_FORMATS:
            raise ImageError("Upload a JPG, PNG or WebP image.")
        if width * height > MAX_PIXELS:
            raise ImageError("The image is too large. Please use a photo under 40 megapixels.")
        opened = Image.open(io.BytesIO(data))
        if getattr(opened, "n_frames", 1) > 1:
            raise ImageError("Animated images are not supported.")
        img: Image.Image = ImageOps.exif_transpose(opened) or opened
        img.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise ImageError("This file is not a valid image.") from exc
    if img.width < 50 or img.height < 50:
        raise ImageError("The image is too small (at least 50 x 50 pixels).")
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")

    now = datetime.now(UTC)
    rel = f"{folder}/{now:%Y}/{now:%m}/{secrets.token_hex(12)}.webp"
    target = _safe_target(media_root, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    large = img.copy()
    large.thumbnail((LARGE, LARGE))
    large.save(target, "WEBP", quality=82, method=4)  # metadata (EXIF, GPS) is not copied
    small = img.copy()
    small.thumbnail((SMALL, SMALL))
    small.save(_safe_target(media_root, small_path(rel)), "WEBP", quality=78, method=4)
    return rel


def copy_image(rel: str, media_root: Path, folder: str = "products") -> str | None:
    """Duplicate stored files for a cloned product, so deleting one never breaks the other."""
    try:
        src = _safe_target(media_root, rel)
    except ImageError:
        return None
    if not src.exists():
        return None
    now = datetime.now(UTC)
    new_rel = f"{folder}/{now:%Y}/{now:%m}/{secrets.token_hex(12)}.webp"
    dst = _safe_target(media_root, new_rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    src_sm = _safe_target(media_root, small_path(rel))
    if src_sm.exists():
        shutil.copyfile(src_sm, _safe_target(media_root, small_path(new_rel)))
    return new_rel


def delete_files(rel: str, media_root: Path) -> None:
    for p in (rel, small_path(rel)):
        try:
            _safe_target(media_root, p).unlink(missing_ok=True)
        except (ImageError, OSError):
            pass
