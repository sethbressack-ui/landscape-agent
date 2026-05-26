# Company Landscape Agent

Keeps the [Company Landscape Google Sheet](https://docs.google.com/spreadsheets/d/1HmUfduG838w0m9BCxicWmqDQ5T0S5lwXzwEQ-v5lx3A/edit) up to date automatically.

## What it does

| Schedule | Job | What happens |
|---|---|---|
| Every Monday 9 AM | **Weekly** | Reads the last 7 days of `#company-landscape`, adds new companies, updates existing fields |
| 1st of each month | **Monthly** | Searches the web for funding rounds, acquisitions, leadership changes, and shutdowns across all active companies |

Both jobs write changes directly back to the sheet. The monthly job skips companies already marked Acquired or Closed.

---

## One-time setup (≈ 20 minutes)

### 1 — Slack bot token

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name it `Landscape Agent`, pick your workspace
3. **OAuth & Permissions** → Scopes → add these Bot Token Scopes:
   - `channels:read`
   - `channels:history`
   - `groups:read` *(if the channel is private)*
   - `groups:history` *(if the channel is private)*
4. **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-…`)
5. Invite the bot to `#company-landscape`: `/invite @Landscape Agent`

### 2 — Google service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → create or pick a project
2. **APIs & Services → Library** → enable **Google Sheets API**
3. **IAM & Admin → Service Accounts** → **Create Service Account**
   - Name: `landscape-agent`
4. Click the new service account → **Keys → Add Key → Create new key → JSON** → download
5. Open the downloaded JSON, copy the `client_email` value (looks like `landscape-agent@…iam.gserviceaccount.com`)
6. Open the Google Sheet → **Share** → paste that email → give it **Editor** access

### 3 — GitHub repository secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `SLACK_BOT_TOKEN` | The `xoxb-…` token from step 1 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The **entire contents** of the JSON file from step 2 |

### 4 — Push the files

```bash
git add agent.py requirements.txt .github/
git commit -m "Add company landscape agent"
git push
```

The agent will run on schedule automatically. You can also trigger it manually from the **Actions** tab → **Company Landscape Agent** → **Run workflow**.

---

## Running locally

```bash
# Install deps
pip install -r requirements.txt

# Set env vars
export ANTHROPIC_API_KEY="sk-ant-..."
export SLACK_BOT_TOKEN="xoxb-..."
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/service-account.json)"

# Run
python agent.py --mode weekly
python agent.py --mode monthly
```

---

## What the agent changes (and doesn't)

The agent **only updates cells with new information it found** — it never blanks out existing data. Every update is printed to the Actions log with the source/reason, so you can review what changed after each run.

The **weekly job** does not use web search on existing companies — it only acts on information explicitly posted in Slack. Use the monthly job (or trigger it manually) for a full web refresh.

The **monthly job** skips companies with Status = `Acquired` or `Closed`, since there's nothing new to find.

---

## Estimated costs

| Job | Approximate cost per run |
|---|---|
| Weekly | < $0.10 (one Claude call, minimal search) |
| Monthly | $1–3 (8–9 Claude calls, each with multiple web searches across 85+ companies) |
