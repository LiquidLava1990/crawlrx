# Third-party notices

CrawlRX is licensed under Apache-2.0 (see `LICENSE`). It installs third-party projects as
Python dependencies or runs them as separate containers, and each keeps its own license,
which ships with the package or image it comes in. One component is distributed as patched
source (botright, below, legacy-lane only); everything else is used unmodified.

## Challenge-solving components (on by default; `CRAWLRX_CAPTCHA_MODE=handoff` turns them off)

| Component | How CrawlRX uses it | Project | License |
|---|---|---|---|
| FlareSolverr | Separate container in the Docker stack, called over its HTTP API | <https://github.com/FlareSolverr/FlareSolverr> | MIT |
| playwright-recaptcha | Dependency, reCAPTCHA v2 audio path | <https://github.com/Xewdy444/Playwright-reCAPTCHA> | MIT |
| SpeechRecognition | Pulled in by playwright-recaptcha to transcribe the audio challenge | <https://github.com/Uberi/speech_recognition> | BSD-3-Clause |
| pydub | Pulled in by playwright-recaptcha for audio conversion (needs ffmpeg) | <https://github.com/jiaaro/pydub> | MIT |
| standard-aifc, standard-chunk, audioop-lts | Python 3.13+ backports of removed stdlib modules SpeechRecognition/pydub need | <https://pypi.org/project/standard-aifc/> | PSF-2.0 |
| FFmpeg | System package in the Docker image | <https://ffmpeg.org/legal.html> | LGPL-2.1+ (Debian/Ubuntu build) |
| botright | Legacy engine lane only (opt-in via `CRAWLRX_STEALTH_ENGINE=botright`); `crawlrx/solver_service/botright_patches/` contains GPL-derived modifications of botright 0.5.1 shipped as source. Not installed by the wheel or the default Docker image | <https://github.com/Vinyzu/Botright> | GPL-3.0 |
| Ultralytics YOLO | Segmentation weights (`yolov8m-seg.pt`, an Ultralytics YOLOv8m-seg export) and the ultralytics runtime used for image-grid classification | <https://github.com/ultralytics/ultralytics> | AGPL-3.0 |
| onnxruntime | ONNX inference runtime for the YOLO models used by the challenge solvers | <https://github.com/microsoft/onnxruntime> | MIT |
| DrissionPage | Dependency, browser-automation path | <https://github.com/g1879/DrissionPage> | MIT |
| hcaptcha-challenger | Dependency, hCaptcha image-challenge classification; also the upstream distributor of the `yolov8n.onnx` weights | <https://github.com/QIN2DIM/hcaptcha-challenger> | GPL-3.0-or-later |

The solver-service client (`crawlrx/solvers/solver_service.py`) is original code that speaks
the widely used 2Captcha-style HTTP API. The CrawlRX wheel and default Docker stack do not
supply an API key for any external service.

The stealth engine is original CrawlRX code (`crawlrx/solver_service/_stealth_engine/`):
it forges coherent Chromium identities with browserforge (Apache-2.0) plus CrawlRX's own
fingerprint pools, drives playwright persistent contexts, and attaches CrawlRX's own
hCaptcha/reCAPTCHA bridges. No botright code is required; the
`solver_service/botright_patches/` directory remains only as an optional compatibility
layer for the legacy lane (``CRAWLRX_STEALTH_ENGINE=botright``) and is inert by default.

The remaining challenge-solver dependencies (aiohttp, curl_cffi, tls-client, camoufox,
pydoll-python, easyocr, paddleocr, faster-whisper, hrequests and others in the
`crawlrx[solver]` extra) are installed unmodified and keep their upstream licenses, which
ship inside each wheel. Model weights are recorded in `crawlrx/model_weights/PROVENANCE.md`.

## Core dependencies with environment-check features

| Component | Use | Project | License |
|---|---|---|---|
| Playwright | Browser automation (Chromium) | <https://github.com/microsoft/playwright-python> | Apache-2.0 |
| playwright-stealth | Stealth patches for headless Chromium | <https://github.com/Mattwmaster58/playwright_stealth> | MIT |
| primp | Browser-impersonating HTTP client (via ddgs) | <https://github.com/deedy5/primp> | MIT |
| cloudscraper | Optional HTTP tier (`crawlrx[extras]`) | <https://github.com/VeNoMouS/cloudscraper> | MIT |
| browserforge | Browser fingerprint/header generation | <https://github.com/daijro/browserforge> | Apache-2.0 |

Licenses were checked against each project's repository or PyPI metadata in November 2026.
