# Vendored dependencies

## echarts.min.js — Apache ECharts 5.5.1

Source: https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js
License: Apache-2.0 (Apache Software Foundation)

Vendored rather than loaded from a CDN for two reasons:

1. The dashboard must render on a machine that can reach the GTFS-RT feed and
   nothing else. A CDN outage should not blank every chart.
2. Headless screenshot and offline review both need the page to be
   self-contained.

To update, re-download the same path at the new version and bump this file.

## maplibre-gl.js / maplibre-gl.css — MapLibre GL JS 5.24.0

Source: https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/dist/
License: BSD-3-Clause (MapLibre contributors)

Pinned to the 5.x line deliberately: 6.x ships ESM-only and no longer publishes
the UMD `dist/maplibre-gl.js` this page loads with a plain `<script>` tag.

The library is vendored for the same reasons as ECharts. **Basemap tiles are
not** — they are fetched at runtime from OpenFreeMap (https://openfreemap.org,
ODbL, no API key). The map degrades to network-and-trains-only when tiles are
unreachable, so the page still works offline; it just loses the terrain
underneath.
