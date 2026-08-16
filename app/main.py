import logging
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.routers import repos
from app.core.config import settings
from app.core.middleware import RequestIdMiddleware

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory()
)

logger = structlog.get_logger()

app = FastAPI(
    title=settings.app_name,
    description="A retrieval-augmented code review engine",
    version=settings.app_version
)

app.add_middleware(RequestIdMiddleware)
app.include_router(repos.router)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    logger.warning("http_error", status_code=exc.status_code, path=str(request.url), detail=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail,
            "status_code": exc.status_code,
            "path": str(request.url)
        }
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("unhandled_error", path=str(request.url), error=str(exc))
    return JSONResponse(
        status_code=500,
        content={
            "error": "An unexpected error occurred",
            "status_code": 500,
            "path": str(request.url)
        }
    )


@app.get("/health")
async def health_check():
    logger.info("health_check")
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version
    }