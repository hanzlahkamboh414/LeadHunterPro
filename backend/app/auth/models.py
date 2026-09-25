"""User model + SQLite persistence for LeadHunter Pro auth.

Separate database (users.db) — never mixed with lead_research.db.
Admin seed: username=admin4269, password=223344, is_admin=True (first boot only).
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import bcrypt

from app.core.db_paths import operational_db_path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class User:
    id: str
    username: str
    email: str
    password_hash: str
    is_admin: bool
    created_at: str
    name: str = ""
    #: Product verticals this account signed up for: "emails" | "phones" |
    #: "both". Gates which vertical APIs the account may call (P3). Admins
    #: are always allowed every vertical.
    category: str = "both"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "is_admin": self.is_admin,
            "created_at": self.created_at,
            "name": self.name,
            "category": self.category,
        }


def _display_name(username: str) -> str:
    """A friendly display name from a username — first English letters
    capitalised ("skye schooly" -> "Skye Schooly", "test1" -> "Test1")."""
    return (username or "").strip().title()


class UserStore:
    """SQLite persistence for users."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = operational_db_path(os.path.join(
                os.path.dirname(__file__), "..", "..", "output", "users.db"
            ))
        self._db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT ''
            )
        """)
        # Additive migration for DBs created before `name` existed (guarded —
        # the column append is a no-op once present).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "name" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        # P3 signup category (emails/phones/both). Existing accounts keep
        # access to everything they already had -> default 'both'.
        if "category" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN category TEXT NOT NULL DEFAULT 'both'"
            )
        # Backfill: existing NON-admin accounts with no display name get one
        # derived from their username (first letters capitalised). The admin
        # account keeps whatever it has — the UI falls back to the username.
        # Title-casing is per-row in Python (SQLite has no TITLE()).
        rows = conn.execute(
            "SELECT id, username FROM users "
            "WHERE is_admin = 0 AND (name IS NULL OR name = '')"
        ).fetchall()
        for uid, uname in rows:
            conn.execute(
                "UPDATE users SET name = ? WHERE id = ?", (_display_name(uname), uid)
            )
        conn.commit()
        conn.close()

    def _row_to_user(self, row: tuple) -> User:
        return User(
            id=row[0],
            username=row[1],
            email=row[2],
            password_hash=row[3],
            is_admin=bool(row[4]),
            created_at=row[5],
            name=(row[6] if len(row) > 6 else "") or "",
            category=((row[7] if len(row) > 7 else "") or "both"),
        )

    def create(self, username: str, email: str, password: str,
               name: str = "", category: str = "both",
               tenant_id: str | None = None) -> User:
        """Create a new user. Raises ValueError on duplicate username/email.

        ``name`` is the display name (shown in the topbar). Empty -> derived
        from the username (first letters capitalised).

        ``category`` is the signup vertical answer: "emails" | "phones" |
        "both" (default) — anything else falls back to "both" so a bad
        client payload can never lock an account out of everything.

        When ``tenant_id`` is supplied, the account and its first membership
        commit together; an invalid tenant leaves no orphaned account.
        """
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        if category not in ("emails", "phones", "both"):
            category = "both"
        user = User(
            id=uuid.uuid4().hex[:12],
            username=username.strip(),
            email=email.strip().lower(),
            password_hash=pw_hash,
            is_admin=False,
            created_at=_now(),
            name=(name or "").strip() or _display_name(username),
            category=category,
        )
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO users (id, username, email, password_hash, is_admin, created_at, name, category) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user.id, user.username, user.email, user.password_hash,
                 int(user.is_admin), user.created_at, user.name, user.category),
            )
            if tenant_id is not None:
                if conn.execute(
                    "SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)
                ).fetchone() is None:
                    raise ValueError("tenant does not exist")
                conn.execute(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                    "VALUES (?, ?, 'member')",
                    (tenant_id, user.id),
                )
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            if "username" in str(e):
                raise ValueError("Username already taken") from e
            if "email" in str(e):
                raise ValueError("Email already registered") from e
            raise ValueError("User already exists") from e
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return user

    def get_by_username(self, username: str) -> User | None:
        conn = sqlite3.connect(self._db_path)
        cur = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username.strip(),)
        )
        row = cur.fetchone()
        conn.close()
        return self._row_to_user(row) if row else None

    def get_by_id(self, user_id: str) -> User | None:
        conn = sqlite3.connect(self._db_path)
        cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return self._row_to_user(row) if row else None

    def verify_password(self, username: str, password: str) -> User | None:
        """Return User if credentials valid, None otherwise."""
        user = self.get_by_username(username)
        if user is None:
            return None
        if bcrypt.checkpw(password.encode(), user.password_hash.encode()):
            return user
        return None

    def reset_password(self, username: str, new_password: str) -> bool:
        """Reset a user's password by username. Returns True if found + updated."""
        user = self.get_by_username(username)
        if user is None:
            return False
        pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (pw_hash, user.id),
        )
        conn.commit()
        conn.close()
        return True

    def list_all(self) -> list[User]:
        """Every user, oldest first — the admin user-management view."""
        conn = sqlite3.connect(self._db_path)
        cur = conn.execute("SELECT * FROM users ORDER BY created_at ASC, username ASC")
        rows = cur.fetchall()
        conn.close()
        return [self._row_to_user(row) for row in rows]

    def create_tenant(self, name: str, owner_user_id: str | None = None) -> str:
        """Create an organization and, when supplied, its first owner atomically."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("tenant name is required")
        tenant_id = uuid.uuid4().hex
        with sqlite3.connect(self._db_path) as conn:
            if owner_user_id is not None and conn.execute(
                "SELECT 1 FROM users WHERE id = ? AND is_admin = 1",
                (owner_user_id,),
            ).fetchone() is None:
                raise ValueError("platform admin does not exist")
            conn.execute(
                "INSERT INTO tenants (id, name) VALUES (?, ?)",
                (tenant_id, clean_name),
            )
            if owner_user_id is not None:
                conn.execute(
                    "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                    "VALUES (?, ?, 'owner')",
                    (tenant_id, owner_user_id),
                )
        return tenant_id

    def list_tenants(self) -> list[dict[str, str | int]]:
        """Platform-admin registry view with live membership counts."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT t.id, t.name, COUNT(m.user_id) FROM tenants t "
                "LEFT JOIN tenant_memberships m ON m.tenant_id = t.id "
                "GROUP BY t.id, t.name ORDER BY t.name, t.id"
            ).fetchall()
        return [
            {"id": row[0], "name": row[1], "member_count": row[2]}
            for row in rows
        ]

    def list_tenant_members(self, tenant_id: str) -> list[dict[str, str]] | None:
        """Return None for an unknown tenant, including an empty known one."""
        with sqlite3.connect(self._db_path) as conn:
            if conn.execute(
                "SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone() is None:
                return None
            rows = conn.execute(
                "SELECT u.id, u.username, m.role FROM tenant_memberships m "
                "JOIN users u ON u.id = m.user_id WHERE m.tenant_id = ? "
                "ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 "
                "ELSE 2 END, u.username, u.id",
                (tenant_id,),
            ).fetchall()
        return [
            {"user_id": row[0], "username": row[1], "role": row[2]}
            for row in rows
        ]

    def tenant_role(self, user_id: str, tenant_id: str) -> str | None:
        """Read current membership on every authorization check, not from JWT."""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT role FROM tenant_memberships "
                "WHERE tenant_id = ? AND user_id = ?",
                (tenant_id, user_id),
            ).fetchone()
        return row[0] if row else None

    def list_user_tenants(self, user_id: str) -> list[dict[str, str]]:
        """List only this user's current workspaces for the tenant selector."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT t.id, t.name, m.role FROM tenant_memberships m "
                "JOIN tenants t ON t.id = m.tenant_id "
                "WHERE m.user_id = ? ORDER BY t.name, t.id",
                (user_id,),
            ).fetchall()
        return [{"id": row[0], "name": row[1], "role": row[2]} for row in rows]

    def grant_membership(self, tenant_id: str, user_id: str, role: str) -> None:
        """Assign a user to one tenant without changing any other membership."""
        if role not in ("owner", "admin", "member"):
            raise ValueError("invalid tenant role")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone() is None:
                raise ValueError("tenant does not exist")
            if conn.execute(
                "SELECT 1 FROM users WHERE id = ?", (user_id,)
            ).fetchone() is None:
                raise ValueError("user does not exist")
            current = conn.execute(
                "SELECT role FROM tenant_memberships "
                "WHERE tenant_id = ? AND user_id = ?",
                (tenant_id, user_id),
            ).fetchone()
            if current == ("owner",) and role != "owner" and conn.execute(
                "SELECT COUNT(*) FROM tenant_memberships "
                "WHERE tenant_id = ? AND role = 'owner'",
                (tenant_id,),
            ).fetchone()[0] <= 1:
                raise ValueError("cannot remove the last tenant owner")
            conn.execute(
                "INSERT INTO tenant_memberships (tenant_id, user_id, role) "
                "VALUES (?, ?, ?) ON CONFLICT(tenant_id, user_id) "
                "DO UPDATE SET role = excluded.role",
                (tenant_id, user_id, role),
            )

    def revoke_membership(self, tenant_id: str, user_id: str) -> bool:
        """Remove access immediately; existing JWTs confer no membership."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            role = conn.execute(
                "SELECT role FROM tenant_memberships "
                "WHERE tenant_id = ? AND user_id = ?",
                (tenant_id, user_id),
            ).fetchone()
            if role is None:
                return False
            if role[0] == "owner" and conn.execute(
                "SELECT COUNT(*) FROM tenant_memberships "
                "WHERE tenant_id = ? AND role = 'owner'",
                (tenant_id,),
            ).fetchone()[0] <= 1:
                raise ValueError("cannot remove the last tenant owner")
            result = conn.execute(
                "DELETE FROM tenant_memberships "
                "WHERE tenant_id = ? AND user_id = ?",
                (tenant_id, user_id),
            )
        return result.rowcount > 0

    def delete(self, user_id: str) -> bool:
        """Delete a user account (row only — dossiers/jobs keep their user_id
        and stay visible to the admin panel; the user's JWT dies on the next
        request because get_by_id no longer resolves)."""
        conn = sqlite3.connect(self._db_path)
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        removed = cur.rowcount > 0
        conn.commit()
        conn.close()
        return removed

    def ensure_admin(self, username: str = "admin4269",
                     password: str = "223344",
                     email: str = "admin@leadhunter.local") -> User:
        """Seed admin user on first boot. Idempotent."""
        existing = self.get_by_username(username)
        if existing:
            return existing
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        user = User(
            id=uuid.uuid4().hex[:12],
            username=username,
            email=email,
            password_hash=pw_hash,
            is_admin=True,
            created_at=_now(),
        )
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT OR IGNORE INTO users (id, username, email, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, user.username, user.email, user.password_hash,
             int(user.is_admin), user.created_at),
        )
        conn.commit()
        conn.close()
        return self.get_by_username(username) or user

    def require_secure_platform_admin(self) -> None:
        """Fail tenant-mode startup if bootstrap identity is missing or unsafe."""
        admins = [user for user in self.list_all() if user.is_admin]
        if not admins:
            raise RuntimeError("multi-tenant mode requires a platform admin")
        for admin in admins:
            try:
                uses_factory_password = bcrypt.checkpw(
                    b"223344", admin.password_hash.encode()
                )
            except ValueError as exc:
                raise RuntimeError("platform admin password hash is invalid") from exc
            if uses_factory_password:
                raise RuntimeError("platform admin factory password must be rotated")
        try:
            first_tenant_owners = any(
                self.tenant_role(admin.id, "the-best-estimators-llc") == "owner"
                for admin in admins
            )
        except sqlite3.Error as exc:
            raise RuntimeError("tenant registry is unavailable") from exc
        if not first_tenant_owners:
            raise RuntimeError("first tenant has no platform-admin owner")

    def ensure_shared(self) -> User:
        """The account token-less visitors run as when login auth is OFF.

        Idempotent. NEVER admin — anonymous access must never carry admin
        powers. The password is a random secret nobody is told, so "shared"
        cannot be logged into directly; it exists only as the identity the
        API assigns to unauthenticated requests while the site is open.
        """
        existing = self.get_by_username("shared")
        if existing:
            return existing
        return self.create(
            username="shared",
            email="shared@leadhunter.local",
            password=uuid.uuid4().hex + uuid.uuid4().hex,
            name="Shared Workspace",
        )
