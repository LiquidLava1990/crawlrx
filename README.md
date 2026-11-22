# CrawlRX: self-hosted AI web scraper with environment-check handling, for local LLMs

**CrawlRX is an open-source AI web scraper and web crawler that runs on your own machine
with local LLMs** (Ollama, LM Studio, vLLM, llama.cpp) or any OpenAI-compatible API. It
**handles environment checks**: it fetches with browser-grade TLS/HTTP fingerprints, detects
Cloudflare challenges, interaction challenges and other bot walls, and escalates automatically to a stealth
headless Chromium. **Challenges are solved automatically** where a solver can (an external
challenge gateway for Cloudflare, reCAPTCHA v2 through its audio challenge, or a solving service you run);
anything left over is **handed to you**, in a visible browser if you like, and the crawl
carries on by itself once it is cleared. Pages come back as clean,
LLM-ready markdown; it also searches the web through SearXNG, extracts structured JSON
with an LLM and describes screenshots with a vision model. Built for developers, AI agents
and RAG pipelines that need web data without a paid scraping service. Crawling, scraping and
search work with no model at all.

## Install: 2-minute quick start

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/LiquidLava1990/crawlrx/main/install.ps1 | iex
crawlrx serve
```

**macOS / Linux**:

```sh
curl -fsSL https://raw.githubusercontent.com/LiquidLava1990/crawlrx/main/install.sh | sh
crawlrx serve        # or ~/.local/bin/crawlrx serve if ~/.local/bin is not on your PATH yet
```

**Docker** (any OS; also starts Ollama with a small model):

```sh
git clone https://github.com/LiquidLava1990/crawlrx && cd crawlrx
docker compose --profile ollama up -d
```

Then open <http://127.0.0.1:7337/dashboard>, or try it:

```sh
curl -s http://127.0.0.1:7337/crawl -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

Already have Python 3.10+? `pipx install "git+https://github.com/LiquidLava1990/crawlrx"`
then `crawlrx setup` (this downloads Chromium and checks your model).

## Environment checks and interaction challenges

CrawlRX is built for sites that push back. What the open-source build does:

