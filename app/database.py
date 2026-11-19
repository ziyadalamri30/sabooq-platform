import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB_PATH = Path(os.environ.get("SABOOQ_DB_PATH", str(BASE_DIR / "sabooq.db")))
SEED_FILE = Path(__file__).resolve().parent / "seed_data.json"


def db_engine_name() -> str:
    return "postgresql" if IS_POSTGRES else "sqlite"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_secret(plain: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${dk.hex()}"


def verify_secret(plain: str, stored: str) -> bool:
    try:
        _, salt, expected_hex = stored.split("$", 2)
        candidate = hash_secret(plain, salt).split("$", 2)[2]
        return hmac.compare_digest(candidate, expected_hex)
    except Exception:
        return False


def _translate_sql_for_pg(sql: str) -> Tuple[str, bool]:
    """Translates SQLite-style SQL queries to PostgreSQL cleanly."""
    s = sql.strip()
    # Replace ? placeholders with %s
    s = s.replace("?", "%s")
    # SQLite MAX(0, expr) -> PostgreSQL GREATEST(0, expr)
    s = re.sub(r"\bMAX\s*\(\s*0\s*,", "GREATEST(0,", s, flags=re.I)
    # Detect INSERT statements that need RETURNING id for lastrowid
    is_insert = bool(re.match(r"^INSERT\s+INTO\s+", s, flags=re.I))
    has_returning = "returning " in s.lower()
    is_sessions = bool(re.match(r"^INSERT\s+INTO\s+sessions\b", s, flags=re.I))
    add_returning = is_insert and not has_returning and not is_sessions
    if add_returning:
        s = s.rstrip(";") + " RETURNING id"
    return s, add_returning


class PgCursorWrapper:
    def __init__(self, cur, lastrowid: Optional[int] = None) -> None:
        self._cur = cur
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


_PG_POOL = None


def _get_pg_pool():
    global _PG_POOL
    if _PG_POOL is None:
        import psycopg2.pool  # type: ignore

        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if "sslmode=" not in url and "localhost" not in url and "127.0.0.1" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}sslmode=require"
        _PG_POOL = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=url)
    return _PG_POOL


class PgConnectionWrapper:
    """Presents the same .execute() / .executescript() API as sqlite3.Connection for pooled PostgreSQL."""

    def __init__(self, raw_conn, pool=None) -> None:
        self._conn = raw_conn
        self._pool = pool

    def execute(self, sql: str, params: Any = ()) -> PgCursorWrapper:
        import psycopg2.extras  # type: ignore

        pg_sql, wants_id = _translate_sql_for_pg(sql)
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(pg_sql, tuple(params) if params else None)
        lastrowid = None
        if wants_id:
            row = cur.fetchone()
            if row and "id" in row:
                lastrowid = row["id"]
        return PgCursorWrapper(cur, lastrowid=lastrowid)

    def executescript(self, script: str) -> None:
        cur = self._conn.cursor()
        cur.execute(script)
        cur.close()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.putconn(self._conn)
                return
            except Exception:
                pass
        self._conn.close()


def get_connection():
    if IS_POSTGRES:
        pool = _get_pg_pool()
        raw = pool.getconn()
        # Verify pooled connection is alive; reconnect if dropped by Supabase idle timeout
        if raw.closed:
            pool.putconn(raw, close=True)
            raw = pool.getconn()
        return PgConnectionWrapper(raw, pool=pool)

    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


