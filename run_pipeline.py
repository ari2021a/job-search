#!/usr/bin/env python3
"""Job Search Pipeline — Aaron (Eric) Barrari"""

import json, os, re, time, hashlib, argparse, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.request

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
PROFILE = (ROOT / "profile.md").read_text()
JOBS_FILE = ROOT / CONFIG["pipeline"]["output_file"]
DOCS_DIR = ROOT / CONFIG["pipeline"]["docs_dir"]
LAST_RUN_FILE = ROOT / ".last_run"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPUS_MODEL = "claude-opus-4-5"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ── Direct career page targets ────────────────────────────────────────────────
# These companies often post jobs that never appear on job boards
CAREER_PAGES = [
    ("Stripe",        "https://stripe.com/jobs"),
    ("Wise",          "https://wise.com/gb/careers"),
    ("Revolut",       "https://www.revolut.com/en-GB/careers"),
    ("Adyen",         "https://www.adyen.com/careers"),
    ("Checkout.com",  "https://www.checkout.com/careers"),
    ("Klarna",        "https://www.klarna.com/careers"),
    ("Brex",          "https://www.brex.com/careers"),
    ("Rippling",      "https://www.rippling.com/careers"),
    ("Deel",          "https://www.deel.com/careers"),
    ("Workday",       "https://www.workday.com/en-us/company/careers.html"),
    ("HubSpot",       "https://www.hubspot.com/careers"),
    ("Salesforce",    "https://www.salesforce.com/company/careers"),
    ("Pipedrive",     "https://www.pipedrive.com/en/jobs"),
    ("Personio",      "https://www.personio.com/about-personio/careers"),
    ("Factorial",     "https://factorialhr.com/careers"),
    ("Typeform",      "https://www.typeform.com/careers"),
    ("Glovo",         "https://careers.glovoapp.com"),
    ("Cabify",        "https://cabify.com/en/jobs"),
    ("Payhawk",       "https://payhawk.com/careers"),
    ("Pleo",          "https://www.pleo.io/en/careers"),
]

# ── Time window ───────────────────────────────────────────────────────────────
def get_search_hours():
    if not LAST_RUN_FILE.exists():
        print("[time] First run — searching last 72 hours")
        return 72
    print("[time] Subsequent run — searching last 24 hours")
    return 24

def mark_run():
    LAST_RUN_FILE.write_text(datetime.now(tz=timezone.utc).isoformat())

def hours_label(h):
    return "72 hours" if h == 72 else "24 hours"

# ── Claude API ────────────────────────────────────────────────────────────────
def call_claude(model, system, user, tools=None, max_tokens=4096):
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set")
    payload = {"model": model, "max_tokens": max_tokens, "system": system,
               "messages": [{"role": "user", "content": user}]}
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())

def extract_text(resp):
    return "".join(b.get("text","") for b in resp.get("content",[]) if b.get("type")=="text")

def extract_json(text):
    text = re.sub(r"^```(?:json)?\s*","",text.strip(),flags=re.MULTILINE)
    text = re.sub(r"\s*```$","",text,flags=re.MULTILINE)
    for s,e in [("[","]"),("{","}")]:
        si,ei = text.find(s), text.rfind(e)
        if si!=-1 and ei>si:
            try: return json.loads(text[si:ei+1])
            except: pass
    return json.loads(text)

# ── Job ID + deduplication ────────────────────────────────────────────────────
def job_id(job):
    """Stable ID based on company + normalised title + url."""
    company = job.get("company","").lower().strip()
    title   = re.sub(r"\s+"," ", job.get("title","").lower().strip())
    url     = job.get("url","").split("?")[0].rstrip("/")  # strip query params
    return hashlib.md5(f"{company}|{title}|{url}".encode()).hexdigest()[:12]

def dedup(jobs):
    """Remove duplicates within a batch — keep highest-quality entry."""
    seen = {}
    for j in jobs:
        jid = job_id(j)
        if jid not in seen:
            seen[jid] = j
        else:
            # Prefer the one with a longer description
            if len(j.get("description","")) > len(seen[jid].get("description","")):
                seen[jid] = j
    removed = len(jobs) - len(seen)
    if removed:
        print(f"[dedup] Removed {removed} duplicate(s)")
    return list(seen.values())

