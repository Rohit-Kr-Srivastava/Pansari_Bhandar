"""FastAPI application factory."""

from __future__ import annotations

import logging
import mimetypes
import sys
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import Settings, get_settings
from app.core.ratelimit import RateLimiter
from app.web import account, admin, cart, checkout, public, seller, shop
from app.web.deps import (
    CsrfFailed,
    Forbidden,
    LoginRequired,
    NotFound,
    RateLimited,
    csrf_protect,
    is_htmx,
)
from app.web.middleware import (
    AllowedHostsMiddleware,
    BodySizeLimitMiddleware,
    RequestLogMiddleware,
    SecurityHeadersMiddleware,
    apply_security_headers,
)
from app.web.templating import STATIC_DIR, PageCache, render, toast_headers

log = logging.getLogger("app")
# Not every OS (or slim Docker image) maps .webp; uploads are always stored as WebP.
mimetypes.add_type("image/webp", ".webp")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        stream=sys.stdout,
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # RequestLogMiddleware logs


def build_engine(settings: Settings) -> Engine:
    if settings.database_url.startswith("sqlite"):
        return create_engine(settings.database_url, connect_args={"check_same_thread": False})
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_size,
        pool_recycle=1800,
    )


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = engine or build_engine(settings)

    app = FastAPI(
        title="Pansari Bhandar",
        debug=settings.debug,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(csrf_protect)],
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app.state.ratelimiter = RateLimiter()
    app.state.cache = PageCache()

    settings.media_root.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/media", StaticFiles(directory=settings.media_root), name="media")

    for module in (public, shop, cart, checkout, account, seller, admin):
        app.include_router(module.router)

    # Middleware runs bottom-up: the last added is the outermost.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="pb_session",
        max_age=settings.session_max_age_days * 86400,
        same_site="lax",
        https_only=settings.is_production,
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=256 << 10,  # ordinary forms
        upload_max_bytes=(settings.max_upload_mb * 9 + 1) << 20,  # up to 8 photos + fields
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    if settings.host_list != ["*"]:
        app.add_middleware(AllowedHostsMiddleware, hosts=settings.host_list)
    app.add_middleware(RequestLogMiddleware)

    _register_error_handlers(app)
    return app


def _db(request: Request) -> Session:
    db: Session = request.app.state.sessionmaker()
    return db


def _error_page(request: Request, status: int, title: str, message: str) -> Response:
    if is_htmx(request):
        return Response(status_code=status, headers=toast_headers(message, "error"))
    # Objects loaded by the failed request belong to a closed session; load them again.
    for attr in ("user", "site"):
        if hasattr(request.state, attr):
            delattr(request.state, attr)
    with _db(request) as db:
        try:
            return render(
                request,
                db,
                "pages/error.html",
                {"status": status, "title": title, "message": message},
                status_code=status,
            )
        except Exception:  # never fail while showing an error
            log.exception("error page failed")
            return PlainTextResponse(message, status_code=status)


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired) -> Response:
        url = f"/login?next={quote(exc.next_url)}"
        if is_htmx(request):
            return Response(status_code=200, headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden) -> Response:
        if exc.message == "pending-seller":
            return RedirectResponse("/seller/pending", status_code=303)
        return _error_page(request, 403, "Not allowed", exc.message)

    @app.exception_handler(CsrfFailed)
    async def _csrf(request: Request, exc: CsrfFailed) -> Response:
        return _error_page(
            request,
            403,
            "Session expired",
            "Your session expired or the page was open too long. Please reload and try again.",
        )

    @app.exception_handler(RateLimited)
    async def _limited(request: Request, exc: RateLimited) -> Response:
        return _error_page(request, 429, "Please wait", exc.message)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound) -> Response:
        return _error_page(request, 404, "Page not found", "We could not find that page.")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return _error_page(request, 404, "Page not found", "We could not find that page.")
        if exc.status_code == 405:
            return _error_page(request, 405, "Not allowed", "That action is not allowed here.")
        return _error_page(request, exc.status_code, "Something went wrong", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> Response:
        return _error_page(
            request, 400, "Please check your input", "Some of the information sent was not valid."
        )

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception) -> Response:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        response = _error_page(
            request,
            500,
            "Something went wrong",
            "Sorry, something went wrong on our side. Please try again in a moment.",
        )
        apply_security_headers(response.headers)  # this handler runs outside our middleware
        return response


def app_factory() -> FastAPI:
    """Entry point for uvicorn: uvicorn app.main:app_factory --factory"""
    return create_app()
