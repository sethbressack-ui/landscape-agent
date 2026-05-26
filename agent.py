#!/usr/bin/env python3
"""
Company Landscape Agent
=======================
Keeps the Grow Therapy company-landscape Google Sheet up to date.

Modes
-----
weekly   Read the last 7 days of #company-landscape, add new companies,
         update existing entries with any new facts mentioned.

monthly  Search the web for changes to every active company: funding rounds,
         acquisitions, leadership changes, shutdowns.

Required environment variables
-------------------------------
ANTHROPIC_API_KEY           Your Anthropic API key
SLACK_BOT_TOKEN             Slack bot token  (xoxb-…)
GOOGLE_SERVICE_ACCOUNT_JSON Full JSON of a GCP service-account key file
                            (share the Sheet with the SA's email as Editor)

Usage
-----
python agent.py --mode weekly
python agent.py --mode monthly
"""

import os, sys, json, time, argparse, textwrap, re
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic
from slack_sdk import WebClient as Slack
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build as gbuild

# ── Config ─────────────────────────────────────────────────────────────────────

SPREADSHEET_ID = "1HmUfduG838w0m9BCxicWmqDQ5T0S5lwXzwEQ-v5lx3A"
SHEET_TAB      = "Company Landscape Database — May 2026"
SLACK_CHANNEL  = "company-landscape"   # without the #
MODEL          = "claude-sonnet-4-20250514"
MONTHLY_BATCH  = 10   # companies per web-search batch

# Must match the sheet's actual header row (order matters for new-row appends)
COLUMNS = [
    "Category", "Company", "Website", "CEO LinkedIn", "Status",
    "Year Founded", "# Employees (Approx)", "Cumulative Funding Raised",
    "Last Round Date", "Last Round Size", "Key Investors",
    "Revenue (if available)", "Acquired By", "Acquisition Date",
]


# ── Google Sheets helpers ──────────────────────────────────────────────────────

def sheets_svc():
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gbuild("sheets", "v4", credentials=creds).spreadsheets()


def sheet_read(svc) -> list[list[str]]:
    res = svc.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_TAB}'!A1:N500",
    ).execute()
    return res.get("values", [])


def sheet_write(svc, cell_range: str, values: list[list]):
    """Write to an A1-notation range, e.g. 'E5' or 'B2:C2'."""
    svc.values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_TAB}'!{cell_range}",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def sheet_append(svc, rows: list[list]):
    svc.values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_TAB}'!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def col_letter(idx: int) -> str:
    """0-indexed column number → A, B, … Z, AA, …"""
    result = ""
    while True:
        result = chr(ord("A") + idx % 26) + result
        idx = idx // 26 - 1
        if idx < 0:
            break
    return result


def apply_updates(svc, updates: list[dict]):
    """
    Apply a list of {company, field, new_value} dicts to the sheet.
    Re-reads the sheet once, then applies all updates.
    """
    rows      = sheet_read(svc)
    header    = rows[0] if rows else COLUMNS
    data_rows = rows[1:]
    co_col    = header.index("Company") if "Company" in header else 1

    for upd in updates:
        co_name = upd.get("company", "").strip()
        field   = upd.get("field", "").strip()
        value   = str(upd.get("new_value", "")).strip()
        reason  = upd.get("reason", "")

        if not co_name or not field or not value:
            continue
        if field not in header:
            print(f"  ⚠  Unknown field '{field}' — skipping")
            continue

        col_idx = header.index(field)
        row_idx = next(
            (i for i, r in enumerate(data_rows)
             if len(r) > co_col and r[co_col].strip() == co_name),
            None,
        )
        if row_idx is None:
            print(f"  ⚠  Company not found: '{co_name}' — skipping")
            continue

        sheet_row = row_idx + 2   # 1-indexed + skip header
        cell      = f"{col_letter(col_idx)}{sheet_row}"
        print(f"  ✎  {co_name} / {field} → {value!r}  [{reason}]")
        sheet_write(svc, cell, [[value]])
        time.sleep(0.25)          # stay under Sheets API rate limit


# ── Slack helpers ──────────────────────────────────────────────────────────────

