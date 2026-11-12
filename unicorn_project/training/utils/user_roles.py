from __future__ import annotations

ROLE_PRIORITY = ("admin", "instructor", "engineer", "inspector")

ROLE_LABELS = {
    "admin": "Admin",
    "instructor": "Instructor",
    "engineer": "Engineer",
    "inspector": "Inspector",
}

ROLE_DASHBOARDS = {
    "admin": "app_admin_dashboard",
    "instructor": "instructor_dashboard",
    "engineer": "engineer_dashboard",
    "inspector": "inspector_dashboard",
}


def user_has_role(user, role: str) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False

    role = (role or "").lower()
    roles = get_user_roles(user)
    if role == "admin":
        return user.is_superuser or "admin" in roles
    return role in roles


def get_user_roles(user) -> set[str]:
    """Load group roles once per user per request."""
    cached = getattr(user, "_pegasus_roles", None)
    if cached is not None:
        return cached

    roles: set[str] = set()
    if user.is_authenticated:
        if user.is_superuser:
            roles.add("admin")
        try:
            roles.update(
                name.lower()
                for name in user.groups.values_list("name", flat=True)
                if name
            )
        except Exception:
            pass

    user._pegasus_roles = roles
    return roles


def available_roles_for_user(user) -> list[str]:
    return [role for role in ROLE_PRIORITY if user_has_role(user, role)]


def automatic_role(user) -> str | None:
    for role in ROLE_PRIORITY:
        if user_has_role(user, role):
            return role
    return None


def role_choice_label(role: str) -> str:
    return ROLE_LABELS.get(role, role.replace("_", " ").title())


def resolve_active_role(user, session, personnel=None) -> str | None:
    """
    Pick the active role for this request:
    1. Valid in-session role (from role switcher)
    2. Saved profile default dashboard
    3. Automatic priority (admin, then instructor, …)
    """
    available = set(available_roles_for_user(user))
    if not available:
        return None

    session_role = session.get("active_role")
    if session_role in available:
        return session_role

    if personnel is None:
        personnel = getattr(user, "personnel", None)
    preferred = (getattr(personnel, "default_dashboard_role", None) or "").strip()
    if preferred in available:
        return preferred

    return automatic_role(user)


def dashboard_url_name_for_role(role: str | None) -> str | None:
    if not role:
        return None
    return ROLE_DASHBOARDS.get(role)


def set_active_role(session, role: str) -> None:
    session["active_role"] = role


ADMIN_BOOKING_URL_NAMES = frozenset({
    "admin_booking_list",
    "admin_matrix_import",
    "admin_matrix_import_confirm",
    "admin_booking_new",
    "admin_booking_edit",
    "admin_booking_delete",
    "admin_booking_cancel",
    "admin_booking_reinstate",
    "admin_booking_telegram_send",
    "admin_booking_email_send",
    "admin_booking_course_pack_pdf",
    "admin_booking_unlock",
    "admin_booking_certificates_selected",
    "admin_certificate_name_edit",
    "admin_booking_day_registers",
    "admin_register_delete",
})


def sync_active_role_from_path(request) -> None:
    """Keep the sidebar menu group aligned with the section being viewed."""
    user = getattr(request, "user", None)
    session = getattr(request, "session", None)
    if not user or not getattr(user, "is_authenticated", False) or session is None:
        return

    path = request.path or ""
    roles = get_user_roles(user)

    if path.startswith("/app/admin/") and (user.is_superuser or "admin" in roles):
        set_active_role(session, "admin")
        return

    if path.startswith("/delegates/") and (user.is_superuser or "admin" in roles):
        set_active_role(session, "admin")
        return

    if path.startswith("/app/instructor/") and "instructor" in roles:
        set_active_role(session, "instructor")
        return

    if (
        (path == "/app/inbox/" or path.startswith("/app/inbox/"))
        and "instructor" in roles
    ):
        set_active_role(session, "instructor")
        return

    if path.startswith("/app/engineer/") and "engineer" in roles:
        set_active_role(session, "engineer")
        return

    if path.startswith("/app/inspector/") and "inspector" in roles:
        set_active_role(session, "inspector")
        return
