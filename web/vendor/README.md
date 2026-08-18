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
