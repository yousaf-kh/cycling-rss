# RSS Aggregator

Combines many source RSS feeds into a small number of **merged output feeds**,
keeps a rolling **365-day archive**, and refreshes everything **every 3 hours**
using free GitHub infrastructure (GitHub Actions + GitHub Pages).

Your spreadsheet had 5 tabs, so this repo produces **5 output feeds**, one per tab:

| Output feed | Built from | Source feeds |
|-------------|-----------|--------------|
| `news.xml`     | News tab     | 49 |
| `races.xml`    | Races tab    | 140 |
| `teams.xml`    | Teams tab    | 184 |
| `youtube.xml`  | YouTube tab  | 18 |
| `podcasts.xml` | Podcasts tab | 18 |

Each output is a standard RSS 2.0 feed you can put into any reader
(Feedly, Inoreader, NetNewsWire, etc.).

---

## How it works

```
feeds/*.json   ->  aggregate.py  ->  data/*.json   (365-day archive, committed back)
                                 ->  public/*.xml   (published feeds, served by Pages)
                                 ->  public/index.html
```

1. A scheduled GitHub Action runs `aggregate.py` every 3 hours.
2. For each tab it fetches all source feeds in parallel.
3. New items are merged into the archive in `data/<tab>.json`, de-duplicated by
   GUID/link, so the same article is never added twice.
4. Items older than 365 days are pruned from the archive.
5. A combined RSS file is written to `public/<tab>.xml`.
6. The archive and feeds are committed back to the repo and the `public/` folder
   is deployed to GitHub Pages.

The script is **idempotent and self-healing**: de-duplication is based on stable
item IDs, not on a 3-hour time window, so if a scheduled run is skipped or fails,
the next run still catches every item the sources are still serving. (GitHub
sometimes delays scheduled jobs under load — this design tolerates that.)

---

## One-time setup

1. **Create a new GitHub repository** (public is recommended — Actions minutes are
   unlimited for public repos and GitHub Pages is free).

2. **Upload these files** to the repo (keep the folder structure):
   ```
   aggregate.py
   requirements.txt
   feeds/        (the 5 config files)
   data/         (empty, with .gitkeep)
   public/       (will be generated)
   .github/workflows/aggregate.yml
   ```

3. **Set your Pages URL.** Edit `.github/workflows/aggregate.yml` and change:
   ```yaml
   SITE_BASE_URL: "https://YOUR_USERNAME.github.io/YOUR_REPO"
   ```
   to your own username and repo name.

4. **Enable GitHub Pages.**
   Repo *Settings → Pages → Build and deployment → Source = "GitHub Actions"*.

5. **Allow Actions to write to the repo.**
   *Settings → Actions → General → Workflow permissions → "Read and write permissions"*.

6. **Run it once manually.** Go to the *Actions* tab → "Aggregate RSS feeds" →
   *Run workflow*. After it finishes, your feeds will be live at:
   ```
   https://YOUR_USERNAME.github.io/YOUR_REPO/news.xml
   https://YOUR_USERNAME.github.io/YOUR_REPO/races.xml
   https://YOUR_USERNAME.github.io/YOUR_REPO/teams.xml
   https://YOUR_USERNAME.github.io/YOUR_REPO/youtube.xml
   https://YOUR_USERNAME.github.io/YOUR_REPO/podcasts.xml
   ```
   and an index page at `https://YOUR_USERNAME.github.io/YOUR_REPO/`.

After that, it runs by itself every 3 hours.

---

## Configuration

All settings are environment variables in the workflow (`aggregate.yml`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `RETENTION_DAYS`   | `365` | How long items stay in the archive. |
| `MAX_OUTPUT_ITEMS` | `500` | Max items written to each `*.xml` (the archive keeps everything within the retention window; this just keeps the published file small enough for readers). Raise it if you want longer feeds. |
| `FETCH_WORKERS`    | `12`  | Parallel feed downloads. |
| `FETCH_TIMEOUT`    | `30`  | Per-feed timeout in seconds. |
| `SITE_BASE_URL`    | —     | Your Pages URL (fills the self-link in each feed). |
| `SITE_TITLE`       | `My Cycling RSS Feeds` | Title prefix shown in readers. |

### Adding or removing sources

Edit the relevant file in `feeds/`. Each is just a list of sources:

```json
{
  "title": "News",
  "sources": [
    { "name": "Feltet", "url": "https://rss.app/feeds/XXXX.xml", "country": "Denmark" }
  ]
}
```

`name` and `url` are required. `country`, `platform`, and `language` are optional
and become `<category>` tags on each item. Commit the change and the next run
(or a manual run) picks it up.

---

## Run locally (optional)

```bash
pip install -r requirements.txt
python aggregate.py
# outputs land in public/ , archive in data/
```

---

## Why GitHub (and alternatives)

GitHub Actions + Pages is genuinely free for this and needs no server. The only
real limits: scheduled runs can be delayed a few minutes under load (handled by
the design above), and a single job is capped at 6 hours (you're nowhere near that).

If you later outgrow it, the same `aggregate.py` runs unchanged on:
- **Cloudflare Workers + R2 / Cron Triggers** (generous free tier, faster cron),
- a **$5/month VPS** with a simple `cron` entry,
- **Fly.io / Render** scheduled jobs.

Nothing in the script is GitHub-specific except the workflow file.
