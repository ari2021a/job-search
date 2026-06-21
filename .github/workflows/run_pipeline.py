#!/usr/bin/env python3
"""
Job Search Pipeline — Aaron (Eric) Barrari
Searches for open jobs, scores them against profile, saves to scored_jobs.json,
builds docs/index.html, and optionally pushes to GitHub Pages.
"""

import json
import os
import re
import sys
import time
import hashlib
import argparse
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
import urllib.request
import urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
PROFILE = (ROOT / "profile.md").read_text()
JOBS_FILE = ROOT / CONFIG["pipeline"]["output_file"]
DOCS_DIR = ROOT / CONFIG["pipeline"]["docs_dir"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

OPUS_MODEL = "claude-opus-4-5"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ── Anthropic API helper ───────────────────────────────────────────────────────
def call_claude(model: str, system: str, user: str, tools: list = None, max_tokens: int = 4096) -> dict:
    """Call the Anthropic messages API."""
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if tools:
        payload["tools"] = tools

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def extract_text(response: dict) -> str:
    return "".join(b.get("text", "") for b in response.get("content", []) if b.get("type") == "text")


def extract_json(text: str) -> any:
    """Extract JSON from a string that may contain markdown fences."""
    text = text.strip()
    # Strip ```json ... ``` fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    # Find first [ or { and last ] or }
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return json.loads(text)


# ── Job ID ─────────────────────────────────────────────────────────────────────
def job_id(job: dict) -> str:
    key = f"{job.get('company','')}-{job.get('title','')}-{job.get('url','')}"
    return hashlib.md5(key.lower().encode()).hexdigest()[:12]


# ── Load / Save jobs ───────────────────────────────────────────────────────────
def load_jobs() -> dict:
    if JOBS_FILE.exists():
        return json.loads(JOBS_FILE.read_text())
    return {}


def save_jobs(jobs: dict):
    JOBS_FILE.write_text(json.dumps(jobs, indent=2, ensure_ascii=False))
    print(f"[save] {len(jobs)} jobs saved to {JOBS_FILE.name}")


# ── Step 1: Search ─────────────────────────────────────────────────────────────
SEARCH_SYSTEM = """You are a senior technical recruiter with real-time web search access.
Your job is to find CURRENTLY OPEN job postings. Return ONLY valid JSON — no prose, no markdown fences."""

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
}


def search_jobs() -> list:
    roles = CONFIG["search"]["roles"]
    locations = CONFIG["search"]["locations"]
    industries = CONFIG["search"]["industries"]

    roles_str = ", ".join(roles[:8])
    loc_str = ", ".join(locations)
    ind_str = ", ".join(industries)

    prompt = f"""Find CURRENTLY OPEN jobs posted in the last 30 days for this candidate:

Target roles: {roles_str}
Industries: {ind_str}
Work arrangement: {loc_str} ONLY — no on-site positions
Seniority: Director / VP / Head of / C-suite level

Search for real, active job postings on LinkedIn, Greenhouse, Lever, Ashby, Workday, and company career pages.

Return 10-15 jobs as a JSON array. Each object must have EXACTLY these fields:
- company: string
- title: string
- location: string (e.g. "Remote, EMEA" or "Remote, Worldwide")
- url: string (direct link to the specific job posting)
- posted: string (ISO date or relative like "2 days ago")
- description: string (2-4 sentence summary of the role and requirements)
- source: string (e.g. "LinkedIn", "Greenhouse", "company careers page")

Only include roles where the URL is a real, specific job posting link (not a search results page).
Focus on FinTech, SaaS, Payments companies with remote-friendly cultures."""

    print("[search] Calling Claude Opus with web search...")
    try:
        resp = call_claude(
            model=OPUS_MODEL,
            system=SEARCH_SYSTEM,
            user=prompt,
            tools=[WEB_SEARCH_TOOL],
            max_tokens=8192,
        )
        text = extract_text(resp)
        jobs = extract_json(text)
        if isinstance(jobs, list):
            print(f"[search] Found {len(jobs)} jobs")
            return jobs
        print(f"[search] Unexpected JSON shape: {type(jobs)}")
        return []
    except Exception as e:
        print(f"[search] ERROR: {e}")
        return []


# ── Step 2: ATS Scraper ────────────────────────────────────────────────────────
ATS_BOARDS = [
    # (company, board_type, board_id)  — add more as needed
    ("Stripe", "greenhouse", "stripe"),
    ("Wise", "greenhouse", "wise"),
    ("Revolut", "lever", "revolut"),
    ("Checkout.com", "greenhouse", "checkoutcom"),
    ("Klarna", "greenhouse", "klarna"),
    ("Adyen", "greenhouse", "adyen"),
    ("Plaid", "greenhouse", "plaid"),
    ("Brex", "greenhouse", "brex"),
    ("Rippling", "greenhouse", "rippling"),
]