@contextmanager
def db_session():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SQLITE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS brands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name_ar TEXT NOT NULL,
    name_en TEXT NOT NULL DEFAULT '',
    category_ar TEXT NOT NULL DEFAULT '',
    category_en TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    logo_url TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    theme_bg TEXT NOT NULL DEFAULT '#04261d',
    theme_bg2 TEXT NOT NULL DEFAULT '#06342a',
    theme_card TEXT NOT NULL DEFAULT '#07382c',
    theme_tx TEXT NOT NULL DEFAULT '#f4efdc',
    theme_mut TEXT NOT NULL DEFAULT '#a8c9bd',
    theme_ac TEXT NOT NULL DEFAULT '#4de3b6',
    theme_on TEXT NOT NULL DEFAULT '#03261c',
    rating REAL NOT NULL DEFAULT 4.8,
    reviews_count INTEGER NOT NULL DEFAULT 0,
    commission_rate REAL NOT NULL DEFAULT 5.0,
    loyalty_points_per_sar REAL NOT NULL DEFAULT 1.0,
    vat_rate REAL NOT NULL DEFAULT 15.0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    lat REAL NOT NULL DEFAULT 24.4672,
    lng REAL NOT NULL DEFAULT 39.6111,
    busy_level TEXT NOT NULL DEFAULT 'calm',
    prep_minutes INTEGER NOT NULL DEFAULT 10,
    delivery_fee REAL NOT NULL DEFAULT 12.0,
    is_open INTEGER NOT NULL DEFAULT 1,
    accepts_delivery INTEGER NOT NULL DEFAULT 1,
    accepts_pickup INTEGER NOT NULL DEFAULT 1,
    accepts_drivethru INTEGER NOT NULL DEFAULT 1,
    accepts_dinein INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staff_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    pin_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('super_admin','brand_admin','branch_manager','cashier','kitchen','driver')),
    brand_id INTEGER REFERENCES brands(id) ON DELETE CASCADE,
    branch_id INTEGER REFERENCES branches(id) ON DELETE CASCADE,
    phone TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_type TEXT NOT NULL CHECK(user_type IN ('staff','customer')),
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    brand_id INTEGER,
    branch_id INTEGER,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name_ar TEXT NOT NULL,
    name_en TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(brand_id, code)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    name_ar TEXT NOT NULL,
    name_en TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL DEFAULT 0,
    sizes_json TEXT NOT NULL DEFAULT '[]',
    image_url TEXT NOT NULL DEFAULT '',
    calories INTEGER NOT NULL DEFAULT 0,
    discount_percent INTEGER NOT NULL DEFAULT 0,
    is_offer INTEGER NOT NULL DEFAULT 0,
    is_available INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL DEFAULT 'عميل سبوق',
    email TEXT NOT NULL DEFAULT '',
    otp_code TEXT NOT NULL DEFAULT '',
    otp_expires_at TEXT NOT NULL DEFAULT '',
    total_points INTEGER NOT NULL DEFAULT 0,
    total_visits INTEGER NOT NULL DEFAULT 0,
    wallet_balance REAL NOT NULL DEFAULT 0.0,
    tier TEXT NOT NULL DEFAULT 'برونزي',
    created_at TEXT NOT NULL,
    last_visit_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS customer_brand_loyalty (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    points INTEGER NOT NULL DEFAULT 0,
    visits INTEGER NOT NULL DEFAULT 0,
    last_visit_at TEXT NOT NULL DEFAULT '',
    UNIQUE(customer_id, brand_id)
);

CREATE TABLE IF NOT EXISTS loyalty_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL,
    order_id INTEGER,
    tx_type TEXT NOT NULL CHECK(tx_type IN ('earn','redeem','bonus','checkin')),
    points INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    verified_via TEXT NOT NULL DEFAULT 'order',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qr_nfc_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    tag_type TEXT NOT NULL DEFAULT 'both' CHECK(tag_type IN ('qr','nfc','both')),
    purpose TEXT NOT NULL DEFAULT 'table_order' CHECK(purpose IN ('table_order','branch_checkin','loyalty_redeem','drivethru')),
    label TEXT NOT NULL,
    table_number TEXT NOT NULL DEFAULT '',
    scan_count INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT UNIQUE NOT NULL,
    brand_id INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    branch_id INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    customer_name TEXT NOT NULL DEFAULT 'عميل زائر',
    customer_phone TEXT NOT NULL DEFAULT '',
    order_mode TEXT NOT NULL DEFAULT 'pickup' CHECK(order_mode IN ('table','drivethru','pickup','delivery')),
    table_number TEXT NOT NULL DEFAULT '',
    car_info TEXT NOT NULL DEFAULT '',
    delivery_address TEXT NOT NULL DEFAULT '',
    delivery_lat REAL,
    delivery_lng REAL,
    qr_nfc_code TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','accepted','preparing','ready','out_for_delivery','completed','cancelled')),
    kitchen_status TEXT NOT NULL DEFAULT 'pending' CHECK(kitchen_status IN ('pending','preparing','ready','served')),
    payment_method TEXT NOT NULL DEFAULT 'mada' CHECK(payment_method IN ('apple_pay','mada','card','wallet','cash')),
    payment_status TEXT NOT NULL DEFAULT 'pending' CHECK(payment_status IN ('pending','paid','failed','refunded')),
    payment_ref TEXT NOT NULL DEFAULT '',
    subtotal REAL NOT NULL DEFAULT 0,
    discount_amount REAL NOT NULL DEFAULT 0,
    delivery_fee REAL NOT NULL DEFAULT 0,
    vat_amount REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,
    points_earned INTEGER NOT NULL DEFAULT 0,
    points_redeemed INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    rating INTEGER,
    review_comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_name TEXT NOT NULL,
    size_label TEXT NOT NULL DEFAULT '',
    unit_price REAL NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    line_total REAL NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS payment_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'sandbox',
    method TEXT NOT NULL DEFAULT 'mada',
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'SAR',
    status TEXT NOT NULL DEFAULT 'initiated',
    gateway_tx_id TEXT NOT NULL DEFAULT '',
    webhook_payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT 'استفسار عام',
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_type TEXT NOT NULL,
    actor_id INTEGER,
    actor_role TEXT NOT NULL DEFAULT '',
    brand_id INTEGER,
    branch_id INTEGER,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_branches_brand ON branches(brand_id);
CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand_id);
CREATE INDEX IF NOT EXISTS idx_orders_brand_branch ON orders(brand_id, branch_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_staff_brand ON staff_users(brand_id, branch_id);
CREATE INDEX IF NOT EXISTS idx_qr_code ON qr_nfc_tags(code);
"""

POSTGRES_SCHEMA_SQL = SQLITE_SCHEMA_SQL.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY").replace("REAL", "DOUBLE PRECISION")


def compute_tier(points: int) -> str:
    if points >= 1000:
        return "بلاتيني"
    if points >= 500:
        return "ذهبي"
    if points >= 200:
        return "فضي"
    return "برونزي"


def init_db() -> None:
    with db_session() as conn:
        conn.executescript(POSTGRES_SCHEMA_SQL if IS_POSTGRES else SQLITE_SCHEMA_SQL)
        existing = conn.execute("SELECT COUNT(*) AS c FROM brands").fetchone()["c"]
        if existing == 0:
            _seed_initial_data(conn)


def _seed_initial_data(conn) -> None:
    now = utc_now_iso()

    # 1. Super Admin (matches original Sabooq login 401 / 0000)
    conn.execute(
        """INSERT INTO staff_users (username, full_name, pin_hash, role, brand_id, branch_id, phone, is_active, created_at)
           VALUES (?, ?, ?, 'super_admin', NULL, NULL, '0500000401', 1, ?)""",
        ("401", "مدير منصة سبوق", hash_secret("0000"), now),
    )

    if not SEED_FILE.exists():
        return

    with open(SEED_FILE, "r", encoding="utf-8") as f:
        seed_brands: List[Dict[str, Any]] = json.load(f)

    for b in seed_brands:
        theme = b.get("theme", {})
        cur = conn.execute(
            """INSERT INTO brands (
                slug, name_ar, name_en, category_ar, category_en, description,
                logo_url, cover_url, theme_bg, theme_bg2, theme_card, theme_tx, theme_mut, theme_ac, theme_on,
                rating, reviews_count, commission_rate, loyalty_points_per_sar, vat_rate, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 5.0, 1.0, 15.0, 1, ?)""",
            (
                b["slug"],
                b["name_ar"],
                b.get("name_en", ""),
                b.get("category_ar", ""),
                b.get("category_en", ""),
                f"{b['name_ar']} — {b.get('category_ar', '')}",
                b.get("logo_url", ""),
                b.get("cover_url", ""),
                theme.get("bg", "#04261d"),
                theme.get("bg2", "#06342a"),
                theme.get("card", "#07382c"),
                theme.get("tx", "#f4efdc"),
                theme.get("mut", "#a8c9bd"),
                theme.get("ac", "#4de3b6"),
                theme.get("on", "#03261c"),
                b.get("rating", 4.7),
                b.get("reviews_count", 100),
                now,
            ),
        )
        brand_id = cur.lastrowid

        first_branch_id = None
        for idx, br in enumerate(b.get("branches", [])):
            bcur = conn.execute(
                """INSERT INTO branches (
                    brand_id, name, address, phone, lat, lng, busy_level, prep_minutes,
                    delivery_fee, is_open, accepts_delivery, accepts_pickup, accepts_drivethru, accepts_dinein, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'calm', 8, 12.0, 1, 1, 1, 1, 1, ?)""",
                (
                    brand_id,
                    br["name"],
                    br.get("address", ""),
                    br.get("phone", "0500000000"),
                    br.get("lat", 24.4672),
                    br.get("lng", 39.6111),
                    now,
                ),
            )
            branch_id = bcur.lastrowid
            if first_branch_id is None:
                first_branch_id = branch_id

            slug_up = b["slug"].upper()
            tags = [
                (f"SBQ-{slug_up}-B{idx+1}-T1", "both", "table_order", f"طاولة 1 — {br['name']}", "1"),
                (f"SBQ-{slug_up}-B{idx+1}-NFC", "nfc", "branch_checkin", f"نقطة ولاء NFC — {br['name']}", ""),
                (f"SBQ-{slug_up}-B{idx+1}-DT", "qr", "drivethru", f"مسار درايف ثرو — {br['name']}", ""),
            ]
            for code, ttype, purpose, label, tnum in tags:
                conn.execute(
                    """INSERT INTO qr_nfc_tags (code, brand_id, branch_id, tag_type, purpose, label, table_number, scan_count, is_active, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?)""",
                    (code, brand_id, branch_id, ttype, purpose, label, tnum, now),
                )

        slug = b["slug"]
        staff_seed = [
            (f"{slug}_admin", f"مدير براند {b['name_ar']}", "1111", "brand_admin", brand_id, None),
            (f"{slug}_mgr", f"مدير فرع {b['name_ar']}", "2222", "branch_manager", brand_id, first_branch_id),
            (f"{slug}_cashier", f"كاشير {b['name_ar']}", "3333", "cashier", brand_id, first_branch_id),
            (f"{slug}_kitchen", f"مطبخ {b['name_ar']}", "4444", "kitchen", brand_id, first_branch_id),
        ]
        for uname, fname, pin, role, bid, brid in staff_seed:
            conn.execute(
                """INSERT INTO staff_users (username, full_name, pin_hash, role, brand_id, branch_id, phone, is_active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, '', 1, ?)""",
                (uname, fname, hash_secret(pin), role, bid, brid, now),
            )

        cat_map: Dict[str, int] = {}
        for c in b.get("categories", []):
            ccur = conn.execute(
                """INSERT INTO categories (brand_id, code, name_ar, name_en, sort_order, is_active)
                   VALUES (?, ?, ?, '', ?, 1)""",
                (brand_id, c["code"], c["name_ar"], c.get("sort_order", 0)),
            )
            cat_map[c["code"]] = ccur.lastrowid

        if any(p.get("cat_code") == "o" for p in b.get("products", [])) and "o" not in cat_map:
            ccur = conn.execute(
                """INSERT INTO categories (brand_id, code, name_ar, name_en, sort_order, is_active)
                   VALUES (?, 'o', 'العروض والبوكسات', 'Offers', -1, 1)""",
                (brand_id,),
            )
            cat_map["o"] = ccur.lastrowid

        for p in b.get("products", []):
            cat_id = cat_map.get(p.get("cat_code", ""))
            conn.execute(
                """INSERT INTO products (
                    brand_id, category_id, name_ar, name_en, description, price, sizes_json,
                    image_url, calories, discount_percent, is_offer, is_available, sort_order, created_at
                ) VALUES (?, ?, ?, '', '', ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    brand_id,
                    cat_id,
                    p["name_ar"],
                    p["price"],
                    json.dumps(p.get("sizes", []), ensure_ascii=False),
                    p.get("image_url", ""),
                    p.get("calories", 0),
                    p.get("discount_percent", 0),
                    p.get("is_offer", 0),
                    p.get("sort_order", 0),
                    now,
                ),
            )

    conn.execute(
        """INSERT INTO customers (phone, name, email, total_points, total_visits, wallet_balance, tier, created_at, last_visit_at)
           VALUES ('0551234567', 'زياد العميل التجريبي', 'customer@sabooq.sa', 120, 4, 150.0, 'برونزي', ?, ?)""",
        (now, now),
    )


def create_session(
    conn,
    user_type: str,
    user_id: int,
    role: str,
    brand_id: Optional[int] = None,
    branch_id: Optional[int] = None,
    hours: int = 72,
) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=hours)
    conn.execute(
        """INSERT INTO sessions (token, user_type, user_id, role, brand_id, branch_id, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (token, user_type, user_id, role, brand_id, branch_id, exp.isoformat(), now.isoformat()),
    )
    return token


def log_audit(
    conn,
    actor_type: str,
    actor_id: Optional[int],
    actor_role: str,
    action: str,
    details: Dict[str, Any],
    brand_id: Optional[int] = None,
    branch_id: Optional[int] = None,
) -> None:
    conn.execute(
        """INSERT INTO audit_logs (actor_type, actor_id, actor_role, brand_id, branch_id, action, details_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (actor_type, actor_id, actor_role, brand_id, branch_id, action, json.dumps(details, ensure_ascii=False), utc_now_iso()),
    )
