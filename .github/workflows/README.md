# ⚡ Eric's AI Job Search Engine

Automated job search pipeline + tracker for **Aaron (Eric) Barrari** — Revenue Operations & Strategy leader.

## Architecture

```
.
├── run_pipeline.py          # Main pipeline (search → score → filter → save → build)
├── config.json              # Search config, roles, locations, thresholds
├── profile.md               # Candidate profile (used for AI scoring)
├── scored_jobs.json         # Single source of truth — NEVER manually edit
├── docs/
│   ├── index.html           # Tracker PWA (GitHub Pages)
│   ├── scored_jobs.json     # Pipeline output for frontend
│   └── manifest.json        # PWA manifest
└── .github/workflows/
    ├── pipeline.yml         # Runs pipeline 3x daily (Sun–Thu)
    └── deploy-pages.yml     # Deploys docs/ to GitHub Pages
```

## Setup (one-time)

### 1. Create GitHub Repository

```bash
git init
git add .
git commit -m "feat: initial job search engine"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/job-search.git
git push -u origin main
```

### 2. Set GitHub Secret

Go to: **Repository → Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|------|-------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (`sk-ant-…`) |

### 3. Enable GitHub Pages

Go to: **Repository → Settings → Pages**
- Source: **GitHub Actions**
- The `deploy-pages.yml` workflow handles the rest.

### 4. Enable GitHub Actions

Go to: **Repository → Actions → Enable Actions**

The pipeline will run automatically 3x daily (Sun–Thu at 07:00, 13:00, 19:00 UTC).
To trigger manually: **Actions → Job Search Pipeline → Run workflow**

## Local Development

```bash
# Run full pipeline
ANTHROPIC_API_KEY=sk-ant-... python run_pipeline.py

# Skip web search (faster, ATS only)
ANTHROPIC_API_KEY=sk-ant-... python run_pipeline.py --no-search

# Skip git push (test locally)
ANTHROPIC_API_KEY=sk-ant-... python run_pipeline.py --no-push

# Rebuild HTML only (no API calls)
python run_pipeline.py --build-only

# Serve tracker locally
python -m http.server 8000 --directory docs
# Open http://localhost:8000
```

## Tracker Features

Open your GitHub Pages URL (e.g. `https://username.github.io/job-search/`)

### Per-job AI Features (requires Anthropic API key in browser)
- **✉️ Cover Letter** — personalised, ATS-optimised
- **🎯 Interview Prep** — 8 questions + STAR talking points + questions to ask
- **🎤 Voice Simulation** — stage-aware dialogue (phone/HR/technical/manager)
- **🏢 Company Dossier** — funding, culture, red flags, smart questions (uses web search)
- **📄 CV Tailoring** — bullet points rewritten to mirror job description
- **💙 Emotional Support** — context-aware mental coaching

### Global Features
- Add any job by pasting a URL → Claude extracts details instantly
- Status tracking: New → Saved → Applied → Interview → Offer → Rejected
- Interview rounds tracker (date, stage, interviewer, outcome)
- Notes per job
- Analytics: funnel chart, conversion rates, avg fit score
- Dark mode only (OLED-friendly)
- PWA — installable on mobile

### First Visit
You'll be prompted for your Anthropic API key. It's stored **only in localStorage** — never sent anywhere except `api.anthropic.com`.

## Adding Jobs Manually

Paste any job URL in the "Add by URL" bar at the top. Claude will:
1. Extract company, title, location, description
2. Score it against your profile (1-10)
3. Add it immediately with status `new`

Manual jobs are **never deleted** by the pipeline (they have `initial_status: "manual"`).

## scored_jobs.json Schema

```json
{
  "id": "abc123def456",
  "company": "Stripe",
  "title": "Head of Revenue Operations, EMEA",
  "location": "Remote, EMEA",
  "url": "https://stripe.com/jobs/...",
  "posted": "2025-01-15",
  "description": "...",
  "source": "Greenhouse API",
  "fit_score": 9,
  "score_reason": "...",
  "ai_opener": "...",
  "location_ok": true,
  "status": "applied",
  "notes": "Spoke to recruiter on LinkedIn",
  "interviews": [
    { "date": "2025-01-20", "stage": "Phone Screen", "interviewer": "Sarah M.", "outcome": "Passed" }
  ],
  "added_at": "2025-01-16T10:00:00Z",
  "last_updated": "2025-01-20T14:30:00Z",
  "initial_status": null
}
```

## Extending ATS Sources

Add entries to `ATS_BOARDS` in `run_pipeline.py`:

```python
ATS_BOARDS = [
    ("Stripe", "greenhouse", "stripe"),
    ("YourTargetCompany", "greenhouse", "their-board-id"),
    ("AnotherCompany", "lever", "their-lever-slug"),
    ...
]
```

Find Greenhouse board IDs at: `https://boards-api.greenhouse.io/v1/boards/{id}/jobs`
Find Lever board IDs at: `https://api.lever.co/v0/postings/{id}`
