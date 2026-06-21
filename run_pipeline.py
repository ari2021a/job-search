#!/usr/bin/env python3
"""
Job Search Pipeline — Aaron (Eric) Barrari
"""
import json, os, re, time, hashlib, argparse, subprocess
from datetime import datetime, timezone
from pathlib import Path
import urllib.request

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
PROFILE = (ROOT / "profile.md").read_text()
JOBS_FILE = ROOT / CONFIG["pipeline"]["output_file"]
DOCS_DIR = ROOT / CONFIG["pipeline"]["docs_dir"]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPUS_MODEL = "claude-opus-4-5"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

def call_claude(model, system, user, tools=None, max_tokens=4096):
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")
    payload = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
    if tools:
        payload["tools"] = tools
    data = json.dumps(payload).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=data,
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())

def extract_text(response):
    return "".join(b.get("text","") for b in response.get("content",[]) if b.get("type")=="text")

def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*","",text,flags=re.MULTILINE)
    text = re.sub(r"\s*```$","",text,flags=re.MULTILINE)
    for s,e in [("[","]"),("{","}")]:
        si,ei = text.find(s), text.rfind(e)
        if si!=-1 and ei!=-1 and ei>si:
            try: return json.loads(text[si:ei+1])
            except: continue
    return json.loads(text)

def job_id(job):
    key = f"{job.get('company','')}-{job.get('title','')}-{job.get('url','')}"
    return hashlib.md5(key.lower().encode()).hexdigest()[:12]

def load_jobs():
    if JOBS_FILE.exists():
        return json.loads(JOBS_FILE.read_text())
    return {}

def save_jobs(jobs):
    JOBS_FILE.write_text(json.dumps(jobs, indent=2, ensure_ascii=False))
    print(f"[save] {len(jobs)} jobs saved")

def search_jobs():
    roles_str = ", ".join(CONFIG["search"]["roles"][:8])
    prompt = f"""Find CURRENTLY OPEN jobs posted in the last 30 days.
Target roles: {roles_str}
Industries: FinTech, SaaS, Payments
Work arrangement: Remote EMEA/worldwide ONLY
Seniority: Director / VP / Head of / C-suite
Return 10-15 jobs as JSON array with fields: company, title, location, url, posted, description, source"""
    print("[search] Searching for jobs...")
    try:
        resp = call_claude(OPUS_MODEL, "Find open jobs. Return ONLY valid JSON array.", prompt,
                          tools=[{"type":"web_search_20250305","name":"web_search"}], max_tokens=8192)
        jobs = extract_json(extract_text(resp))
        if isinstance(jobs, list):
            print(f"[search] Found {len(jobs)} jobs")
            return jobs
    except Exception as e:
        print(f"[search] ERROR: {e}")
    return []

ATS_BOARDS = [("Stripe","greenhouse","stripe"),("Wise","greenhouse","wise"),
              ("Revolut","lever","revolut"),("Klarna","greenhouse","klarna"),
              ("Adyen","greenhouse","adyen"),("Brex","greenhouse","brex")]
ROLE_KW = ["revenue operations","revops","sales operations","business development",
           "partnerships","customer success","commercial","head of sales","director of sales"]

def fetch_greenhouse(company, board_id):
    try:
        with urllib.request.urlopen(f"https://boards-api.greenhouse.io/v1/boards/{board_id}/jobs?content=true",timeout=10) as r:
            data = json.loads(r.read())
        jobs = []
        for j in data.get("jobs",[]):
            if not any(kw in j.get("title","").lower() for kw in ROLE_KW): continue
            loc = j.get("location",{}).get("name","")
            if not any(kw in loc.lower() for kw in ["remote","emea","europe","anywhere"]): continue
            jobs.append({"company":company,"title":j["title"],"location":loc,
                        "url":j.get("absolute_url",""),"posted":j.get("updated_at","")[:10],
                        "description":j.get("content","")[:400],"source":"Greenhouse API"})
        return jobs
    except Exception as e:
        print(f"[ats] {board_id}: {e}"); return []

def fetch_lever(company, board_id):
    try:
        with urllib.request.urlopen(f"https://api.lever.co/v0/postings/{board_id}?mode=json",timeout=10) as r:
            data = json.loads(r.read())
        jobs = []
        for j in data:
            if not any(kw in j.get("text","").lower() for kw in ROLE_KW): continue
            loc = j.get("categories",{}).get("location","")
            if not any(kw in loc.lower() for kw in ["remote","emea","europe","anywhere"]): continue
            jobs.append({"company":company,"title":j["text"],"location":loc,
                        "url":j.get("hostedUrl",""),
                        "posted":datetime.fromtimestamp(j.get("createdAt",0)/1000,tz=timezone.utc).strftime("%Y-%m-%d"),
                        "description":j.get("descriptionPlain","")[:400],"source":"Lever API"})
        return jobs
    except Exception as e:
        print(f"[ats] {board_id}: {e}"); return []

