"""Image decode + preprocessing helpers for ``text_challenge``.

Pure-image utilities split out of the god-file. Each helper takes
explicit arguments (no ``self``) so they remain trivially testable
and so the orchestrator stays a thin facade.

Optional-dep flags (``PADDLE_AVAILABLE``, ``EASYOCR_AVAILABLE``,
``TESSERACT_AVAILABLE``) live here too; the orchestrator re-exports
them so the cc_harness ``public_api`` snapshot stays stable.
"""
from __future__ import annotations

import base64
import logging
import re
import ssl
import urllib.request
from typing import Any

import cv2
import numpy as np

from ..solver_base import SolverValidationError
from ..models import Task

from ._solver_helpers import BROAD_EXC


logger = logging.getLogger(__name__)


# --- Optional OCR-engine availability flags ---------------------------------

try:
    from paddleocr import PaddleOCR  # noqa: F401
    PADDLE_AVAILABLE = True
except ImportError:
    PaddleOCR = None  # type: ignore[assignment, misc] because optional dep; rebind to None for None-checks downstream.
    PADDLE_AVAILABLE = False

try:
    import easyocr  # noqa: F401
    EASYOCR_AVAILABLE = True
except (ImportError, OSError, RuntimeError) as exc:
    logger.warning("Optional EasyOCR engine could not load: %s", type(exc).__name__)
    easyocr = None  # type: ignore[assignment, misc] because optional dep; rebind to None for None-checks downstream.
    EASYOCR_AVAILABLE = False

try:
    import pytesseract  # noqa: F401
    TESSERACT_AVAILABLE = True
except ImportError:
    pytesseract = None  # type: ignore[assignment, misc] because optional dep; rebind to None for None-checks downstream.
    TESSERACT_AVAILABLE = False


# --- Image decoding ---------------------------------------------------------