def slack_fetch(days_back: int = 7) -> str:
    """Return formatted messages from #company-landscape for the last N days."""
    client  = Slack(token=os.environ["SLACK_BOT_TOKEN"])
    oldest  = str((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp())

    # Resolve channel name → ID
    channel_id, cursor = None, None
    while not channel_id:
        resp = client.conversations_list(
            types="public_channel,private_channel", limit=200, cursor=cursor
        )
        for ch in resp["channels"]:
            if ch["name"] == SLACK_CHANNEL:
                channel_id = ch["id"]
                break
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    if not channel_id:
        raise RuntimeError(f"#{SLACK_CHANNEL} not found — is the bot invited?")

    # Page through history
    msgs, cursor = [], None
    while True:
        resp = client.conversations_history(
            channel=channel_id, oldest=oldest, limit=200, cursor=cursor
        )
        msgs.extend(resp["messages"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    lines = []
    for m in sorted(msgs, key=lambda x: float(x["ts"])):
        ts   = datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc).strftime("%Y-%m-%d")
        text = m.get("text", "").strip()
        if text:
            lines.append(f"[{ts}] {text}")
    return "\n".join(lines) if lines else "(no messages found in this window)"


# ── Anthropic helpers ──────────────────────────────────────────────────────────

def llm(
    client: anthropic.Anthropic,
    system: str,
    user: str,
    use_search: bool = False,
) -> str:
    """
    Run a single agent call, handling the tool-use loop.

    For Claude's built-in web_search tool, Anthropic executes the search
    server-side.  We keep the loop running until stop_reason == 'end_turn'.
    """
    tools    = [{"type": "web_search_20250305", "name": "web_search"}] if use_search else []
    messages = [{"role": "user", "content": user}]

    for _ in range(20):   # safety cap on iterations
        kwargs = dict(
            model      = MODEL,
            max_tokens = 8192,
            system     = system,
            messages   = messages,
        )
        if tools:
            kwargs["tools"] = tools

        resp = client.messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if b.type == "text")

        if resp.stop_reason == "end_turn":
            return text

        if resp.stop_reason == "tool_use":
            # Anthropic executes web_search server-side; we acknowledge each
            # tool call with an empty result block and continue the loop.
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": b.id, "content": ""}
                    for b in resp.content if b.type == "tool_use"
                ],
            })
        else:
            return text   # max_tokens or other stop

    return text   # fallback after cap


