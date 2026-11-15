"""CSV Data converter.

This module contains functions for converting data, such as image paths
or URLs to base64 strings, for use in Odoo imports.
"""

import base64

import polars as pl

from .lib import mapper
from .lib.mapper import ImageResize
from .lib.transform import Processor
from .logging_config import log


def _build_resize(max_dimension: int | None, quality: int) -> ImageResize | None:
    """Build an :class:`ImageResize` from CLI-style knobs, or None when off.

    Args:
        max_dimension: The longest-side bound in pixels, or None to disable
            resizing (the default, so output is unchanged for existing callers).
        quality: The JPEG quality used when an image is re-encoded.

    Returns:
        ImageResize | None: An :class:`ImageResize` when ``max_dimension`` is
        set, else ``None``.
    """
    if max_dimension is None:
        return None
    return ImageResize(max_dimension=max_dimension, quality=quality)


def to_base64(filepath: str) -> str:
    """Reads a local file and returns its base64 encoded content."""
    try:
        with open(filepath, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except FileNotFoundError:
        log.warning(f"File not found at '{filepath}', skipping.")
        return ""  # Return empty string if file is not found


def run_path_to_image(
    file: str,
    fields: str,
    out: str,
    path: str,
    delimiter: str = ";",
    max_dimension: int | None = None,
    quality: int = 85,
) -> None:
    """Path to image.

    Converts local file paths in specified columns into base64 encoded strings.
    When ``max_dimension`` is set, each image is down-scaled to that bound and
    re-encoded as JPEG before encoding (#315); left as None, images are encoded
    unchanged.
    """
    log.info(f"Starting path-to-image conversion for file: {file}")
    resize = _build_resize(max_dimension, quality)
    o2o_mapping = Processor(
        source_filename=file, mapping={}, separator=delimiter
    ).get_o2o_mapping()

    # Create the final mapping, overriding the image path fields
    mapping = {
        **o2o_mapping,
        **{
            f: mapper.path_to_image(f, path=path, resize=resize)
            for f in fields.split(",")
        },
    }

    processor = Processor(
        mapping=mapping,
        source_filename=file,
        separator=delimiter,
    )

    result_df = processor.process(filename_out=out)
    cast_expressions = [
        pl.col(c.name).map_elements(
            lambda x: str(x) if x is not None else "", return_dtype=pl.String
        )
        for c in result_df
        if c.dtype == pl.Object
    ]
    if cast_expressions:
        result_df = result_df.with_columns(cast_expressions)

    result_df.write_csv(out, separator=delimiter)


def run_url_to_image(
    file: str,
    fields: str,
    out: str,
    delimiter: str = ";",
    b64: bool = False,
    max_dimension: int | None = None,
    quality: int = 85,
) -> None:
    """URL to image.

    Downloads images from URLs in specified columns and converts them to base64.
    When ``max_dimension`` is set, each image is down-scaled to that bound and
    re-encoded as JPEG before encoding (#315); left as None, images are encoded
    unchanged.
    """
    log.info(f"Starting URL-to-image conversion for file: {file}")
    resize = _build_resize(max_dimension, quality)
    o2o_mapping = Processor(
        mapping={}, source_filename=file, separator=delimiter
    ).get_o2o_mapping()
    mapping = {
        **o2o_mapping,
        **{f: mapper.binary_url_to_base64(f, resize=resize) for f in fields.split(",")},
    }

    # FIX: Initialize processor with the correct mapping
    processor = Processor(
        mapping=mapping,
        source_filename=file,
        separator=delimiter,
    )

    # FIX: Get the resulting DataFrame and write it directly to a CSV
    result_df = processor.process(filename_out=out)

    cast_expressions = [
        pl.col(c.name).map_elements(
            lambda x: str(x) if x is not None else "", return_dtype=pl.String
        )
        for c in result_df
        if c.dtype == pl.Object
    ]
    if cast_expressions:
        result_df = result_df.with_columns(cast_expressions)

    result_df.write_csv(out, separator=delimiter)
