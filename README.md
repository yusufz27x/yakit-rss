# yakit-rss

An RSS tracker for Turkish fuel prices: **zam/indirim announcements** from the
news, **confirmed pump prices** from Petrol Ofisi's public price table, and a
**city selector** covering all 81 provinces (Istanbul is split into Europe and
Anatolia). Istanbul (Europe) is the default; every city gets its own RSS feed.

The feed contains three kinds of items:

```text
[Haber]   Benzine 4 lira 30 kuruş indirim bekleniyor
[Fiyat]   İstanbul (Avrupa) · Motorin 96,80 → 98,04 TL (+1,24 TL)
[Tahmin]  Benzin · +1,24 TL zam yönünde (tahmini 77,54 TL)
```

`[Haber]` items arrive when the change is announced (often the evening before
it takes effect). `[Fiyat]` items arrive when the price table actually changes.
`[Tahmin]` items are model-based estimates from Brent, refined-product futures,
and USD/TRY. That separation matters: a headline can be premature, a confirmed
price is a fact, and a prediction is only an estimate. You can filter on the RSS
category (`Haber` / `Fiyat` / `Tahmin`) in most readers.

The tracker uses only the Python standard library.

---

## Fastest option: no code

The tracker defaults to three curated publishers: **Enerji Günlüğü** (energy
sector), **Dünya** (business), and **Bloomberg HT** (business). For a quick
no-code feed, Enerji Günlüğü’s RSS is much quieter than a broad news aggregator:

```text
https://www.enerjigunlugu.net/rss/
```

Add it to Feedly, Inoreader, NetNewsWire, Thunderbird, or any other RSS reader.
It is sector news, not a national price-change wire: some days it has no fuel
price story. The tracker adds the confirmed Petrol Ofisi price changes and
model-based predictions, so the price data still updates when the news is quiet.

Two other ready-made feeds are useful as complements:

| Feed | URL | What it is |
| --- | --- | --- |
| Yakıt Vakti | `https://yakitvakti.com/feed/degisimler.xml` | Purpose-built Atom feed of fuel-price changes. It only has entries when there is something to report. |
| COM Akaryakıt | `https://com.com.tr/rss/akaryakit.xml` | One item per province, updated daily. It tells you which city pages changed, not the prices themselves. |

Enerji Günlüğü content belongs to its publisher; respect their terms. Do not
republish their headlines as your own news product.

---

## Run your own tracker

### Requirements

- Python 3.9 or newer (3.11+ recommended).
- No third-party packages.

### 1. Choose the default city (and optional subset)

Petrol Ofisi publishes one row per province, with Istanbul split into Europe and
Anatolia. List the exact names:

```bash
python3 yakit_rss.py --list-cities
```

Typical names: `ISTANBUL (AVRUPA)`, `ISTANBUL (ANADOLU)`, `ANKARA`, `IZMIR`.
The script also accepts `istanbul (avrupa)` or `ankara` and ignores Turkish
diacritics when matching.

`city` is the **default** city: it is selected initially in the selector and its
feed is `feeds/<slug>.xml`. `cities` controls the options in the selector:

- `"cities": []` (default) — all 82 rows.
- `"cities": ["ANKARA", "ISTANBUL (AVRUPA)"]` — only those rows, plus the
  default.

The selector groups **İstanbul, Ankara, and İzmir** at the top under
“Popüler şehirler”; the rest follow alphabetically.

### 2. Edit `config.json`

At minimum, set `city`, `city_label`, and optionally `cities`:

```json
{
  "city": "ISTANBUL (AVRUPA)",
  "city_label": "İstanbul (Avrupa)",
  "cities": [],
  "products": ["benzin", "motorin", "lpg"]
}
```

`city_label` is only the display name for the default city; `city` must match
the price table.

### 3. Test, then run

```bash
# Fetch the sources and show what would be added, without writing files:
python3 yakit_rss.py --dry-run --verbose

# Write docs/feeds/*.xml, docs/index.html and state.json:
python3 yakit_rss.py
```

