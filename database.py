"""
Cluster storage using SQLite.
Passwords are encrypted with Fernet (AES-128-CBC + HMAC-SHA256). The Fernet key
is derived from a master password using PBKDF2-HMAC-SHA256 and is never stored on
disk. A random salt (salt.bin) and an encrypted verifier (verifier.bin) are kept
alongside the database so the password can be verified and the key reproduced on
each startup.
"""

import base64
import hashlib
import os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DB_PATH      = Path(__file__).parent / "clusters.db"
SALT_PATH    = Path(__file__).parent / "salt.bin"
VERIFIER_PATH = Path(__file__).parent / "verifier.bin"

_SENTINEL          = b"cucm-tools-ok"
_PBKDF2_ITERATIONS = 600_000

_fernet: Fernet | None = None


# ---------------------------------------------------------------------------
# Key derivation and lifecycle
# ---------------------------------------------------------------------------

def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 32-byte URL-safe base64-encoded Fernet key via PBKDF2-HMAC-SHA256."""
    raw = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=32
    )
    return base64.urlsafe_b64encode(raw)


def is_first_run() -> bool:
    """True when no salt file exists — the master password has never been set."""
    return not SALT_PATH.exists()


def setup_password(password: str) -> None:
    """
    Generate a new salt, derive a Fernet key, write the verifier.
    Sets the module-level Fernet instance for this session.
    """
    global _fernet
    salt = os.urandom(32)
    key  = _derive_key(password, salt)
    f    = Fernet(key)
    SALT_PATH.write_bytes(salt)
    VERIFIER_PATH.write_bytes(f.encrypt(_SENTINEL))
    for path in (SALT_PATH, VERIFIER_PATH):
        try:
            os.chmod(path, 0o600)
        except (AttributeError, NotImplementedError):
            pass  # Windows does not support POSIX permissions
    _fernet = f


def unlock(password: str) -> bool:
    """
    Attempt to unlock using the given password.
    Returns True and sets the module-level Fernet instance on success.
    """
    global _fernet
    if not SALT_PATH.exists() or not VERIFIER_PATH.exists():
        return False
    salt     = SALT_PATH.read_bytes()
    verifier = VERIFIER_PATH.read_bytes()
    key      = _derive_key(password, salt)
    f        = Fernet(key)
    try:
        f.decrypt(verifier)
    except InvalidToken:
        return False
    _fernet = f
    return True


def reset_and_setup(password: str) -> None:
    """Wipe all cluster data and set up a new master password."""
    with get_conn() as conn:
        conn.execute("DELETE FROM clusters")
        conn.commit()
    setup_password(password)


def _get_fernet() -> Fernet:
    if _fernet is None:
        raise RuntimeError("Database is locked. Start the app via launch.py.")
    return _fernet


# ---------------------------------------------------------------------------
# Encrypt / decrypt helpers
# ---------------------------------------------------------------------------

def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _get_fernet().decrypt(ciphertext.encode()).decode()


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clusters (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL UNIQUE,
                host        TEXT    NOT NULL,
                port        INTEGER NOT NULL DEFAULT 8443,
                username    TEXT    NOT NULL,
                password    TEXT    NOT NULL,  -- Fernet-encrypted
                verify_ssl  INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def list_clusters() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, name, host, port, username, verify_ssl FROM clusters ORDER BY name"
        ).fetchall()


def get_cluster(cluster_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, name, host, port, username, password, verify_ssl FROM clusters WHERE id = ?",
            (cluster_id,),
        ).fetchone()


def create_cluster(name: str, host: str, port: int, username: str,
                   password: str, verify_ssl: bool) -> int:
    enc = encrypt(password)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO clusters (name, host, port, username, password, verify_ssl) VALUES (?, ?, ?, ?, ?, ?)",
            (name, host, port, username, enc, int(verify_ssl)),
        )
        conn.commit()
        return cur.lastrowid


def update_cluster(cluster_id: int, fields: dict):
    if "password" in fields:
        fields["password"] = encrypt(fields["password"])
    if "verify_ssl" in fields:
        fields["verify_ssl"] = int(fields["verify_ssl"])
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [cluster_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE clusters SET {set_clause} WHERE id = ?", values)
        conn.commit()


def delete_cluster(cluster_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM clusters WHERE id = ?", (cluster_id,))
        conn.commit()


def get_cluster_credentials(cluster_id: int) -> dict | None:
    """Return host, port, username, plaintext password, and verify_ssl for AXL calls."""
    row = get_cluster(cluster_id)
    if not row:
        return None
    return {
        "host":       row["host"],
        "port":       row["port"],
        "username":   row["username"],
        "password":   decrypt(row["password"]),
        "verify_ssl": bool(row["verify_ssl"]),
    }