# ── Load / Save ───────────────────────────────────────────────────────────────
def load_jobs():
    if JOBS_FILE.exists():
        data = json.loads(JOBS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    return {}

def save_jobs(jobs):
    JOBS_FILE.write_text(json.dumps(jobs, indent=2, ensure_ascii=False))
    print(f"[save] {len(jobs)} jobs saved")

# ── Step 1: Job board search (Spanish sites first) ────────────────────────────
def search_job_boards(hours):
    tier_a = CONFIG["search"].get("roles_tier_a",[])[:6]
    tier_b = CONFIG["search"].get("roles_tier_b",[])[:4]
    tier_c = CONFIG["search"].get("roles_tier_c",[])[:3]
    window = hours_label(hours)

    prompt = f"""Search the following job sites for OPEN remote jobs posted in the last {window}.

SEARCH ORDER — start with Spanish sites:
1. Indeed España — es.indeed.com — search "remote" + role titles
2. Glassdoor España — glassdoor.es — search remote roles
3. InfoJobs — infojobs.net — Spain's biggest job board
4. Tecnoempleo — tecnoempleo.com — Spanish tech jobs
5. LinkedIn Jobs — linkedin.com/jobs — filter "Remote" + "Past 24 hours" or "Past week"
6. Wellfound (AngelList) — wellfound.com/jobs — remote startup jobs

Role titles to search (in English AND Spanish):
TIER A: {', '.join(tier_a)}
TIER B: {', '.join(tier_b)}
TIER C: {', '.join(tier_c)}

Spanish equivalents to also search: "Director de Operaciones", "Jefe de Operaciones",
"Responsable de Transformación", "Manager de Operaciones", "Chief of Staff"

ALL of these must be true — exclude anything that fails even one:
✅ Posted in the last {window} only
✅ Fully REMOTE — workable from Spain without relocation
✅ NO hybrid, NO on-site, NO "office presence required"
✅ No significant travel (max occasional trips, under 10%)
✅ Seniority: Manager / Senior Manager / Director / VP / Head of / Chief of Staff
✅ Industries: FinTech, SaaS, Payments, B2B Technology, Consulting, Scale-up
✅ Job is OPEN and still accepting applications right now

Return a JSON array of 10-15 jobs. Each object:
{{
  "company": "...",
  "title": "...",
  "location": "Remote, EMEA" or "Remote, Worldwide" etc,
  "url": "direct link to this specific posting",
  "posted": "YYYY-MM-DD or relative like '3 hours ago'",
  "description": "3-4 sentences about role and requirements",
  "source": "Indeed España / Glassdoor / InfoJobs / LinkedIn / etc"
}}"""

    print(f"[boards] Searching Spanish job boards + LinkedIn (last {window})...")
    try:
        resp = call_claude(OPUS_MODEL,
            "Search job boards and return ONLY a valid JSON array of job postings.",
            prompt,
            tools=[{"type":"web_search_20250305","name":"web_search"}],
            max_tokens=8192)
        jobs = extract_json(extract_text(resp))
        if isinstance(jobs, list):
            print(f"[boards] Found {len(jobs)} jobs from job boards")
            return jobs
    except Exception as e:
        print(f"[boards] ERROR: {e}")
    return []

# ── Step 2: Direct career page scraping ───────────────────────────────────────
def search_career_pages(hours):
    """Ask Claude to visit each company's career page directly and extract matching roles."""
    window = hours_label(hours)
    role_kw = CONFIG["search"].get("roles_tier_a",[])[:5] + CONFIG["search"].get("roles_tier_b",[])[:3]

    companies_str = "\n".join([f"- {name}: {url}" for name, url in CAREER_PAGES])
    roles_str = ", ".join(role_kw)

    prompt = f"""Visit each of these company career pages directly and find open remote jobs posted in the last {window}.

CAREER PAGES TO CHECK:
{companies_str}

Look for roles matching these titles (or similar): {roles_str}

For each career page:
1. Go to the URL
2. Search or filter for remote positions
3. Find any roles posted in the last {window} that match Operations / Strategy / Transformation / Revenue / Change Management / Chief of Staff

Requirements:
✅ Posted in last {window}
✅ Fully remote (workable from Spain)
✅ No hybrid, no on-site
✅ Senior level (Manager / Director / VP / Head of / Chief of Staff)

Return a JSON array of matching jobs found. Each object:
{{
  "company": "...",
  "title": "...",
  "location": "...",
  "url": "direct link to the specific job posting page",
  "posted": "YYYY-MM-DD or relative",
  "description": "3-4 sentences from the job description",
  "source": "Company Career Page"
}}

If no matching jobs found on a career page, skip it. Return empty array [] if nothing found."""

    print(f"[careers] Checking {len(CAREER_PAGES)} company career pages directly...")
    try:
        resp = call_claude(OPUS_MODEL,
            "Visit career pages and find open remote jobs. Return ONLY a valid JSON array.",
            prompt,
            tools=[{"type":"web_search_20250305","name":"web_search"}],
            max_tokens=8192)
        jobs = extract_json(extract_text(resp))
        if isinstance(jobs, list):
            print(f"[careers] Found {len(jobs)} jobs from career pages")
            return jobs
    except Exception as e:
        print(f"[careers] ERROR: {e}")
    return []

# ── Step 3: ATS API scraper ───────────────────────────────────────────────────
ATS_BOARDS = [
    ("Stripe","greenhouse","stripe"),
    ("Wise","greenhouse","wise"),
    ("Adyen","greenhouse","adyen"),
    ("Brex","greenhouse","brex"),
    ("Rippling","greenhouse","rippling"),
    ("Checkout.com","greenhouse","checkoutcom"),
    ("Personio","greenhouse","personio"),
    ("Pleo","greenhouse","pleo"),
    ("Revolut","lever","revolut"),
    ("Deel","lever","deel"),
    ("Cabify","lever","cabify"),
]
ROLE_KW = ["chief of staff","strategy","operations","transformation","change",
           "revenue","business operations","operational excellence","process",
           "customer success","program manager","pmo","gtm","delivery"]

def fetch_greenhouse(company, board_id, hours):
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    try:
        url = f"https://boards-api.greenhouse.io/v1/boards/{board_id}/jobs?content=true"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        jobs = []
        for j in data.get("jobs",[]):
            if not any(kw in j.get("title","").lower() for kw in ROLE_KW): continue
            loc = j.get("location",{}).get("name","")
            if not any(kw in loc.lower() for kw in ["remote","emea","europe","anywhere","worldwide"]): continue
            if any(kw in loc.lower() for kw in ["hybrid","on-site","onsite"]): continue
            updated = j.get("updated_at","")[:10]
            try:
                if datetime.strptime(updated,"%Y-%m-%d").replace(tzinfo=timezone.utc) < cutoff: continue
            except: pass
            jobs.append({"company":company,"title":j["title"],"location":loc,
                "url":j.get("absolute_url",""),"posted":updated,
                "description":re.sub('<[^<]+?>',' ',j.get("content",""))[:400].strip(),
                "source":"Greenhouse API"})
        return jobs
    except Exception as e:
        print(f"[ats] {board_id}: {e}"); return []

def fetch_lever(company, board_id, hours):
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    try:
        url = f"https://api.lever.co/v0/postings/{board_id}?mode=json"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        jobs = []
        for j in data:
            if not any(kw in j.get("text","").lower() for kw in ROLE_KW): continue
            loc = j.get("categories",{}).get("location","")
            if not any(kw in loc.lower() for kw in ["remote","emea","europe","anywhere","worldwide"]): continue
            if any(kw in loc.lower() for kw in ["hybrid","on-site","onsite"]): continue
            created = datetime.fromtimestamp(j.get("createdAt",0)/1000, tz=timezone.utc)
            if created < cutoff: continue
            jobs.append({"company":company,"title":j["text"],"location":loc,
                "url":j.get("hostedUrl",""), "posted":created.strftime("%Y-%m-%d"),
                "description":j.get("descriptionPlain","")[:400].strip(),
                "source":"Lever API"})
        return jobs
    except Exception as e:
        print(f"[ats] {board_id}: {e}"); return []

def ats_scraper(hours):
    print(f"[ats] Scraping {len(ATS_BOARDS)} ATS boards...")
    found = []
    for company, btype, bid in ATS_BOARDS:
        if btype=="greenhouse": found.extend(fetch_greenhouse(company, bid, hours))
        elif btype=="lever":    found.extend(fetch_lever(company, bid, hours))
        time.sleep(0.25)
    print(f"[ats] Found {len(found)} postings")
    return found

# ── Score ─────────────────────────────────────────────────────────────────────
SCORE_PROMPT = """Evaluate this job for the candidate. Return ONLY a JSON object, no prose.

CANDIDATE PROFILE:
{profile}

JOB:
Company: {company}
Title: {title}
Location: {location}
Description: {description}

Return exactly:
{{
  "fit_score": <1-10>,
  "location_ok": <true if fully remote from Spain / false if hybrid or on-site>,
  "score_reason": "<2 sentence overall assessment>",
  "ai_opener": "<one compelling opening sentence for cover letter>",
  "match_perfect": ["<requirement candidate fully meets>"],
  "match_partial": ["<requirement candidate partially meets>"],
  "match_missing": ["<important requirement candidate lacks>"],
  "red_flags": ["<concern to know before applying — travel, seniority mismatch, niche tool, etc>"]
}}

Rules:
- fit_score 0 if location_ok is false OR if travel > 10%
- 8-10: exact match remote role + industry + seniority
- 6-7: good match minor gaps
- 4-5: partial match
- 1-3: weak
- red_flags can be []"""

def score_job(job):
    try:
        resp = call_claude(HAIKU_MODEL, "Return ONLY valid JSON.",
            SCORE_PROMPT.format(profile=PROFILE[:2500], **{
                k: job.get(k,"") for k in ["company","title","location","description"]}),
            max_tokens=800)
        return extract_json(extract_text(resp))
    except Exception as e:
        return {"fit_score":0,"score_reason":str(e),"ai_opener":"","location_ok":False,
                "match_perfect":[],"match_partial":[],"match_missing":[],"red_flags":[]}

def score_all(jobs):
    print(f"[score] Scoring {len(jobs)} jobs...")
    for i,j in enumerate(jobs):
        print(f"[score] {i+1}/{len(jobs)}: {j.get('company')} — {j.get('title')}")
        j.update(score_job(j)); time.sleep(0.2)
    return jobs

# ── Filter ────────────────────────────────────────────────────────────────────
def is_recent(s, hours):
    if not s: return True
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ","%Y-%m-%dT%H:%M:%S+00:00","%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:10],"%Y-%m-%d").replace(tzinfo=timezone.utc)
            return dt >= cutoff
        except: pass
    m = re.search(r"(\d+)\s*(hour|day|week|month)",s.lower())
    if m:
        n,u = int(m.group(1)),m.group(2)
        return {"hour":n,"day":n*24,"week":n*168,"month":n*720}[u] <= hours
    return True

