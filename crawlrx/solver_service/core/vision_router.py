"""Local OpenAI-compatible vision endpoint for image analysis."""
from __future__ import annotations

import base64
import asyncio
import logging
import math
import os
import re
from io import BytesIO
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


def _context_retry_images(images: list[bytes], response: httpx.Response,
                          output_tokens: int) -> list[bytes] | None:
    """Pack ordered frames into one labeled image on a reported context overflow."""
    if response.status_code != 400:
        return None
    data = response.json()
    error = data.get('error') if isinstance(data, dict) else None
    message = error.get('message') if isinstance(error, dict) else None
    if not isinstance(message, str):
        return None
    match = re.fullmatch(
        r"Input length \((\d+)\) exceeds model's maximum context length \((\d+)\)\.",
        message,
    )
    if match is None:
        return None
    actual, limit = map(int, match.groups())
    available = limit - output_tokens - 512
    if actual <= limit or available <= 0:
        return None
    if len(images) < 2:
        return None
    from PIL import Image, ImageDraw, ImageFont
    panels = []
    try:
        for encoded in images:
            with Image.open(BytesIO(encoded)) as image:
                if image.width * image.height > 32 * 1024 * 1024:
                    return None
                if min(image.size) < 32:
                    return None
                image.thumbnail((512, 512), Image.Resampling.LANCZOS)
                panels.append(image.convert('RGB'))
        columns = math.ceil(math.sqrt(len(panels)))
        rows = math.ceil(len(panels) / columns)
        width = max(image.width for image in panels)
        height = max(image.height for image in panels) + 28
        sheet = Image.new('RGB', (columns * width, rows * height), 'white')
        draw = ImageDraw.Draw(sheet)
        font = ImageFont.load_default(size=18)
        for index, panel in enumerate(panels):
            x, y = (index % columns) * width, (index // columns) * height
            draw.text((x + 4, y + 3), f'Frame {index + 1}', fill='black', font=font)
            sheet.paste(panel, (x, y + 28))
        output = BytesIO()
        sheet.save(output, format='PNG')
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    encoded = output.getvalue()
    return [encoded] if len(encoded) <= 4 * 1024 * 1024 else None

def _image_content(images: list[bytes], prompt: str) -> list[dict]:
    content = [{"type": "text", "text": prompt}]
    for number, image_bytes in enumerate(images, start=1):
        encoded = base64.b64encode(image_bytes).decode("ascii")
        mime = "image/png" if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
        if len(images) > 1:
            content.append({"type": "text", "text": f"Frame {number} of {len(images)}"})
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return content


async def _analyze_images(images: list[bytes], *, prompt: str, model: str | None,
                          timeout: float) -> str | None:
    if (not 1 <= len(images) <= 8 or any(not isinstance(image, bytes) or not image
                                      or len(image) > 4 * 1024 * 1024 for image in images)
            or sum(map(len, images)) > 12 * 1024 * 1024):
        logger.warning("Vision image count or size is unsupported")
        return None
    url = os.environ.get("CRAWLRX_VISION_URL", "http://10.0.0.122:18020/v1/chat/completions")
    model = model or os.environ.get("CRAWLRX_VISION_MODEL", "qwen3.8-27b")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _image_content(images, prompt)}],
        "temperature": 0,
        "max_tokens": 512 if len(images) > 1 else 256,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
            if response.status_code == 400:
                contact_sheet = await asyncio.to_thread(
                    _context_retry_images, images, response, payload['max_tokens'])
                if contact_sheet is not None:
                    logger.info('Retrying vision with a labeled contact sheet of %s ordered frames', len(images))
                    retry_prompt = (prompt + f'\nThe following image is a contact sheet of {len(images)} ordered frames. '
                                    'Panels are numbered Frame 1 onward, read left to right, top to bottom. '
                                    'Compare panels as consecutive animation frames, not as one scene.')
                    payload = {**payload, 'messages': [
                        {'role': 'user', 'content': _image_content(contact_sheet, retry_prompt)}]}
                    response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            logger.warning("Vision backend returned malformed choices")
            return None
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            logger.warning("Vision analysis did not complete")
            return None
        message = choice.get("message")
        if not isinstance(message, dict):
            logger.warning("Vision backend returned a malformed message")
            return None
        answer = message.get("content")
        return answer.strip() if isinstance(answer, str) and answer.strip() else None
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("Vision analysis unavailable: %s", type(exc).__name__)
        return None


async def analyze_bytes(image_bytes: bytes, *, prompt: str = "Describe this image in detail.",
                        model: str | None = None, timeout: float = 60.0) -> str | None:
    """Analyze one image through the configured local OpenAI-compatible endpoint."""
    return await _analyze_images([image_bytes], prompt=prompt, model=model, timeout=timeout)


async def analyze_frames(images: list[bytes], *, prompt: str,
                         model: str | None = None, timeout: float = 60.0) -> str | None:
    """Analyze two to eight ordered frames in one completed model response."""
    if not 2 <= len(images) <= 8:
        return None
    return await _analyze_images(images, prompt=prompt, model=model, timeout=timeout)


async def analyze_captcha(image_path: str, *, prompt: str = "Describe this image in detail.",
                          model: str | None = None, timeout: float = 60.0) -> str | None:
    try:
        image_bytes = Path(image_path).read_bytes()
    except OSError as exc:
        logger.warning("Cannot read vision image: %s", type(exc).__name__)
        return None
    return await analyze_bytes(image_bytes, prompt=prompt, model=model, timeout=timeout)
