# Data sources and deployment scope

## Global Fishing Watch

The three GFW tools query `public-global-presence:v4.0` and `public-global-sar-presence:v4.0`. They summarize upstream processed data; this server does not detect vessels from raw SAR. Results and exported provenance carry attribution, and summary PNGs display the source credit and URL. Preserve the credit when presenting or re-exporting these outputs.

[GFW API terms and limits](https://globalfishingwatch.org/our-apis/documentation/docs/license-rate-limits) describe noncommercial use under CC BY-NC 4.0, attribution, and account/token restrictions. Before deployment, agree with EVE and, where necessary, GFW on the account authorized to serve the intended users. A personal hackathon token is not a transferable deployment credential. The implementation currently reads only a server environment token; per-request credential headers are not implemented.

## Detailed AIS archive

[AISDataPortal/DB](https://huggingface.co/datasets/AISDataPortal/DB) labels its dataset Apache-2.0. This records the publisher's label, not independent verification of every underlying data right. Retain the repository, resolved commit, date/file and checksum from result provenance. Coverage is historical and uneven; some published daily files are malformed and are deliberately rejected. A successful known-date demo does not establish current worldwide coverage.

## Optical scene context

Scene identity and footprint come from [Copernicus Data Space STAC](https://stac.dataspace.copernicus.eu/v1/). Matching public Sentinel-2 COG assets come from the [Earth Search catalogue](https://earth-search.aws.element84.com/v1/). Keep source/product references and applicable Copernicus attribution when redistributing imagery. Scene overlays show reported AIS positions at nearby timestamps, not independently detected vessels.

## Scope for the first integration

Available: historical AIS presence/comparisons, processed SAR matched/unmatched counts, detailed AIS reports and exact Sentinel-2 scene context. Fisheries and port questions can use these observations subject to their limitations. Oil-spill detection, raw SAR processing, individual dark-vessel geolocation/identification, and live ship tracking are not implemented.

Deployment uses stateless MCP HTTP on port 8000. The runtime keeps its cache and exports on local disk. A resource read must reach the instance that produced the file. The AIS cache budget applies only to downloaded AIS data.
