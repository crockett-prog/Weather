# Model Compare Weather Dashboard

Enter a US ZIP, or tap **Nashville**, **Alamo**, **Dresden**, **Knoxville** or **Kingsport** for high-res maps. The page shows ECMWF, GFS, RRFS and HRRR as four map panels in a 2×2 grid. Tap a panel to open it full-screen, where you can pan, zoom, step through forecast hours and tap the map to read values. Above the maps is the official **NWS forecast** card: current conditions at the nearest NWS station, a **48-hour rain gauge** (measured by that station's gauge, with an Open-Meteo estimate as backup), and the 7-day forecast — tap a day for the full NWS description. Below the maps are a comparison chart, live NEXRAD radar, daily highs/lows and NWS alerts. It uses no API keys.

## Repo layout
```
index.html                       the site
README.md
scripts/build_maps.py            builds the high-res town data (runs on GitHub, not in your browser)
.github/workflows/maps.yml       runs the script every hour and publishes the site
```

## Setup (one time)
1. **Upload the files.** In the repo, click **Add file → Upload files**. Drag in `index.html` and `README.md`, then drag in the `scripts` folder (it keeps the folder name). Commit.
2. **Add the workflow.** Mac Finder hides folders whose names start with a dot, so create this one by hand: **Add file → Create new file**, and type the name `.github/workflows/maps.yml` (typing the `/` makes the folders). Paste in the contents of `maps.yml`, then commit.
3. **Switch Pages to Actions.** Go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**, replacing "Deploy from a branch".
4. **Run it once now.** Go to the **Actions** tab → **Build maps and deploy site** → **Run workflow**. The first run takes about 10–20 minutes because it downloads every model. After that it runs every hour on its own, and only rebuilds models that have a new run.
5. Open the site and tap a town.

**Updating `index.html` later:** upload it as usual. Every commit triggers the workflow, which redeploys the site within a few minutes; it reuses cached model data, so it's quick.

**If a town map says "high-res data not built yet"**, the workflow hasn't finished a successful run. Open the Actions tab, click the latest run, open **build → Build map data**, and read the log. Each model logs its own lines, and a failure in one model doesn't stop the others.

## Two map modes

### Town buttons (high-res)
- Built from the raw model data, not Open-Meteo:
  - **HRRR** comes from the NOAA Open Data bucket on AWS (NOMADS fallback). **RRFS** — NOAA's replacement for NAM, which was retired on Nov 3, 2026 — comes from NOMADS, with requests paced to stay under its rate limit. Maps show detail every 3 km.
  - **GFS** comes from the same NOAA source, and **ECMWF** from ECMWF open data. Both are natively about 25 km, so they're smooth but not sharper. That's the model, not the map.
- Variables: Temp, **Sim. radar** (composite reflectivity; HRRR, RRFS and GFS only), Precip rate, Total precip, Gusts, Clouds, Dew point.
- Model runs used:

  | Model | Runs used | Forecast range | Time step |
  |---|---|---|---|
  | HRRR | 00/06/12/18Z | 48 h | hourly |
  | RRFS | 00/06/12/18Z | 84 h | hourly |
  | GFS | 00/06/12/18Z | 120 h | 3-hourly |
  | ECMWF | 00/12Z | 120 h | 3-hourly |
- Each town covers 75 miles around it. To add or move a town, edit `SITES` in **both** `scripts/build_maps.py` and `index.html`, and add a button in the header of `index.html`.

### ZIP code
- Same layout, drawn live in your browser from Open-Meteo: a 9×9 grid of forecast points (about 30 km apart) covering 75 miles around the ZIP.
- Maps open at a 50-mile radius. To change that, edit `VIEW_MI` in `index.html`.

## Data sources
| What | Source | Notes |
|---|---|---|
| Town maps | NOAA HRRR / GFS 0.25° (AWS Open Data, NOMADS fallback), RRFS 3 km (NOMADS); ECMWF IFS 0.25° open data (CC-BY-4.0) | built hourly by the workflow |
| ZIP maps + all point forecasts | Open-Meteo (`ecmwf_ifs025`, `gfs_global`, `ncep_rrfs_conus` (fallback `ncep_rrfs_north_america`), `ncep_hrrr_conus`) | free, non-commercial |
| ZIP → lat/lon | api.zippopotam.us (Nominatim fallback) | |
| Radar | Iowa Environmental Mesonet NEXRAD n0q tiles | 50-minute loop |
| Alerts + NWS forecast panel | api.weather.gov (`/points` → `/forecast`, nearest station `/observations/latest`) | same official forecast as forecast.weather.gov |
| Basemap | Esri Canvas Light/Dark Gray | CARTO was dropped because it now requires an API key |

## Limits
- **Open-Meteo free tier** (ZIP mode only): non-commercial, 10,000 calls/day and 600/minute. One ZIP load costs about 330 calls, so roughly 30 ZIP loads a day. Town buttons cost only 4 calls each, because their maps come from the pre-built data.
- **GitHub Actions** is free for public repos. Pages has a soft limit of 10 deploys an hour, and the hourly schedule stays well under it.
- GitHub may run scheduled workflows a few minutes late, and it pauses them if the repo has no activity for 60 days. Re-enable them on the Actions tab if that happens.

Model output is raw guidance, not an official forecast.
