# Hantavirus Global Tracker

A holographic 3D outbreak tracker that updates itself.

## What's in this bundle

| File | Role |
|---|---|
| `hantavirus_tracker.html` | The webpage — a single-file 3D globe. Open in any browser. |
| `agent.py` | Autonomous updater. Uses Claude + web search to maintain `data.json`. |
| `data.json` | Current outbreak data. The tracker reads this on load. |
| `update-data.yml` | GitHub Actions workflow — runs `agent.py` every 2 hours. |

## How the auto-updater works

```
   ┌───────────────────────┐  every 2h   ┌─────────────────┐
   │  GitHub Actions cron  ├────────────►│   agent.py      │
   └───────────────────────┘             │                 │
                                         │ Claude + web    │
                                         │ search → JSON   │
                                         └────────┬────────┘
                                                  │ commit
                                                  ▼
                                         ┌─────────────────┐
                                         │  data.json      │
                                         │  (in your repo) │
                                         └────────┬────────┘
                                                  │ raw URL
                                                  ▼
   ┌───────────────────────┐    fetch    ┌─────────────────┐
   │ Anyone visits page    │◄────────────┤ tracker .html   │
   └───────────────────────┘             └─────────────────┘
```

When the page loads, it pulls the latest `data.json` from your repo. While the
page is open it re-polls every 5 minutes. Every 2 hours, GitHub Actions wakes
up, runs `agent.py`, and commits any changes back. If the fetch ever fails,
the page falls back to data baked into the HTML — it never breaks.

## One-time setup (~10 minutes)

### 1 — Create a GitHub repo and add these files

```
your-repo/
├── hantavirus_tracker.html
├── agent.py
├── data.json
└── .github/
    └── workflows/
        └── update-data.yml
```

(The workflow file goes inside `.github/workflows/` — that path is required
by GitHub.)

### 2 — Get an Anthropic API key

[console.anthropic.com](https://console.anthropic.com) → API Keys → Create.
Keep the key handy for the next step.

### 3 — Add the key as a repo secret

Repo → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**

- Name: `ANTHROPIC_API_KEY`
- Value: paste your key

### 4 — Point the tracker at your data.json

Open `hantavirus_tracker.html`, find the `CONFIG` block near the top of the
script tag (search for `liveDataUrl`), and paste your raw GitHub URL:

```js
const CONFIG = {
  liveDataUrl: 'https://raw.githubusercontent.com/YOUR_USER/YOUR_REPO/main/data.json',
  refreshIntervalMs: 5 * 60 * 1000,
};
```

Commit the change.

### 5 — Trigger the first run

Repo → **Actions** tab → **Update outbreak data** → **Run workflow**.

Watch it run. You should see a green check, and a fresh commit titled
`auto: outbreak data ...`. Done — it'll keep running every 2 hours forever.

## Hosting the page

The HTML is fully self-contained. Pick whichever you like:

- **Open it directly** — `file://path/to/hantavirus_tracker.html` works.
- **GitHub Pages** — Settings → Pages → Source: `main`. Free, instant.
- **Any static host** — Vercel, Netlify, Cloudflare Pages, S3, all fine.

## Costs

Each agent run is ~3–5K input tokens + ~2–4K output. With Claude Sonnet 4.6,
that's about **$0.05–0.10 per run**. Running every 2 hours forever:

- 12 runs/day × $0.08 ≈ **$1/day** ≈ **$30/month**

Want it cheaper? Edit the cron in `update-data.yml`:
- Every 6 hours: `0 */6 * * *` → ~$10/month
- Every 12 hours: `0 */12 * * *` → ~$5/month

## Manual updates

Don't want to wait for the cron? Just re-run the workflow from the Actions
tab — same button you used in step 5.

You can also run `agent.py` locally:

```sh
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python agent.py
# data.json is now updated locally — commit and push to deploy
```

## What the tracker shows

- 3D wireframe globe with real country borders (286 polygons embedded inline)
- Glowing beacons at every outbreak/monitoring site, color-coded by status
- Cruise ship route arc traced from Ushuaia to Cape Verde
- Click any beacon to fly the camera in and read site intel
- Filter by signal type, toggle the route, auto-rotate, etc.
- "LIVE · 12m AGO" indicator that turns green when live data is current,
  amber when offline (cached), grey when no `liveDataUrl` is set

## Notes & limitations

- **Claude.ai artifact preview won't fetch live data.** The artifact sandbox
  blocks cross-origin requests. The page works fine in the preview using its
  embedded fallback, but live updates only kick in once you host the file
  somewhere else (or open the local file).
- **Agent never deletes sites.** Even if a country drops out of the news, its
  entry stays in the dataset (with status updated as appropriate). This is
  intentional — historical record matters in surveillance.
- **Data quality depends on what Claude finds.** If reporting is sparse on a
  given day, the dataset will be lightly updated. The agent doesn't fabricate;
  if it can't find new info, it preserves prior values.
