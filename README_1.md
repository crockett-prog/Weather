# Model Compare Weather Dashboard

Single-file dashboard (`index.html`): enter a US ZIP and see ECMWF, GFS, NAM and HRRR as four map panels in a 2×2 grid. Tap a panel to open that model full-screen, where you can pan, zoom, step through forecast hours, tap the map to read values, and see stats for your ZIP. The page also has a comparison chart, live NEXRAD radar, a daily highs/lows table and NWS alerts. It uses no API keys and no backend.

## Deploy on GitHub Pages
1. On GitHub, create a new **public** repo, e.g. `weather`.
2. Choose **Add file → Upload files**, upload `index.html` to the repo root, and commit to `main`.
3. Go to **Settings → Pages → Build and deployment**, choose **Deploy from a branch**, then branch `main`, folder `/ (root)`, and click Save.
4. After about a minute the site is live at `https://<your-username>.github.io/weather/`.
5. You can link a ZIP directly: `https://<your-username>.github.io/weather/?zip=73102`. On a phone, use "Add to Home Screen" to get it like an app.

**Updating:** upload the new `index.html` over the old one and commit. The site redeploys in about a minute.

## How the model maps work
- Each map is drawn in the browser from a grid of Open-Meteo forecast points around your ZIP: 9×9 points per panel and 10×10 in the full-screen view. The grid is smoothed into a color layer, with state lines and city labels on top.
- The panels show the regional pattern. Grid points are roughly 50–80 km apart, so this is coarser than Pivotal Weather's maps. **Zoom in on the full-screen view** to refetch a tighter grid with more detail.
- Map variables are Temp, Precip 1 h, Total precip (accumulated from now), Gusts, Clouds and Dew point.
- Every panel shows its run time (e.g. "18Z Sun run"). This comes from Open-Meteo's model metadata, and the data is always the newest run they have.

## Data sources (verified Oct 2026)
| What | Source | Notes |
|---|---|---|
| ZIP → lat/lon | api.zippopotam.us (Nominatim fallback) | |
| ECMWF | Open-Meteo `ecmwf_ifs025` | 10 days |
| GFS | Open-Meteo `gfs_global` (fallback `gfs_seamless`) | Pure GFS. `gfs_seamless` blends HRRR into the first hours over the US, so it's only a fallback |
| NAM | Open-Meteo `ncep_nam_conus` | 3 km CONUS nest, about 60 h; **no precip %** (shows n/a) |
| HRRR | Open-Meteo `ncep_hrrr_conus` (fallback `gfs_hrrr`) | 48 h on 00/06/12/18Z runs, 18 h otherwise |
| Model run times | `api.open-meteo.com/data/<model>/static/meta.json` | |
| Radar | Iowa Environmental Mesonet NEXRAD n0q tiles | 50-minute loop, refreshes every 5 min |
| Alerts | api.weather.gov | |
| Basemap | CARTO light/dark (no-labels + labels-only on model maps) | |

## API limits (read this)
- Open-Meteo's free tier is **non-commercial only**: **10,000 calls/day, 5,000/hour, 600/minute**.
- **Each grid point counts as about one call.** One ZIP load costs about **330 calls** (4 models × 81 points, plus 4 point forecasts). Panning or zooming the full-screen view costs about 100 more each time it refetches.
- That works out to roughly **30 ZIP loads a day** for everyone using the page. That's fine for personal use, but too tight if the whole staff shares it.
- Loading two ZIPs within the same minute can hit the 600/minute cap. A panel will then say "API limit hit, try again in a minute". Wait a minute and reload.
- Results are cached in memory for the current hour, so switching variables or hours, or reopening a model, costs nothing.
- If usage outgrows this, the options are Open-Meteo's paid plan, or setting `GRID_N` lower in `index.html` (7 gives 49 points per model).

Model output is raw guidance, not an official forecast.
