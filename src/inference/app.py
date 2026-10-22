"""FastAPI serving process: one offline model load per worker."""

from __future__ import annotations

import logging
import math
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictInt, field_validator

from inference.bundle import validate_release
from inference.metrics import Metrics

LOG = logging.getLogger(__name__)
NYC = ZoneInfo("America/New_York")


class PredictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pickup_location_id: StrictInt
    dropoff_location_id: StrictInt
    pickup_datetime: AwareDatetime

    @field_validator("pickup_location_id", "dropoff_location_id")
    @classmethod
    def valid_location(cls, value: int) -> int:
        if not 1 <= value <= 263:
            raise ValueError("location ID must be from 1 through 263")
        return value

    @field_validator("pickup_datetime", mode="before")
    @classmethod
    def timestamp_string(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("pickup_datetime must be an ISO 8601 string with timezone")
        return value


class PredictionResponse(BaseModel):
    predicted_duration_minutes: float
    model_name: str
    model_version: str


def make_app(bundle_path: Path | None = None) -> FastAPI:
    root = bundle_path or Path(os.getenv("MODEL_BUNDLE_DIR", "/app/release"))
    metrics_store = Metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.model = None
        app.state.release = None
        try:
            app.state.release, app.state.model = validate_release(root)
            release = app.state.release
            metrics_store.model = (release["model_name"], release["model_version"], release["source_run_id"])
        except Exception:
            LOG.exception("Model release failed startup validation")
        yield

    app = FastAPI(title="NYC Taxi Trip Duration Inference", version="3.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def measure(request: Request, call_next):
        start = time.perf_counter()
        path = request.url.path if request.url.path in {"/predict", "/healthz", "/readyz", "/model-info", "/metrics", "/docs", "/openapi.json"} else "other"
        method = request.method if request.method in {"GET", "POST"} else "other"
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            metrics_store.observe(method, path, status, time.perf_counter() - start)

    @app.get("/healthz")
    def healthz():
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz(request: Request):
        if request.app.state.model is None:
            raise HTTPException(status_code=503, detail="Model release is unavailable")
        return {"status": "ready"}

    @app.get("/model-info")
    def get_model_info(request: Request):
        if request.app.state.release is None:
            raise HTTPException(status_code=503, detail="Model release is unavailable")
        return {**request.app.state.release, "serving_git_sha": os.getenv("SERVING_GIT_SHA", "unknown")}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(metrics_store.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.post("/predict", response_model=PredictionResponse)
    def predict(payload: PredictionRequest, request: Request):
        model = request.app.state.model
        release = request.app.state.release
        if model is None:
            raise HTTPException(status_code=503, detail="Model release is unavailable")
        local: datetime = payload.pickup_datetime.astimezone(NYC).replace(tzinfo=None)
        features = {
            "tpep_pickup_datetime": local,
            "PULocationID": payload.pickup_location_id,
            "DOLocationID": payload.dropoff_location_id,
        }
        try:
            prediction = model.predict(features)
            if not math.isfinite(prediction):
                raise ValueError("Non-finite model output")
        except Exception as error:
            LOG.exception("Prediction failed")
            raise HTTPException(status_code=500, detail="Prediction failed") from error
        return PredictionResponse(predicted_duration_minutes=prediction, model_name=release["model_name"], model_version=release["model_version"])

    return app


app = make_app()
