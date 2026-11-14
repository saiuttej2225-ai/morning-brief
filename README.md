# Morning Brief

A free, personalised daily news email. Each subscriber gives a **job title** and **topics**; every morning a GitHub Actions job gathers fresh stories, fact-checks them, has Gemini write one plain sentence each, and emails the result.

- Sign up: see the Pages site for this repo (`docs/index.html`).
- How it was built, step by step: [BUILD-LOG.md](BUILD-LOG.md)

## How it works
1. `runner.py` reads subscribers from a published Google Sheet (filled by the signup form).
2. Gathers stories from 27 RSS feeds (`sources.json`) plus Google News searches for each person's title and topics.
3. Ranks by keyword match, then `verify.py` counts independent outlets (Official / Confirmed / Single source).
4. One Gemini call per person picks about 10 stories and writes the summaries (falls back to feed snippets if the AI is unavailable).
5. Sends through Gmail SMTP. `state/sent.json` stores hashed IDs and URLs so stories never repeat.

## Set up your own copy
1. Fork or copy this repo.
2. Make a Google Form with fields Name, Email, Job title, Topics, Action; link it to a Sheet; File > Share > Publish to web as CSV.
3. Add Actions secrets: `GEMINI_API_KEY` (free at aistudio.google.com), `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` (Google Account > Security > App passwords), `SUBSCRIBERS_CSV_URL`.
4. Edit the form IDs in `docs/index.html`, enable Pages from `/docs`.
5. Run the "Daily brief" workflow once by hand from the Actions tab.

Test locally without sending: `DRY_RUN=1 python runner.py` writes HTML previews to `preview/`.
Python 3 standard library only.