def extract_json(text: str) -> Any:
    """Pull the first valid JSON object or array out of a text blob."""
    text = re.sub(r"```(?:json)?", "", text).strip()
    for opener, closer in [("{", "}"), ("[", "]")]:
        s, e = text.find(opener), text.rfind(closer)
        if s != -1 and e != -1:
            try:
                return json.loads(text[s : e + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"No valid JSON in response:\n{text[:400]}")


# ── Weekly job ─────────────────────────────────────────────────────────────────

WEEKLY_SYSTEM = textwrap.dedent("""
    You maintain a database of behavioral-health and digital-health startups.

    You will receive:
    1. Recent Slack messages from #company-landscape
    2. The current list of companies in the database

    Identify:
    A) New companies mentioned that are NOT yet in the database.
       Use web search to fill in as many details as possible.
    B) New factual information about companies that ARE in the database.

    Return ONLY valid JSON in this exact shape:
    {
      "new_companies": [
        {
          "Company": "",
          "Website": "",
          "Status": "Active",
          "Year Founded": "",
          "# Employees (Approx)": "",
          "Cumulative Funding Raised": "",
          "Last Round Date": "",
          "Last Round Size": "",
          "Key Investors": "",
          "Revenue (if available)": "",
          "Acquired By": "",
          "Acquisition Date": ""
        }
      ],
      "updates": [
        {
          "company": "<exact name as in database>",
          "field": "<column name>",
          "new_value": "<value>",
          "reason": "<≤15 word source/justification>"
        }
      ]
    }

    Rules:
    - Status must be: Active, Acquired, or Closed
    - Only include fields you are confident about; leave others as ""
    - Do not invent data not present in the messages or web search results
    - Return empty arrays if nothing new was found
    - No markdown, no explanation — JSON only
""").strip()


def run_weekly(client: anthropic.Anthropic, svc):
    print("\n── WEEKLY JOB ─────────────────────────────────────────────")

    print("Fetching Slack messages (last 7 days)…")
    messages = slack_fetch(days_back=7)

    print("Reading sheet…")
    rows      = sheet_read(svc)
    header    = rows[0] if rows else COLUMNS
    data_rows = rows[1:] if len(rows) > 1 else []
    co_col    = header.index("Company") if "Company" in header else 1
    companies = [r[co_col] for r in data_rows if len(r) > co_col and r[co_col]]

    user_msg = f"""
RECENT #COMPANY-LANDSCAPE MESSAGES:
{messages}

COMPANIES ALREADY IN DATABASE ({len(companies)} total):
{json.dumps(companies, indent=2)}

Today: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}
""".strip()

    print("Running agent analysis (with web search)…")
    raw    = llm(client, WEEKLY_SYSTEM, user_msg, use_search=True)
    result = extract_json(raw)

    # ── New companies ──────────────────────────────────────────────────────────
    new_cos = result.get("new_companies", [])
    if new_cos:
        print(f"\nAdding {len(new_cos)} new companies:")
        new_rows = []
        for co in new_cos:
            print(f"  + {co.get('Company', '?')}")
            row = [co.get(col, "") for col in COLUMNS]
            new_rows.append(row)
        sheet_append(svc, new_rows)
    else:
        print("No new companies found.")

    # ── Updates ────────────────────────────────────────────────────────────────
    updates = result.get("updates", [])
    if updates:
        print(f"\nApplying {len(updates)} field updates:")
        apply_updates(svc, updates)
    else:
        print("No field updates found.")

    print("\n✓ Weekly job complete.")


# ── Monthly job ────────────────────────────────────────────────────────────────

MONTHLY_SYSTEM = textwrap.dedent("""
    You maintain a database of behavioral-health and digital-health startups.

    For each company listed, search the web for news in the last 30 days that
    would change any of these fields:

      Status, Cumulative Funding Raised, Last Round Date, Last Round Size,
      Key Investors, Revenue (if available), Acquired By, Acquisition Date,
      # Employees (Approx), CEO LinkedIn, Website

    Return ONLY a JSON array:
    [
      {
        "company": "<exact company name>",
        "field":   "<column name>",
        "new_value": "<updated value>",
        "reason":  "<≤15 word source>"
      }
    ]

    Rules:
    - Status must be: Active, Acquired, or Closed
    - Only return entries with concrete new information from this month
    - Return [] if nothing changed
    - No markdown, no explanation — JSON only
""").strip()


def run_monthly(client: anthropic.Anthropic, svc):
    print("\n── MONTHLY JOB ────────────────────────────────────────────")

    print("Reading sheet…")
    rows      = sheet_read(svc)
    header    = rows[0] if rows else COLUMNS
    data_rows = rows[1:] if len(rows) > 1 else []
    co_col    = header.index("Company") if "Company" in header else 1
    st_col    = header.index("Status")  if "Status"  in header else None

    # Skip already-acquired or closed companies (nothing new to find)
    active = [
        r for r in data_rows
        if len(r) > co_col and r[co_col]
        and (st_col is None
             or len(r) <= st_col
             or r[st_col] not in ("Acquired", "Closed"))
    ]
    print(f"{len(active)} active companies to refresh.")

    all_updates = []
    today       = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for i in range(0, len(active), MONTHLY_BATCH):
        batch = active[i : i + MONTHLY_BATCH]
        names = [r[co_col] for r in batch]
        print(f"\nBatch {i // MONTHLY_BATCH + 1} / {-(-len(active) // MONTHLY_BATCH)}: "
              f"{', '.join(names)}")

        # Give Claude brief current-state context so it knows what already exists
        summaries = []
        for r in batch:
            rd = {header[j]: r[j] for j in range(min(len(header), len(r)))}
            summaries.append(
                f"• {rd.get('Company','?')}  "
                f"[Status: {rd.get('Status','?')}]  "
                f"[Last round: {rd.get('Last Round Size','?')} on "
                f"{rd.get('Last Round Date','?')}]  "
                f"[Acquired By: {rd.get('Acquired By','?')}]"
            )

        user_msg = f"""
Today: {today}

Search for news from the last 30 days about each company below.
Look specifically for: new funding rounds, acquisitions (as buyer or target),
leadership changes, new products, and shutdowns.

COMPANIES TO RESEARCH:
{chr(10).join(summaries)}
""".strip()

        try:
            raw     = llm(client, MONTHLY_SYSTEM, user_msg, use_search=True)
            updates = extract_json(raw)
            if isinstance(updates, list):
                print(f"  → {len(updates)} update(s) found")
                all_updates.extend(updates)
            else:
                print("  → unexpected response shape, skipping batch")
        except (ValueError, Exception) as e:
            print(f"  ⚠  Error processing batch: {e}")

        time.sleep(3)   # brief pause between batches

    if all_updates:
        print(f"\nApplying {len(all_updates)} total updates:")
        apply_updates(svc, all_updates)
    else:
        print("\nNo updates found this month.")

    print("\n✓ Monthly job complete.")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Company Landscape Agent")
    parser.add_argument("--mode", choices=["weekly", "monthly"], required=True)
    args = parser.parse_args()

    missing = [v for v in
               ("ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN", "GOOGLE_SERVICE_ACCOUNT_JSON")
               if not os.environ.get(v)]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}")

    ai  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    svc = sheets_svc()

    if args.mode == "weekly":
        run_weekly(ai, svc)
    else:
        run_monthly(ai, svc)


if __name__ == "__main__":
    main()
