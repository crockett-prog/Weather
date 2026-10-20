# Builds the high-res town maps (Nashville, Alamo, Dresden) every hour and publishes the site.
# Settings -> Pages -> Build and deployment -> Source must be "GitHub Actions".
name: Build maps and deploy site

on:
  schedule:
    - cron: "17 * * * *"      # hourly; unchanged model runs are reused from cache, so most runs are quick
  workflow_dispatch:           # "Run workflow" button on the Actions tab
  push:
    branches: [main]

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 50
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install
        run: pip install numpy scipy pygrib pyproj requests ecmwf-opendata

      - name: Restore model cache
        uses: actions/cache/restore@v4
        with:
          path: .mapcache
          key: mapcache-${{ github.run_id }}
          restore-keys: mapcache-

      - name: Build map data
        run: |
          mkdir -p _site
          cp index.html _site/
          python scripts/build_maps.py --out _site/data --cache .mapcache

      - name: Save model cache
        if: always()
        uses: actions/cache/save@v4
        with:
          path: .mapcache
          key: mapcache-${{ github.run_id }}

      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: _site

  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