def ats_scraper():
    print("[ats] Scraping ATS boards...")
    found = []
    for company, btype, bid in ATS_BOARDS:
        if btype=="greenhouse": found.extend(fetch_greenhouse(company,bid))
        elif btype=="lever": found.extend(fetch_lever(company,bid))
        time.sleep(0.3)
    print(f"[ats] Found {len(found)} postings")
    return found

SCORE_TPL = """Evaluate this job for candidate: {profile}
Job: {company} — {title} — {location}
Description: {description}
Return JSON only: {{"fit_score":1-10,"score_reason":"2-3 sentences","ai_opener":"one sentence","location_ok":true/false}}
If not remote/EMEA, fit_score must be 0."""

def score_job(job):
    try:
        resp = call_claude(HAIKU_MODEL,"Return ONLY JSON, no prose.",
            SCORE_TPL.format(profile=PROFILE[:2000],**{k:job.get(k,"") for k in ["company","title","location","description"]}),
            max_tokens=512)
        return extract_json(extract_text(resp))
    except Exception as e:
        return {"fit_score":0,"score_reason":str(e),"ai_opener":"","location_ok":False}

def score_jobs(jobs):
    print(f"[score] Scoring {len(jobs)} jobs...")
    for i,j in enumerate(jobs):
        print(f"[score] {i+1}/{len(jobs)}: {j.get('company')} — {j.get('title')}")
        j.update(score_job(j)); time.sleep(0.2)
    return jobs

def is_recent(s, max_days=30):
    if not s: return True
    try:
        dt = datetime.strptime(s[:10],"%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(tz=timezone.utc)-dt).days <= max_days
    except: pass
    m = re.search(r"(\d+)\s*(day|week|month)",s.lower())
    if m:
        n,u = int(m.group(1)),m.group(2)
        return {"day":n,"week":n*7,"month":n*30}[u] <= max_days
    return True

def filter_jobs(scored):
    passing = [j for j in scored if j.get("location_ok") and j.get("fit_score",0)>=CONFIG["search"]["min_score"] and is_recent(j.get("posted",""))]
    print(f"[filter] {len(passing)}/{len(scored)} passed"); return passing

def merge_jobs(existing, new_jobs):
    added = 0
    for job in new_jobs:
        jid = job_id(job)
        if jid not in existing:
            existing[jid] = {**job,"id":jid,"status":"new","notes":"","interviews":[],
                "added_at":datetime.now(tz=timezone.utc).isoformat(),
                "last_updated":datetime.now(tz=timezone.utc).isoformat(),"initial_status":None}
            added += 1
        else:
            for k in ["fit_score","score_reason","ai_opener"]:
                existing[jid][k] = job.get(k, existing[jid].get(k))
            existing[jid]["last_updated"] = datetime.now(tz=timezone.utc).isoformat()
    print(f"[merge] Added {added} new jobs"); return existing

def build_html(jobs):
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR/"scored_jobs.json").write_text(json.dumps(list(jobs.values()),indent=2))
    print(f"[build] Wrote docs/scored_jobs.json ({len(jobs)} jobs)")

def git_push():
    for cmd in [["git","add","scored_jobs.json","docs/scored_jobs.json"],
                ["git","commit","-m",f"pipeline run {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"],
                ["git","push"]]:
        r = subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True)
        if r.returncode!=0 and "nothing to commit" not in r.stdout:
            print(f"[git] {r.stderr.strip()}")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--no-search",action="store_true")
    p.add_argument("--no-ats",action="store_true")
    p.add_argument("--no-push",action="store_true")
    args = p.parse_args()
    print(f"\n{'='*50}\nPipeline — {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n{'='*50}\n")
    existing = load_jobs()
    new_raw = []
    if not args.no_search: new_raw.extend(search_jobs())
    if not args.no_ats: new_raw.extend(ats_scraper())
    if new_raw:
        passing = filter_jobs(score_jobs(new_raw))
        existing = merge_jobs(existing, passing)
        save_jobs(existing)
    build_html(existing)
    if not args.no_push: git_push()
    print(f"\n[done] {len(existing)} total jobs in tracker")

if __name__ == "__main__":
    main()
