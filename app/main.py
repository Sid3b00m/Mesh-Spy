from __future__ import annotations

import argparse
import logging
import os
import sys
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

from app import __version__
from app.api.routes import router as api_router
from app.core.auth import (
    COOKIE,
    attach_session_cookie,
    auth_enabled,
    create_session,
    drop_session,
    public_path,
    session_valid,
    verify_login,
)
from app.core.config import ROOT, get_config
from app.core.security import SECURITY_HEADERS

APP_DIR = Path(__file__).resolve().parent
log = logging.getLogger("mesh-spy")


@asynccontextmanager
async def lifespan(_: FastAPI):
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    from app.core.mesh.registry import MeshRegistry, set_registry
    from app.core.mesh.store import init_db

    init_db()
    registry = MeshRegistry()
    set_registry(registry)
    await registry.start()
    try:
        yield
    finally:
        # Radios hold serial ports and BLE handles, so a clean close matters
        # more here than it does for a read-only dashboard.
        await registry.stop()
        set_registry(None)


app = FastAPI(title="Mesh-Spy", version=__version__, lifespan=lifespan)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not auth_enabled() or public_path(request.url.path):
            return await call_next(request)
        token = request.cookies.get(COOKIE)
        if session_valid(token):
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": "auth required"}, status_code=401)
        return RedirectResponse("/login", status_code=302)


app.add_middleware(AuthMiddleware)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response


app.add_middleware(SecurityHeadersMiddleware)

# Node lists and message history compress well. Added last so it wraps
# everything, and SSE is exempted below because gzip would buffer the stream.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str | None = None):
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "auth_on": auth_enabled()}
    )


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if not auth_enabled():
        return RedirectResponse("/", status_code=302)
    client = request.client.host if request.client else "unknown"
    if not verify_login(username, password, client_key=client):
        return RedirectResponse("/login?error=1", status_code=302)
    token = create_session()
    resp = RedirectResponse("/", status_code=302)
    attach_session_cookie(resp, token)
    return resp


@app.get("/logout")
def logout(request: Request):
    drop_session(request.cookies.get(COOKIE))
    resp = RedirectResponse("/login" if auth_enabled() else "/", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    from app.core.mesh.store import SPARK_POINTS
    from app.core.security import MESSAGE_MAX_CHARS

    cfg = get_config()
    ctx = {
        "version": __version__,
        "auth_on": auth_enabled(),
        "read_only": cfg.mesh.read_only,
        "spark_points": SPARK_POINTS,
        "message_max": MESSAGE_MAX_CHARS,
    }
    return templates.TemplateResponse(request, "index.html", ctx)


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def configure_logging() -> Path | None:
    """Log to stderr, or to a file when there is no stderr to log to.

    Windows autostart runs the app under pythonw.exe so no console window
    appears at logon, and pythonw gives the process no stderr at all: every log
    line would go nowhere and a radio that failed to open would look like the
    app doing nothing. Falling back to a file keeps that diagnosable. Rotation
    is capped because this also runs off a Pi's SD card.

    Returns the file being written to, or None when logging to stderr.
    """
    destination = os.environ.get("MESH_SPY_LOG_FILE", "").strip()
    if not destination and sys.stderr is None:
        destination = str(ROOT / "data" / "mesh-spy.log")

    if not destination:
        logging.basicConfig(level=logging.INFO)
        return None

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app.main", description="Mesh-Spy console.")
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="list serial ports that look like a mesh radio, then exit",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    if args.list_ports:
        from app.core.ports import format_ports, list_ports

        print(format_ports(list_ports()))
        return

    log_file = configure_logging()
    cfg = get_config()
    (ROOT / "data").mkdir(parents=True, exist_ok=True)

    host = cfg.server.host
    if host in ("0.0.0.0", "::") and not auth_enabled():
        msg = (
            f"Refusing to bind {host}:{cfg.server.port} with auth disabled. "
            "Enable auth (auth.enabled + MESH_SPY_PASSWORD), bind 127.0.0.1, "
            "or set MESH_SPY_ALLOW_INSECURE_LAN=1 to override."
        )
        if _truthy("MESH_SPY_ALLOW_INSECURE_LAN"):
            log.warning("%s Override accepted.", msg)
        else:
            log.error(msg)
            raise SystemExit(2)

    if cfg.mesh.read_only:
        log.info("mesh.read_only is true - transmit endpoints are refused")
    if _truthy("MESH_SPY_NO_DEMO"):
        log.info("MESH_SPY_NO_DEMO=1 - the simulated network is disabled")

    # uvicorn's default log config installs its own stdout handlers, which are
    # useless under pythonw (there is no stdout) and would bypass the file
    # anyway. Handing it None makes it inherit the root logger set up above, so
    # "Uvicorn running on ..." and any bind error land in the same place.
    uvicorn.run(
        app,
        host=host,
        port=cfg.server.port,
        reload=False,
        log_config=None if log_file else uvicorn.config.LOGGING_CONFIG,
    )


if __name__ == "__main__":
    main()
