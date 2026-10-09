from __future__ import annotations

import json
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from pyproj import CRS

app = FastAPI(
    title="Geospatial File Measurement API",
    version="1.0.0",
    description="Upload KML or zipped Shapefiles and retrieve feature measurements.",
)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
records: dict[str, dict[str, Any]] = {}


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, float) and (pd.isna(value) or value in (float("inf"), float("-inf"))):
        return None
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _read_geodata(upload_name: str, content: bytes) -> gpd.GeoDataFrame:
    suffix = Path(upload_name).suffix.lower()
    if suffix not in {".kml", ".zip"}:
        raise HTTPException(400, "Only .kml and zipped Shapefile (.zip) uploads are supported.")
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(content)) as archive:
                shp_names = [n for n in archive.namelist() if n.lower().endswith(".shp") and not n.startswith(("/", "\\")) and ".." not in Path(n).parts]
                if not shp_names:
                    raise HTTPException(400, "The ZIP archive must contain at least one .shp file.")
                # Require a matching DBF and SHX beside the selected shapefile.
                stem = shp_names[0][:-4].lower()
                siblings = {n.lower() for n in archive.namelist()}
                if stem + ".dbf" not in siblings or stem + ".shx" not in siblings:
                    raise HTTPException(400, "The Shapefile ZIP must include matching .shp, .shx, and .dbf files.")
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "The uploaded ZIP file is invalid.") from exc
        with tempfile.TemporaryDirectory(prefix="geo-upload-") as directory:
            source = Path(directory) / "upload.zip"
            source.write_bytes(content)
            try:
                frame = gpd.read_file(f"zip://{source}!{shp_names[0]}")
            except Exception as exc:
                raise HTTPException(422, f"Could not read the Shapefile: {exc}") from exc
    else:
        with tempfile.TemporaryDirectory(prefix="geo-upload-") as directory:
            source = Path(directory) / "upload.kml"
            source.write_bytes(content)
            try:
                frame = gpd.read_file(source, driver="KML")
            except Exception as exc:
                raise HTTPException(422, f"Could not read the KML file. Ensure GDAL has KML support. Details: {exc}") from exc

    if frame.empty:
        raise HTTPException(422, "The uploaded file contains no features.")
    if frame.geometry.name not in frame.columns:
        raise HTTPException(422, "The uploaded data has no geometry column.")
    return frame


def _measurement_geometry(frame: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame | None, str | None, float]:
    if frame.crs is None:
        return None, None, 1.0
    source_crs = CRS.from_user_input(frame.crs)
    if source_crs.is_geographic:
        try:
            target = frame.estimate_utm_crs()
        except Exception:
            target = None
        if target is None:
            return None, None, 1.0
        return frame.to_crs(target), target.to_string(), 1.0
    # Convert projected axis units (for example US survey feet) to metres.
    factor = source_crs.axis_info[0].unit_conversion_factor if source_crs.axis_info else 1.0
    return frame, source_crs.to_string(), float(factor or 1.0)


def _serialize_features(frame: gpd.GeoDataFrame) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    measured, measurement_crs, unit_factor = _measurement_geometry(frame)
    features: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    for index, (_, row) in enumerate(frame.iterrows()):
        geometry = row.geometry
        geometry_type = geometry.geom_type if geometry is not None and not geometry.is_empty else "GeometryCollection"
        properties = {str(key): _json_value(value) for key, value in row.drop(labels=[frame.geometry.name]).items()}
        feature_id = str(index)
        features.append({
            "id": feature_id,
            "geometry_type": geometry_type,
            "geometry": None if geometry is None or geometry.is_empty else geometry.__geo_interface__,
            "crs": frame.crs.to_string() if frame.crs else None,
            "properties": properties,
        })
        result: dict[str, Any] = {"feature_id": feature_id, "geometry_type": geometry_type, "measurement_crs": measurement_crs}
        projected_geometry = measured.geometry.iloc[index] if measured is not None else None
        if geometry_type in {"Polygon", "MultiPolygon"}:
            if projected_geometry is None or projected_geometry.is_empty:
                result.update(status="UNAVAILABLE", reason="A known CRS is required to measure this geometry safely.")
            else:
                result.update(status="COMPLETED", area_m2=round(float(projected_geometry.area * unit_factor**2), 4))
        elif geometry_type in {"LineString", "MultiLineString", "LinearRing"}:
            if projected_geometry is None or projected_geometry.is_empty:
                result.update(status="UNAVAILABLE", reason="A known CRS is required to measure this geometry safely.")
            else:
                result.update(status="COMPLETED", length_m=round(float(projected_geometry.length * unit_factor), 4))
        elif geometry_type in {"Point", "MultiPoint"}:
            result.update(status="NOT_REQUIRED")
        else:
            result.update(status="UNSUPPORTED", reason=f"Measurement is not supported for {geometry_type} geometries.")
        measurements.append(result)
    return features, measurements


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/files/", status_code=201)
async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = Path(file.filename or "").name
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Upload exceeds the 50 MB limit.")
    if not content:
        raise HTTPException(400, "The uploaded file is empty.")
    frame = _read_geodata(filename, content)
    features, measurements = _serialize_features(frame)
    file_id = uuid.uuid4().hex
    info = {
        "id": file_id,
        "filename": filename,
        "feature_count": len(features),
        "crs": frame.crs.to_string() if frame.crs else None,
        "status": "COMPLETED",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    records[file_id] = {"info": info, "features": features, "measurements": measurements}
    return info


@app.get("/api/files/{file_id}/")
def get_file(file_id: str) -> dict[str, Any]:
    record = records.get(file_id)
    if record is None:
        raise HTTPException(404, "File not found.")
    return record["info"]


@app.get("/api/files/{file_id}/measurements/")
def get_measurements(file_id: str) -> dict[str, Any]:
    record = records.get(file_id)
    if record is None:
        raise HTTPException(404, "File not found.")
    return {"file_id": file_id, "feature_count": len(record["measurements"]), "measurements": record["measurements"]}


@app.get("/api/files/{file_id}/features/")
def get_features(file_id: str) -> dict[str, Any]:
    record = records.get(file_id)
    if record is None:
        raise HTTPException(404, "File not found.")
    return {"file_id": file_id, "feature_count": len(record["features"]), "features": record["features"]}
