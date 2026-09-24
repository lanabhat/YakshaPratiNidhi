"""Who is calling. Three ways in, in priority order:

1. Authorization: Bearer <token>
   - AUTH_MODE=dev (local development only): token is "dev:<email>[:<Name>]",
     trusted as-is. Lets the whole role workflow be exercised with no Google/
     Firebase account. Never enable outside a developer machine.
   - AUTH_MODE=firebase: token is a Firebase ID token (Google sign-in),
     verified against Google's published keys. NOTE: written to Firebase's
     documented verification steps but not yet exercised against a real
     project - verify once the Firebase project exists (see README).
2. X-Guest-Id: <random id the browser keeps> -> an anonymous "guest" that may
   view and suggest but never approve.
3. Neither -> anonymous read-only.

Roles are stored in our own database, never taken from the token, so an admin
assigning "editor" in the app is the single source of truth. Emails listed in
SUPERADMIN_EMAILS are made superadmin on first sight (bootstrap)."""

import os

import requests

from lipisampada.reviewapi.db import Db

_FIREBASE_CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"


class AuthError(Exception):
    pass


def _superadmin_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("SUPERADMIN_EMAILS", "").split(",") if e.strip()}


def _verify_firebase(token: str) -> dict:
    try:
        import jwt  # PyJWT (+ cryptography), only needed in firebase mode
    except ImportError as e:
        raise AuthError("firebase auth needs: pip install pyjwt cryptography") from e
    project = os.environ.get("FIREBASE_PROJECT_ID")
    if not project:
        raise AuthError("FIREBASE_PROJECT_ID is not set")
    kid = jwt.get_unverified_header(token).get("kid")
    certs = requests.get(_FIREBASE_CERTS_URL, timeout=10).json()
    if kid not in certs:
        raise AuthError("unknown signing key")
    from cryptography.x509 import load_pem_x509_certificate

    key = load_pem_x509_certificate(certs[kid].encode()).public_key()
    try:
        return jwt.decode(
            token, key, algorithms=["RS256"], audience=project, issuer=f"https://securetoken.google.com/{project}"
        )
    except jwt.PyJWTError as e:
        raise AuthError(f"invalid token: {e}") from e


def current_user(db: Db, headers) -> dict | None:
    authz = headers.get("Authorization", "")
    if authz.startswith("Bearer "):
        token = authz[7:].strip()
        mode = os.environ.get("AUTH_MODE", "dev")
        if mode == "dev":
            if not token.startswith("dev:"):
                raise AuthError("dev mode expects 'dev:<email>[:<Name>]'")
            parts = token.split(":", 2)
            email = parts[1].strip().lower()
            name = parts[2] if len(parts) > 2 else email.split("@")[0]
            uid = f"dev:{email}"
        elif mode == "firebase":
            claims = _verify_firebase(token)
            uid, email, name = f"fb:{claims['sub']}", (claims.get("email") or "").lower(), claims.get("name")
        else:
            raise AuthError(f"unknown AUTH_MODE {mode!r}")
        role = "superadmin" if email in _superadmin_emails() else "reviewer"
        user = db.get_or_create_user(uid, email, name, role)
        if email in _superadmin_emails() and user["role"] != "superadmin":
            db.conn.execute("UPDATE users SET role='superadmin' WHERE uid=?", (uid,))
            db.conn.commit()
            user = db.get_or_create_user(uid)
        return user

    guest = headers.get("X-Guest-Id", "").strip()
    if guest:
        if not guest.replace("-", "").isalnum() or len(guest) > 64:
            raise AuthError("bad guest id")
        return db.get_or_create_user(f"guest:{guest}", None, "Guest", "guest")
    return None
