"""Who may do what - the single place rights are defined.

Every permission is granted from a minimum role upward (guest < reviewer < editor < admin < superadmin).
To change what a role can do, edit PERMISSIONS below (and the test that pins the matrix,
tests/test_permissions.py). Nothing else in the code compares role names to decide access; it calls
require(user, "<permission>") or can(user, "<permission>")."""


class Forbidden(Exception):
    pass


ROLES = ["guest", "reviewer", "editor", "admin", "superadmin"]
ASSIGNABLE_ROLES = ["reviewer", "editor", "admin", "superadmin"]  # guests are anonymous; nobody is "made" one

# name: (minimum role, what it means)
PERMISSIONS = {
    "read": ("guest", "Browse the library, pages and full text"),
    "suggest": ("guest", "Suggest a corrected text, or mark a snippet as looking right"),
    "approve_text": ("editor", "Accept a suggested word change; approve a snippet or a whole page"),
    "reopen": ("admin", "Re-open a finalized snippet so it can be changed again"),
    "manage_users": ("admin", "See users, change roles up to admin, invite by email, deactivate accounts"),
    "manage_books": ("admin", "Hide or show a book in the library; see hidden books; delete a snippet"),
    "view_stats": ("admin", "See per-contributor activity"),
    "grant_superadmin": ("superadmin", "Grant, change or remove the superadmin role"),
}

_RANK = {r: i for i, r in enumerate(ROLES)}
ROLE_PERMS = {r: sorted(p for p, (minimum, _) in PERMISSIONS.items() if _RANK[r] >= _RANK[minimum]) for r in ROLES}

GUEST_LEVEL = ROLE_PERMS["guest"]  # read + suggest, nothing more

# A brand-new Google sign-in ("new") hasn't answered the onboarding form yet; "pending" said yes and
# is waiting on an admin; "declined"/"rejected" said or were told no. All four sit at guest-level
# permissions regardless of the role column, until an admin approval (or a direct role change, which
# counts as approval) moves them to "approved". "unset" is the default for accounts this gate doesn't
# apply to at all: pre-existing rows from before this feature, invited accounts, and guests.
GATED_STATUSES = {"new", "pending", "declined", "rejected"}


def roles_with(permission: str) -> set[str]:
    return {r for r in ROLES if permission in ROLE_PERMS[r]}


def permissions_for(user: dict | None) -> list[str]:
    """What this person may do right now."""
    if user is None:
        return ["read"]
    if user.get("banned"):
        return []  # not even read - a banned person gets nothing back from the API but /api/me
    if not user.get("active", 1):
        return ["read"]
    if user.get("application_status") in GATED_STATUSES:
        return GUEST_LEVEL
    return ROLE_PERMS.get(user["role"], ["read"])


def can(user: dict | None, permission: str) -> bool:
    return permission in permissions_for(user)


def require(user: dict | None, permission: str) -> dict:
    if can(user, permission):
        return user
    if user is not None:
        if user.get("banned"):
            raise Forbidden(f"this account has been banned{': ' + user['ban_reason'] if user.get('ban_reason') else ''}")
        if not user.get("active", 1):
            raise Forbidden("this account has been deactivated")
        if user.get("application_status") in GATED_STATUSES:
            raise Forbidden("your volunteer application is still awaiting admin approval")
    minimum = PERMISSIONS[permission][0]
    raise Forbidden(f"needs the {minimum} role or higher: {PERMISSIONS[permission][1].lower()}")


def matrix() -> dict:
    return {
        "roles": ROLES,
        "permissions": [
            {"name": p, "minimum_role": m, "description": d, "roles": sorted(roles_with(p), key=_RANK.get)}
            for p, (m, d) in PERMISSIONS.items()
        ],
    }