ROLE_KEYWORDS = [
    "revenue operations", "revops", "sales operations", "business development",
    "partnerships", "customer success", "commercial", "head of sales",
    "vp sales", "director of sales", "operations manager", "go-to-market",
]


def fetch_greenhouse(company: str, board_id: str) -> list:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_id}/jobs?content=true"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        jobs = []
        for j in data.get("jobs", []):
            title_lower = j.get("title", "").lower()
            if not any(kw in title_lower for kw in ROLE_KEYWORDS):
                continue
            loc = j.get("location", {}).get("name", "")
            if not any(kw in loc.lower() for kw in ["remote", "emea", "europe", "anywhere"]):
                continue
            jobs.append({
                "company": company,
                "title": j["title"],
                "location": loc,
                "url": j.get("absolute_url", ""),
                "posted": j.get("updated_at", "")[:10],
                "description": j.get("content", "")[:400].strip(),
                "source": "Greenhouse API",
            })
        return jobs
    except Exception as e:
        print(f"[ats] Greenhouse {board_id}: {e}")
        return []


def fetch_lever(company: str, board_id: str) -> list:
    url = f"https://api.lever.co/v0/postings/{board_id}?mode=json"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        jobs = []
        for j in data:
            title_lower = j.get("text", "").lower()
            if not any(kw in title_lower for kw in ROLE_KEYWORDS):
                continue
            categories = j.get("categories", {})
            loc = categories.get("location", "")
            if not any(kw in loc.lower() for kw in ["remote", "emea", "europe", "anywhere"]):
                continue
            jobs.append({
                "company": company,
                "title": j["text"],
                "location": loc,
                "url": j.get("hostedUrl", ""),
                "posted": datetime.fromtimestamp(j.get("createdAt", 0) / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                "description": j.get("descriptionPlain", "")[:400].strip(),
                "source": "Lever API",
            })
        return jobs
    except Exception as e:
        print(f"[ats] Lever {board_id}: {e}")
        return []


def ats_scraper() -> list:
    print("[ats] Scraping ATS boards...")
    found = []
    for company, board_type, board_id in ATS_BOARDS:
        if board_type == "greenhouse":
            found.extend(fetch_greenhouse(company, board_id))
        elif board_type == "lever":
            found.extend(fetch_lever(company, board_id))
        time.sleep(0.3)
    print(f"[ats] Found {len(found)} matching ATS postings")
    return found


# ── Step 3: Score ──────────────────────────────────────────────────────────────
SCORE_SYSTEM = """You are an expert career coach and ATS specialist.
Evaluate job-candidate fit and return ONLY a JSON object — no prose, no markdown."""

SCORE_PROMPT_TPL = """Evaluate this job posting for the following candidate profile.

CANDIDATE PROFILE:
{profile}

JOB POSTING:
Company: {company}
Title: {title}
Location: {location}
URL: {url}
Description: {description}

Return ONLY a JSON object with these exact fields:
{{
  "fit_score": <integer 1-10>,
  "score_reason": "<2-3 sentence explanation of fit>",
  "ai_opener": "<one compelling sentence Eric could use to open a cover letter or outreach message>",
  "location_ok": <true if remote/EMEA/EU/worldwide, false if on-site only>
}}

Scoring rules:
- If location_ok is false, fit_score must be 0
- 8-10: Excellent match — RevOps/Sales Ops/BD leadership in FinTech/SaaS, remote, senior level
- 6-7: Good match — related role or industry, remote, senior level
- 4-5: Partial match — adjacent role or industry
- 1-3: Weak match — significant gaps
- 0: Location mismatch (on-site only)"""


def score_job(job: dict) -> dict:
    prompt = SCORE_PROMPT_TPL.format(
        profile=PROFILE[:3000],  # keep within token budget
        company=job.get("company", ""),
        title=job.get("title", ""),
        location=job.get("location", ""),
        url=job.get("url", ""),
        description=job.get("description", ""),
    )
    try:
        resp = call_claude(model=HAIKU_MODEL, system=SCORE_SYSTEM, user=prompt, max_tokens=512)
        text = extract_text(resp)
        scoring = extract_json(text)
        return scoring
    except Exception as e:
        print(f"[score] ERROR for {job.get('title')}: {e}")
        return {"fit_score": 0, "score_reason": f"Scoring error: {e}", "ai_opener": "", "location_ok": False}


def score_jobs(new_jobs: list) -> list:
    print(f"[score] Scoring {len(new_jobs)} jobs...")
    scored = []
    for i, job in enumerate(new_jobs):
        print(f"[score] {i+1}/{len(new_jobs)}: {job.get('company')} — {job.get('title')}")
        scoring = score_job(job)
        job.update(scoring)
        scored.append(job)
        time.sleep(0.2)  # rate limit courtesy
    return scored


# ── Step 4: Filter ─────────────────────────────────────────────────────────────
def is_recent(posted_str: str, max_days: int = 30) -> bool:
    if not posted_str:
        return True  # give benefit of doubt
    now = datetime.now(tz=timezone.utc)
    # Try ISO date
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(posted_str[:10], fmt[:len(posted_str[:10])])
            dt = dt.replace(tzinfo=timezone.utc)
            return (now - dt).days <= max_days
        except ValueError:
            pass
    # Try relative strings like "2 days ago", "3 weeks ago"
    rel = posted_str.lower()
    m = re.search(r"(\d+)\s*(day|week|month)", rel)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"day": n, "week": n * 7, "month": n * 30}[unit]
        return delta <= max_days
    return True  # unknown format → include


