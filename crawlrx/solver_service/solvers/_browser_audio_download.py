"""Fetch recordings through their owning browser context and persist uniquely."""
from __future__ import annotations

import asyncio
import base64
import os
import tempfile
from typing import Union
from urllib.parse import urlsplit

from playwright.async_api import Frame, Page


async def fetch_browser_audio(frame: Union[Frame, Page], url: str) -> bytes:
    """Resolve document bases; retain context cookies or document-owned blobs."""
    resolved = await frame.evaluate('(url) => new URL(url, document.baseURI).href', url)
    scheme = urlsplit(resolved).scheme
    if scheme in {'http', 'https'}:
        page = frame if isinstance(frame, Page) else frame.page
        headers = {'Referer': frame.url} if frame.url.startswith(('http:', 'https:')) else {}
        response = await page.request.get(resolved, headers=headers, timeout=60000)
        try:
            if response.status != 200:
                raise RuntimeError(f'Audio download returned HTTP {response.status}')
            return await response.body()
        finally:
            await response.dispose()
    if scheme not in {'blob', 'data'}:
        raise ValueError('Unsupported audio URL scheme')
    async with asyncio.timeout(60):
        encoded = await frame.evaluate('''async (url) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 60000);
            try {
                const response = await fetch(url, {signal: controller.signal});
                if (!response.ok) throw new Error('Audio download failed');
                const data = new Uint8Array(await response.arrayBuffer());
                const parts = [];
                for (let i=0;i<data.length;i+=8192)
                    parts.push(String.fromCharCode(...data.subarray(i,i+8192)));
                return btoa(parts.join(''));
            } finally { clearTimeout(timer); }
        }''', resolved)
    return base64.b64decode(encoded, validate=True)


def write_audio_temp(data: bytes, prefix: str) -> str:
    """Leave complete recordings for the decoder; remove partial writes."""
    path = None
    completed = False
    try:
        with tempfile.NamedTemporaryFile(prefix=prefix, suffix='.mp3', delete=False) as output:
            path = output.name
            output.write(data)
        completed = True
        return path
    finally:
        if path is not None and not completed:
            os.unlink(path)
