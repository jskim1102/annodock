"""FastAPI application factory for the dataset viewer service."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.jwks import JWKSVerifier
from app.auth.middleware import AuthenticationMiddleware
from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.routers.annotations import router as annotations_router
from app.routers.datasets import router as datasets_router
from app.routers.images import router as images_router
from app.routers.jobs import router as jobs_router
from app.routers.models import router as models_router
from app.routers.projects import router as projects_router
from app.routers.runs import router as runs_router
from app.routers.training import router as training_router
from app.routers.training import runs_router as training_runs_router
from app.routers.uploads import router as uploads_router
from app.services.reaper import run_reaper_loop


def create_app(
    settings: Settings | None = None,
    *,
    auto_start_jobs: bool = True,
    jwks_verifier: JWKSVerifier | None = None,
) -> FastAPI:
    runtime = settings or get_settings()
    verifier = jwks_verifier or JWKSVerifier(str(runtime.auth_base_url))
    owns_verifier = jwks_verifier is None

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        reaper_task = None
        if application.state.auto_start_jobs:
            reaper_task = asyncio.create_task(
                run_reaper_loop(
                    application.state.session_factory,
                    storage_dir=runtime.storage_dir,
                    keep_count=runtime.run_artifact_keep_count,
                    keep_days=runtime.run_artifact_keep_days,
                ),
                name="training-run-reaper",
            )
        application.state.reaper_task = reaper_task
        try:
            yield
        finally:
            if reaper_task is not None:
                reaper_task.cancel()
                with suppress(asyncio.CancelledError):
                    await reaper_task
            if owns_verifier:
                await verifier.aclose()

    application = FastAPI(title="dataset-viewer", lifespan=lifespan)
    application.state.settings = runtime
    application.state.engine = create_engine(runtime.database_url)
    application.state.session_factory = create_session_factory(
        application.state.engine
    )
    application.state.auto_start_jobs = auto_start_jobs
    application.state.job_tasks = set()
    application.state.jwks_verifier = verifier
    application.add_middleware(
        AuthenticationMiddleware,
        verifier=verifier,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @application.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    application.include_router(datasets_router)
    application.include_router(projects_router)
    application.include_router(images_router)
    application.include_router(annotations_router)
    application.include_router(uploads_router)
    application.include_router(jobs_router)
    application.include_router(runs_router)
    application.include_router(training_router)
    application.include_router(training_runs_router)
    application.include_router(models_router)
    return application


app = create_app()