On the first run each city feed gets a `[Başlangıç]` item with the current
prices, plus the last few days of headlines. On later runs only new events are
added. The script keeps its history in `state.json`; leave that file in place.
The first run also creates the prediction baseline for every city; see below.

### 4. Subscribe

Every city has `docs/feeds/<slug>.xml` (for example
`docs/feeds/ankara.xml`); there is no separate duplicate default feed. An RSS
reader needs a URL, so serve the directory locally:

```bash
python3 -m http.server 8080 --directory docs
```

Then open `http://localhost:8080/` and use the city selector; the RSS button
always points to the selected city's feed. You can also add a feed URL directly,
such as `http://localhost:8080/feeds/istanbul-avrupa.xml`. The page includes
current prices, prediction cards, market inputs, a Brent chart, and the latest
feed items. It refreshes every 30 minutes, stores your city selection in a
cookie (`yakit_city`), and supports `?city=ankara` deep links. The `Sıfırla`
button deletes the cookie and returns to the `config.json` default.

For a phone or another machine, put the `docs/` directory on any static host
(GitHub Pages, a small VPS, object storage) and use its public URL.

### Push notifications

Feed readers with mobile apps (Feedly, Inoreader, NetNewsWire) can send push
notifications for new items. To route items to Telegram, Discord, or Slack,
connect the feed URL to an RSS bot or automation service and filter on the RSS
category: `Haber` for announcements, `Fiyat` for confirmed changes, `Tahmin` for
model estimates.

---

## Price predictions (Brent + product futures + USD/TRY)

As of **26 October 2026**, the eşel mobil system is **not active**. It was
phased out during 2026 and ended on **1 October 2026**. The model therefore
assumes full pass-through of the international product-cost change, plus VAT.
If eşel is reintroduced, set `prediction_pass_through` to the announced ratio
(for example `0.25` if only 25% of the cost change is passed to the pump) and
update `esel_note`.

Predictions are computed for every selected city. The market cost is national;
the current pump price and the price-change baseline are per city, so the
predicted TL delta can differ after a city changes its pump price. The predictor
is deliberately transparent:

1. Fetch **Brent** (`BZ=F`), **RBOB gasoline** (`RB=F`), **ULSD diesel**
   (`HO=F`), and **USD/TRY** (Yahoo Finance, with TCMB/Frankfurter fallbacks).
2. Convert the product futures from USD/gallon to TL/litre:

   ```text
   maliyet_TL/L = fiyat_$_gal / 3.78541 × USD/TRY
   ```

3. Store a baseline `(pump price, product cost)` when the pump price is first
   seen or changes.
4. Predict the pump delta since that baseline:

   ```text
   Δpompa = (güncel_maliyet − baz_maliyet) × (1 + KDV) × yansıtma_oranı
   ```

5. Show the result on `docs/index.html` and add a `[Tahmin]` feed item when the
   absolute delta is at least `prediction_min_delta_tl` (default `0.25 TL`), at
   most one item per 0.5 TL bucket per fuel per city per day.

Important limitations:

- This is **not** the official EPDK/distributor formula. The official/eşel
  calculation uses **Platts CIF Med** product prices per ton (gasoline density
  ~0.775, diesel ~0.845) plus distributor/dealer margins and taxes. RBOB and
  ULSD are free, liquid proxies, not CIF Med quotes.
- Tax changes (including the motorin ÖTV schedule), distributor margin changes,
  and local competition are not modelled.
- LPG is **not** predicted: it is priced mainly off Saudi CP/import parity, not
  Brent/RBOB.
- The first run only creates the baseline for each city. Predictions stay at 0
  until the market cost moves after that baseline.
- Treat predictions as a rough pressure indicator, not a promise of the next
  pump price.

To disable predictions entirely, set `"prediction_enabled": false`. To keep
them on the page but out of the feed, set `"prediction_in_feed": false`.

---

## Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `city` | `ISTANBUL (AVRUPA)` | Default city; selected initially in the selector and used as the fallback if no cookie is set. |
| `city_label` | `İstanbul (Avrupa)` | Display name for the default city. |
| `cities` | `[]` | Selector options. Empty means all 82 price-table rows; otherwise a list of city names from `--list-cities` (display names such as `Afyonkarahisar` also work). |
| `products` | `["benzin", "motorin", "lpg"]` | Any of `benzin`, `motorin`, `lpg`, `gazyagi`, `kalorifer`, `fuel_oil`. The last two are priced per kg on the source page. |
| `price_source_url` | Petrol Ofisi table | Override only if the page moves. |
| `news_enabled` | `true` | Set to `false` to publish only confirmed prices. |
| `news_sources` | Enerji Günlüğü, Dünya, Bloomberg HT | List of `{ "name": "...", "url": "..." }` RSS/Atom feeds. Add reputable energy/business feeds here. |
| `news_filter` | `changes` | `changes` keeps zam/indirim stories and drops "var mı / ne kadar" roundups; `all` keeps every fuel headline from the configured sources. |
| `news_max_age_days` | `3` | Ignore older headlines. |
| `max_items` | `80` | Maximum number of items kept in the feed. |
| `state_file` | `state.json` | Persistent multi-city price/news/prediction history. |
| `page_output` | `docs/index.html` | Generated overview page with the city selector; per-city feeds are written to its sibling `feeds/` directory. |
| `page_url` | empty | Public URL of the overview page; used for canonical links and per-city feed `atom:link` self URLs. |
| `timezone` | `Europe/Istanbul` | Timestamps in the feed and page. |
| `timeout_seconds` | `30` | HTTP timeout. |
| `user_agent` | `yakit-rss/1.0 ...` | Sent to the price/news sources. |
| `prediction_enabled` | `true` | Fetch market data and show predictions. |
| `prediction_in_feed` | `true` | Add `[Tahmin]` items to each city's RSS feed. |
| `prediction_pass_through` | `1.0` | Share of the cost change passed to the pump. Use `0.25` for a 25% eşel pass-through. |
| `prediction_vat` | `0.20` | VAT applied to the cost change. |
| `prediction_min_delta_tl` | `0.25` | Minimum absolute delta for a `[Tahmin]` feed item. |
| `prediction_products` | `["benzin", "motorin"]` | Products to predict. Only `benzin` and `motorin` are supported. |
| `market_history_days` | `30` | Brent history length for the page chart. |
| `market_user_agent` | browser-like | User agent for Yahoo/TCMB market requests. |
| `esel_note` | see `config.json` | Policy note shown on the page. Update it if eşel returns. |

Relative paths are resolved from the directory containing the config file.
Command-line flags override the config:

```bash
python3 yakit_rss.py --city ANKARA --city-label Ankara
```

Changing `city` only changes the default city; state is kept for every city.
Use `cities` to limit the selector/feeds. To run a separate tracker with a
different default or subset, use another config, state file, and page output:

```bash
cp config.json config-ankara.json
# edit city, cities, state_file, page_output in config-ankara.json
python3 yakit_rss.py --config config-ankara.json
```

---

## Scheduling

### cron

Run every 30 minutes and keep a log:

```cron
*/30 * * * * cd /home/user/Documents/yakit-rss && /usr/bin/python3 yakit_rss.py >> yakit-rss.log 2>&1
```

Fuel changes usually take effect at midnight, and announcements arrive in the
evening. Every 30 minutes is plenty; hourly also works. Avoid polling every
minute.

### GitHub Actions + Pages

The included workflow (`.github/workflows/feed.yml`) runs every 30 minutes,
regenerates all per-city feeds and the overview page, commits `state.json` when
it changes, and deploys `docs/` to GitHub Pages.

1. Create a repository and push this directory.
2. In **Settings → Pages**, set **Source** to **GitHub Actions**. The workflow
   also tries to enable Pages automatically, but setting it manually avoids
   permission surprises.
3. In `config.json`, set the public page URL and commit the change:

   ```json
   "page_url": "https://YOURUSER.github.io/yakit-rss/"
   ```

   If the repository is named `YOURUSER.github.io`, use
   `https://YOURUSER.github.io/` instead.

4. Push. The workflow runs automatically on `main`/`master`; you can also run it
   manually from the **Actions** tab (**Update fuel feed → Run workflow**).
