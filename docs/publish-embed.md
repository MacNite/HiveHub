# Publish data — public charts you can embed in a website

The built-in dashboard is protected end to end: every reading sits behind a
login, which is exactly right for a control surface but leaves no way to answer
"how heavy is the hive today?" on a club page, a blog or a farm-shop site.

**Publish data** adds that one missing step. An admin publishes a chart from the
dashboard; the server then serves *that chart and nothing else* under an
unguessable link, with no login:

```html
<iframe src="https://hive.example.org/embed/chart/INidRIA2wEtqiO45w94lng"
        title="Weight — Bienenstand Musterstadt"
        width="100%" height="440" style="border:0" loading="lazy"></iframe>
```

The same slice is also available as **JSON** and **CSV**, for a site that would
rather draw its own chart or bake the numbers into a static build.

---

## Enable it

Publishing lives with the dashboard, so it needs:

```bash
ENABLE_LOCAL_DASHBOARD=true    # the dashboard and its API
ENABLE_PUBLIC_EMBEDS=true      # publishing (default: true)
```

Set `ENABLE_PUBLIC_EMBEDS=false` to switch the whole feature off server-side:
the **Publish data** panel disappears from the dashboard, and every previously
published link returns 404 — a single kill switch that does not require deleting
anything. (The flag is read at startup, so restart the container after changing
it.)

> Nothing becomes public by installing this. A publication exists only after
> somebody explicitly creates one, and it can be taken offline or revoked at any
> time from the same panel.

---

## Publish a chart

1. In the top bar's **Hives** menu, tick the hives you want to show. Publishing
   follows that selection, so what you are looking at is what you publish.
2. Go to **Device & admin** and expand the **Publish data** panel (between
   *Configuration* and *Admin*; the form itself is admin-only), then fill in:

   | Field | What it does |
   |---|---|
   | **Data** | Which reading to publish: weight, hive temperature, in-hive humidity, sound level, bee traffic, or a device-level value (ambient temperature/humidity, collector battery, solar power, signal). |
   | **Hives** | Tick the hives to include, and give each the name the public should see. |
   | **Title / Subtitle** | The heading of the embedded chart. |
   | **Period shown** | A rolling window — 7 days … 1 year. The embed always shows the last *n* days, it never freezes. |
   | **Display** | *Line chart*, or *Current value only* — one big number per hive plus its change over the period. |
   | **Resolution** | *Every reading*, or one point per day (max / min / average). Daily points read far better over a month or a year. |
   | **Colour scheme** | Follow the visitor's system setting, or pin light/dark to match the host site. |
   | **Chart height**, **legend**, **"updated …"** | Presentation details. |

3. Press **Publish chart**. The new publication appears on the right with four
   copy buttons: the **embed code**, the **direct link**, and the **JSON** and
   **CSV** data URLs.

Each publication also shows how often it has been fetched and when it was last
viewed — a sign of life for a chart sitting on somebody else's page.

### Editing and revoking

- **Edit** keeps the token, so a chart already embedded in a website keeps
  working while its title, period or hives change under it.
- **Take offline** makes the link return 404 without deleting the publication;
  put it back online at any time.
- **Revoke** deletes it. The link stops working immediately and cannot be
  restored — publishing again mints a new one.

---

## What a visitor can see

The public payload carries **only what was published**: the chart's title, the
labels you typed, the numbers of that one metric, and its unit.

It does **not** carry device IDs, hive numbers, firmware versions, battery or
signal readings, insights, or any other measurement column — nor does the token
grant access to any other endpoint. Publishing your weight curve does not
disclose the fleet behind it.

Two things worth knowing before you paste a link into a public page:

- **The link is the permission.** Anyone who has it can view the chart; there is
  no second check. Treat it like an unlisted video link — and revoke it if it
  ends up somewhere you did not intend.
- **Location.** Nothing in the payload states where your hives are, but the name
  you give a series is published verbatim. If that matters, use a neutral label.

---

## The public API

Three routes, all unauthenticated, all read-only, all subject to the server's
normal per-IP rate limit:

| Route | Returns |
|---|---|
| `GET /embed/chart/{token}` | The self-contained HTML page to `<iframe>` |
| `GET /api/v1/public/charts/{token}` | The chart as JSON (`Access-Control-Allow-Origin: *`) |
| `GET /api/v1/public/charts/{token}.csv` | The same points as CSV (`timestamp,series,value,unit`) |

```jsonc
// GET /api/v1/public/charts/{token}
{
  "title": "Weight — Bienenstand Musterstadt",
  "subtitle": "Live from the apiary",
  "metric": "weight",
  "metric_label": "Weight",
  "unit": "kg",
  "digits": 2,
  "chart_type": "line",          // "line" | "value"
  "aggregate": "daily_max",      // "none" | "daily_min" | "daily_max" | "daily_avg"
  "range_days": 30,
  "theme": "auto",
  "height": 320,
  "show_legend": true,
  "show_updated": true,
  "updated_at": "2026-08-07T11:02:18Z",   // newest reading in the payload
  "generated_at": "2026-08-07T11:05:01Z",
  "series": [
    {
      "label": "Linden",
      "color": "#f2a900",
      "points": [["2026-07-09T12:00:00Z", 41.02], ["2026-07-10T12:00:00Z", 41.44]],
      "latest": { "at": "2026-08-07T11:02:18Z", "value": 44.9 },
      "change": 3.88                        // over the published period
    }
  ]
}
```

Drawing your own chart from it takes a few lines:

```js
const res = await fetch("https://hive.example.org/api/v1/public/charts/<token>");
const chart = await res.json();
for (const s of chart.series) {
  const xy = s.points.map(([t, v]) => ({ x: new Date(t), y: v }));
  // …hand xy to Chart.js / D3 / your static site generator
}
```

Responses are cached for a minute in the server and carry
`Cache-Control: public, max-age=300`, so an embed on a busy page costs one
measurement query per minute rather than one per visitor.

---

## Notes and limits

- **Rolling window, not a snapshot.** A published chart always shows the last
  *n* days, so it keeps itself current with no further action.
- **Down-sampling.** Line charts are built from up to ~600 evenly spaced samples
  across the whole period, which is what the chart can draw anyway. Daily
  aggregates sample more densely (~8 readings per day) and then collapse each UTC
  calendar day to one point — on a multi-year period a "daily maximum" is
  therefore a very close approximation rather than an exhaustive scan.
- **Time zone.** Points are timestamped in UTC (ISO-8601 with a trailing `Z`);
  the embedded page renders them in the *visitor's* local time.
- **Framing.** The embed page deliberately sets no `X-Frame-Options` /
  `frame-ancestors` restriction — being embedded on someone else's page is the
  point. It is marked `noindex`, so search engines index the host page rather
  than the bare fragment.
- **Reverse proxies.** The copy-me URLs are built from `PUBLIC_BASE_URL` when it
  is set (otherwise from the request), so an embed snippet carries the address
  the outside world actually reaches — set it if the dashboard is behind a proxy.
- **Storage.** Publications live in the `published_charts` table
  (migration `023_published_charts.sql`, created automatically by `init_db`).
  They hold the selection and the labels only — no copy of the readings.