def _build_unverified_ssl_context() -> ssl.SSLContext:
    """SSL ctx with verification off — opaque CAPTCHA-image fetch piped to OCR."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # cc-allow:cc.config.urllib_no_verify  captcha-image-fetch: bytes piped to OCR, host-identity not load-bearing
    return ctx


def _bytes_to_bgr(image_bytes: bytes, source_label: str) -> np.ndarray:
    """Decode raw image bytes to OpenCV BGR -- raises on decode failure."""
    nparr = np.frombuffer(image_bytes, np.uint8)
    image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if image is None:
        raise SolverValidationError(
            f"Could not decode image from {source_label}",
        )
    return image


def _decode_base64_image(base64_data: str) -> np.ndarray:
    """Decode base64 image data to OpenCV BGR array."""
    if "," in base64_data:
        base64_data = base64_data.split(",", 1)[1]
    base64_data = base64_data.strip()
    try:
        image_bytes = base64.b64decode(base64_data)
    except BROAD_EXC as exc:
        raise SolverValidationError(f"Invalid base64 data: {exc}") from exc
    return _bytes_to_bgr(image_bytes, "base64 data")


def _fetch_image_bytes(url: str) -> bytes:
    """HTTP GET ``url`` returning raw response body."""
    ssl_context = _build_unverified_ssl_context()
    req = urllib.request.Request(  # noqa: S310 — controlled URL
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36"
            ),
        },
    )
    with urllib.request.urlopen(  # noqa: S310 — see _build_unverified_ssl_context
        req, context=ssl_context, timeout=30,
    ) as response:
        return response.read()


def _download_image(url: str) -> np.ndarray:
    """Download image bytes from ``url`` and decode to OpenCV BGR array."""
    try:
        image_bytes = _fetch_image_bytes(url)
    except BROAD_EXC as exc:
        raise SolverValidationError(
            f"Failed to download image from URL: {exc}",
        ) from exc
    return _bytes_to_bgr(image_bytes, f"URL: {url}")


def _resolve_image_source(params: dict[str, Any]) -> tuple[str, str]:
    """Return ``(source, payload)`` -- source in {body, file, url}."""
    if params.get("body"):
        return "body", params["body"]
    if params.get("file"):
        return "file", params["file"]
    if params.get("image_url"):
        return "url", params["image_url"]
    if params.get("url"):
        return "url", params["url"]
    raise SolverValidationError(
        "No image data provided. Expected 'body', 'file', or "
        "'image_url' parameter",
    )


def decode_image(task: Task) -> np.ndarray:
    """Decode the image referenced by ``task.params``."""
    source, payload = _resolve_image_source(task.params)
    try:
        if source == "url":
            return _download_image(payload)
        return _decode_base64_image(payload)
    except SolverValidationError:
        raise
    except BROAD_EXC as exc:
        logger.error("Failed to decode image from %s: %s", source, exc)
        raise SolverValidationError(
            f"Failed to decode image: {exc}",
        ) from exc


# --- Preprocessing pipeline -------------------------------------------------

def _resize_if_large(
    image: np.ndarray, max_image_size: int,
) -> np.ndarray:
    """Down-scale ``image`` if its larger dim exceeds ``max_image_size``."""
    h, w = image.shape[:2]
    max_dim = max(h, w)
    if max_dim <= max_image_size:
        return image
    scale = max_image_size / max_dim
    new_w = int(w * scale)
    new_h = int(h * scale)
    logger.debug(
        "Resized image from %sx%s to %sx%s", w, h, new_w, new_h,
    )
    return cv2.resize(
        image, (new_w, new_h), interpolation=cv2.INTER_AREA,
    )


def _to_grayscale(image: np.ndarray) -> np.ndarray:
    """BGR/gray -> single-channel grayscale."""
    if len(image.shape) == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return image.copy()


def _denoise_and_enhance(gray: np.ndarray) -> np.ndarray:
    """NL-means denoise followed by CLAHE contrast enhancement."""
    denoised = cv2.fastNlMeansDenoising(gray, None, 10, 7, 21)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(denoised)


def _binarize(enhanced: np.ndarray) -> np.ndarray:
    """Adaptive Gaussian threshold + small morphological cleanup."""
    binary = cv2.adaptiveThreshold(
        enhanced, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        11, 2,
    )
    kernel = np.ones((2, 2), np.uint8)
    morph = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    return cv2.morphologyEx(morph, cv2.MORPH_CLOSE, kernel, iterations=1)


def _deskew_image(image: np.ndarray) -> np.ndarray:
    """Correct minor rotation via min-area-rect of foreground pixels."""
    try:
        coords = np.column_stack(np.where(image > 0))
        if len(coords) < 100:
            return image
        angle = cv2.minAreaRect(coords)[-1]
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
        if abs(angle) < 0.5 or abs(angle) > 45:
            return image
        (h, w) = image.shape[:2]
        center = (w // 2, h // 2)
        rotation_matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(
            image, rotation_matrix, (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,
        )
        logger.debug("Deskewed image by %.2f degrees", angle)
        return rotated
    except BROAD_EXC as exc:
        logger.debug("Deskew failed: %s", exc)
        return image  # explicit recovery: deskew is best-effort


def _remove_borders(image: np.ndarray) -> np.ndarray:
    """Crop unnecessary border whitespace using contour bounding box."""
    try:
        contours, _ = cv2.findContours(
            image, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )
        if not contours:
            return image
        all_points = np.vstack(
            [cnt for cnt in contours if cv2.contourArea(cnt) > 10],
        )
        x, y, w, h = cv2.boundingRect(all_points)
        padding = 5
        x = max(0, x - padding)
        y = max(0, y - padding)
        w = min(image.shape[1] - x, w + 2 * padding)
        h = min(image.shape[0] - y, h + 2 * padding)
        cropped = image[y:y + h, x:x + w]
        return cropped if cropped.size > 0 else image
    except BROAD_EXC as exc:
        logger.debug("Border removal failed: %s", exc)
        return image  # explicit recovery: cropping is best-effort


def preprocess_image(
    image: np.ndarray, *,
    enabled: bool, max_image_size: int,
) -> np.ndarray:
    """Apply the full preprocessing pipeline; falls back to input on error."""
    if not enabled:
        return image
    try:
        image = _resize_if_large(image, max_image_size)
        gray = _to_grayscale(image)
        enhanced = _denoise_and_enhance(gray)
        binary = _binarize(enhanced)
        deskewed = _deskew_image(binary)
        return _remove_borders(deskewed)
    except BROAD_EXC as exc:
        logger.warning("Preprocessing failed: %s, using original image", exc)
        return image  # explicit recovery: preprocessing is best-effort


# --- Post-processing --------------------------------------------------------

_CAPTCHA_KEEP_RE = re.compile(r"[^A-Z0-9]")


def post_process_text(text: str) -> str:
    """Strip whitespace, uppercase, drop non-alphanumerics."""
    if not text:
        return ""
    text = text.strip().replace(" ", "").upper()
    return _CAPTCHA_KEEP_RE.sub("", text)
