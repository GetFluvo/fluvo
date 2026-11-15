"""Tests for the optional image-resize transform (issue #315).

The module must collect without Pillow: the ``images`` extra is optional, so
anything that needs it is guarded by ``needs_pillow`` and generates its test
image in-process (no committed binary fixtures). The opt-out path and the
missing-Pillow error are verified without needing Pillow at all.
"""

import base64
import io
from pathlib import Path
from typing import Any

import pytest

from fluvo.lib.mapper import (
    ImageResize,
    _encode_image_bytes,
    _resize_image_bytes,
    binary,
    binary_url_to_base64,
    path_to_image,
)

try:
    from PIL import Image

    PILLOW = True
except ImportError:  # pragma: no cover - exercised only in a Pillow-less env
    PILLOW = False

needs_pillow = pytest.mark.skipif(
    not PILLOW, reason="Pillow (the fluvo[images] extra) is not installed"
)


def _png_bytes(
    width: int, height: int, mode: str = "RGB", color: tuple[int, ...] | None = None
) -> bytes:
    """Encode a solid test image as PNG bytes (Pillow required)."""
    img = Image.new(mode, (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Opt-out path: no Pillow needed, output must be byte-identical to today.
# --------------------------------------------------------------------------- #


def test_no_resize_is_byte_identical() -> None:
    """resize=None encodes the raw bytes exactly as a plain base64 would."""
    raw = b"\x00\x01\x02 arbitrary bytes, not an image \xff\xfe"
    assert _encode_image_bytes(raw, None) == base64.b64encode(raw).decode("utf-8")


def test_path_to_image_default_does_not_resize(tmp_path: Path) -> None:
    """The path mapper without a resize config returns today's output."""
    raw = b"\x89PNG\r\n\x1a\n not a real image but stable bytes"
    (tmp_path / "pic.bin").write_bytes(raw)
    out = path_to_image("img", path=str(tmp_path))({"img": "pic.bin"}, {})
    assert out == base64.b64encode(raw).decode("utf-8")


def test_binary_default_does_not_resize(tmp_path: Path) -> None:
    """The local-file binary mapper without a resize config is unchanged."""
    raw = b"some bytes \x00\x10"
    (tmp_path / "f.bin").write_bytes(raw)
    out = binary("img", path_prefix=str(tmp_path))({"img": "f.bin"}, {})
    assert out == base64.b64encode(raw).decode("utf-8")


# --------------------------------------------------------------------------- #
# Missing-Pillow behaviour: simulated by forcing the import to fail, so this
# runs and asserts the actionable error regardless of whether Pillow is present.
# --------------------------------------------------------------------------- #


def test_resize_without_pillow_raises_actionable_error(monkeypatch: Any) -> None:
    """When Pillow is absent, the resize path raises a clear install hint."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("No module named 'PIL'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match=r"fluvo\[images\]"):
        _resize_image_bytes(b"anything", ImageResize())


def test_encode_without_pillow_still_works_when_not_resizing(monkeypatch: Any) -> None:
    """The opt-out path never imports Pillow, so it works without it."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("No module named 'PIL'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert _encode_image_bytes(b"hi", None) == base64.b64encode(b"hi").decode("utf-8")


# --------------------------------------------------------------------------- #
# Resize behaviour (needs Pillow).
# --------------------------------------------------------------------------- #


@needs_pillow
def test_large_image_is_resized_to_bound_as_jpeg() -> None:
    """A 3000x1500 image is shrunk to <=1920px JPEG, aspect preserved."""
    raw = _png_bytes(3000, 1500, color=(10, 20, 30))
    out = _resize_image_bytes(raw, ImageResize(max_dimension=1920, quality=85))
    assert out != raw
    with Image.open(io.BytesIO(out)) as result:
        assert result.format == "JPEG"
        assert max(result.size) <= 1920
        assert result.size == (1920, 960)  # aspect ratio preserved


@needs_pillow
def test_small_image_is_passed_through_untouched() -> None:
    """An image already within the bound is not re-encoded (byte-identical)."""
    raw = _png_bytes(800, 600, color=(1, 2, 3))
    out = _resize_image_bytes(raw, ImageResize(max_dimension=1920))
    assert out == raw


@needs_pillow
def test_rgba_is_flattened_to_rgb_jpeg() -> None:
    """A transparent RGBA image converts to RGB before the JPEG encode."""
    raw = _png_bytes(2500, 2500, mode="RGBA", color=(255, 0, 0, 128))
    out = _resize_image_bytes(raw, ImageResize())
    with Image.open(io.BytesIO(out)) as result:
        assert result.mode == "RGB"
        assert result.format == "JPEG"
        assert max(result.size) <= 1920


@needs_pillow
def test_palette_image_is_handled() -> None:
    """A palette ('P' mode) image re-encodes without error."""
    raw = _png_bytes(2200, 2200, mode="P")
    out = _resize_image_bytes(raw, ImageResize())
    with Image.open(io.BytesIO(out)) as result:
        assert result.mode == "RGB"
        assert max(result.size) <= 1920


@needs_pillow
def test_exif_orientation_applied_before_measuring() -> None:
    """Orientation is honoured first, so the correct side is bounded."""
    img = Image.new("RGB", (3000, 1000), (5, 5, 5))
    exif = img.getexif()
    exif[274] = 6  # Orientation: rotate 90 CW -> stored 3000x1000 displays 1000x3000
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    out = _resize_image_bytes(buf.getvalue(), ImageResize(max_dimension=1920))
    with Image.open(io.BytesIO(out)) as result:
        assert max(result.size) <= 1920
        # After transpose the image is portrait: height is the long side.
        assert result.size[1] > result.size[0]


@needs_pillow
def test_non_image_bytes_degrade_to_passthrough() -> None:
    """Undecodable bytes are returned unchanged rather than corrupted."""
    raw = b"this is definitely not an image payload"
    assert _resize_image_bytes(raw, ImageResize()) == raw


@needs_pillow
def test_mapper_resizes_through_the_seam(tmp_path: Path) -> None:
    """path_to_image with a resize config actually shrinks the stored image."""
    raw = _png_bytes(4000, 2000, color=(7, 8, 9))
    (tmp_path / "big.png").write_bytes(raw)
    mapper_fn = path_to_image("img", path=str(tmp_path), resize=ImageResize())
    out_b64 = mapper_fn({"img": "big.png"}, {})
    assert out_b64 is not None
    decoded = base64.b64decode(out_b64)
    assert decoded != raw
    with Image.open(io.BytesIO(decoded)) as result:
        assert result.format == "JPEG"
        assert max(result.size) <= 1920


@needs_pillow
def test_url_mapper_resizes(monkeypatch: Any) -> None:
    """binary_url_to_base64 applies the resize to the downloaded bytes."""
    raw = _png_bytes(3000, 3000, color=(9, 9, 9))

    class _Resp:
        content = raw

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr("fluvo.lib.mapper.httpx.get", lambda *a, **k: _Resp())
    out = binary_url_to_base64("img", resize=ImageResize())(
        {"img": "http://x/y.png"}, {}
    )
    assert out is not None
    with Image.open(io.BytesIO(base64.b64decode(out))) as result:
        assert result.format == "JPEG"
        assert max(result.size) <= 1920