5. Open the overview at `https://YOURUSER.github.io/yakit-rss/` and use the city
   selector. Each city has
   `https://YOURUSER.github.io/yakit-rss/feeds/<slug>.xml` (for example
   `/feeds/ankara.xml`); the RSS button follows the selected city.

Notes:

- The workflow needs permission to push `state.json` back to the repository.
  If the default branch is protected, allow GitHub Actions to bypass the rule or
  commit the state to a separate branch.
- Private repositories need a GitHub plan that includes Pages.
- GitHub disables scheduled workflows after 60 days of repository inactivity.
  The workflow's own commits may not reset that timer, so for long-term
  unattended use a small VPS with cron is more reliable.
- The checked-in `docs/feeds/*.xml` and `docs/index.html` are samples. GitHub
  Pages serves the freshly generated artifact, not the stale files in the
  repository.
- The workflow assumes the default `state_file` (`state.json`) and
  `page_output` (`docs/index.html`). If you change either in `config.json`,
  update the workflow's `git add` and `upload-pages-artifact` paths too.
- GitHub Pages on a public repository is public. If you make the feed public,
  check the terms of your configured news source; otherwise set
  `news_enabled` to `false` and publish only the confirmed prices, or host the
  combined feed privately.

---

## How it works

### Announcements

The script reads the configured RSS/Atom sources (Enerji Günlüğü, Dünya, and
Bloomberg HT by default), keeps items that mention a fuel and `zam`/`indirim`,
drops question/roundup headlines, and groups the rest by
**fuel + direction + day + status**
(`expected`, `confirmed`, `iptal`, `other`).

To add a source, append it to `news_sources`:

```json
"news_sources": [
  { "name": "Enerji Günlüğü", "url": "https://www.enerjigunlugu.net/rss/" },
  { "name": "Dünya", "url": "https://www.dunya.com/rss" },
  { "name": "Enerji Ekonomisi", "url": "https://www.enerjiekonomisi.com/rss/" }
]
```

Prefer sector or business outlets. General feeds work, but the fuel/change
filter will discard most of their items; this is intentional.
One representative headline survives per group, so twenty outlets reporting the
same change produce one item. The representative prefers headlines with a
number, normal capitalization, and a reasonable length.

### Confirmed prices

Petrol Ofisi's public table is fetched once per run. The parser reads the
`with-tax` price for the selected city and each configured product. Prices are
compared with the previous run; an item is emitted only when a value actually
changes. The first run records a baseline instead of pretending every price is
new.

The price table is a distributor's recommended list price (VAT included), not a
station-specific price. Stations can differ. The `[Fiyat]` item is a
confirmation signal, not a guarantee of what you will pay at a particular pump.

### Predictions

The market fetch uses Yahoo Finance for Brent (`BZ=F`), RBOB (`RB=F`), ULSD
(`HO=F`), and USD/TRY (`TRY=X`); if Yahoo is unavailable it falls back to TCMB
or Frankfurter for USD/TRY, and to a Brent-based proxy if a product future is
missing. The model stores a baseline whenever the pump price is first seen or
changes. It then compares the current product cost with that baseline and
applies VAT and `prediction_pass_through`. No paid Platts/CIF Med feed is used.

### Cities, selector and feeds

The price table is parsed once per run and all configured cities are processed
in that pass. Price-change items and prediction items are stored per city;
national news is stored once and shared by every feed. Each city gets
`docs/feeds/<slug>.xml`; there is no separate default feed copy.
The selector uses a small inline vanilla-JS snippet (no external dependencies).
The overview page embeds the rendered cards for every city as JSON and switches
them client-side. The default comes from `config.json`; changing the selection
stores it in the `yakit_city` cookie, `?city=slug` deep-links to a city, and the
`Sıfırla` button deletes the cookie and returns to the configured default.

### State and deduplication

`state.json` stores per-city prices, per-city prediction baselines, per-city
price/prediction items, the shared news items, the news event keys already seen,
and the prediction-item keys. It is written atomically. If it is corrupted, the
script moves it aside and starts fresh. Delete it deliberately if you want a
clean slate (you will get new baseline items and the prediction baselines will be
recreated). Older single-city state is migrated automatically.