def filter_jobs(scored, hours):
    passing = [j for j in scored
               if j.get("location_ok") and
               j.get("fit_score",0) >= CONFIG["search"]["min_score"] and
               is_recent(j.get("posted",""), hours)]
    print(f"[filter] {len(passing)}/{len(scored)} passed filters")
    return passing

# ── Merge (never delete manual jobs) ─────────────────────────────────────────
def merge_jobs(existing, new_jobs):
    added = 0
    for job in new_jobs:
        jid = job_id(job)
        if jid not in existing:
            existing[jid] = {
                **job, "id": jid, "status": "new", "notes": "", "interviews": [],
                "added_at": datetime.now(tz=timezone.utc).isoformat(),
                "last_updated": datetime.now(tz=timezone.utc).isoformat(),
                "initial_status": None}
            added += 1
        else:
            # Update scores but never overwrite user data
            for k in ["fit_score","score_reason","ai_opener","location_ok",
                      "match_perfect","match_partial","match_missing","red_flags"]:
                existing[jid][k] = job.get(k, existing[jid].get(k))
            existing[jid]["last_updated"] = datetime.now(tz=timezone.utc).isoformat()
    print(f"[merge] +{added} new jobs | {len(existing)} total")
    return existing

# ── Build ─────────────────────────────────────────────────────────────────────
def build_html(jobs):
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR/"scored_jobs.json").write_text(json.dumps(list(jobs.values()),indent=2))
    print(f"[build] docs/scored_jobs.json updated ({len(jobs)} jobs)")

