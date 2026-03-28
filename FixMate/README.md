# FIXMATE — AI Windows Troubleshooter

> Describe your Windows problem in plain English. FIXMATE finds the fix, verifies it with Claude, and executes it — then learns from the outcome.

---

## How It Works

FIXMATE runs a **three-layer AI pipeline** for every problem submitted:

```
User input (text or screenshot)
        │
        ▼
┌─────────────────────────────────┐
│  Layer 1: Semantic Search       │  sentence-transformers (all-MiniLM-L6-v2)
│  175-entry local knowledge base │  Cosine similarity, no API call needed
└────────────────┬────────────────┘
                 │  match found?
        ┌────────┴────────┐
       YES               NO
        │                 │
        ▼                 ▼
┌───────────────┐  ┌──────────────────────────────┐
│  Layer 2:     │  │  Layer 3: Gemini Fallback     │
│  Claude       │  │  Generates fix suggestions   │
│  Safety Check │  │  for unknown problems        │
│  (optional)   │  └──────────────────────────────┘
└───────┬───────┘
        │  verified fixes
        ▼
 Execute commands on the machine
        │
        ▼
 Feedback recorded → SQLite → success rates update
```

### Layer 1 — Local Semantic Search
`ai_engine.py` loads `all-MiniLM-L6-v2` locally at startup, precomputes embeddings for all 175 KB entries, and returns ranked matches by cosine similarity. No internet required for common problems.

### Layer 2 — Claude Safety Verification (optional toggle)
When enabled, matched fixes are sent to `claude-opus-4-6` which confirms relevance, reorders by confidence, and adds diagnostic notes. Shows a green **Claude Verified** badge in the UI.

### Layer 3 — Gemini Fallback + Vision
- If no confident match exists, `gemini-2.0-flash` generates custom fix suggestions
- Screenshot upload: Gemini Vision extracts the visible error text before semantic search runs

### Self-Learning Feedback Loop
Every executed command is logged to SQLite (`command_executions` table). User feedback (`/feedback`) updates the `fix_outcomes` table. `get_solution_success_rate()` uses this history to show real success percentages per command.

---

## Features

| Feature | Details |
|---|---|
| Semantic search | `all-MiniLM-L6-v2`, cosine similarity, local — no API call |
| Claude safety layer | Verifies fixes before showing them; toggle on/off |
| Gemini Vision | Upload a screenshot, Gemini reads the error text |
| Gemini fallback | Generates fixes for problems not in the KB |
| Command execution | Runs actual PowerShell/CMD fixes with live output |
| Feedback loop | SQLite tracks per-command success rates over time |
| AI Activity Log | Terminal-style panel showing which AI layers fired |
| Stats dashboard | Total fixes, success rate, 24h activity — all from real DB |
| War Room UI | Custom dark diagnostic theme — Rajdhani + JetBrains Mono, amber/jade palette, scanline overlay, animated solution cards |

---

## Setup

**Requirements:** Python 3.10+, Windows

```powershell
git clone <repo-url>
cd FIXMATE

pip install -r requirements.txt

copy .env.example .env
# Edit .env and fill in your API keys

python app.py
```

Open `http://127.0.0.1:5050`

**Access from another device (phone/tablet):** Flask binds to `0.0.0.0`, so any device on the same network can reach it. Run `ipconfig` to find your machine's local IP, then open `http://<your-ip>:5050` on the other device.

### Environment Variables

```env
GEMINI_API_KEY=your_key_here
ANTHROPIC_API_KEY=your_key_here
GEMINI_MODEL=gemini-2.0-flash
AI_TS_DEBUG=0
AI_TS_DEBUG_GEMINI=0
AI_TS_ALLOW_TFIDF_FALLBACK=0
GEMINI_TIMEOUT_SECONDS=20
```

---

## Project Structure

```
FIXMATE/
├── app.py                  # Flask routes + AI orchestration (~1400 lines)
├── ai_engine.py            # Semantic search engine (sentence-transformers)
├── db.py                   # SQLite schema, analytics, feedback helpers
├── issues.json             # 175-entry knowledge base
├── templates/
│   └── enhanced_index.html # War Room diagnostic UI (custom CSS/JS, no framework)
├── scripts/
│   └── expand_kb.py        # KB generation/expansion tool (AI-generated via Gemini)
├── requirements.txt        # Pinned dependencies
└── .env.example            # API key template
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/analyze` | Main analysis — returns ranked fixes |
| POST | `/analyze-image` | Screenshot diagnosis via Gemini Vision |
| POST | `/execute` | Execute a single fix command |
| POST | `/execute-all` | Execute all recommended commands |
| POST | `/feedback` | Record user outcome (fixed/not fixed) |
| GET | `/stats` | Execution stats from SQLite |
| GET | `/logs` | Recent command execution log |
| GET | `/system-info` | Current machine info (OS, RAM, disk) |
| GET | `/health` | Health check |

---

## Tech Stack

| Component | Technology |
|---|---|
| Backend | Python 3.10+, Flask |
| Semantic search | `sentence-transformers` (all-MiniLM-L6-v2) |
| Safety verification | Anthropic Claude (claude-opus-4-6) |
| Vision + fallback | Google Gemini (gemini-2.0-flash) |
| Database | SQLite via `db.py` |
| Frontend | Vanilla JS, custom CSS (Rajdhani + JetBrains Mono), Bootstrap 5 utilities |

---

## Demo Script

1. Type **"my computer keeps freezing"** → watch Layer 1 semantic match fire
2. Enable **Claude Safety Verification** toggle → watch Layer 2 verify and badge appears
3. Upload a **screenshot of a blue screen** → watch Gemini Vision extract the stop code
4. Click **Execute Fix** → see real command output
5. Click **This Fixed It** → feedback recorded, success rate updates

> Note: Command execution requires Administrator privileges on Windows.
