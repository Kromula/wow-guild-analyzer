# 🛡️ WoW Guild Analyzer

Pull your guild's **public WarcraftLogs** reports and run a dynamic set of checks
("who's topping damage", "who dies first", "who never pops a defensive"…) over a
selectable timeframe — all on a styled local dashboard.

The check framework is **plugin-based**: add a file → new check appears; delete it → it's gone.

---

## 1. Get WarcraftLogs API credentials

1. Log in at <https://www.warcraftlogs.com> and open **<https://www.warcraftlogs.com/api/clients/>**.
2. Click **Create Client**:
   - **Name**: anything (e.g. `guild-analyzer`)
   - **Redirect URLs**: `http://localhost` (required field; we don't use it)
   - **Public Client**: leave **unchecked** — we use the *client-credentials* flow.
3. Copy the generated **Client ID** and **Client Secret**.

> These give read access to **public** logs. Private logs are not accessible via
> client credentials.

## 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | What it is |
| --- | --- |
| `WCL_CLIENT_ID` / `WCL_CLIENT_SECRET` | from step 1 |
| `GUILD_NAME` | exact guild name |
| `GUILD_SERVER_SLUG` | realm, lowercased, spaces → hyphens (e.g. `argent-dawn`) |
| `GUILD_REGION` | `US`, `EU`, `KR`, `TW`, or `CN` |

## 3. Install & run

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open <http://localhost:8000>. Use the **7d / 14d / 30d / 60d** pills to change the
window and **⟳ Refresh** to re-query (results are cached for 10 minutes per window).

---

## How the check framework works

```
WCL API ──► ingest.fetcher ──► ingest.normalize ──► AnalysisDataset (Polars frames)
                                                          │
                                          checks.registry.run_all()
                                                          │
                                                      JSON API ──► dashboard
```

`AnalysisDataset` gives every check tidy Polars frames: `damage`, `healing`,
`casts`, `deaths`, `fights`, `players`.

### Add a new check

Drop a file in `app/checks/builtin/`:

```python
# app/checks/builtin/example.py
import polars as pl
from app.checks.base import Category, Check, CheckRow, Severity
from app.checks.registry import register
from app.ingest.normalize import AnalysisDataset

@register
class HealingDone(Check):
    id = "healing-done"            # unique, kebab-case
    name = "Top Healing"
    description = "Highest healing-per-second over the timeframe."
    category = Category.PERFORMANCE
    order = 12                      # display order (lower = earlier)

    def run(self, ds: AnalysisDataset):
        agg = (ds.healing.group_by("player")
               .agg(pl.col("hps").mean().alias("hps"), pl.col("player_class").first())
               .sort("hps", descending=True))
        rows = [CheckRow(player=r["player"], player_class=r["player_class"],
                         value=r["hps"], display=f"{r['hps']:.0f} HPS")
                for r in agg.head(10).to_dicts()]
        return self.result(severity=Severity.GOOD,
                           headline=f"{rows[0].player} tops healing." if rows else "No data.",
                           columns=["Player", "HPS", "Detail"], rows=rows)
```

Reload the page — it appears automatically. **Remove a check** by deleting its file.

### Tune the survival/consumable check

Edit `app/checks/builtin/survival_abilities.json` — add/remove ability names
(case-insensitive substring match). No code change needed.

---

## Built-in checks (overall view)

| id | Category | What it flags |
| --- | --- | --- |
| `high-damage` | Performance | Top time-weighted DPS |
| `low-damage` | Performance | Bottom DPS (healers excluded data-driven) |
| `frequent-deaths` | Survival | Most deaths in the window |
| `dies-first` | Survival | Earliest/first to die per pull |

## Boss drill-down

Use the **Raid ▾** and **Boss ▾** selectors to tunnel into a single encounter.
The panel fetches that boss's data across the timeframe and shows a focused view:
per-boss **DPS** and **HPS** leaderboards, a **deaths** breakdown (count, first-death
frequency, avg time into pull, top killer), and accurate **per-player defensive &
consumable usage**.

> Per-player defensives/consumables are only computed in the drill-down. They live
> in WarcraftLogs' **Buffs** data (not Casts), and attributing them per player needs
> scoped event queries — cheap for one boss, expensive across all bosses. The overall
> view therefore omits them by design.

## Content & roster filtering

All views respect (configurable in `.env`):
- **`RAID_DIFFICULTY=5`** — Mythic only (1=LFR, 3=Normal, 4=Heroic, 5=Mythic, 0=all).
- **`EXCLUDE_MYTHIC_PLUS=true`** — raid content only.
- **`MIN_ATTENDANCE_PCT=0.5`** — core raiders only; excludes pugs/socials who attended
  fewer than this fraction of raid nights (computed from each fight's participants).

## Notes & limits

- Only **public** logs are visible to client-credentials apps.
- `MAX_REPORTS` in `.env` caps how many reports a refresh pulls (API courtesy).
- WCL `table` JSON shapes can vary by game version; parsing lives in
  `app/ingest/normalize.py` — the one place to adjust if a field looks off.
