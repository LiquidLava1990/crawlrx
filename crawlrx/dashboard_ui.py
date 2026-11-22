"""CrawlRX operator dashboard HTML."""
from __future__ import annotations

OPERATOR_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CrawlRX Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --page: #f5f7fa;
      --surface: #ffffff;
      --surface-soft: #f9fbfd;
      --ink: #101828;
      --muted: #5d6675;
      --quiet: #7c8797;
      --line: #d9e0ea;
      --line-strong: #b9c3d1;
      --accent: #0f766e;
      --accent-soft: #dff7f3;
      --blue: #2563eb;
      --blue-soft: #e8f0ff;
      --green: #15803d;
      --green-soft: #e5f8eb;
      --amber: #b7791f;
      --amber-soft: #fff4d8;
      --red: #b91c1c;
      --red-soft: #fde8e8;
      --radius: 8px;
      --shadow: 0 14px 36px rgba(16, 24, 40, 0.08);
    }

    * { box-sizing: border-box; }

    html { min-width: 320px; }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--page);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 16px;
      letter-spacing: 0;
    }

    a { color: inherit; }

    .shell {
      width: min(1280px, calc(100% - 32px));
      margin: 0 auto;
      padding: 22px 0 40px;
    }

    .topbar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 16px;
      align-items: center;
      margin-bottom: 14px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 13px;
      min-width: 0;
    }

    .mark {
      width: 44px;
      height: 44px;
      border-radius: var(--radius);
      display: grid;
      place-items: center;
      background: #102a43;
      color: #ffffff;
      font-weight: 900;
      letter-spacing: 0;
      flex: 0 0 auto;
    }

    h1 {
      margin: 0;
      font-size: 1.55rem;
      line-height: 1.1;
      letter-spacing: 0;
    }

    .subtitle {
      margin-top: 4px;
      color: var(--muted);
      font-size: 0.94rem;
    }

    .toolbar {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
    }

    .button,
    button {
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface);
      color: var(--ink);
      padding: 0 13px;
      font: inherit;
      font-weight: 750;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      white-space: nowrap;
      transition: background 160ms ease, border-color 160ms ease, transform 160ms ease;
    }

    button:disabled {
      cursor: not-allowed;
      opacity: 0.62;
    }

    .button:hover,
    button:hover:not(:disabled) {
      border-color: var(--line-strong);
      background: #f7f9fc;
      transform: translateY(-1px);
    }

    .button:focus-visible,
    button:focus-visible,
    input:focus-visible,
    select:focus-visible,
    textarea:focus-visible,
    summary:focus-visible {
      outline: 3px solid rgba(37, 99, 235, 0.28);
      outline-offset: 2px;
    }

    .button.primary,
    button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #ffffff;
    }

    .button.primary:hover,
    button.primary:hover:not(:disabled) {
      background: #0d625c;
      border-color: #0d625c;
    }

    svg {
      width: 18px;
      height: 18px;
      flex: 0 0 auto;
    }

    .hero {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 18px;
      margin-bottom: 14px;
    }

    .hero-line {
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      align-items: center;
      gap: 14px;
      margin-bottom: 14px;
    }

    .big-status {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      min-height: 44px;
      padding: 0 14px;
      border-radius: var(--radius);
      font-weight: 900;
      text-transform: uppercase;
      border: 1px solid var(--line);
      background: var(--surface-soft);
    }

    .big-status.ok { color: var(--green); background: var(--green-soft); border-color: #b7e9c5; }
    .big-status.warn { color: var(--amber); background: var(--amber-soft); border-color: #f1d48b; }
    .big-status.down { color: var(--red); background: var(--red-soft); border-color: #f4b4b4; }
    .big-status.unknown { color: var(--muted); }

    .hero-title {
      min-width: 0;
      font-size: 1.15rem;
      font-weight: 850;
      overflow-wrap: anywhere;
    }

    .hero-meta {
      margin-top: 3px;
      color: var(--muted);
      font-size: 0.92rem;
    }

    .proof-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }

    .proof-card,
    .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }

    .proof-card {
      min-height: 96px;
      padding: 14px;
      display: grid;
      align-content: space-between;
      gap: 12px;
    }

    .proof-head,
    .panel-head,
    .result-head,
    .service-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      min-width: 0;
    }

    .proof-name,
    .service-name {
      font-weight: 850;
      min-width: 0;
      overflow-wrap: anywhere;
    }

    .proof-detail,
    .service-detail {
      color: var(--muted);
      line-height: 1.42;
      overflow-wrap: anywhere;
    }

    .proof-detail {
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .meta-row {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      color: var(--quiet);
      font-size: 0.82rem;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 30px;
      padding: 0 10px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--surface-soft);
      font-size: 0.79rem;
      font-weight: 850;
      text-transform: uppercase;
      flex: 0 0 auto;
    }

    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--quiet);
      flex: 0 0 auto;
    }

    .ok .dot { background: var(--green); }
    .warn .dot { background: var(--amber); }
    .down .dot,
    .fail .dot { background: var(--red); }

    .pill.ok { color: var(--green); border-color: #b7e9c5; background: var(--green-soft); }
    .pill.warn { color: var(--amber); border-color: #f1d48b; background: var(--amber-soft); }
    .pill.down,
    .pill.fail { color: var(--red); border-color: #f4b4b4; background: var(--red-soft); }

    .workspace {
      display: grid;
      grid-template-columns: minmax(340px, 0.82fr) minmax(0, 1.18fr);
      gap: 14px;
      align-items: start;
      margin-bottom: 14px;
    }

    .panel { padding: 16px; }

    .panel-head { margin-bottom: 14px; }

    h2 {
      margin: 0;
      font-size: 1rem;
      line-height: 1.2;
      font-weight: 850;
      letter-spacing: 0;
    }

    .small {
      color: var(--quiet);
      font-size: 0.88rem;
      white-space: nowrap;
    }

    .mode-row {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 14px;
    }

    .mode-button {
      background: var(--surface-soft);
      color: var(--muted);
    }

    .mode-button.active {
      background: var(--accent-soft);
      border-color: #93d8cf;
      color: #0f4f4a;
    }

    .field {
      display: grid;
      gap: 7px;
      margin-bottom: 12px;
    }

    label,
    .label {
      color: var(--muted);
      font-size: 0.88rem;
      font-weight: 780;
    }

    input,
    select,
    textarea {
      width: 100%;
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: #ffffff;
      color: var(--ink);
      padding: 10px 12px;
      font: inherit;
    }

    textarea {
      min-height: 140px;
      resize: vertical;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.88rem;
      line-height: 1.5;
    }

    .form-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 128px;
      gap: 10px;
    }

    details.advanced {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface-soft);
      margin: 0 0 12px;
    }

    details.advanced summary {
      min-height: 44px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 12px;
      cursor: pointer;
      font-weight: 800;
      color: var(--muted);
      list-style: none;
    }

    details.advanced summary::-webkit-details-marker { display: none; }

    .advanced-body {
      padding: 0 12px 12px;
      display: grid;
      gap: 10px;
    }

    .search-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }

    .search-grid.two {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .check-row {
      min-height: 44px;
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      font-size: 0.9rem;
      font-weight: 760;
      margin-bottom: 0;
    }

    .check-row input {
      width: 18px;
      min-height: 18px;
      accent-color: var(--accent);
    }

    .hidden { display: none !important; }

    .quick-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }

    .quick-row button {
      min-height: 38px;
      padding: 0 11px;
      font-size: 0.88rem;
      color: var(--muted);
      background: var(--surface-soft);
    }

    .result-head {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface-soft);
      padding: 12px;
      margin-bottom: 12px;
    }

    .result-title { font-weight: 850; }

    .result-meta {
      color: var(--muted);
      font-size: 0.9rem;
      margin-top: 2px;
      overflow-wrap: anywhere;
    }

    .result-visual {
      display: grid;
      gap: 10px;
      margin-bottom: 12px;
    }

    .summary-row,
    .route-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }

    .summary-chip,
    .route-chip {
      min-height: 32px;
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 0 10px;
      background: var(--surface-soft);
      color: var(--muted);
      font-size: 0.82rem;
      font-weight: 800;
    }

    .route-chip.ok { border-color: #b7e9c5; color: var(--green); background: var(--green-soft); }
    .route-chip.warn,
    .route-chip.skipped { border-color: #f1d48b; color: var(--amber); background: var(--amber-soft); }
    .route-chip.fail { border-color: #f4b4b4; color: var(--red); background: var(--red-soft); }

    .result-list {
      display: grid;
      gap: 8px;
    }

    .result-card {
      display: grid;
      gap: 7px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface);
    }

    .result-card a {
      color: var(--blue);
      text-decoration: none;
      font-weight: 850;
      overflow-wrap: anywhere;
    }

    .result-card a:hover { text-decoration: underline; }

    .result-snippet {
      color: var(--muted);
      line-height: 1.45;
    }

    .result-card-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--quiet);
      font-size: 0.82rem;
    }

    pre {
      margin: 0;
      min-height: 300px;
      max-height: 58vh;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: #111827;
      color: #edf2f7;
      padding: 14px;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.88rem;
      line-height: 1.55;
    }

    .details-stack {
      display: grid;
      gap: 10px;
    }

    .drawer {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .drawer summary {
      min-height: 52px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 0 16px;
      cursor: pointer;
      list-style: none;
      font-weight: 850;
    }

    .drawer summary::-webkit-details-marker { display: none; }

    .drawer-body {
      padding: 0 16px 16px;
      overflow-x: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 0.9rem;
    }

    th,
    td {
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }

    th {
      color: var(--muted);
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-weight: 900;
    }

    td {
      color: var(--ink);
      overflow-wrap: anywhere;
    }

    .service-list {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }

    .service-card {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface-soft);
      padding: 12px;
      display: grid;
      gap: 9px;
    }

    .mono {
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      font-size: 0.84rem;
    }

    .truncate {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .empty {
      color: var(--quiet);
      padding: 18px 0;
      text-align: center;
    }

    .toast {
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 20;
      max-width: min(420px, calc(100vw - 36px));
      border: 1px solid var(--line);
      background: var(--surface);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 13px 14px;
      color: var(--ink);
      display: none;
    }

    .toast.show { display: block; }

    @media (max-width: 1120px) {
      .proof-grid { grid-template-columns: 1fr; }
      .workspace { grid-template-columns: 1fr; }
      .service-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }

    @media (max-width: 760px) {
      .shell { width: min(100% - 20px, 1280px); padding-top: 12px; }
      .topbar { grid-template-columns: 1fr; }
      .toolbar { justify-content: flex-start; }
      .toolbar .button,
      .toolbar button { flex: 1 1 136px; }
      .hero-line { grid-template-columns: 1fr; }
      .hero { padding: 14px; }
      .proof-card { min-height: 74px; padding: 12px; gap: 6px; }
      .proof-card .meta-row { display: none; }
      .proof-detail { font-size: 0.92rem; }
      .form-grid,
      .search-grid,
      .search-grid.two,
      .service-list { grid-template-columns: 1fr; }
      .mode-row { grid-template-columns: 1fr; }
      h1 { font-size: 1.38rem; }
      pre { min-height: 240px; }
      table { min-width: 720px; }
    }

    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        scroll-behavior: auto !important;
        transition-duration: 0.01ms !important;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="topbar" aria-label="Dashboard header">
      <div class="brand">
        <div class="mark" aria-hidden="true">CRX</div>
        <div>
          <h1>CrawlRX</h1>
          <div class="subtitle" id="subtitle">Loading</div>
        </div>
      </div>
      <nav class="toolbar" aria-label="Quick links">
        <a class="button" href="/docs">
          <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/></svg>
          Docs
        </a>
        <a class="button" href="http://localhost:8888" target="_blank" rel="noreferrer">
          <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
          SearXNG
        </a>
        <button class="primary" id="refreshButton" type="button">
          <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 0 1-15.5 6.3"/><path d="M3 12A9 9 0 0 1 18.5 5.7"/><path d="M18 3v5h-5"/><path d="M6 21v-5h5"/></svg>
          Refresh
        </button>
      </nav>
    </section>

    <section class="hero" aria-label="System overview">
      <div class="hero-line">
        <div class="big-status unknown" id="overallBadge"><span class="dot"></span>Loading</div>
        <div>
          <div class="hero-title" id="heroTitle">Checking CrawlRX</div>
          <div class="hero-meta" id="heroMeta">Waiting for dashboard status</div>
        </div>
      </div>
      <div class="proof-grid" id="proofGrid" aria-label="Primary proofs"></div>
    </section>

    <section class="workspace">
      <div class="panel">
        <div class="panel-head">
          <h2>Run</h2>
          <span class="small" id="modeHint">Crawl</span>
        </div>
        <div class="mode-row" role="group" aria-label="Operation mode">
          <button class="mode-button active" type="button" data-mode="crawl">Crawl</button>
          <button class="mode-button" type="button" data-mode="scrape">Scrape</button>
          <button class="mode-button" type="button" data-mode="search">Search</button>
        </div>

        <div class="field">
          <label id="targetLabel" for="targetInput">URL</label>
          <input id="targetInput" autocomplete="off" spellcheck="false" value="https://example.com">
        </div>

        <div class="form-grid">
          <div class="field" id="selectorField">
            <label for="selectorInput">Selector</label>
            <input id="selectorInput" autocomplete="off" spellcheck="false" value="h1">
          </div>
          <div class="field">
            <label for="timeoutInput">Timeout</label>
            <select id="timeoutInput">
              <option value="10000">10s</option>
              <option value="15000" selected>15s</option>
              <option value="30000">30s</option>
            </select>
          </div>
        </div>

        <details class="advanced hidden" id="searchOptions">
          <summary>
            Search options
            <span class="small" id="searchOptionHint">Auto</span>
          </summary>
          <div class="advanced-body">
            <div class="search-grid">
              <div class="field">
                <label for="searchKindInput">Type</label>
                <select id="searchKindInput">
                  <option value="auto" selected>Auto</option>
                  <option value="web">Web</option>
                  <option value="fusion">Fusion</option>
                  <option value="images">Images</option>
                  <option value="news">News</option>
                  <option value="videos">Videos</option>
                  <option value="deep">Deep</option>
                  <option value="google-news">Google News</option>
                  <option value="local">Local</option>
                  <option value="ecommerce">Ecommerce</option>
                </select>
              </div>
              <div class="field" id="searchEngineField">
                <label for="searchEngineInput">Engine</label>
                <select id="searchEngineInput">
                  <option value="duckduckgo" selected>DuckDuckGo First</option>
                  <option value="bing">Bing</option>
                  <option value="google">Google</option>
                  <option value="searxng">SearXNG</option>
                </select>
              </div>
              <div class="field">
                <label for="resultCountInput">Results</label>
                <select id="resultCountInput">
                  <option value="3">3</option>
                  <option value="5" selected>5</option>
                  <option value="10">10</option>
                  <option value="20">20</option>
                </select>
              </div>
            </div>
            <div class="search-grid" id="webOptions">
              <div class="field">
                <label for="languageInput">Language</label>
                <input id="languageInput" autocomplete="off" spellcheck="false" placeholder="en">
              </div>
              <div class="field">
                <label for="countryInput">Country</label>
                <input id="countryInput" autocomplete="off" spellcheck="false" placeholder="US">
              </div>
              <label class="check-row" for="includeContentInput">
                <input id="includeContentInput" type="checkbox">
                Include content
              </label>
            </div>
            <div class="search-grid hidden" id="fusionOptions">
              <div class="field">
                <label for="fusionSourcesInput">Sources</label>
                <select id="fusionSourcesInput">
                  <option value="ddg,bing" selected>DDG + Bing</option>
                  <option value="ddg">DDG</option>
                  <option value="bing">Bing</option>
                </select>
              </div>
              <div class="field">
                <label for="minScoreInput">Min Score</label>
                <select id="minScoreInput">
                  <option value="0" selected>Any</option>
                  <option value="0.2">0.2</option>
                  <option value="0.4">0.4</option>
                  <option value="0.6">0.6</option>
                </select>
              </div>
              <label class="check-row" for="dedupeInput">
                <input id="dedupeInput" type="checkbox" checked>
                Dedupe
              </label>
            </div>
            <div class="search-grid two hidden" id="deepOptions">
              <div class="field">
                <label for="siteFilterInput">Site</label>
                <input id="siteFilterInput" autocomplete="off" spellcheck="false" placeholder="example.com">
              </div>
              <div class="field">
                <label for="fileTypeInput">File Type</label>
                <select id="fileTypeInput">
                  <option value="" selected>Any</option>
                  <option value="pdf">PDF</option>
                  <option value="doc">DOC</option>
                  <option value="csv">CSV</option>
                  <option value="xls">XLS</option>
                </select>
              </div>
              <div class="field">
                <label for="exactPhraseInput">Exact Phrase</label>
                <input id="exactPhraseInput" autocomplete="off" spellcheck="false">
              </div>
              <div class="field">
                <label for="excludeTermsInput">Exclude</label>
                <input id="excludeTermsInput" autocomplete="off" spellcheck="false" placeholder="ads, spam">
              </div>
            </div>
            <div class="field hidden" id="localOptions">
              <label for="zipInput">ZIP</label>
              <input id="zipInput" autocomplete="off" inputmode="numeric" spellcheck="false" placeholder="10001">
            </div>
          </div>
        </details>

        <button class="primary" id="runButton" type="button">Run Crawl</button>
        <div class="quick-row" aria-label="Examples">
          <button type="button" data-example-mode="crawl" data-example-value="https://example.com">Example</button>
          <button type="button" data-example-mode="scrape" data-example-value="https://example.com" data-example-selector="h1">Scrape H1</button>
          <button type="button" data-example-mode="search" data-example-value="crawlrx health" data-example-kind="auto">Search</button>
          <button type="button" data-example-mode="search" data-example-value="crawlrx logo" data-example-kind="images">Images</button>
          <button type="button" data-example-mode="search" data-example-value="wireless headphones deals" data-example-kind="ecommerce">Products</button>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          <h2>Result</h2>
          <span class="small" id="resultClock">Idle</span>
        </div>
        <div class="result-head">
          <div>
            <div class="result-title" id="resultTitle">Ready</div>
            <div class="result-meta" id="resultMeta">No request yet</div>
          </div>
          <button type="button" id="copyButton">
            <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
            Copy
          </button>
        </div>
        <div class="result-visual hidden" id="resultVisual"></div>
        <pre id="resultOutput">{}</pre>
      </div>
    </section>

    <section class="details-stack" aria-label="Diagnostic details">
      <details class="drawer">
        <summary>
          Services
          <span class="small" id="serviceMeta">Loading</span>
        </summary>
        <div class="drawer-body">
          <div class="service-list" id="serviceList"></div>
        </div>
      </details>

      <details class="drawer">
        <summary>
          Activity
          <span class="small" id="activityMeta">No requests</span>
        </summary>
        <div class="drawer-body" id="activityTable"></div>
      </details>

      <details class="drawer">
        <summary>
          External Signals
          <span class="small" id="externalMeta">No upstream checks</span>
        </summary>
        <div class="drawer-body" id="externalTable"></div>
      </details>

      <details class="drawer">
        <summary>
          Circuit Breakers
          <span class="small" id="circuitMeta">No active breakers</span>
        </summary>
        <div class="drawer-body" id="circuitTable"></div>
      </details>
    </section>
  </main>

  <div class="toast" id="toast" role="status" aria-live="polite"></div>

  <script>
    const state = { mode: 'crawl', lastStatus: null };
    const subtitle = document.getElementById('subtitle');
    const overallBadge = document.getElementById('overallBadge');
    const heroTitle = document.getElementById('heroTitle');
    const heroMeta = document.getElementById('heroMeta');
    const proofGrid = document.getElementById('proofGrid');
    const serviceList = document.getElementById('serviceList');
    const serviceMeta = document.getElementById('serviceMeta');
    const activityTable = document.getElementById('activityTable');
    const externalTable = document.getElementById('externalTable');
    const circuitTable = document.getElementById('circuitTable');
    const activityMeta = document.getElementById('activityMeta');
    const externalMeta = document.getElementById('externalMeta');
    const circuitMeta = document.getElementById('circuitMeta');
    const resultTitle = document.getElementById('resultTitle');
    const resultMeta = document.getElementById('resultMeta');
    const resultClock = document.getElementById('resultClock');
    const resultOutput = document.getElementById('resultOutput');
    const resultVisual = document.getElementById('resultVisual');
    const targetInput = document.getElementById('targetInput');
    const selectorInput = document.getElementById('selectorInput');
    const selectorField = document.getElementById('selectorField');
    const searchOptions = document.getElementById('searchOptions');
    const searchOptionHint = document.getElementById('searchOptionHint');
    const searchKindInput = document.getElementById('searchKindInput');
    const searchEngineField = document.getElementById('searchEngineField');
    const searchEngineInput = document.getElementById('searchEngineInput');
    const resultCountInput = document.getElementById('resultCountInput');
    const webOptions = document.getElementById('webOptions');
    const fusionOptions = document.getElementById('fusionOptions');
    const deepOptions = document.getElementById('deepOptions');
    const localOptions = document.getElementById('localOptions');
    const languageInput = document.getElementById('languageInput');
    const countryInput = document.getElementById('countryInput');
    const includeContentInput = document.getElementById('includeContentInput');
    const fusionSourcesInput = document.getElementById('fusionSourcesInput');
    const minScoreInput = document.getElementById('minScoreInput');
    const dedupeInput = document.getElementById('dedupeInput');
    const siteFilterInput = document.getElementById('siteFilterInput');
    const fileTypeInput = document.getElementById('fileTypeInput');
    const exactPhraseInput = document.getElementById('exactPhraseInput');
    const excludeTermsInput = document.getElementById('excludeTermsInput');
    const zipInput = document.getElementById('zipInput');
    const targetLabel = document.getElementById('targetLabel');
    const modeHint = document.getElementById('modeHint');
    const runButton = document.getElementById('runButton');
    const timeoutInput = document.getElementById('timeoutInput');
    const toast = document.getElementById('toast');

    function esc(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    function statusClass(status) {
      if (status === 'ok') return 'ok';
      if (status === 'warn' || status === 'stale') return 'warn';
      if (status === 'down' || status === 'fail') return 'down';
      return 'unknown';
    }

    function showToast(message) {
      toast.textContent = message;
      toast.classList.add('show');
      window.setTimeout(() => toast.classList.remove('show'), 2200);
    }

    function statusPill(status) {
      const cls = statusClass(status);
      return `<span class="pill ${cls}"><span class="dot"></span>${esc(status || 'unknown')}</span>`;
    }

    function serviceByName(services, name) {
      return (services || []).find((item) => item.name === name) || null;
    }

    function proofMeta(item) {
      if (!item) return [];
      return [
        item.result_count == null ? '' : `${item.result_count} green`,
        item.latency_ms == null ? '' : `${item.latency_ms}ms`,
      ].filter(Boolean);
    }

    function renderProofCard(title, item, fallbackDetail) {
      const status = item?.status || 'unknown';
      const cls = statusClass(status);
      const meta = proofMeta(item);
      return `
        <article class="proof-card">
          <div class="proof-head">
            <div class="proof-name">${esc(title)}</div>
            ${statusPill(status)}
          </div>
          <div class="proof-detail">${esc(item?.detail || fallbackDetail || '')}</div>
          <div class="meta-row">${meta.map((value) => `<span>${esc(value)}</span>`).join('')}</div>
        </article>`;
    }

    function renderProofs(data) {
      const services = data.services || [];
      const searchProof = serviceByName(services, 'Search Quality Proof');
      const scrapeProof = serviceByName(services, 'Scrape Quality Proof');
      const model = serviceByName(services, 'AI Model');
      proofGrid.innerHTML = [
        renderProofCard('Search', searchProof, 'No search proof yet'),
        renderProofCard('Scrape', scrapeProof, 'No scrape proof yet'),
        renderProofCard('AI Model', model, 'No model configured'),
      ].join('');
    }

    function renderServices(services) {
      serviceMeta.textContent = `${services.length} tracked`;
      serviceList.innerHTML = services.map((item) => {
        const meta = [
          item.latency_ms == null ? '' : `${item.latency_ms}ms`,
          item.layer || '',
          item.url || '',
        ].filter(Boolean);
        return `
          <article class="service-card">
            <div class="service-head">
              <div class="service-name">${esc(item.name)}</div>
              ${statusPill(item.status)}
            </div>
            <div class="service-detail">${esc(item.detail || '')}</div>
            <div class="meta-row">${meta.map((value) => `<span class="truncate">${esc(value)}</span>`).join('')}</div>
          </article>`;
      }).join('');
    }

    function renderTable(target, columns, rows, emptyLabel) {
      if (!rows || rows.length === 0) {
        target.innerHTML = `<div class="empty">${esc(emptyLabel)}</div>`;
        return;
      }
      const head = columns.map((col) => `<th>${esc(col.label)}</th>`).join('');
      const body = rows.map((row) => {
        const cells = columns.map((col) => `<td class="${col.className || ''}">${esc(row[col.key])}</td>`).join('');
        return `<tr>${cells}</tr>`;
      }).join('');
      target.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function renderStatus(data) {
      state.lastStatus = data;
      const overall = data.summary?.overall || 'unknown';
      const overallCls = statusClass(overall);
      const api = serviceByName(data.services || [], 'CrawlRX API');
      overallBadge.className = `big-status ${overallCls}`;
      overallBadge.innerHTML = `<span class="dot"></span>${esc(overall)}`;
      subtitle.textContent = `${overall.toUpperCase()} - ${data.uptime?.label || '-'}`;
      heroTitle.textContent = api?.detail || 'CrawlRX status loaded';
      heroMeta.textContent = `Uptime ${data.uptime?.label || '-'} - Success ${data.summary?.success_rate_pct ?? '-'}%`;
      renderProofs(data);
      renderServices(data.services || []);

      const activity = data.recent_requests || [];
      activityMeta.textContent = `${data.stats?.total_requests || 0} total`;
      renderTable(activityTable, [
        { key: 'endpoint', label: 'Endpoint', className: 'mono truncate' },
        { key: 'count', label: 'Count' },
        { key: 'avg_ms', label: 'Avg' },
      ], activity, 'No requests yet');

      const external = data.external_checks || [];
      externalMeta.textContent = external.length ? `${external.length} watched` : 'No upstream checks';
      renderTable(externalTable, [
        { key: 'name', label: 'Check', className: 'mono truncate' },
        { key: 'status', label: 'State' },
        { key: 'detail', label: 'Detail', className: 'truncate' },
        { key: 'url', label: 'Source', className: 'truncate' },
      ], external, 'No upstream checks');

      const breakers = data.circuit_breakers || [];
      circuitMeta.textContent = breakers.length ? `${breakers.length} tracked` : 'No active breakers';
      renderTable(circuitTable, [
        { key: 'domain', label: 'Domain', className: 'mono truncate' },
        { key: 'state', label: 'State' },
        { key: 'failure_count', label: 'Failures' },
        { key: 'last_failure', label: 'Last Failure', className: 'truncate' },
      ], breakers, 'No circuit breakers active');
    }

    // When the server has CRAWLRX_API_KEYS set, ask once for a key and keep it in this browser.
    function storedApiKey() {
      try { return localStorage.getItem('crawlrx-api-key') || ''; } catch (_) { return ''; }
    }
    let apiKeyPrompted = false;
    async function apiFetch(path, init = {}) {
      const withKey = () => {
        const headers = new Headers(init.headers || {});
        const key = storedApiKey();
        if (key) headers.set('X-API-Key', key);
        return fetch(path, { ...init, headers });
      };
      let response = await withKey();
      if (response.status === 401 && !apiKeyPrompted) {
        apiKeyPrompted = true;
        const key = window.prompt('This CrawlRX server needs an API key (X-API-Key):', '');
        if (key) {
          try { localStorage.setItem('crawlrx-api-key', key.trim()); } catch (_) { /* private mode */ }
          response = await withKey();
        }
      }
      return response;
    }

    async function refreshStatus() {
      try {
        const response = await apiFetch('/dashboard/status', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        renderStatus(await response.json());
      } catch (error) {
        subtitle.textContent = `Refresh failed - ${error.message}`;
        overallBadge.className = 'big-status down';
        overallBadge.innerHTML = '<span class="dot"></span>Down';
        heroTitle.textContent = 'Dashboard status failed';
        heroMeta.textContent = error.message;
      }
    }

    function setHidden(element, hidden) {
      element.classList.toggle('hidden', hidden);
    }

    function searchKindLabel(kind) {
      const option = searchKindInput.querySelector(`option[value="${kind}"]`);
      return option ? option.textContent : 'Search';
    }

    function updateSearchOptions() {
      const kind = searchKindInput.value;
      const isSearch = state.mode === 'search';
      const webLike = kind === 'web' || kind === 'auto';
      setHidden(searchOptions, !isSearch);
      setHidden(searchEngineField, kind !== 'web');
      setHidden(webOptions, !webLike);
      setHidden(fusionOptions, kind !== 'fusion');
      setHidden(deepOptions, kind !== 'deep');
      setHidden(localOptions, kind !== 'local');
      searchOptionHint.textContent = searchKindLabel(kind);
      if (isSearch) {
        runButton.textContent = `Run ${searchKindLabel(kind)}`;
        modeHint.textContent = searchKindLabel(kind);
      }
    }

    function cleanText(value) {
      return String(value || '').trim();
    }

    function optionalText(input) {
      const value = cleanText(input.value);
      return value || undefined;
    }

    function commaList(value) {
      return cleanText(value)
        .split(',')
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function statusDot(status) {
      return `<span class="dot ${esc(status || 'unknown')}"></span>`;
    }

    function resultUrlHtml(item) {
      const url = item.url || item.website || item.store_url || '';
      const title = item.title || item.name || item.headline || url || 'Untitled result';
      if (url.startsWith('http')) {
        return `<a href="${esc(url)}" target="_blank" rel="noreferrer">${esc(title)}</a>`;
      }
      return `<div class="result-title">${esc(title)}</div>`;
    }

    function resultSnippet(item) {
      return item.snippet || item.content || item.address || item.published || '';
    }

    function resultMetaParts(item) {
      const metadata = item.metadata || {};
      return [
        item.kind,
        item.source,
        item.engine,
        item.platform,
        item.score == null ? '' : `score ${Number(item.score).toFixed(2)}`,
        metadata.price == null ? '' : `$${Number(metadata.price).toFixed(2)}`,
        metadata.rating == null ? '' : `rating ${metadata.rating}`,
        metadata.route ? `route ${metadata.route}` : '',
      ].filter(Boolean);
    }

    function renderResultVisual(data) {
      const results = Array.isArray(data?.results) ? data.results : [];
      const routes = Array.isArray(data?.routes) ? data.routes : [];
      if (!results.length && !routes.length) {
        resultVisual.classList.add('hidden');
        resultVisual.innerHTML = '';
        return;
      }
      const chips = [
        data.intent ? `Intent ${data.intent}` : '',
        data.mode ? `Mode ${data.mode}` : '',
        data.confidence == null ? '' : `Confidence ${data.confidence}`,
        data.total == null ? `${results.length} result(s)` : `${data.total} result(s)`,
        data.cache_hit ? (data.stale ? 'Stale cache' : 'Cache hit') : '',
        Array.isArray(data.warnings) && data.warnings.length ? `${data.warnings.length} warning(s)` : '',
      ].filter(Boolean);
      const routeHtml = routes.length ? `
        <div class="route-row">
          ${routes.map((route) => `
            <span class="route-chip ${esc(route.status || 'unknown')}">
              ${statusDot(route.status)}${esc(route.route || 'route')}
              ${route.result_count == null ? '' : esc(` ${route.result_count}`)}
              ${route.engine ? esc(` ${route.engine}`) : ''}
            </span>`).join('')}
        </div>` : '';
      const cardHtml = results.slice(0, 8).map((item) => {
        const parts = resultMetaParts(item);
        const snippet = resultSnippet(item);
        return `
          <article class="result-card">
            ${resultUrlHtml(item)}
            ${snippet ? `<div class="result-snippet">${esc(snippet)}</div>` : ''}
            ${parts.length ? `<div class="result-card-meta">${parts.map((part) => `<span>${esc(part)}</span>`).join('')}</div>` : ''}
          </article>`;
      }).join('');
      resultVisual.innerHTML = `
        ${chips.length ? `<div class="summary-row">${chips.map((chip) => `<span class="summary-chip">${esc(chip)}</span>`).join('')}</div>` : ''}
        ${routeHtml}
        ${cardHtml ? `<div class="result-list">${cardHtml}</div>` : ''}`;
      resultVisual.classList.remove('hidden');
    }

    function buildOrchestratedSearch(query, count, timeout, mode) {
      const body = {
        query,
        mode,
        count,
        timeout,
        include_content: includeContentInput.checked,
        sources: commaList(fusionSourcesInput.value),
        min_score: Number(minScoreInput.value),
        dedupe: dedupeInput.checked,
      };
      if (optionalText(languageInput)) body.language = optionalText(languageInput);
      if (optionalText(countryInput)) body.country = optionalText(countryInput);
      if (optionalText(siteFilterInput)) body.site_filter = optionalText(siteFilterInput);
      if (optionalText(fileTypeInput)) body.file_type = optionalText(fileTypeInput);
      if (optionalText(exactPhraseInput)) body.exact_phrase = optionalText(exactPhraseInput);
      const excluded = commaList(excludeTermsInput.value);
      if (excluded.length) body.exclude_terms = excluded;
      if (optionalText(zipInput)) body.zip_code = optionalText(zipInput);
      return { path: '/search/orchestrate', body };
    }

    function buildSearchRequest(timeout) {
      const query = cleanText(targetInput.value);
      const count = Number(resultCountInput.value);
      const kind = searchKindInput.value;
      if (!query) throw new Error('Query required');
      if (kind === 'auto') {
        return buildOrchestratedSearch(query, count, timeout, 'auto');
      }
      if (kind !== 'web') {
        return buildOrchestratedSearch(query, count, timeout, kind);
      }
      const body = {
        query,
        engine: searchEngineInput.value,
        num_results: count,
        timeout,
        include_content: includeContentInput.checked,
      };
      if (optionalText(languageInput)) body.language = optionalText(languageInput);
      if (optionalText(countryInput)) body.country = optionalText(countryInput);
      return { path: '/search', body };
    }

    function setMode(mode) {
      state.mode = mode;
      document.querySelectorAll('.mode-button').forEach((button) => {
        button.classList.toggle('active', button.dataset.mode === mode);
      });
      selectorField.style.display = mode === 'search' ? 'none' : 'grid';
      targetLabel.textContent = mode === 'search' ? 'Query' : 'URL';
      runButton.textContent = `Run ${mode[0].toUpperCase()}${mode.slice(1)}`;
      modeHint.textContent = mode === 'crawl' ? 'Crawl' : mode === 'scrape' ? 'Scrape' : 'Search';
      if (mode === 'search' && targetInput.value.startsWith('http')) targetInput.value = 'crawlrx health';
      if (mode !== 'search' && !targetInput.value.startsWith('http')) targetInput.value = 'https://example.com';
      updateSearchOptions();
    }

    function requestForMode() {
      const timeout = Number(timeoutInput.value);
      if (state.mode === 'search') {
        return buildSearchRequest(timeout);
      }
      if (state.mode === 'scrape') {
        return {
          path: '/scrape',
          body: {
            url: targetInput.value,
            selector: selectorInput.value || 'h1',
            extract: 'text',
            wait_for: 'domcontentloaded',
            scroll: false,
            timeout,
            cache: false,
          },
        };
      }
      return {
        path: '/crawl',
        body: {
          url: targetInput.value,
          wait_for: 'domcontentloaded',
          scroll: false,
          timeout,
          cache: false,
          include_images: false,
          screenshot: false,
        },
      };
    }

    async function runAction() {
      let spec;
      try {
        spec = requestForMode();
      } catch (error) {
        showToast(error.message);
        return;
      }
      const started = performance.now();
      runButton.disabled = true;
      resultTitle.textContent = `Running ${state.mode}`;
      resultMeta.textContent = spec.path;
      resultClock.textContent = 'Working';
      resultVisual.classList.add('hidden');
      resultVisual.innerHTML = '';
      resultOutput.textContent = JSON.stringify(spec.body, null, 2);
      try {
        const response = await apiFetch(spec.path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(spec.body),
        });
        const data = await response.json();
        const elapsed = Math.round(performance.now() - started);
        const warningCount = Array.isArray(data.warnings) ? data.warnings.length : 0;
        resultTitle.textContent = data.success === false
          ? 'Completed with error'
          : warningCount ? 'Completed with warnings' : 'Completed';
        const cacheLabel = data.cache_hit ? (data.stale ? ' - stale cache' : ' - cache hit') : '';
        resultMeta.textContent = `${response.status} - ${elapsed}ms${cacheLabel}`;
        resultClock.textContent = new Date().toLocaleTimeString();
        renderResultVisual(data);
        resultOutput.textContent = JSON.stringify(data, null, 2);
        await refreshStatus();
      } catch (error) {
        resultTitle.textContent = 'Request failed';
        resultMeta.textContent = error.message;
        resultClock.textContent = new Date().toLocaleTimeString();
        resultVisual.classList.add('hidden');
        resultVisual.innerHTML = '';
        resultOutput.textContent = JSON.stringify({ error: error.message }, null, 2);
      } finally {
        runButton.disabled = false;
      }
    }

    document.querySelectorAll('.mode-button').forEach((button) => {
      button.addEventListener('click', () => setMode(button.dataset.mode));
    });

    document.querySelectorAll('[data-example-mode]').forEach((button) => {
      button.addEventListener('click', () => {
        setMode(button.dataset.exampleMode);
        targetInput.value = button.dataset.exampleValue;
        if (button.dataset.exampleKind) searchKindInput.value = button.dataset.exampleKind;
        if (button.dataset.exampleSelector) selectorInput.value = button.dataset.exampleSelector;
        updateSearchOptions();
      });
    });

    searchKindInput.addEventListener('change', updateSearchOptions);
    document.getElementById('runButton').addEventListener('click', runAction);
    document.getElementById('refreshButton').addEventListener('click', () => {
      refreshStatus();
      showToast('Refreshed');
    });
    document.getElementById('copyButton').addEventListener('click', async () => {
      await navigator.clipboard.writeText(resultOutput.textContent || '');
      showToast('Copied');
    });

    setMode('crawl');
    refreshStatus();
    window.setInterval(refreshStatus, 15000);
  </script>
</body>
</html>
"""
