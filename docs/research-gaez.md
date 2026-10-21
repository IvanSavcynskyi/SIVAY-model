# GAEZ / geo: implemented scope and remaining verification

Research and import date: 2026-10-21 local time (2026-10-20 UTC).

The MVP reads a local `manifest.json` and local single-band rasters. An absent
manifest means no GAEZ observations, not synthetic suitability. The adapter never
guesses filenames or fetches data while ranking. Ten real historical SUX rasters
and six official dimension CSVs are now downloaded. Their contents are not treated
as crop subtype evidence beyond their explicit captions.

## Actual import and smoke verification

```sh
.venv/bin/python scripts/import_mvp_data.py
```

The reproducible importer lists the official GCS bucket, selects only filenames
actually returned by the listing, and pins downloads to their object generation.
It verifies size, source MD5, local SHA-256, datatype, NoData, native resolution,
and suitability range. It retries transient network errors, reuses matching local
files, and defaults to a 200 MiB total transfer cap; `--budget-mb` cannot exceed
the authorized 10,240 MiB ceiling. The selected ten SUX layers total **15,177,285
bytes** for BRL, MZE, OAT, PEA, RSD, RYE, SFL, SOY, SUB, WHE; all use
HP0120/AGERA5/HIST/HRLM. Current-cropland suitability is in `index_0_10000` units.

Actual SUX files revealed a discrepancy with the specification: in addition to
declared NoData −9, the source includes the value **−1**. Its semantic label is
unverified. Every imported entry explicitly declares `excluded_values: [-1]`;
this flag becomes missing data, never a zero score or a guessed water category.
Other out-of-range values still fail import. Excluded values are also part of
the extraction cache key.

Real SCX BRL contains **class 10**, despite the specification's legend ending
at 9. Its complete legend could not be independently established. The importer
therefore excludes SCX entirely; a preliminary downloaded file remains outside
the active manifest. Class shares are unknown in the live baseline. The strict
class validator remains unchanged. No G4 exclusion can be claimed from these
unverified classes. Do not enable categorical production data until the source
legend has been reconciled.

`data/gaez/gcs-object-evidence.json` records source generation, MD5, original GCS
creation/finalization timestamps, and observed public access time. GCS creation
in February 2025 proves object existence, not historical public access; the
manifest conservatively uses the actual October 2026 retrieval timestamp for
`available_at`. Historical requests before that observed availability omit the
layers. Re-running with identical data preserves these timestamps.

