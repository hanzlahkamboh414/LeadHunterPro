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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "is_admin": self.is_admin,
            "created_at": self.created_at,
        }


class UserStore:
    """SQLite persistence for users."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "output", "users.db"
            )
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
                created_at TEXT NOT NULL
            )
        """)
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
        )

    def create(self, username: str, email: str, password: str) -> User:
        """Create a new user. Raises ValueError on duplicate username/email."""
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        user = User(
            id=uuid.uuid4().hex[:12],
            username=username.strip(),
            email=email.strip().lower(),
            password_hash=pw_hash,
            is_admin=False,
            created_at=_now(),
        )
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO users (id, username, email, password_hash, is_admin, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user.id, user.username, user.email, user.password_hash,
                 int(user.is_admin), user.created_at),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            if "username" in str(e):
                raise ValueError("Username already taken") from e
            if "email" in str(e):
                raise ValueError("Email already registered") from e
            raise ValueError("User already exists") from e
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
