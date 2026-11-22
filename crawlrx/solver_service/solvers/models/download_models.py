"""Fetch inference weights used by the challenge-solving service.

Primary weights (crawlrx-detr-v1.onnx) are published as GitHub release assets
of this repository; auxiliary grid-classification weights come from the
hcaptcha-challenger project releases (see THIRD_PARTY_NOTICES.md and
crawlrx/model_weights/PROVENANCE.md). Every download is SHA256-verified.
"""
from __future__ import annotations

import hashlib
import os
import ssl
import urllib.error
import urllib.request

try:
    from ...core.sentinel_errors import ErrCode
except ImportError:  # standalone use before the package is importable
    class _E:
        def __getattr__(self: object, _: object) -> object:
            return "ERR_UNKNOWN"
    ErrCode = _E()

_CRAWLRX_RELEASE_BASE = (
    'https://github.com/LiquidLava1990/crawlrx/releases/download'
)
_CHALLENGER_VERSIONS = ('v0.10.1', 'v0.10.0')

# (filename, sha256, candidate URLs). First URL whose download matches the
# hash wins; a mismatched file is deleted and the next candidate is tried.
MODELS: list[tuple[str, str, tuple[str, ...]]] = [
    (
        'crawlrx-detr-v1.onnx',
        '37f4bc4e749f7f071848ab0b1fc329cf260150b7f9f1fe445a59c0097b3424f3',
        (
            f'{_CRAWLRX_RELEASE_BASE}/weights-v1/crawlrx-detr-v1.onnx',
        ),
    ),
    (
        'yolov8n.onnx',
        '',  # upstream may re-cut releases; size+source checked, hash optional
        tuple(
            url.format(ver=ver)
            for ver in _CHALLENGER_VERSIONS
            for url in (
                'https://github.com/QIN2DIM/hcaptcha-challenger/releases/download/{ver}/yolov8n.onnx',
                'https://huggingface.co/QIN2DIM/hcaptcha-challenger/resolve/main/yolov8n.onnx',
            )
        ) + (
            'https://raw.githubusercontent.com/QIN2DIM/hcaptcha-challenger/main/models/yolov8n.onnx',
        ),
    ),
    (
        'resnet.onnx',
        '',
        tuple(
            url.format(ver=ver)
            for ver in _CHALLENGER_VERSIONS
            for url in (
                'https://github.com/QIN2DIM/hcaptcha-challenger/releases/download/{ver}/resnet.onnx',
                'https://huggingface.co/QIN2DIM/hcaptcha-challenger/resolve/main/resnet.onnx',
            )
        ) + (
            'https://raw.githubusercontent.com/QIN2DIM/hcaptcha-challenger/main/models/resnet.onnx',
        ),
    ),
]

_USER_AGENT = 'Mozilla/5.0'
_TIMEOUT_S = 10
_MIN_BYTES = 1 << 20  # hash-less entries must still be at least 1 MiB


def target_dir() -> str:
    """Directory verified downloads are written to (env-overridable)."""
    default = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'model_weights')
    return os.environ.get('CRAWLRX_MODEL_WEIGHTS_DIR') or os.path.normpath(default)


def _sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _url_is_present(url: str, ctx: ssl.SSLContext) -> bool:
    req = urllib.request.Request(url, method='HEAD')
    req.add_header('User-Agent', _USER_AGENT)
    with urllib.request.urlopen(req, context=ctx, timeout=_TIMEOUT_S) as resp:
        return resp.status == 200


def _download(url: str, dest: str, ctx: ssl.SSLContext) -> None:
    req = urllib.request.Request(url)
    req.add_header('User-Agent', _USER_AGENT)
    tmp = dest + '.part'
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp, open(tmp, 'wb') as fh:
            for chunk in iter(lambda: resp.read(1 << 20), b''):
                fh.write(chunk)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)  # never leave partial files behind
        raise


def _fetch(model_name: str, expected_sha256: str, urls: tuple[str, ...]) -> bool:
    ctx = ssl.create_default_context()
    dest = os.path.join(target_dir(), model_name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        if not expected_sha256 or _sha256_of(dest) == expected_sha256:
            print(f'\n{model_name}: already present and verified at {dest}')
            return True
        print(f'\n{model_name}: present but stale (hash mismatch); re-fetching')
        os.remove(dest)
    print(f'Looking for {model_name}...')
    for url in urls:
        try:
            if not _url_is_present(url, ctx):
                continue
            print(f'  FOUND: {url}')
            print('  Downloading...')
            _download(url, dest, ctx)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ssl.SSLError, ValueError) as exc:
            print(f'  X {url[:60]}... - {str(exc)[:50]}')
            continue
        size = os.path.getsize(dest)
        if expected_sha256 and _sha256_of(dest) != expected_sha256:
            print('  X SHA256 mismatch; deleting and trying next mirror')
            os.remove(dest)
            continue
        if not expected_sha256 and size < _MIN_BYTES:
            print(f'  X truncated download ({size} bytes); deleting and trying next mirror')
            os.remove(dest)
            continue
        note = ', SHA256 verified' if expected_sha256 else ', size OK'
        print(f'  Saved: {dest} ({size} bytes{note})')
        return True
    print(f'  !! {model_name}: no mirror succeeded')
    return False


def main() -> None:
    results = {name: _fetch(name, sha, urls) for name, sha, urls in MODELS}
    missing = [name for name, ok in results.items() if not ok]
    if missing:
        raise SystemExit(
            f'missing weights: {", ".join(missing)} '
            f'(set CRAWLRX_MODEL_WEIGHTS_DIR={target_dir()} and rerun with network access)'
        )


if __name__ == '__main__':
    main()