# ── Git push ──────────────────────────────────────────────────────────────────
def git_push():
    for cmd in [
        ["git","add","scored_jobs.json","docs/scored_jobs.json",".last_run"],
        ["git","commit","-m",
         f"pipeline {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"],
        ["git","push"]]:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in r.stdout:
            print(f"[git] {r.stderr.strip()}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--no-boards",   action="store_true", help="Skip job board search")
    p.add_argument("--no-careers",  action="store_true", help="Skip career page search")
    p.add_argument("--no-ats",      action="store_true", help="Skip ATS API scraper")
    p.add_argument("--no-push",     action="store_true", help="Skip git push")
    p.add_argument("--hours",       type=int, default=None, help="Override time window")
    args = p.parse_args()

    hours = args.hours if args.hours else get_search_hours()

    print(f"\n{'='*55}")
    print(f"Pipeline — {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Window: last {hours_label(hours)} | Sources: boards + career pages + ATS")
    print(f"{'='*55}\n")

    existing = load_jobs()
    new_raw  = []

    if not args.no_boards:
        new_raw.extend(search_job_boards(hours))

    if not args.no_careers:
        new_raw.extend(search_career_pages(hours))

    if not args.no_ats:
        new_raw.extend(ats_scraper(hours))

    if new_raw:
        # Dedup before scoring (saves API calls)
        new_raw = dedup(new_raw)
        # Also remove jobs already in tracker
        new_raw = [j for j in new_raw if job_id(j) not in existing]
        print(f"[pipeline] {len(new_raw)} new unique jobs to score")

        if new_raw:
            scored  = score_all(new_raw)
            passing = filter_jobs(scored, hours)
            existing = merge_jobs(existing, passing)
            save_jobs(existing)
    else:
        print("[pipeline] No new jobs found this run")

    build_html(existing)
    mark_run()

    if not args.no_push:
        git_push()

    print(f"\n✅ Done — {len(existing)} total jobs in tracker")

if __name__ == "__main__":
    main()