- **Browser-grade fingerprints over plain HTTP.** `/scrape` first tries HTTP clients that
  impersonate a real browser's TLS and HTTP/2 fingerprint
  ([primp](https://github.com/deedy5/primp) Chrome profiles, included in the base install;
  cloudscraper with `pip install "crawlrx[extras]"`), then plain `requests`.
- **Automatic escalation to a stealth browser.** If the HTTP response is a bot wall, or the
  page needs JavaScript, CrawlRX switches to headless Chromium with
  [playwright-stealth](https://pypi.org/project/playwright-stealth/) patches, randomized
  user agents and viewports, and human-like mouse movement. `/crawl` always uses the browser.
  A visible browser is one flag away (`crawlrx serve --headful`).
- **Bot-wall and challenge detection.** Every page is checked for Cloudflare "Just a moment"
  and browser-verification pages, Cloudflare Turnstile, reCAPTCHA, hCaptcha, DataDome and
  generic "verify you are human" walls. Blocked results are labelled
  (`error_type: "blocked"` or `"captcha_required"`, plus the challenge `kind`) instead of
  passing a challenge page off as content.
- **Retries with backoff and per-domain circuit breakers.** Failed attempts are retried
  (up to 3, with 1.5 s and 3 s waits). Domains that keep failing are paused for a while so
  you do not hammer a site that is blocking you (see `GET /circuit-breakers`).
- **Challenge hand-off to a human (the fallback).** When no solver clears a challenge, or
  solving is off, the hand-off decides what happens:
  - `CRAWLRX_CAPTCHA_HANDOFF=none` (default): the response has
    `"error_type": "captcha_required"` and a `captcha` block (`url`, `kind`, what to do),
    so your program can involve a person.
  - `CRAWLRX_CAPTCHA_HANDOFF=browser` (or `crawlrx serve --headful`): CrawlRX opens a
    visible browser window. **You** solve the challenge there, CrawlRX notices that the
    page has cleared and the crawl continues by itself. This needs a desktop session and
    waits up to `CRAWLRX_CAPTCHA_WAIT` seconds (default 180).
  - Integrations can plug in their own strategy with
    `crawlrx.captcha_handoff.set_handoff(...)`, for example to send the page to a person's
    browser.
- **Challenge solving (on by default).** CrawlRX tries every usable solver for the
  challenge it found, then falls back to the hand-off above, so nothing is lost when a
  solver fails. `CRAWLRX_CAPTCHA_MODE=handoff` turns solving off.

  | Solver | Challenges | What it needs |
  |---|---|---|
  | `flaresolverr` | Cloudflare "Just a moment" / browser checks, Turnstile interstitials | An external challenge-gateway service speaking the `CRAWLRX_FLARESOLVERR_URL` HTTP API (MIT-licensed third-party component; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)). The Docker stack runs one automatically as `challenge-gateway`; otherwise run it yourself and set `CRAWLRX_FLARESOLVERR_URL=http://localhost:8191/v1` |
  | `recaptcha-audio` | reCAPTCHA v2 checkbox, via its audio challenge ([playwright-recaptcha](https://github.com/Xewdy444/Playwright-reCAPTCHA), installed with CrawlRX) | ffmpeg on the PATH (included in the Docker image). The audio clip is transcribed by Google's free speech recognition service |
  | `service` | reCAPTCHA v2, hCaptcha, Turnstile widgets (sitekey + token) | A 2Captcha-compatible solver service you run: `CRAWLRX_SOLVER_SERVICE_URL` (and `CRAWLRX_SOLVER_SERVICE_KEY` if it wants one) |

  `crawlrx doctor` shows which solvers are usable on your machine.
  `CRAWLRX_SOLVERS=flaresolverr,service` limits which ones run. When a solver succeeds, the
  response `captcha` block records `solved_by`. CrawlRX bundles **no paid solving service
  and no API keys**. **Use it responsibly:** solving challenges can break a site's terms of
  service. Only use it where you are allowed to, and you are responsible for how you use it.

## Features: web crawling, scraping, search, LLM extraction and vision

- **Web crawler** (`POST /crawl`): render any page with headless Chromium and get markdown,
  text, links and a screenshot, ready for RAG and AI agents
- **Web scraper** (`POST /scrape`): CSS-selector extraction with an HTTP-first fast path,
  browser-fingerprint impersonation, stealth-browser fallback and retries
- **Web search API** (`POST /search`): metasearch through SearXNG, with built-in fallbacks
- **LLM data extraction** (`POST /extract`): "give me the price, title and rating as JSON",
  using your local LLM
- **Vision / screenshot understanding** (`POST /vision/analyze`): ask a vision-language model
  about a screenshot or image
- **Environment-check handling**: Cloudflare, Turnstile, reCAPTCHA, hCaptcha and DataDome
  detection, and challenge hand-off to a human (see [above](#environment-checks-and-interaction-challenges))
- **Site-specific scrapers** for e-commerce, real estate, jobs, maps, video and more, plus a
  job queue, caching and rate limiting
- **Operator dashboard** at `/dashboard` and interactive OpenAPI docs at `/docs`
- **Runs anywhere**: pip/pipx, one-line installers for Windows, macOS and Linux, or Docker
  Compose with Ollama included

## Check your setup and pick a model size

```sh
crawlrx doctor
```

```
CrawlRX doctor
========================================
RAM 16 GB | GPU NVIDIA GeForce RTX 3060 (12 GB)
Recommended model (medium): qwen2.5vl:7b -- Good quality for extraction and vision.
Found Ollama at http://localhost:11434/v1 (3 model(s))

[OK  ] browser: Playwright Chromium is installed.
[OK  ] model: http://localhost:11434/v1 [autodetect] text=qwen2.5vl:7b vision=qwen2.5vl:7b
[WARN] searxng: SearXNG not reachable at http://localhost:8888 (optional; search falls back to built-in engines).

Ready.
```

`doctor` detects Ollama and LM Studio, checks RAM and GPU memory, recommends a model
size, and prints the exact command for anything that is missing. `crawlrx doctor --save`
remembers the detected endpoint. In Docker, run `docker compose exec crawlrx crawlrx doctor`.

## Local LLM setup: Ollama, LM Studio, vLLM or any OpenAI-compatible API

CrawlRX speaks the OpenAI-compatible API, so **one setting** picks the model server:

| You use | Set `CRAWLRX_LLM_BASE_URL` to | Also set |
|---|---|---|
| Ollama | `http://localhost:11434/v1` (auto-detected) | |
| LM Studio | `http://localhost:1234/v1` (auto-detected) | |
| vLLM / llama.cpp / TGI | `http://your-host:8000/v1` | |
| OpenAI or any compatible cloud | the provider's `/v1` URL | `CRAWLRX_LLM_API_KEY` |
| Docker + Ollama on your host | `http://host.docker.internal:11434/v1` (auto-detected) | |

Optional: `CRAWLRX_LLM_MODEL` (text model), `CRAWLRX_VISION_MODEL` (vision model) and
`CRAWLRX_LLM_TIMEOUT` (seconds, default 120). Leave the model names empty and CrawlRX picks
from what the server offers. `OPENAI_BASE_URL` and `OPENAI_API_KEY` also work. Reasoning
models (qwen3, deepseek-r1 and similar) that answer with only "thinking" are retried once
with reasoning turned off; to force a level, set `CRAWLRX_LLM_REASONING_EFFORT`.

**Which model?** One multimodal model handles both extraction and vision:

| Machine | Model | Pull command |
|---|---|---|
| Under 6 GB RAM | `qwen2.5:1.5b` + `moondream` | `ollama pull qwen2.5:1.5b && ollama pull moondream` |
| Typical laptop (8–16 GB) | `qwen2.5vl:3b` (default) | `ollama pull qwen2.5vl:3b` |
| 8 GB+ GPU or 16 GB+ Apple Silicon | `qwen2.5vl:7b` | `ollama pull qwen2.5vl:7b` |
| 24 GB GPU or 48 GB+ Apple Silicon | `qwen2.5vl:32b` | `ollama pull qwen2.5vl:32b` |

`docker compose --profile ollama up -d` pulls `CRAWLRX_OLLAMA_MODEL` (default
`qwen2.5vl:3b`) automatically. For an NVIDIA GPU, uncomment the `deploy:` block in
`docker-compose.yml`.

## Examples: crawl, scrape, search, extract JSON, analyze screenshots

```sh
# Search the web
curl -s localhost:7337/search -H "Content-Type: application/json" \
  -d '{"query": "playwright python tutorial", "num_results": 5}'

# Scrape with CSS selectors
curl -s localhost:7337/scrape -H "Content-Type: application/json" \
  -d '{"url": "https://quotes.toscrape.com", "selectors": {"first_quote": ".quote .text", "author": ".quote .author"}}'

# LLM extraction to JSON
curl -s localhost:7337/extract/ -H "Content-Type: application/json" \
  -d '{"url": "https://books.toscrape.com", "prompt": "List the first 3 book titles and prices", "schema": {"type": "array"}}'

# Vision: describe an image
curl -s localhost:7337/vision/analyze -H "Content-Type: application/json" \
  -d "{\"image_base64\": \"$(base64 -w0 screenshot.png)\", \"prompt\": \"What buttons are on this page?\"}"

# One page as markdown, no server needed
crawlrx crawl https://example.com
```

Python:

```python
import httpx

r = httpx.post("http://127.0.0.1:7337/crawl", json={"url": "https://example.com"}, timeout=120)
print(r.json()["markdown"])
```

## Configuration

Everything is an environment variable. The Docker stack reads `.env`; copy `.env.example`
for the full list. The most useful ones:

| Variable | Default | Purpose |
|---|---|---|
| `CRAWLRX_LLM_BASE_URL` | auto-detect | Model server (see above) |
| `CRAWLRX_API_KEYS` | empty (no auth) | Comma-separated keys clients send as `X-API-Key` |
| `CRAWLRX_BIND` (Docker) / `--host` | `127.0.0.1` | A non-loopback bind is refused unless API keys are set (or `CRAWLRX_ALLOW_NO_AUTH=1`) |
| `CRAWLRX_CORS_ORIGINS` | empty (no browser origins) | Origins allowed to call the API from a web page or extension |
| `CRAWLRX_DATA_DIR` | `%LOCALAPPDATA%\crawlrx` or `~/.local/share/crawlrx` | Caches, logs, health files |
| `CRAWLRX_BROWSER_POOL` | `2` | Concurrent browsers |
| `CRAWLRX_CDP_URL` | `http://localhost:9222` | Attach to your own Chrome (`--remote-debugging-port=9222`) |
| `CRAWLRX_IGNORE_ROBOTS` | `false` | robots.txt is honoured by default |
| `CRAWLRX_CAPTCHA_MODE` | `solve` | Try challenge solvers before the hand-off; `handoff` turns solving off |
| `CRAWLRX_CAPTCHA_HANDOFF` | `none` | `browser` opens a visible window for you to solve challenges |
| `SEARXNG_URL` | `http://localhost:8888` | SearXNG instance for `/search` |

## Security defaults

CrawlRX binds to `127.0.0.1`, does not send CORS headers (so other websites open in your
browser cannot drive it), refuses to listen on a network address without API keys, and
never writes your model API key to disk. To use it from another machine, set
`CRAWLRX_API_KEYS` and then `CRAWLRX_BIND=0.0.0.0` (Docker) or `crawlrx serve --host 0.0.0.0`.
With keys set, everything except `/health`, `/docs` and the dashboard page needs the
`X-API-Key` header (the dashboard asks for the key once and keeps it in your browser).

CrawlRX only fetches public `http(s)` URLs. Any URL in a request body or query string that
points at loopback, private, link-local or cloud-metadata addresses is refused with
`error_type: "blocked"`, and HTTP redirects are checked hop by hop, so a public page
cannot bounce a fetch into your network.

Plain `docker run`: `docker run -e CRAWLRX_BIND=127.0.0.1 -p 127.0.0.1:7337:7337 crawlrx:local`
(build it with `docker build -t crawlrx:local .`).

## Use responsibly

Only scrape sites whose terms allow it, respect `robots.txt` (on by default) and rate
limits, and do not collect personal data you have no right to. Challenge solving is on by
default: set `CRAWLRX_CAPTCHA_MODE=handoff` wherever a site's terms do not allow automated
access. You are
responsible for how you use this software. Third-party components and their licenses are
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Development

```sh
git clone https://github.com/LiquidLava1990/crawlrx && cd crawlrx
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[test]" && python -m playwright install chromium
pytest
```

## License

[Apache-2.0](LICENSE)
