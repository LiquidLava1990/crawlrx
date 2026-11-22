"""JS extraction snippets used by angellist.py (split for fn_length)."""

from __future__ import annotations

# ─────────────────────────────────────────────
# JS extraction snippets (verbatim to preserve scraping behaviour)
# ─────────────────────────────────────────────

COMPANY_LOOKUP_JS = r"""
() => {
    const links = document.querySelectorAll('a[href*="/company/"], a[href*="/companies/"]');
    for (const link of links) {
        const href = link.getAttribute('href');
        if (href && !href.includes('/jobs') && !href.includes('/people')) {
            return 'https://wellfound.com' + href.split('?')[0];
        }
    }
    const resultLinks = document.querySelectorAll('a[href^="/company"], a[href^="/companies"]');
    for (const link of resultLinks) {
        const href = link.getAttribute('href');
        if (href) {
            return 'https://wellfound.com' + href.split('?')[0];
        }
    }
    return null;
}
"""


STARTUP_EXTRACT_JS = r"""
() => {
    const data = {
        name: '', tagline: '', description: '', founded: null, team_size: null,
        location: null, funding_stage: null, total_raised: null, investors: [],
        jobs_count: 0, website: null, markets: [], logo_url: null,
        wellfound_url: window.location.href
    };
    const nameSelectors = ['h1', '[data-testid="company-name"]', '[class*="companyName"]', 'h1[class*="title"]'];
    for (const sel of nameSelectors) {
        const el = document.querySelector(sel);
        if (el && el.textContent.trim()) { data.name = el.textContent.trim(); break; }
    }
    const taglineSelectors = ['[data-testid="company-tagline"]', '[class*="tagline"]', 'h1 + div', 'h2[class*="subtitle"]'];
    for (const sel of taglineSelectors) {
        const el = document.querySelector(sel);
        if (el && el.textContent.trim()) { data.tagline = el.textContent.trim(); break; }
    }
    const descSelectors = ['[data-testid="company-description"]', '[class*="description"]', '[class*="about"] p', 'section p'];
    for (const sel of descSelectors) {
        const el = document.querySelector(sel);
        if (el && el.textContent.trim().length > 20) { data.description = el.textContent.trim(); break; }
    }
    const text = document.body.innerText;
    const foundedMatch = text.match(/Founded[:\s]+(\d{4})/i);
    if (foundedMatch) data.founded = foundedMatch[1];
    const teamMatch = text.match(/([\d-]+)\s*(people|employees|team members)/i);
    if (teamMatch) data.team_size = teamMatch[1];
    const locationSelectors = ['[data-testid="company-location"]', '[class*="location"]', 'a[href*="/location/"]'];
    for (const sel of locationSelectors) {
        const el = document.querySelector(sel);
        if (el && el.textContent.trim()) { data.location = el.textContent.trim(); break; }
    }
    const stageSelectors = ['[data-testid="funding-stage"]', '[class*="fundingStage"]', '[class*="stage"]'];
    for (const sel of stageSelectors) {
        const el = document.querySelector(sel);
        if (el && el.textContent.trim()) {
            const stext = el.textContent.trim();
            if (/seed|series|ipo|acquired/i.test(stext)) { data.funding_stage = stext; break; }
        }
    }
    const raisedMatch = text.match(/([\$\d,.]+[BMK]?)\s*(raised|funding)/i);
    if (raisedMatch) data.total_raised = raisedMatch[1];
    const jobsMatch = text.match(/(\d+)\s*open\s*(role|job|position)/i);
    if (jobsMatch) data.jobs_count = parseInt(jobsMatch[1]);
    const websiteSelectors = ['a[data-testid="company-website"]', 'a[rel*="noopener"]', 'a[href^="http"]'];
    for (const sel of websiteSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            const href = el.getAttribute('href');
            if (href && !href.includes('wellfound.com') && !href.includes('angel.co')) {
                data.website = href; break;
            }
        }
    }
    document.querySelectorAll('a[href*="/market/"], a[href*="/markets/"], [class*="market"], [class*="tag"]').forEach(el => {
        const t = el.textContent.trim();
        if (t && t.length < 50 && !data.markets.includes(t)) data.markets.push(t);
    });
    const logoSelectors = ['img[data-testid="company-logo"]', 'img[alt*="logo"]', '[class*="logo"] img'];
    for (const sel of logoSelectors) {
        const el = document.querySelector(sel);
        if (el) { data.logo_url = el.getAttribute('src'); break; }
    }
    document.querySelectorAll('[data-testid="investor"], [class*="investor"]').forEach(el => {
        const name = el.textContent.trim();
        if (name && name.length < 100) data.investors.push({name: name, type: null});
    });
    return data;
}
"""


