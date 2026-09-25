import os, secrets
from fastapi import Request
from fastapi.responses import JSONResponse

PUBLIC_PATHS = {"/health", "/mobile"}

def install_auth(app):
    @app.middleware("http")
    async def edge_token_guard(request: Request, call_next):
        expected = os.getenv("EDGE_ACCESS_TOKEN", "").strip()
        if not expected:
            return await call_next(request)
        supplied = (request.headers.get("x-edge-token") or request.query_params.get("token") or request.cookies.get("edge_token") or "").strip()
        if request.url.path in PUBLIC_PATHS:
            if request.url.path == "/mobile" and request.query_params.get("token") and not secrets.compare_digest(supplied, expected):
                return JSONResponse({"detail":"invalid access token"}, status_code=401)
            response = await call_next(request)
            if request.url.path == "/mobile" and supplied and secrets.compare_digest(supplied, expected):
                response.set_cookie("edge_token", expected, httponly=True, secure=True, samesite="lax", max_age=60*60*24*30)
            return response
        if not supplied or not secrets.compare_digest(supplied, expected):
            return JSONResponse({"detail":"access token required"}, status_code=401)
        return await call_next(request)
