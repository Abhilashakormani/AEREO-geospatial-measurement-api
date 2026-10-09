# Geospatial File Measurement API

A FastAPI service that accepts KML files or ZIP archives containing a Shapefile, extracts the features and their attributes, and calculates polygon area and line length in metres.

## Requirements and setup

- Python 3.10 or newer
- GDAL with its KML driver enabled (GeoPandas uses GDAL/Fiona or Pyogrio to read geospatial formats)

From this directory, create an environment and install the packages:

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The service runs at `http://127.0.0.1:8000`. Interactive API docs are at `/docs`.

## API

### Upload and process

`POST /api/files/` with multipart form field named `file`. Accepts `.kml` or `.zip` with matching `.shp`, `.shx`, and `.dbf` components. Maximum upload size is 50 MB.

```bash
curl -F "file=@survey.kml" http://127.0.0.1:8000/api/files/
```

Example response (`201 Created`):

```json
{
  "id": "8f3266460ca748c79d1e0133ee8a6574",
  "filename": "survey.kml",
  "feature_count": 2,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "created_at": "2026-10-09T12:00:00+00:00"
}
```

### Get file information

`GET /api/files/{id}/` returns the metadata above. Unknown IDs return `404`.

### Get feature measurements

`GET /api/files/{id}/measurements/` returns one result per feature:

```json
{
  "file_id": "8f3266460ca748c79d1e0133ee8a6574",
  "feature_count": 2,
  "measurements": [
    {"feature_id": "0", "geometry_type": "Polygon", "measurement_crs": "EPSG:32643", "status": "COMPLETED", "area_m2": 2315.4182},
    {"feature_id": "1", "geometry_type": "Point", "measurement_crs": "EPSG:32643", "status": "NOT_REQUIRED"}
  ]
}
```

Polygon results include `area_m2`; line results include `length_m`. Points are marked `NOT_REQUIRED`. Unsupported geometry types receive `UNSUPPORTED` with an explanation. If CRS is missing, required measurements are `UNAVAILABLE`; degrees are never reported as area or length.

### Get extracted features

`GET /api/files/{id}/features/` returns the feature index, geometry type, GeoJSON geometry, source CRS, and properties for every feature.

Errors use normal HTTP status codes and a JSON `detail`: `400` for invalid uploads, `413` for files over the limit, `422` when data cannot be read, and `404` for unknown file IDs.

## Architecture and processing flow

`app/main.py` defines HTTP routes, input validation, geospatial processing, and the temporary in-memory record store. Upload bytes are written to a short-lived temporary directory for GDAL/GeoPandas to read, and removed immediately after parsing. The API retains only parsed response data for subsequent lookups.

The upload route validates the extension, size, ZIP structure, and presence of shapefile components. GeoPandas parses features and properties. Each geometry is serialized in its original CRS. Measurement uses a projected CRS: geographic inputs are transformed to the UTM zone estimated from the dataset extent, while projected inputs retain their CRS and are converted from its axis units to metres. Polygon area is converted to square metres and line length to metres. Missing CRS makes these calculations unavailable rather than silently measuring degrees.

## Design decisions and limitations

- **FastAPI + GeoPandas:** FastAPI provides typed, self-documenting REST routes; GeoPandas and pyproj handle common GIS formats and CRS transformation. Django REST Framework would be a valid alternative but adds more structure than this focused service needs.
- **Automatic UTM selection:** A dataset-local UTM projection is a practical choice for local surveys. Large or cross-zone datasets may need a user-selected projection or geodesic measurement strategy for best accuracy.
- **In-memory metadata:** This keeps the assignment runnable without external infrastructure. Records disappear on restart and are not shared across workers. Production deployment should use object storage for originals and PostgreSQL/PostGIS for durable metadata and geometries.
- **Deployment hardening:** Authentication, quotas, asynchronous processing, persistent job status, malware scanning, and distributed storage are intentionally future production steps.
- **Learning and future scope:** This project demonstrates REST API design, Python data handling, file validation, geometry processing, CRS awareness, and clear error handling. Next steps include authentication, PostGIS persistence, background jobs, Docker deployment, and a React/TypeScript map for viewing uploaded features.

## Submission

To submit publicly, create a GitHub repository, add this project, and include its public URL with your application. This workspace implementation does not publish a repository.

## Sample upload

A small sample Shapefile archive is included at `sample_data/ne_110m_admin_0_countries.zip`. Upload this ZIP directly to `POST /api/files/` to try the upload, feature extraction, and polygon measurement endpoints. It is Natural Earth's generalized 1:110m world country boundaries (258 polygon features, WGS 84 / EPSG:4326), from the [official Natural Earth download](https://www.naturalearthdata.com/downloads/110m-cultural-vectors/). Since this sample spans the globe, treat its computed areas as an API demonstration only; accurate measurement of a global dataset requires an area-appropriate projection or per-feature CRS strategy.