An actual national Ukraine boundary is downloaded via the
[geoBoundaries UKR ADM0 API](https://www.geoboundaries.org/api/current/gbOpen/UKR/ADM0/).
The API identifies `UKR-ADM0-60985579`, OpenStreetMap/Wambacher source, and
Open Data Commons Open Database License 1.0, with licence attribution at
[OpenStreetMap copyright](https://www.openstreetmap.org/copyright). The importer
follows the API's pinned GeoJSON URL, preserves the geometry, and adds region
code `UA`. `data/regions.source.json` records source metadata and checksums.
Kyiv, Simferopol, Donetsk, and Luhansk control points are covered; the boundary
does not follow present military control. This check confirms coverage of those
places, not a survey-level certification of every border coordinate.

Local API smoke checks using five small geometries near Poltava, Kyiv, Lviv,
Kherson, and Simferopol each returned HTTP 200, `LIMITED`, five results, and no
source errors. Health reports `recommendation_ready=true`, `demo=false` for the
GAEZ baseline. These checks establish real data integration; they are not the
20-point comparison with portal values or agronomic validation.

## Confirmed sources and subtype decision

* [Official RES05 crop dimension](https://storage.googleapis.com/fao-gismgr-gaez-v5-data/DATA/GAEZ-V5/DIMENSION/GAEZ-V5.CROP-RES05.csv):
  contains generic `WHE`, `BRL`, `RSD`, `RYE`, without winter/spring variants.
* [Official RES02 crop dimension](https://storage.googleapis.com/fao-gismgr-gaez-v5-data/DATA/GAEZ-V5/DIMENSION/GAEZ-V5.CROP-RES02.csv):
  `SWHE` is spring wheat; `HWHE` is highland wheat in the tropics;
  `WWHE` is winter wheat for subtropical/tropical highlands; `TWHE` is
  temperate/subtropical wheat. These captions do not justify assigning `HWHE` to
  Ukrainian winter wheat.
* [FAO Module V](https://github.com/un-fao/gaezv5/wiki/07.-Module-V-(Integration-of-climatic-and-edaphic-evaluation)):
  integrates climate with soils and terrain. Its current-cropland products assume
  cropland occupies the better part of the suitability distribution. `SUX` and
  `SCX` therefore remain distinct from whole-cell products.
* [FAO Appendix 4-1](https://github.com/un-fao/gaezv5/wiki/14.-Appendix-4%E2%80%901-Crops)
  links an Excel crop list. Its subtype inconsistency reported in the design has
  **not** been independently resolved here. Neither a LUT lookup for `RES02-IDX`
  nor a verified interpretation of its numeric identifiers is available in this
  implementation. Do not infer winter suitability from these IDs.

Decision: winter and spring candidates retain their generic RES05 parent code
and `gaez_match: parent_crop`. RES02 cannot replace the soil-aware RES05 score.

Known datatype claims are enforced for SUX/SXX and SCX/SIX. The observed extra
codes and active import exclusions are documented above. The design's FAO
catalogue attribution is FAO & IIASA, GAEZ v5; the catalogue page currently
returns 403 to the web tool, so licence terms for each actual imported product
have not been independently reverified in this run. Portal control points and
agronomic validation remain external verification tasks.

## Local import

`GaezStore(root, manifest_path=None)` defaults to `root/manifest.json`. The JSON
object contains a `layers` array. Every entry requires:

* `layer_code`, `variable`, `unit`, `crop_code`, `path`;
* `source_url` copied from a verified bucket listing/metadata;
* `available_at`, `retrieved_at`, both timestamps with explicit timezones;
* `sha256`, `dtype`, `nodata`, `native_res_deg`;
* optional `excluded_values` for explicitly recorded source flags;
* scenario identifiers `wsim`, `period`, `clim`, `ssp` (defaults are HRLM,
  HP0120, AGERA5, HIST).

`variable` accepts `RES05-SUX` or `SUX` (likewise the other supported variables).
Suitability index values remain in native units 0–10000, without converting to
percent or score in the adapter. Yield units must be explicitly verified by the
operator; raw GAEZ yield never becomes an estimate of farm yield here.

To register an already downloaded file, prepare a JSON entry with all fields
above except `sha256`, `dtype`, `nodata`, `native_res_deg` and run:

```sh
python -m app.adapters.gaez_store register --root data/gaez --record /path/to/layer-record.json
python -m app.adapters.gaez_store verify --root data/gaez
```

Registration computes metadata/checksum from disk and checks every raster block
for known class/index ranges. Paths must be relative to the root; traversal,
remote GDAL paths, and symlinks escaping the root are rejected. Registering an
existing layer code is rejected. Data files and the manifest belong outside git.

`available_at` means first independently established public availability of this
dataset version. Never substitute the climate period's end, or an invented
publication date. If the only evidence is today's retrieval, use the actual
retrieval timestamp conservatively; past recommendations will omit that layer.
For date-only requests the cutoff includes the entire UTC day. File availability
after the cutoff cannot leak through the extraction cache.

Bucket discovery, downloads, dimension import, and source evidence are implemented
by `scripts/import_mvp_data.py`. Country clipping and 30-arcsecond datasets are
not needed for the small 5-arcminute baseline and are not downloaded. The API
reads the prepared local files independently of network access.

## Geometry and extraction

`validate_geometry` accepts Polygon/MultiPolygon, requires an explicit CRS,
transforms coordinates with longitude-first axis order, checks finite coordinates
and geographic bounds, and repairs invalid polygons only when area changes by
less than 1%. A reversed axis order that is still geographically plausible cannot
be detected reliably without an authoritative region boundary; coordinates are
never swapped automatically. Antimeridian-spanning input is rejected explicitly.

The geometry hash is SHA-256 of normalized WKB after rounding to 1e-7 degrees.
Extraction uses the same canonical geometry, so a hash cannot identify two
slightly different calculation inputs. Hole orientation, ring start, and component
ordering are normalized.

Shapely computes exact polygon intersections with native raster cells. This
performs the clipping operation needed from `exactextract` without another native
dependency. WGS84 geodesic areas of the clipped polygons supply weights; edges are
densified to at most 0.01 degrees to preserve the raster's longitude/latitude
edges. Rasters are not reprojected or resampled. A one-million-cell extraction
window limit protects the synchronous MVP from accidental continent-sized input.

Numeric means use valid area only; extrema use valid cells occupying at least
10% of the field. Classes are never averaged: shares use valid non-water area,
class 9 is reported as water separately, and water/NoData/outside-raster area
reduce coverage. Missing numeric observations are `None`. An extraction cache
uses geometry hash, source checksum, and file stat signature; changed local files
must pass verification again.

Synthetic tests cover one/multiple pixels, unequal intersections, latitude area
weights, holes, MultiPolygon, CRS roundtrip, NoData, water, missing raster extent,
availability cutoff, metadata/range/checksum validation, and safe local paths.
These tests do not replace real portal validation or agronomic approval.
