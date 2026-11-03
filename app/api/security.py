"""Optional shared bearer credential for a private backend deployment."""
from secrets import compare_digest

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)


def require_api_key(request: Request,
                    credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    expected = request.app.state.settings.api_key
    if expected is None:
        return
    supplied = credentials.credentials if credentials else ""
    if not compare_digest(supplied.encode(), expected.get_secret_value().encode()):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED"},
                            headers={"WWW-Authenticate": "Bearer"})