---

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `City '...' was not found` | Run `--list-cities`. Remember Istanbul is two rows. |
| A city is missing from the selector | `cities` is probably a subset. Set it to `[]` for all rows, or add the city's exact name. |
| The selector does not change anything | The selector needs JavaScript. Without it, the page shows the default city. Check the browser console for errors. |
| A city feed returns 404 | Use `feeds/<slug>.xml`, e.g. `feeds/istanbul-avrupa.xml`, not the display name. The page's RSS button always uses the right URL. |
| `Column '...' not found` | Petrol Ofisi changed its table layout. Check the page headings and update `PRODUCT_COLUMNS` in `yakit_rss.py`. |
| No price items for days | The page may be unchanged, blocked, or restructured. Run with `--verbose` and check the warnings. News items should still appear. |
| No news items | The source may be down or may simply have no fuel-price story. Check the source URL with `--verbose`; add another reputable feed under `news_sources` if you want broader coverage. |
| Predictions are always `0,00 TL` | The first run creates the prediction baseline. They appear after the market cost moves. Also check that Yahoo/TCMB data was fetched with `--verbose`. |
| `Piyasa verisi alınamadı` on the page | Yahoo may be rate-limiting or blocked. The page falls back to TCMB/Frankfurter for FX, but product futures need Yahoo. Try again later or run from another network. |
| Prediction differs from the news | Expected: the model uses RBOB/ULSD proxies, not Platts CIF Med, and excludes tax/margin changes. Use it as a pressure indicator, not an official forecast. |
| Feed looks empty | Run once without `--dry-run`; the first run creates the baseline. Check that the city file exists under `docs/feeds/` and is readable (`chmod 644`). |
| Reader does not refresh | Some readers cache aggressively. Check `lastBuildDate` in the feed and force a refresh. |
| `git push` rejected in Actions | Branch protection. Allow GitHub Actions to push, or change the workflow to commit to a separate branch. |
| Timezone errors on Windows | The script falls back to UTC+3. Install `tzdata` if you want the full IANA database. |

Run the tests with:

```bash
python3 -m unittest discover -s tests -v
```

---

## Files

```text
yakit_rss.py                  generator (stdlib only)
config.json                   cities, products, news sources, page path
state.json                    price, news and prediction history (keep between runs)
docs/feeds/*.xml              generated per-city RSS feeds
docs/index.html               generated overview page with city selector
tests/test_yakit_rss.py       unit tests
.github/workflows/feed.yml    scheduled generation + GitHub Pages deploy
```

## Notes on sources

- **Petrol Ofisi** publishes the price table for public viewing. This script
  makes one request per run; do not reduce the interval to seconds.
- For an official cross-check, EPDK publishes dealer ceiling prices at
  `https://www.epdk.gov.tr/bayitavan/bayitavanfiyati.htm`. The script uses the
  distributor table because it is simpler to parse and covers all provinces.
- **Enerji Günlüğü** publishes a sector RSS feed at
  `https://www.enerjigunlugu.net/rss/`; **Dünya** and **Bloomberg HT** provide
  broader business feeds. Their content belongs to the publishers; check their
  terms before republishing. You can add other RSS/Atom feeds in
  `news_sources`, but prefer sector or business outlets over aggregators.
- **Yahoo Finance** provides delayed/indicative futures quotes (`BZ=F`, `RB=F`,
  `HO=F`, `TRY=X`). The model uses them as free proxies, not as official
  settlement or Platts CIF Med prices. Yahoo's terms apply.
- **TCMB** publishes daily USD/TRY rates at
  `https://www.tcmb.gov.tr/kurlar/today.xml`; it is the first fallback when
  Yahoo FX is unavailable. **Frankfurter/ECB** is the second fallback.
- **Eşel mobil** was phased out and ended on **1 October 2026**. The default
  model therefore assumes full pass-through. If the policy changes, update
  `prediction_pass_through` and `esel_note`; the model is not an official
  implementation of the eşel formula.
- This project is not affiliated with Petrol Ofisi, EPDK, Yahoo, TCMB, or any
  fuel distributor. Prices, headlines, and predictions are informational only
  and are not financial advice.