def filter_jobs(scored: list) -> list:
    min_score = CONFIG["search"]["min_score"]
    max_days = CONFIG["search"]["max_days_old"]
    passing = []
    for j in scored:
        if not j.get("location_ok", False):
            continue
        if j.get("fit_score", 0) < min_score:
            continue
        if not is_recent(j.get("posted", ""), max_days):
            continue
        passing.append(j)
    print(f"[filter] {len(passing)}/{len(scored)} jobs passed filters")
    return passing


# ── Step 5: Merge into scored_jobs.json ───────────────────────────────────────
def merge_jobs(existing: dict, new_jobs: list) -> dict:
    """Merge new jobs into existing. Never delete manual entries. Never overwrite status/notes."""
    added = 0
    updated = 0
    for job in new_jobs:
        jid = job_id(job)
        if jid not in existing:
            existing[jid] = {
                "id": jid,
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "location": job.get("location", ""),
                "url": job.get("url", ""),
                "posted": job.get("posted", ""),
                "description": job.get("description", ""),
                "source": job.get("source", "Pipeline"),
                "fit_score": job.get("fit_score", 0),
                "score_reason": job.get("score_reason", ""),
                "ai_opener": job.get("ai_opener", ""),
                "location_ok": job.get("location_ok", True),
                "status": "new",
                "notes": "",
                "interviews": [],
                "added_at": datetime.now(tz=timezone.utc).isoformat(),
                "last_updated": datetime.now(tz=timezone.utc).isoformat(),
                "initial_status": None,
            }
            added += 1
        else:
            # Update score data but preserve user-set fields
            existing[jid]["fit_score"] = job.get("fit_score", existing[jid].get("fit_score", 0))
            existing[jid]["score_reason"] = job.get("score_reason", existing[jid].get("score_reason", ""))
            existing[jid]["ai_opener"] = job.get("ai_opener", existing[jid].get("ai_opener", ""))
            existing[jid]["last_updated"] = datetime.now(tz=timezone.utc).isoformat()
            updated += 1
    print(f"[merge] Added {added} new jobs, updated {updated} existing")
    return existing


# ── Step 6: Build HTML ─────────────────────────────────────────────────────────
def build_html(jobs: dict):
    """Write the tracker HTML — the main UI is self-contained in docs/index.html."""
    DOCS_DIR.mkdir(exist_ok=True)
    jobs_json = json.dumps(list(jobs.values()), indent=2, ensure_ascii=False)
    # Write a companion data file for the HTML to fetch
    (DOCS_DIR / "scored_jobs.json").write_text(json.dumps(list(jobs.values()), indent=2))
    print(f"[build] Wrote docs/scored_jobs.json ({len(jobs)} jobs)")


# ── Step 7: Git push ───────────────────────────────────────────────────────────
def git_push():
    cmds = [
        ["git", "add", "scored_jobs.json", "docs/scored_jobs.json", "docs/index.html"],
        ["git", "commit", "-m", f"chore: pipeline run {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"],
        ["git", "push"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if result.returncode != 0 and "nothing to commit" not in result.stdout:
            print(f"[git] WARNING: {' '.join(cmd)} → {result.stderr.strip()}")
        else:
            print(f"[git] {' '.join(cmd[:2])} ✓")


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Job search pipeline")
    parser.add_argument("--no-search", action="store_true", help="Skip AI web search step")
    parser.add_argument("--no-ats", action="store_true", help="Skip ATS scraper step")
    parser.add_argument("--no-push", action="store_true", help="Skip git push step")
    parser.add_argument("--build-only", action="store_true", help="Only rebuild HTML from existing data")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"Job Search Pipeline — {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    existing_jobs = load_jobs()
    print(f"[load] {len(existing_jobs)} existing jobs loaded")

    if args.build_only:
        build_html(existing_jobs)
        return

    new_raw = []

    if not args.no_search:
        new_raw.extend(search_jobs())

    if not args.no_ats:
        new_raw.extend(ats_scraper())

    if new_raw:
        scored = score_jobs(new_raw)
        passing = filter_jobs(scored)
        existing_jobs = merge_jobs(existing_jobs, passing)
        save_jobs(existing_jobs)
    else:
        print("[pipeline] No new jobs found this run")

    build_html(existing_jobs)

    if not args.no_push:
        git_push()

    print(f"\n[done] Pipeline complete. Total jobs in tracker: {len(existing_jobs)}")


if __name__ == "__main__":
    main()
