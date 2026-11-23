"""
OrbitCast: forecast future Earth-observation acquisitions (when, where, planned or not, cloudy or not).

The MCP tools live in ../server.py; this package holds the logic, layered from low to high level:
    config    folders, limits, colours
    geo       AOI parsing and spherical geometry
    catalog   satellites, sensor models, download latency
    orbits    TLEs + SGP4 -> overpasses and swaths ('geometric')
    plans     ESA Sentinel-1/2 acquisition plans ('planned')
    clouds    Open-Meteo cloud forecast ('likely_usable' / 'cloudy')
    forecast  combines the above into the tool results
    geojson   forecast footprints as GeoJSON files
    coverage  reverse query: which areas are imaged in a window
"""

__version__ = "0.4.0"