JOBS_EXTRACT_JS = r"""
(maxResults) => {
    const jobs = [];
    const cards = document.querySelectorAll('[data-testid="job-card"], [class*="jobCard"], [class*="job-listing"], [class*="_job"]');
    for (let i = 0; i < Math.min(cards.length, maxResults); i++) {
        const card = cards[i];
        const job = {
            title: '', company: '', location: null, remote: false,
            salary_range: null, equity_range: null, job_url: '',
            skills_required: [], posted_date: null, job_type: null
        };
        const titleSelectors = ['h2', 'h3', '[data-testid="job-title"]', '[class*="title"]', 'a[class*="title"]'];
        for (const sel of titleSelectors) {
            const el = card.querySelector(sel);
            if (el && el.textContent.trim()) { job.title = el.textContent.trim(); break; }
        }
        const companySelectors = ['[data-testid="company-name"]', '[class*="companyName"]', 'a[href*="/company/"]'];
        for (const sel of companySelectors) {
            const el = card.querySelector(sel);
            if (el && el.textContent.trim()) { job.company = el.textContent.trim(); break; }
        }
        const locationSelectors = ['[data-testid="location"]', '[class*="location"]', '[class*="workplace"]'];
        for (const sel of locationSelectors) {
            const el = card.querySelector(sel);
            if (el && el.textContent.trim()) {
                const ltext = el.textContent.trim().toLowerCase();
                job.location = el.textContent.trim();
                if (ltext.includes('remote')) job.remote = true;
                break;
            }
        }
        const salarySelectors = ['[data-testid="salary"]', '[class*="salary"]', '[class*="compensation"]'];
        for (const sel of salarySelectors) {
            const el = card.querySelector(sel);
            if (el && el.textContent.trim()) {
                const stext = el.textContent.trim();
                if (stext.includes('$') || stext.includes('K')) { job.salary_range = stext; break; }
            }
        }
        const equitySelectors = ['[data-testid="equity"]', '[class*="equity"]'];
        for (const sel of equitySelectors) {
            const el = card.querySelector(sel);
            if (el && el.textContent.trim()) {
                const etext = el.textContent.trim().toLowerCase();
                if (etext.includes('equity') || etext.includes('%')) { job.equity_range = el.textContent.trim(); break; }
            }
        }
        const linkEl = card.querySelector('a[href*="/jobs/"], a[href*="/l/"]');
        if (linkEl) {
            const href = linkEl.getAttribute('href');
            if (href) job.job_url = 'https://wellfound.com' + href.split('?')[0];
        }
        card.querySelectorAll('[data-testid="skill"], [class*="skill"], [class*="tag"]').forEach(el => {
            const t = el.textContent.trim();
            if (t && t.length < 30) job.skills_required.push(t);
        });
        const dateSelectors = ['time', '[data-testid="posted-date"]', '[class*="posted"]', '[class*="date"]'];
        for (const sel of dateSelectors) {
            const el = card.querySelector(sel);
            if (el) {
                const datetime = el.getAttribute('datetime');
                if (datetime) { job.posted_date = datetime; break; }
                const dtext = el.textContent.trim();
                if (dtext && (dtext.includes('ago') || dtext.includes('day') || dtext.includes('week'))) {
                    job.posted_date = dtext; break;
                }
            }
        }
        const typeSelectors = ['[data-testid="job-type"]', '[class*="jobType"]', '[class*="type"]'];
        for (const sel of typeSelectors) {
            const el = card.querySelector(sel);
            if (el && el.textContent.trim()) {
                const ttext = el.textContent.trim().toLowerCase();
                if (ttext.includes('full-time') || ttext.includes('part-time') || ttext.includes('contract') || ttext.includes('intern')) {
                    job.job_type = el.textContent.trim(); break;
                }
            }
        }
        if (job.title && job.company) jobs.push(job);
    }
    return jobs;
}
"""
