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

## Step 8. Put the code on GitHub
- Created the public repo `morning-brief`, then added files through GitHub's web editor and upload page (README by paste, 7 files by upload, workflow and state file by paste because they live in hidden/sub folders).
- Verified every file by comparing a hash of the copy on GitHub with the local copy: all identical.

## Step 9. Turn on the website
- Settings -> Pages -> Source "Deploy from a branch" -> branch `main`, folder `/docs` -> Save. The signup page went live at https://saiuttej2225-ai.github.io/morning-brief/ (checked: HTTP 200).

## Step 10. Secrets and variables (never put keys in code)
- Settings -> Secrets and variables -> Actions.
- Secrets: `SUBSCRIBERS_CSV_URL` (the published sheet link), `GMAIL_ADDRESS`, `GEMINI_API_KEY`, `GMAIL_APP_PASSWORD`.
- Variable: `SITE_URL` (used for the "change topics / unsubscribe" link in every email).
- Why secrets: they are encrypted, hidden in logs, and only the workflow can read them. The repo is public, so keys in code would be stolen within minutes.

## Step 11. First real test (passed)
- Signed up through the public page as a real user (AI Product Manager, 4 topics + free-text "AI evals"). Checked the row arrived in the Sheet.
- Actions tab -> Daily brief -> Run workflow. Run #1 finished green in 56 seconds and the email arrived with 10 labeled stories and a Gemini-written intro and summaries.

## Known limits (good to say honestly)
- "Confirmed (N outlets)" counts Google News results for the headline. Broad topical headlines can match loosely, so N can be inflated. A stricter same-claim check is the next improvement.
- Gmail app passwords cap sending (about 500 emails a day), so this suits tens to low hundreds of subscribers.
- Gemini free tier has rate limits; the runner waits and retries, and falls back to plain snippets.

## What to say in an interview
- Problem: busy people want news for their exact job, but trust is a problem with AI-written news.
- Solution: automated pipeline with a fact-check label on every story (official / 2+ outlets / single source), personalised by job title and topics.
- Engineering choices: zero-cost stack (Actions, Pages, Forms/Sheets, Gemini free tier), no servers, standard-library Python, secrets management, privacy (hashed IDs, private signup sheet), graceful fallback when the AI is down, dry-run testing.

## Step 12. Tightening the "Confirmed" label
- Problem: the first version counted every Google News result for a headline, so a broad topical headline showed "Confirmed (10 outlets)" without 10 outlets reporting the same event.
- Fix (`verify.same_story`): a headline counts only if it shares at least 3 distinctive words and at least half of the shorter headline's words with the story, outlets are de-duplicated by name, and the original source counts as one.
- Echo-rumor guard: if the headline uses rumor wording (leak, reportedly, allegedly, sources say...), 2+ outlets earn "Reported, not confirmed" instead of "Confirmed". Many outlets repeating one leak is still not confirmation.
- Result on real examples: a loose topical story went from 11 outlets to 2; a widely covered model launch stayed at 18.
