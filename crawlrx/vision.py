"""vision.py -- image understanding over the configured model backend.

HTTP contract (stable; used by browser-automation clients)::

    POST /vision/analyze
        {"image_base64": "<png/jpeg/webp bytes, base64>", "prompt": "..."}
     -> {"success": true, "text": "...", "model": "...", "elapsed_ms": 812}

    GET /model/status
     -> resolved endpoint (API key redacted) + whether a model answers

Python API: ``await analyze_image(image_bytes, prompt)``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import model_backend

logger = logging.getLogger("crawlrx.vision")
router = APIRouter(tags=["vision"])

_MAX_IMAGE_BYTES = 20 * 1024 * 1024


class VisionRequest(BaseModel):
    image_base64: str = Field(..., description="Image bytes (PNG/JPEG/WEBP/GIF), base64-encoded. "
                                               "A data: URL prefix is accepted.")
    prompt: str = Field(default="Describe this image in detail.", description="Question about the image")
    model: Optional[str] = Field(default=None, description="Override the vision model id")
    max_tokens: int = Field(default=1000, ge=1, le=8000)


class VisionResponse(BaseModel):
    success: bool
    text: str = ""
    model: str = ""
    elapsed_ms: int = 0
    error: Optional[str] = None


def decode_image(image_base64: str) -> bytes:
    """Decode base64 (optionally a ``data:`` URL); raises ``ValueError``."""
    data = image_base64.strip()
    if data.startswith("data:") and "," in data:
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"image_base64 is not valid base64: {exc}") from exc
    if not raw:
        raise ValueError("image_base64 decoded to zero bytes")
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ValueError(f"image is larger than {_MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    return raw


async def analyze_image(image_bytes: bytes, prompt: str = "Describe this image in detail.",
                        model: str = "", max_tokens: int = 1000) -> VisionResponse:
    """Describe an image; never raises -- failures come back as ``success=False``."""
    started = time.perf_counter()
    try:
        text, used = await model_backend.describe_image(image_bytes, prompt, model=model, max_tokens=max_tokens)
    except model_backend.ModelBackendUnavailable as exc:
        return VisionResponse(success=False, error=str(exc))
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300] if exc.response is not None else ""
        return VisionResponse(success=False, error=f"Model server returned {exc.response.status_code}: {detail}. "
                                                  "Check that the vision model supports images.")
    except ValueError as exc:
        return VisionResponse(success=False, error=str(exc))
    elapsed = int((time.perf_counter() - started) * 1000)
    return VisionResponse(success=bool(text), text=text, model=used, elapsed_ms=elapsed,
                          error=None if text else "Model returned an empty answer.")


@router.post("/vision/analyze", response_model=VisionResponse, summary="Describe or answer a question about an image")
async def vision_analyze(req: VisionRequest) -> VisionResponse:
    try:
        image = decode_image(req.image_base64)
    except ValueError as exc:
        return VisionResponse(success=False, error=str(exc))
    return await analyze_image(image, req.prompt, model=req.model or "", max_tokens=req.max_tokens)


@router.get("/model/status", summary="Which model endpoint CrawlRX is using")
async def model_status() -> dict:
    settings = await asyncio.to_thread(model_backend.resolve_settings, True)
    if not settings.configured:
        return {"configured": False, "hint": model_backend.setup_hint()}
    try:
        resolved = await asyncio.to_thread(model_backend.require_settings)
    except model_backend.ModelBackendUnavailable as exc:
        return {"configured": True, "ready": False, "settings": settings.public_dict(), "error": str(exc)}
    return {"configured": True, "ready": True, "settings": resolved.public_dict()}


__all__ = ["VisionRequest", "VisionResponse", "analyze_image", "decode_image", "router"]
