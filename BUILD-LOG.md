# Build log: how this project was made (interview walkthrough)

Each step says what was done, how, and why. Use it to explain the project in your own words.

## The idea
A free service that emails each person a daily news brief tailored to their **job title** and **topics**. Runs on GitHub Actions (free), no server to maintain.

## Architecture in one picture
Signup page (GitHub Pages) -> Google Form -> private Google Sheet (subscriber list)
-> GitHub Actions runs `runner.py` every morning -> reads the Sheet, gathers news, fact-checks, asks Gemini to write short summaries -> sends email via Gmail.

## Step 1. Reuse the working single-user brief
- Started from the existing project: `collect.py` (reads 27 RSS feeds + Hacker News) and `verify.py` (counts independent outlets via Google News, checks links).
- Why: both are standard-library Python with no installs, so they run on GitHub's free runners unchanged.

## Step 2. Write `runner.py`, the multi-user engine
How it works, per subscriber:
1. **Read subscribers** from a CSV (the Google Sheet). If someone signed up twice, the newest row wins; "Unsubscribe" removes them.
2. **Gather stories**: the shared 27 feeds (fetched once for everyone) plus Google News searches for that person's job title and topics.
3. **Rank**: score each story by keyword matches (job title words weigh most, title match beats body match, official company sources get a bonus).
4. **Fact-check**: `verify.py` counts independent outlets in the last 3 days. Labels: Official / Confirmed (2+ outlets) / Single source.
5. **Write**: one Gemini API call picks the best ~10 and writes one plain sentence each. If Gemini is unavailable, it falls back to the feed snippets, so an email still goes out.
6. **Send**: Gmail SMTP with an app password.
7. **No repeats**: sent URLs are stored per person as a hash of their email (so the public repo never contains addresses).
Design choices worth mentioning: standard library only, free-tier friendly (delays between calls, retry on rate limits), model fallback, a DRY_RUN mode.

## Step 3. Test it safely
- Ran `DRY_RUN=1 python runner.py` with two fake subscribers (AI Product Manager, Data Analyst). It scanned 2,887 items from 27 feeds and produced a 10-story preview for each, without sending any email.
- Why: test before touching real inboxes.

## Step 4. Write the GitHub Actions workflow
- `.github/workflows/daily-brief.yml`: cron `0 12 * * *` (12:00 UTC, about 7-8 AM New York), plus a manual "Run workflow" button.
- Secrets are injected as environment variables, never written in code.
- After sending, the job commits `state/sent.json` so tomorrow's run knows what was already sent.

## Step 5. Signup form and subscriber list (Google Form + Sheet)
How it was done:
1. In Google Forms, created "Morning Brief Signup" with Google's built-in Gemini form generator from one paragraph describing the five questions: Name, Email, Job title, Topics (13 checkboxes + "Other"), Action (Subscribe / Unsubscribe).
2. Marked every question Required (toggle at the bottom of each question).
3. Responses tab -> Link to Sheets: every submission becomes a row in a private Google Sheet.
4. Published the form (paper-plane icon -> Publish). That gives a public form link.
5. File -> Share -> Publish to web, chose the "Form Responses 1" tab and **CSV** format. That gives a long unguessable link that returns the sheet as CSV. The daily job reads it, so no Google API keys are needed. The link is stored as a GitHub Secret, not in the code.
6. Found the form's hidden field IDs (`entry.123...`) by reading the form's page source (the `FB_PUBLIC_LOAD_DATA_` block). The custom page needs them.
Why: GitHub Pages is static, so it can't store signups safely. A Form gives free private storage and a CSV feed.

## Step 6. Custom signup page (`docs/index.html`)
- Plain HTML/CSS/JS, no framework. The form posts to Google's `formResponse` address using the field IDs from step 5, through a hidden iframe so the visitor stays on our page.
- Topic chips plus a free-text "something else" box, which maps to the Form's "Other" option.
- Hosted free with GitHub Pages (Settings -> Pages -> deploy from `/docs`).

## Step 7. GitHub repo
- Repository with the code, the workflow and this log. Secrets go in Settings -> Secrets and variables -> Actions: `GEMINI_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `SUBSCRIBERS_CSV_URL`. Variable `SITE_URL` = the Pages address (used for the unsubscribe link in each email).
