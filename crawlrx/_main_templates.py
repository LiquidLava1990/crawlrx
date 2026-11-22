"""HTML templates for landing page and dashboard."""
from __future__ import annotations

LANDING_HTML: str = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CrawlRX - Self-Hosted Scraping API</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: #0d1117;
            color: #e6edf3;
            line-height: 1.6;
            min-height: 100vh;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 40px 20px;
        }
        header {
            text-align: center;
            padding: 60px 0 40px;
            border-bottom: 1px solid #30363d;
            margin-bottom: 40px;
        }
        h1 {
            font-size: 3rem;
            font-weight: 700;
            margin-bottom: 10px;
            background: linear-gradient(90deg, #58a6ff, #a371f7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .subtitle {
            font-size: 1.25rem;
            color: #8b949e;
        }
        h2 {
            font-size: 1.5rem;
            margin: 40px 0 20px;
            color: #f0f6fc;
            border-bottom: 1px solid #21262d;
            padding-bottom: 10px;
        }
        .status-links {
            display: flex;
            gap: 15px;
            justify-content: center;
            margin: 30px 0;
        }
        .status-links a {
            background: #21262d;
            color: #58a6ff;
            padding: 10px 20px;
            border-radius: 6px;
            text-decoration: none;
            border: 1px solid #30363d;
            transition: all 0.2s;
        }
        .status-links a:hover {
            background: #30363d;
            border-color: #58a6ff;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            font-size: 0.95rem;
        }
        th {
            background: #161b22;
            padding: 12px 15px;
            text-align: left;
            font-weight: 600;
            color: #f0f6fc;
            border-bottom: 1px solid #30363d;
        }
        td {
            padding: 12px 15px;
            border-bottom: 1px solid #21262d;
        }
        tr:hover td {
            background: #161b22;
        }
        .method {
            font-family: "SF Mono", Monaco, monospace;
            font-size: 0.85rem;
            padding: 4px 8px;
            border-radius: 4px;
            font-weight: 600;
        }
        .method.get { background: #1f6feb33; color: #58a6ff; }
        .method.post { background: #23863633; color: #3fb950; }
        code {
            font-family: "SF Mono", Monaco, monospace;
            background: #161b22;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.9rem;
            color: #d2a8ff;
        }
        .code-block {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 20px;
            margin: 15px 0;
            overflow-x: auto;
        }
        .code-block pre {
            font-family: "SF Mono", Monaco, monospace;
            font-size: 0.9rem;
            color: #e6edf3;
            white-space: pre-wrap;
            word-break: break-all;
        }
        .endpoint-path {
            color: #79c0ff;
            font-family: "SF Mono", Monaco, monospace;
        }
        .description {
            color: #c9d1d9;
        }
        footer {
            text-align: center;
            margin-top: 60px;
            padding-top: 30px;
            border-top: 1px solid #30363d;
            color: #8b949e;
            font-size: 0.9rem;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🕷 CrawlRX</h1>
            <p class="subtitle">Self-Hosted Scraping & Search API</p>
            <div class="status-links">
                <a href="/dashboard">�"� Dashboard</a>
                <a href="/health">[x] Health Check</a>
                <a href="/docs">�"� API Docs</a>
            </div>
        </header>

        <h2>�"� API Endpoints</h2>
        <table>
            <thead>
                <tr>
                    <th>Method</th>
                    <th>Endpoint</th>
                    <th>Description</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td><span class="method get">GET</span></td>
                    <td class="endpoint-path">/health</td>
                    <td class="description">Health check + server status</td>
                </tr>
                <tr>
                    <td><span class="method get">GET</span></td>
                    <td class="endpoint-path">/stats</td>
                    <td class="description">Request counts and uptime</td>
                </tr>
                <tr>
                    <td><span class="method post">POST</span></td>
                    <td class="endpoint-path">/scrape</td>
                    <td class="description">Scrape any URL (JS-rendered)</td>
                </tr>
                <tr>
                    <td><span class="method post">POST</span></td>
                    <td class="endpoint-path">/crawl</td>
                    <td class="description">Full site crawl</td>
                </tr>
                <tr>
                    <td><span class="method post">POST</span></td>
                    <td class="endpoint-path">/search</td>
                    <td class="description">Web search via SearXNG</td>
                </tr>
                <tr>
                    <td><span class="method post">POST</span></td>
                    <td class="endpoint-path">/search/ecommerce</td>
                    <td class="description">Product search across platforms</td>
                </tr>
                <tr>
                    <td><span class="method post">POST</span></td>
                    <td class="endpoint-path">/search/google-shopping</td>
                    <td class="description">Google Shopping results</td>
                </tr>
                <tr>
                    <td><span class="method post">POST</span></td>
                    <td class="endpoint-path">/metadata</td>
                    <td class="description">Extract page metadata</td>
                </tr>
                <tr>
                    <td><span class="method post">POST</span></td>
                    <td class="endpoint-path">/scrape/platform</td>
                    <td class="description">Auto-detect CMS and scrape products</td>
                </tr>
                <tr>
                    <td><span class="method post">POST</span></td>
                    <td class="endpoint-path">/scrape/travel/flights</td>
                    <td class="description">Flight search</td>
                </tr>
                <tr>
                    <td><span class="method post">POST</span></td>
                    <td class="endpoint-path">/scrape/travel/hotels</td>
                    <td class="description">Hotel search</td>
                </tr>
                <tr>
                    <td><span class="method post">POST</span></td>
                    <td class="endpoint-path">/scrape/github</td>
                    <td class="description">GitHub repo scraper</td>
                </tr>
                <tr>
                    <td><span class="method post">POST</span></td>
                    <td class="endpoint-path">/scrape/producthunt</td>
                    <td class="description">ProductHunt scraper</td>
                </tr>
            </tbody>
        </table>

        <h2>🚀 Usage Examples</h2>
        <p>Scrape a webpage:</p>
        <div class="code-block">
            <pre>curl -X POST http://localhost:7337/scrape \\  # TODO: use service_registry
  -H "Content-Type: application/json" \\
  -d '{"url": "https://example.com"}'</pre>
        </div>

        <p>Search the web:</p>
        <div class="code-block">
            <pre>curl -X POST http://localhost:7337/search \\  # TODO: use service_registry
  -H "Content-Type: application/json" \\
  -d '{"query": "OpenAI GPT-5", "engine": "google"}'</pre>
        </div>

        <footer>
            <p>CrawlRX &copy; 2026 &middot; Self-hosted scraping, search and extraction API</p>
        </footer>
    </div>
</body>
</html>"""

DASHBOARD_HTML_TEMPLATE: str = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="10">
    <title>CrawlRX Dashboard</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #0f172a;
            color: #f8fafc;
            line-height: 1.6;
            padding: 20px;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid #334155;
        }}
        h1 {{
            color: #38bdf8;
            font-size: 2rem;
            font-weight: 700;
        }}
        .version {{
            color: #64748b;
            font-size: 0.9rem;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .stat-card {{
            background: #1e293b;
            border-radius: 12px;
            padding: 20px;
            border: 1px solid #334155;
        }}
        .stat-card h3 {{
            color: #94a3b8;
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }}
        .stat-value {{
            color: #38bdf8;
            font-size: 2rem;
            font-weight: 700;
        }}
        .section {{
            background: #1e293b;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
            border: 1px solid #334155;
        }}
        .section h2 {{
            color: #38bdf8;
            font-size: 1.25rem;
            margin-bottom: 15px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th, td {{
            text-align: left;
            padding: 12px;
            border-bottom: 1px solid #334155;
        }}
        th {{
            color: #94a3b8;
            font-weight: 600;
            font-size: 0.85rem;
            text-transform: uppercase;
        }}
        td {{
            color: #e2e8f0;
        }}
        tr:hover td {{
            background: #334155;
        }}
        .btn {{
            display: inline-block;
            background: #38bdf8;
            color: #0f172a;
            padding: 10px 20px;
            border-radius: 8px;
            text-decoration: none;
            font-weight: 600;
            margin-right: 10px;
            margin-bottom: 10px;
        }}
        .btn:hover {{
            background: #7dd3fc;
        }}
        .footer {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #334155;
            color: #64748b;
            font-size: 0.85rem;
        }}
        .status-indicator {{
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #22c55e;
            margin-right: 8px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>CrawlRX</h1>
                <span class="version">Version 1.0.0 | Uptime: {uptime_str}</span>
            </div>
            <div style="text-align: right;">
                <span class="status-indicator"></span>Operational
            </div>
        </header>

        <div class="stats-grid">
            <div class="stat-card">
                <h3>Total Requests</h3>
                <div class="stat-value">{stats.total_requests:,}</div>
            </div>
            <div class="stat-card">
                <h3>Success Rate</h3>
                <div class="stat-value">{success_rate_pct}%</div>
            </div>
            <div class="stat-card">
                <h3>Avg Response</h3>
                <div class="stat-value">{overall_avg_response}ms</div>
            </div>
            <div class="stat-card">
                <h3>Cache Hit Rate</h3>
                <div class="stat-value">{cache_hit_rate:.1f}%</div>
            </div>
        </div>

        <div class="section">
            <h2>Recent Requests by Endpoint</h2>
            <table>
                <thead>
                    <tr>
                        <th>Endpoint</th>
                        <th>Count</th>
                        <th>Avg Response Time</th>
                    </tr>
                </thead>
                <tbody>
                    {recent_rows}
                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>Circuit Breaker Status</h2>
            <table>
                <thead>
                    <tr>
                        <th>Domain</th>
                        <th>State</th>
                        <th>Recent Failures</th>
                    </tr>
                </thead>
                <tbody>
                    {circuit_rows}
                </tbody>
            </table>
        </div>

        <div class="section">
            <h2>Quick Links</h2>
            <a href="/docs" class="btn">API Docs</a>
            <a href="/health" class="btn">Health Check</a>
            <a href="/stats" class="btn">JSON Stats</a>
            <a href="/cache/stats" class="btn">Cache Stats</a>
        </div>

        <div class="footer">
            <p>CrawlRX Dashboard - Auto-refreshes every 10 seconds</p>
            <p>Errors Today: {stats.errors_today} | Last Updated: {time_now}</p>
        </div>
    </div>
</body>
</html>"""
