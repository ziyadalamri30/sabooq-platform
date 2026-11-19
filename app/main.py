import asyncio
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.database import (
    BASE_DIR,
    compute_tier,
    create_session,
    db_engine_name,
    db_session,
    hash_secret,
    init_db,
    log_audit,
    utc_now_iso,
    verify_secret,
)
from app.payments import initiate_payment, payment_gateway_status, verify_webhook_signature
from app.realtime import broker

app = FastAPI(title="Sabooq (سبوق) Platform API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = BASE_DIR / "static"

import time as _time

_PUBLIC_CACHE: Dict[str, Any] = {}
_CACHE_TTL = 30.0


def _cache_get(key: str):
    item = _PUBLIC_CACHE.get(key)
    if item and (_time.monotonic() - item[0]) < _CACHE_TTL:
        return item[1]
    return None


def _cache_set(key: str, val: Any):
    _PUBLIC_CACHE[key] = (_time.monotonic(), val)


def _cache_clear():
    _PUBLIC_CACHE.clear()


@app.middleware("http")
async def add_cache_headers(request: Request, call_next):
    response = await call_next(request)
    p = request.url.path
    if p.startswith("/static/assets/") or p.endswith(".png"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    elif p.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=300"
    return response


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# Auth & RBAC Dependencies
# ---------------------------------------------------------------------------

def _extract_token(authorization: Optional[str], query_token: Optional[str] = None) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    if query_token:
        return query_token.strip()
    return None


def get_optional_session(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> Optional[Dict[str, Any]]:
    tok = _extract_token(authorization, token)
    if not tok:
        return None
    with db_session() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE token = ?", (tok,)).fetchone()
        if not row:
            return None
        if row["expires_at"] < utc_now_iso():
            conn.execute("DELETE FROM sessions WHERE token = ?", (tok,))
            return None
        return dict(row)


def require_staff(sess: Optional[Dict[str, Any]] = Depends(get_optional_session)) -> Dict[str, Any]:
    if not sess or sess["user_type"] != "staff":
        raise HTTPException(status_code=401, detail="يلزم تسجيل الدخول بحساب فريق العمل")
    return sess


def require_super_admin(sess: Dict[str, Any] = Depends(require_staff)) -> Dict[str, Any]:
    if sess["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="هذه الصلاحية خاصة بإدارة منصة سبوق الرئيسية فقط")
    return sess


def require_customer(sess: Optional[Dict[str, Any]] = Depends(get_optional_session)) -> Dict[str, Any]:
    if not sess or sess["user_type"] != "customer":
        raise HTTPException(status_code=401, detail="يلزم تسجيل الدخول كعميل")
    return sess


def assert_brand_access(sess: Dict[str, Any], brand_id: int, allowed_roles: Optional[List[str]] = None) -> None:
    if sess["role"] == "super_admin":
        return
    if allowed_roles and sess["role"] not in allowed_roles:
        raise HTTPException(status_code=403, detail="لا تملك صلاحية تنفيذ هذا الإجراء")
    if sess["brand_id"] != brand_id:
        raise HTTPException(status_code=403, detail="غير مصرح لك بالوصول إلى بيانات هذا البراند")


def assert_branch_access(sess: Dict[str, Any], brand_id: int, branch_id: int) -> None:
    assert_brand_access(sess, brand_id)
    if sess["role"] in ("super_admin", "brand_admin"):
        return
    if sess["branch_id"] is not None and sess["branch_id"] != branch_id:
        raise HTTPException(status_code=403, detail="غير مصرح لك بالوصول إلى بيانات هذا الفرع")


# ---------------------------------------------------------------------------
# Pydantic Request Models
# ---------------------------------------------------------------------------

class CustomerOTPRequest(BaseModel):
    phone: str = Field(..., min_length=8, max_length=20)
    name: Optional[str] = None


class CustomerOTPVerify(BaseModel):
    phone: str
    code: str
    name: Optional[str] = None


class CustomerProfileUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None


class WalletTopupRequest(BaseModel):
    amount: float = Field(..., gt=0, le=5000)


class StaffLoginRequest(BaseModel):
    username: str
    pin: str



class MerchantRegisterRequest(BaseModel):
    name_ar: str = Field(..., min_length=2)
    name_en: str = ""
    slug: str = Field(..., min_length=2, max_length=40)
    category_ar: str = "قهوة ومطاعم"
    city: str = "المدينة المنورة"
    branch_name: str = "الفرع الرئيسي"
    address: str = ""
    owner_name: str = Field(..., min_length=2)
    owner_phone: str = Field(..., min_length=8)
    username: str = Field(..., min_length=3)
    pin: str = Field(..., min_length=4, max_length=20)
    theme_ac: str = "#4de3b6"
    theme_bg: str = "#04261d"


class BrandApproveRequest(BaseModel):
    city: str = "المدينة المنورة"
    address: str = ""
    lat: float = 24.4672
    lng: float = 39.6111
    commission_rate: float = 5.0
    loyalty_points_per_sar: float = 1.0


class BrandCreate(BaseModel):
    slug: str = Field(..., min_length=2, max_length=40)
    name_ar: str
    name_en: str = ""
    category_ar: str = "قهوة ومطاعم"
    category_en: str = "Cafe & Restaurant"
    description: str = ""
    theme_bg: str = "#04261d"
    theme_bg2: str = "#06342a"
    theme_card: str = "#07382c"
    theme_tx: str = "#f4efdc"
    theme_mut: str = "#a8c9bd"
    theme_ac: str = "#4de3b6"
    theme_on: str = "#03261c"
    commission_rate: float = 5.0
    loyalty_points_per_sar: float = 1.0
    first_branch_name: str = "الفرع الرئيسي"
    first_branch_address: str = "المدينة المنورة"
    first_branch_phone: str = "0500000000"
    admin_username: Optional[str] = None
    admin_pin: str = "1111"


class BrandUpdate(BaseModel):
    name_ar: Optional[str] = None
    name_en: Optional[str] = None
    category_ar: Optional[str] = None
    category_en: Optional[str] = None
    description: Optional[str] = None
    theme_bg: Optional[str] = None
    theme_bg2: Optional[str] = None
    theme_card: Optional[str] = None
    theme_tx: Optional[str] = None
    theme_mut: Optional[str] = None
    theme_ac: Optional[str] = None
    commission_rate: Optional[float] = None
    loyalty_points_per_sar: Optional[float] = None
    is_active: Optional[int] = None


class BranchCreate(BaseModel):
    name: str
    address: str = ""
    phone: str = ""
    lat: float = 24.4672
    lng: float = 39.6111
    prep_minutes: int = 10
    delivery_fee: float = 12.0


class BranchUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    busy_level: Optional[str] = None
    prep_minutes: Optional[int] = None
    delivery_fee: Optional[float] = None
    is_open: Optional[int] = None
    accepts_delivery: Optional[int] = None
    accepts_pickup: Optional[int] = None
    accepts_drivethru: Optional[int] = None
    accepts_dinein: Optional[int] = None


class CategoryCreate(BaseModel):
    code: str
    name_ar: str
    name_en: str = ""
    sort_order: int = 0


class ProductCreate(BaseModel):
    category_id: Optional[int] = None
    name_ar: str
    name_en: str = ""
    description: str = ""
    price: float = Field(..., ge=0)
    sizes: List[Dict[str, Any]] = []
    image_url: str = ""
    calories: int = 0
    discount_percent: int = 0
    is_offer: int = 0
    is_available: int = 1


class ProductUpdate(BaseModel):
    category_id: Optional[int] = None
    name_ar: Optional[str] = None
    name_en: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    sizes: Optional[List[Dict[str, Any]]] = None
    image_url: Optional[str] = None
    calories: Optional[int] = None
    discount_percent: Optional[int] = None
    is_offer: Optional[int] = None
    is_available: Optional[int] = None


class StaffCreate(BaseModel):
    username: str
    full_name: str
    pin: str = Field(..., min_length=4, max_length=20)
    role: str
    branch_id: Optional[int] = None
    phone: str = ""


class StaffUpdate(BaseModel):
    full_name: Optional[str] = None
    pin: Optional[str] = None
    role: Optional[str] = None
    branch_id: Optional[int] = None
    phone: Optional[str] = None
    is_active: Optional[int] = None


class QrTagCreate(BaseModel):
    branch_id: int
    tag_type: str = "both"
    purpose: str = "table_order"
    label: str
    table_number: str = ""
    code: Optional[str] = None


class OrderItemInput(BaseModel):
    product_id: int
    quantity: int = Field(..., ge=1, le=50)
    size_label: str = ""
    notes: str = ""


class OrderCreate(BaseModel):
    brand_id: int
    branch_id: int
    order_mode: str = "pickup"  # table | drivethru | pickup | delivery
    table_number: str = ""
    car_info: str = ""
    delivery_address: str = ""
    delivery_lat: Optional[float] = None
    delivery_lng: Optional[float] = None
    qr_nfc_code: str = ""
    payment_method: str = "mada"  # apple_pay | mada | card | wallet | cash
    customer_name: str = ""
    customer_phone: str = ""
    redeem_points: int = 0
    notes: str = ""
    items: List[OrderItemInput]


class OrderStatusUpdate(BaseModel):
    status: Optional[str] = None
    kitchen_status: Optional[str] = None
    payment_status: Optional[str] = None
    extra_minutes: Optional[int] = None


class OrderRateInput(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: str = ""


class SupportTicketCreate(BaseModel):
    name: str
    phone: str = ""
    topic: str = "استفسار عام"
    message: str


class LoyaltyCheckinRequest(BaseModel):
    qr_nfc_code: str


class LoyaltyBonusRequest(BaseModel):
    points: int
    note: str = "مكافأة من إدارة البراند"


# ---------------------------------------------------------------------------
# Helpers

def _dump(m: BaseModel) -> Dict[str, Any]:
    return m.model_dump() if hasattr(m, "model_dump") else m.dict()
# ---------------------------------------------------------------------------

def _serialize_order(conn, order_row) -> Dict[str, Any]:
    o = dict(order_row)
    items = conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (o["id"],)).fetchall()
    o["items"] = [dict(i) for i in items]
    br = conn.execute("SELECT name, address FROM branches WHERE id = ?", (o["branch_id"],)).fetchone()
    bd = conn.execute("SELECT name_ar, slug, logo_url FROM brands WHERE id = ?", (o["brand_id"],)).fetchone()
    o["branch_name"] = br["name"] if br else ""
    o["brand_name"] = bd["name_ar"] if bd else ""
    o["brand_slug"] = bd["slug"] if bd else ""
    o["brand_logo"] = bd["logo_url"] if bd else ""
    return o


# ---------------------------------------------------------------------------
# Public Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    with db_session() as conn:
        bcount = conn.execute("SELECT COUNT(*) AS c FROM brands").fetchone()["c"]
        ocount = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    return {"status": "ok", "platform": "Sabooq", "database_engine": db_engine_name(), "brands": bcount, "orders": ocount, "payments": payment_gateway_status()}


@app.get("/api/payments/config")
def get_payments_config():
    return payment_gateway_status()


@app.get("/api/brands")
def list_public_brands(q: Optional[str] = None):
    cached = _cache_get("all_brands")
    if cached is None:
        with db_session() as conn:
            rows = conn.execute("SELECT * FROM brands WHERE is_active = 1 AND approval_status = 'approved' ORDER BY id ASC").fetchall()
            all_branches = conn.execute("SELECT * FROM branches ORDER BY id ASC").fetchall()
            prod_counts = conn.execute(
                "SELECT brand_id, COUNT(*) AS c FROM products WHERE is_available = 1 GROUP BY brand_id"
            ).fetchall()
        br_by_brand: Dict[int, List[Dict[str, Any]]] = {}
        for br in all_branches:
            brd = dict(br)
            br_by_brand.setdefault(brd["brand_id"], []).append(brd)
        pc_by_brand = {r["brand_id"]: r["c"] for r in prod_counts}
        cached = []
        for r in rows:
            b = dict(r)
            b["branches"] = br_by_brand.get(b["id"], [])
            b["products_count"] = pc_by_brand.get(b["id"], 0)
            cached.append(b)
        _cache_set("all_brands", cached)

    if q:
        ql = q.strip().lower()
        return {
            "brands": [
                b for b in cached
                if ql in f"{b['name_ar']} {b['name_en']} {b['category_ar']} {b['category_en']} {b['slug']}".lower()
            ]
        }
    return {"brands": cached}


@app.get("/api/brands/{slug}")
def get_brand_storefront(slug: str):
    cached = _cache_get(f"brand:{slug}")
    if cached is not None:
        return cached
    with db_session() as conn:
        brand = conn.execute("SELECT * FROM brands WHERE slug = ?", (slug,)).fetchone()
        if not brand:
            raise HTTPException(status_code=404, detail="البراند غير موجود")
        b = dict(brand)
        branches = [
            dict(br)
            for br in conn.execute("SELECT * FROM branches WHERE brand_id = ? ORDER BY id ASC", (b["id"],)).fetchall()
        ]
        categories = [
            dict(c)
            for c in conn.execute(
                "SELECT * FROM categories WHERE brand_id = ? AND is_active = 1 ORDER BY sort_order ASC, id ASC",
                (b["id"],),
            ).fetchall()
        ]
        products_raw = conn.execute(
            "SELECT * FROM products WHERE brand_id = ? ORDER BY sort_order ASC, id ASC",
            (b["id"],),
        ).fetchall()
        products = []
        for p in products_raw:
            pd = dict(p)
            try:
                pd["sizes"] = json.loads(pd.get("sizes_json") or "[]")
            except Exception:
                pd["sizes"] = []
            products.append(pd)

        qr_tags = [
            dict(t)
            for t in conn.execute(
                "SELECT id, code, branch_id, tag_type, purpose, label, table_number FROM qr_nfc_tags WHERE brand_id = ? AND is_active = 1",
                (b["id"],),
            ).fetchall()
        ]
        res = {
            "brand": b,
            "branches": branches,
            "categories": categories,
            "products": products,
            "qr_tags": qr_tags,
            "payments": payment_gateway_status(),
        }
        _cache_set(f"brand:{slug}", res)
        return res


@app.get("/api/qr/{code}")
def resolve_qr_nfc_tag(code: str, sess: Optional[Dict[str, Any]] = Depends(get_optional_session)):
    with db_session() as conn:
        tag = conn.execute(
            """SELECT t.*, b.slug AS brand_slug, b.name_ar AS brand_name, br.name AS branch_name
               FROM qr_nfc_tags t
               JOIN brands b ON b.id = t.brand_id
               JOIN branches br ON br.id = t.branch_id
               WHERE UPPER(t.code) = UPPER(?) AND t.is_active = 1""",
            (code.strip(),),
        ).fetchone()
        if not tag:
            raise HTTPException(status_code=404, detail="رمز QR / NFC غير صالح أو غير مفعّل")

        conn.execute("UPDATE qr_nfc_tags SET scan_count = scan_count + 1 WHERE id = ?", (tag["id"],))
        res = dict(tag)
        res["scan_count"] += 1
        res["checkin_awarded"] = False

        # If a logged-in customer scans a branch_checkin tag, grant check-in visit + 15 bonus points
        if sess and sess["user_type"] == "customer" and tag["purpose"] == "branch_checkin":
            cid = sess["user_id"]
            now = utc_now_iso()
            bonus = 15
            conn.execute(
                "UPDATE customers SET total_points = total_points + ?, total_visits = total_visits + 1, last_visit_at = ? WHERE id = ?",
                (bonus, now, cid),
            )
            cust = conn.execute("SELECT total_points FROM customers WHERE id = ?", (cid,)).fetchone()
            conn.execute("UPDATE customers SET tier = ? WHERE id = ?", (compute_tier(cust["total_points"]), cid))
            conn.execute(
                """INSERT INTO customer_brand_loyalty (customer_id, brand_id, points, visits, last_visit_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(customer_id, brand_id) DO UPDATE SET
                     points = customer_brand_loyalty.points + excluded.points,
                     visits = customer_brand_loyalty.visits + 1,
                     last_visit_at = excluded.last_visit_at""",
                (cid, tag["brand_id"], bonus, now),
            )
            conn.execute(
                """INSERT INTO loyalty_transactions (customer_id, brand_id, branch_id, tx_type, points, note, verified_via, created_at)
                   VALUES (?, ?, ?, 'checkin', ?, ?, ?, ?)""",
                (cid, tag["brand_id"], tag["branch_id"], bonus, f"تسجيل حضور عبر {tag['label']}", tag["tag_type"], now),
            )
            res["checkin_awarded"] = True
            res["bonus_points"] = bonus

        return res


@app.get("/t/{code}")
def qr_short_redirect(code: str):
    with db_session() as conn:
        tag = conn.execute(
            """SELECT t.code, t.branch_id, b.slug
               FROM qr_nfc_tags t JOIN brands b ON b.id = t.brand_id
               WHERE UPPER(t.code) = UPPER(?) AND t.is_active = 1""",
            (code.strip(),),
        ).fetchone()
        if not tag:
            return RedirectResponse(url="/?tag_error=1")
        return RedirectResponse(url=f"/?brand={tag['slug']}&branch={tag['branch_id']}&tag={tag['code']}")


# ---------------------------------------------------------------------------
# Customer Auth, Profile, Loyalty & Wallet
# ---------------------------------------------------------------------------

@app.post("/api/auth/customer/request-otp")
def customer_request_otp(payload: CustomerOTPRequest):
    phone = re.sub(r"\s+", "", payload.phone.strip())
    otp = f"{secrets.randbelow(9000) + 1000}"
    exp = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    now = utc_now_iso()
    with db_session() as conn:
        existing = conn.execute("SELECT id, name FROM customers WHERE phone = ?", (phone,)).fetchone()
        if existing:
            conn.execute("UPDATE customers SET otp_code = ?, otp_expires_at = ? WHERE id = ?", (otp, exp, existing["id"]))
            is_new = False
        else:
            cname = (payload.name or "").strip() or "عميل سبوق"
            conn.execute(
                """INSERT INTO customers (phone, name, otp_code, otp_expires_at, total_points, total_visits, wallet_balance, tier, created_at)
                   VALUES (?, ?, ?, ?, 20, 0, 50.0, 'برونزي', ?)""",
                (phone, cname, otp, exp, now),
            )
            is_new = True
    return {
        "ok": True,
        "phone": phone,
        "is_new_customer": is_new,
        "demo_otp": otp,
        "message": f"تم إرسال رمز التحقق (في الوضع التجريبي الرمز هو: {otp})",
    }


@app.post("/api/auth/customer/verify-otp")
def customer_verify_otp(payload: CustomerOTPVerify):
    phone = re.sub(r"\s+", "", payload.phone.strip())
    code = payload.code.strip()
    now = utc_now_iso()
    with db_session() as conn:
        cust = conn.execute("SELECT * FROM customers WHERE phone = ?", (phone,)).fetchone()
        if not cust:
            raise HTTPException(status_code=404, detail="رقم الجوال غير مسجل")
        if code != "1234" and (not cust["otp_code"] or cust["otp_code"] != code):
            raise HTTPException(status_code=400, detail="رمز التحقق غير صحيح")
        if payload.name and payload.name.strip():
            conn.execute("UPDATE customers SET name = ? WHERE id = ?", (payload.name.strip(), cust["id"]))
        conn.execute("UPDATE customers SET otp_code = '', last_visit_at = ? WHERE id = ?", (now, cust["id"]))
        token = create_session(conn, "customer", cust["id"], "customer", hours=24 * 30)
        updated = dict(conn.execute("SELECT id, phone, name, email, total_points, total_visits, wallet_balance, tier, created_at, last_visit_at FROM customers WHERE id = ?", (cust["id"],)).fetchone())
        return {"token": token, "customer": updated}


@app.get("/api/customers/me")
def get_customer_profile(sess: Dict[str, Any] = Depends(require_customer)):
    cid = sess["user_id"]
    with db_session() as conn:
        cust = conn.execute(
            "SELECT id, phone, name, email, total_points, total_visits, wallet_balance, tier, created_at, last_visit_at FROM customers WHERE id = ?",
            (cid,),
        ).fetchone()
        if not cust:
            raise HTTPException(status_code=404, detail="العميل غير موجود")
        brand_loyalty = [
            dict(r)
            for r in conn.execute(
                """SELECT cbl.*, b.name_ar AS brand_name, b.slug AS brand_slug, b.logo_url
                   FROM customer_brand_loyalty cbl
                   JOIN brands b ON b.id = cbl.brand_id
                   WHERE cbl.customer_id = ?
                   ORDER BY cbl.points DESC""",
                (cid,),
            ).fetchall()
        ]
        orders_raw = conn.execute(
            "SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC LIMIT 30",
            (cid,),
        ).fetchall()
        orders = [_serialize_order(conn, o) for o in orders_raw]
        txs = [
            dict(r)
            for r in conn.execute(
                """SELECT lt.*, b.name_ar AS brand_name
                   FROM loyalty_transactions lt
                   JOIN brands b ON b.id = lt.brand_id
                   WHERE lt.customer_id = ?
                   ORDER BY lt.id DESC LIMIT 30""",
                (cid,),
            ).fetchall()
        ]
        return {
            "customer": dict(cust),
            "brand_loyalty": brand_loyalty,
            "orders": orders,
            "loyalty_transactions": txs,
        }


@app.patch("/api/customers/me")
def update_customer_profile(payload: CustomerProfileUpdate, sess: Dict[str, Any] = Depends(require_customer)):
    cid = sess["user_id"]
    with db_session() as conn:
        if payload.name is not None:
            conn.execute("UPDATE customers SET name = ? WHERE id = ?", (payload.name.strip(), cid))
        if payload.email is not None:
            conn.execute("UPDATE customers SET email = ? WHERE id = ?", (payload.email.strip(), cid))
        cust = conn.execute(
            "SELECT id, phone, name, email, total_points, total_visits, wallet_balance, tier FROM customers WHERE id = ?",
            (cid,),
        ).fetchone()
        return {"customer": dict(cust)}


@app.post("/api/customers/me/topup")
def topup_customer_wallet(payload: WalletTopupRequest, sess: Dict[str, Any] = Depends(require_customer)):
    cid = sess["user_id"]
    with db_session() as conn:
        conn.execute("UPDATE customers SET wallet_balance = wallet_balance + ? WHERE id = ?", (payload.amount, cid))
        cust = conn.execute("SELECT wallet_balance FROM customers WHERE id = ?", (cid,)).fetchone()
        return {"ok": True, "wallet_balance": round(cust["wallet_balance"], 2)}


@app.post("/api/customers/me/checkin")
def customer_qr_nfc_checkin(payload: LoyaltyCheckinRequest, sess: Dict[str, Any] = Depends(require_customer)):
    return resolve_qr_nfc_tag(payload.qr_nfc_code, sess=sess)


# ---------------------------------------------------------------------------
# Orders & Payments
# ---------------------------------------------------------------------------

@app.post("/api/orders")
async def create_order(payload: OrderCreate, sess: Optional[Dict[str, Any]] = Depends(get_optional_session)):
    if not payload.items:
        raise HTTPException(status_code=400, detail="السلة فارغة")
    now = utc_now_iso()
    with db_session() as conn:
        brand = conn.execute("SELECT * FROM brands WHERE id = ? AND is_active = 1", (payload.brand_id,)).fetchone()
        if not brand:
            raise HTTPException(status_code=404, detail="البراند غير متاح")
        branch = conn.execute(
            "SELECT * FROM branches WHERE id = ? AND brand_id = ?", (payload.branch_id, payload.brand_id)
        ).fetchone()
        if not branch:
            raise HTTPException(status_code=404, detail="الفرع غير موجود لهذا البراند")
        if not branch["is_open"]:
            raise HTTPException(status_code=400, detail="الفرع مغلق حالياً ولا يستقبل طلبات")

        # Validate items against DB prices
        line_items: List[Dict[str, Any]] = []
        subtotal = 0.0
        for item in payload.items:
            prod = conn.execute(
                "SELECT * FROM products WHERE id = ? AND brand_id = ? AND is_available = 1",
                (item.product_id, payload.brand_id),
            ).fetchone()
            if not prod:
                raise HTTPException(status_code=400, detail=f"المنتج رقم {item.product_id} غير متوفر")
            unit_price = float(prod["price"])
            sizes = json.loads(prod["sizes_json"] or "[]")
            if item.size_label and sizes:
                matched = next((s for s in sizes if s.get("label") == item.size_label), None)
                if matched:
                    unit_price = float(matched["price"])
            if prod["discount_percent"] and prod["discount_percent"] > 0:
                unit_price = round(unit_price * (1 - prod["discount_percent"] / 100.0), 2)
            line_total = round(unit_price * item.quantity, 2)
            subtotal = round(subtotal + line_total, 2)
            line_items.append(
                {
                    "product_id": prod["id"],
                    "product_name": prod["name_ar"],
                    "size_label": item.size_label,
                    "unit_price": unit_price,
                    "quantity": item.quantity,
                    "line_total": line_total,
                    "notes": item.notes,
                }
            )

        # Customer resolution
        customer_id = sess["user_id"] if (sess and sess["user_type"] == "customer") else None
        cust_name = payload.customer_name.strip() or "عميل زائر"
        cust_phone = re.sub(r"\s+", "", payload.customer_phone.strip())

        if customer_id:
            cust_row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
            if cust_row:
                cust_name = payload.customer_name.strip() or cust_row["name"]
                cust_phone = cust_row["phone"]
        elif cust_phone:
            cust_row = conn.execute("SELECT * FROM customers WHERE phone = ?", (cust_phone,)).fetchone()
            if cust_row:
                customer_id = cust_row["id"]
                cust_name = payload.customer_name.strip() or cust_row["name"]

        # Points redemption (10 points = 1 SAR)
        discount_amount = 0.0
        points_redeemed = 0
        if payload.redeem_points > 0 and customer_id:
            cust_row = conn.execute("SELECT total_points FROM customers WHERE id = ?", (customer_id,)).fetchone()
            usable = min(payload.redeem_points, cust_row["total_points"])
            if usable >= 10:
                max_disc = round(subtotal * 0.5, 2)
                raw_disc = round(usable / 10.0, 2)
                discount_amount = min(raw_disc, max_disc)
                points_redeemed = int(round(discount_amount * 10))

        delivery_fee = float(branch["delivery_fee"]) if payload.order_mode == "delivery" else 0.0
        taxable = max(0.0, subtotal - discount_amount)
        vat_rate = float(brand["vat_rate"] or 15.0)
        # Prices in Saudi cafes are typically VAT-inclusive; extract VAT component for ZATCA-style invoice clarity
        vat_amount = round(taxable - (taxable / (1.0 + vat_rate / 100.0)), 2)
        total = round(taxable + delivery_fee, 2)

        # Wallet check
        if payload.payment_method == "wallet":
            if not customer_id:
                raise HTTPException(status_code=400, detail="يلزم تسجيل الدخول لاستخدام محفظة سبوق")
            wb = conn.execute("SELECT wallet_balance FROM customers WHERE id = ?", (customer_id,)).fetchone()["wallet_balance"]
            if wb < total:
                raise HTTPException(status_code=400, detail=f"رصيد المحفظة ({wb:.2f} ر.س) غير كافٍ لإجمالي الطلب ({total:.2f} ر.س)")

        # Generate order number
        last_id = conn.execute("SELECT COALESCE(MAX(id), 1000) AS m FROM orders").fetchone()["m"]
        order_number = f"SBQ-{last_id + 1}"

        # Initiate payment
        pay_res = initiate_payment(order_number, total, payload.payment_method, cust_name)
        payment_status = "paid" if pay_res["status"] in ("paid", "authorized") else "pending"

        # Deduct wallet if paid by wallet
        if payload.payment_method == "wallet" and customer_id:
            conn.execute("UPDATE customers SET wallet_balance = wallet_balance - ? WHERE id = ?", (total, customer_id))

        # Loyalty points earned
        points_earned = int(round(total * float(brand["loyalty_points_per_sar"] or 1.0))) if customer_id else 0

        # Real estimated preparation time based on branch prep_minutes, busy_level & order_mode
        base_prep = int(branch["prep_minutes"] or 10)
        busy_extra = 4 if branch["busy_level"] == "medium" else (8 if branch["busy_level"] == "busy" else 0)
        mode_extra = 15 if payload.order_mode == "delivery" else 0
        est_minutes = max(3, base_prep + busy_extra + mode_extra)
        est_ready_at = (datetime.now(timezone.utc) + timedelta(minutes=est_minutes)).isoformat()

        cur = conn.execute(
            """INSERT INTO orders (
                order_number, brand_id, branch_id, customer_id, customer_name, customer_phone,
                order_mode, table_number, car_info, delivery_address, delivery_lat, delivery_lng,
                qr_nfc_code, status, kitchen_status, payment_method, payment_status, payment_ref,
                subtotal, discount_amount, delivery_fee, vat_amount, total,
                points_earned, points_redeemed, notes, estimated_minutes, estimated_ready_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_number,
                payload.brand_id,
                payload.branch_id,
                customer_id,
                cust_name,
                cust_phone,
                payload.order_mode,
                payload.table_number,
                payload.car_info,
                payload.delivery_address,
                payload.delivery_lat,
                payload.delivery_lng,
                payload.qr_nfc_code,
                payload.payment_method,
                payment_status,
                pay_res["gateway_tx_id"],
                subtotal,
                discount_amount,
                delivery_fee,
                vat_amount,
                total,
                points_earned,
                points_redeemed,
                payload.notes,
                est_minutes,
                est_ready_at,
                now,
                now,
            ),
        )
        order_id = cur.lastrowid

        for li in line_items:
            conn.execute(
                """INSERT INTO order_items (order_id, product_id, product_name, size_label, unit_price, quantity, line_total, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    li["product_id"],
                    li["product_name"],
                    li["size_label"],
                    li["unit_price"],
                    li["quantity"],
                    li["line_total"],
                    li["notes"],
                ),
            )

        conn.execute(
            """INSERT INTO payment_transactions (order_id, provider, method, amount, currency, status, gateway_tx_id, webhook_payload_json, created_at)
               VALUES (?, ?, ?, ?, 'SAR', ?, ?, ?, ?)""",
            (
                order_id,
                pay_res["provider"],
                payload.payment_method,
                total,
                pay_res["status"],
                pay_res["gateway_tx_id"],
                json.dumps(pay_res, ensure_ascii=False),
                now,
            ),
        )

        # Update customer loyalty & visits
        if customer_id:
            net_points = points_earned - points_redeemed
            conn.execute(
                """UPDATE customers
                   SET total_points = MAX(0, total_points + ?),
                       total_visits = total_visits + 1,
                       last_visit_at = ?
                   WHERE id = ?""",
                (net_points, now, customer_id),
            )
            new_pts = conn.execute("SELECT total_points FROM customers WHERE id = ?", (customer_id,)).fetchone()["total_points"]
            conn.execute("UPDATE customers SET tier = ? WHERE id = ?", (compute_tier(new_pts), customer_id))
            conn.execute(
                """INSERT INTO customer_brand_loyalty (customer_id, brand_id, points, visits, last_visit_at)
                   VALUES (?, ?, MAX(0, ?), 1, ?)
                   ON CONFLICT(customer_id, brand_id) DO UPDATE SET
                     points = MAX(0, customer_brand_loyalty.points + ?),
                     visits = customer_brand_loyalty.visits + 1,
                     last_visit_at = excluded.last_visit_at""",
                (customer_id, payload.brand_id, net_points, now, net_points),
            )
            if points_earned > 0:
                conn.execute(
                    """INSERT INTO loyalty_transactions (customer_id, brand_id, branch_id, order_id, tx_type, points, note, verified_via, created_at)
                       VALUES (?, ?, ?, ?, 'earn', ?, ?, 'order', ?)""",
                    (customer_id, payload.brand_id, payload.branch_id, order_id, points_earned, f"نقاط الطلب {order_number}", now),
                )
            if points_redeemed > 0:
                conn.execute(
                    """INSERT INTO loyalty_transactions (customer_id, brand_id, branch_id, order_id, tx_type, points, note, verified_via, created_at)
                       VALUES (?, ?, ?, ?, 'redeem', ?, ?, 'order', ?)""",
                    (customer_id, payload.brand_id, payload.branch_id, order_id, -points_redeemed, f"استبدال نقاط في الطلب {order_number}", now),
                )

        order_data = _serialize_order(conn, conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())

    # Broadcast real-time event to Cashier & Kitchen screens
    await broker.publish(
        "order_created",
        order_data,
        brand_id=payload.brand_id,
        branch_id=payload.branch_id,
        order_number=order_data["order_number"],
    )

    return {"order": order_data, "payment": pay_res}


@app.get("/api/orders/{order_number}")
def get_order_status(order_number: str):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_number = ?", (order_number,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="الطلب غير موجود")
        return {"order": _serialize_order(conn, row)}


@app.post("/api/orders/{order_number}/rate")
def rate_order(order_number: str, payload: OrderRateInput):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_number = ?", (order_number,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="الطلب غير موجود")
        conn.execute(
            "UPDATE orders SET rating = ?, review_comment = ?, updated_at = ? WHERE id = ?",
            (payload.rating, payload.comment.strip(), utc_now_iso(), row["id"]),
        )
        # Recalculate brand average rating gently
        b = conn.execute("SELECT rating, reviews_count FROM brands WHERE id = ?", (row["brand_id"],)).fetchone()
        new_count = b["reviews_count"] + 1
        new_rating = round(((b["rating"] * b["reviews_count"]) + payload.rating) / new_count, 2)
        conn.execute("UPDATE brands SET rating = ?, reviews_count = ? WHERE id = ?", (new_rating, new_count, row["brand_id"]))
        return {"ok": True, "brand_rating": new_rating, "reviews_count": new_count}


@app.post("/api/orders/{order_number}/cancel")
async def cancel_order(order_number: str):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM orders WHERE order_number = ?", (order_number,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="الطلب غير موجود")
        if row["status"] not in ("new", "accepted"):
            raise HTTPException(status_code=400, detail="لا يمكن إلغاء الطلب بعد بدء التجهيز")
        conn.execute("UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?", (utc_now_iso(), row["id"]))
        order_data = _serialize_order(conn, conn.execute("SELECT * FROM orders WHERE id = ?", (row["id"],)).fetchone())
    await broker.publish(
        "order_updated",
        order_data,
        brand_id=order_data["brand_id"],
        branch_id=order_data["branch_id"],
        order_number=order_data["order_number"],
    )
    return {"order": order_data}


@app.post("/api/payments/webhook/{provider}")
async def payment_webhook(provider: str, request: Request, x_sabooq_signature: str = Header(default="")):
    raw = await request.body()
    ok, reason = verify_webhook_signature(raw, x_sabooq_signature)
    if not ok:
        raise HTTPException(status_code=401, detail=f"توقيع Webhook غير صالح ({reason})")
    data = json.loads(raw.decode("utf-8") or "{}")
    tx_id = data.get("gateway_tx_id") or data.get("id")
    status = data.get("status", "paid")
    with db_session() as conn:
        pt = conn.execute("SELECT * FROM payment_transactions WHERE gateway_tx_id = ?", (tx_id,)).fetchone()
        if not pt:
            return {"ok": False, "detail": "transaction_not_found"}
        conn.execute(
            "UPDATE payment_transactions SET status = ?, webhook_payload_json = ? WHERE id = ?",
            (status, json.dumps(data, ensure_ascii=False), pt["id"]),
        )
        order_pay_status = "paid" if status in ("paid", "captured", "authorized") else ("refunded" if status == "refunded" else "failed")
        conn.execute("UPDATE orders SET payment_status = ?, updated_at = ? WHERE id = ?", (order_pay_status, utc_now_iso(), pt["order_id"]))
        order_data = _serialize_order(conn, conn.execute("SELECT * FROM orders WHERE id = ?", (pt["order_id"],)).fetchone())
    await broker.publish("order_updated", order_data, brand_id=order_data["brand_id"], branch_id=order_data["branch_id"], order_number=order_data["order_number"])
    return {"ok": True, "order_number": order_data["order_number"], "payment_status": order_pay_status}


# ---------------------------------------------------------------------------
# Staff Authentication
# ---------------------------------------------------------------------------


@app.post("/api/merchants/register")
def merchant_self_register(payload: MerchantRegisterRequest):
    slug = re.sub(r"[^a-z0-9_-]", "", payload.slug.lower().strip())
    uname = payload.username.strip()
    if not slug:
        raise HTTPException(status_code=400, detail="المعرف المختصر يجب أن يكون بحروف إنجليزية أو أرقام")
    now = utc_now_iso()
    with db_session() as conn:
        if conn.execute("SELECT id FROM brands WHERE slug = ?", (slug,)).fetchone():
            raise HTTPException(status_code=409, detail="هذا المعرف المختصر مستخدم مسبقاً، اختر معرفاً آخر")
        if conn.execute("SELECT id FROM staff_users WHERE username = ?", (uname,)).fetchone():
            raise HTTPException(status_code=409, detail="اسم المستخدم مستخدم مسبقاً، اختر اسماً آخر")

        cur = conn.execute(
            """INSERT INTO brands (
                slug, name_ar, name_en, category_ar, category_en, description,
                theme_bg, theme_bg2, theme_card, theme_tx, theme_mut, theme_ac, theme_on,
                city, approval_status, owner_name, owner_phone, is_active, created_at
            ) VALUES (?, ?, ?, ?, '', ?, ?, '#06342a', '#07382c', '#f4efdc', '#a8c9bd', ?, '#03261c', ?, 'pending', ?, ?, 0, ?)""",
            (
                slug,
                payload.name_ar.strip(),
                payload.name_en.strip() or slug.title(),
                payload.category_ar.strip(),
                f"{payload.name_ar.strip()} — {payload.category_ar.strip()}",
                payload.theme_bg,
                payload.theme_ac,
                payload.city.strip(),
                payload.owner_name.strip(),
                payload.owner_phone.strip(),
                now,
            ),
        )
        brand_id = cur.lastrowid

        bcur = conn.execute(
            """INSERT INTO branches (brand_id, name, address, phone, lat, lng, busy_level, prep_minutes, delivery_fee, is_open, created_at)
               VALUES (?, ?, ?, ?, 24.4672, 39.6111, 'calm', 10, 12.0, 1, ?)""",
            (brand_id, payload.branch_name.strip() or "الفرع الرئيسي", payload.address.strip(), payload.owner_phone.strip(), now),
        )
        branch_id = bcur.lastrowid

        # Default category so owner can add products immediately after approval
        conn.execute(
            "INSERT INTO categories (brand_id, code, name_ar, name_en, sort_order, is_active) VALUES (?, 'main', 'القائمة الرئيسية', 'Main Menu', 0, 1)",
            (brand_id,),
        )
        conn.execute(
            "INSERT INTO categories (brand_id, code, name_ar, name_en, sort_order, is_active) VALUES (?, 'o', 'العروض والخصومات', 'Offers', -1, 1)",
            (brand_id,),
        )

        # Create owner, cashier, and kitchen accounts as INACTIVE until Super Admin approves
        staff_seed = [
            (uname, payload.owner_name.strip(), payload.pin.strip(), "brand_admin", None),
            (f"{slug}_cashier", f"كاشير {payload.name_ar.strip()}", "3333", "cashier", branch_id),
            (f"{slug}_kitchen", f"مطبخ {payload.name_ar.strip()}", "4444", "kitchen", branch_id),
        ]
        for u, fn, pin, rl, brid in staff_seed:
            if not conn.execute("SELECT id FROM staff_users WHERE username = ?", (u,)).fetchone():
                conn.execute(
                    """INSERT INTO staff_users (username, full_name, pin_hash, role, brand_id, branch_id, phone, is_active, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    (u, fn, hash_secret(pin), rl, brand_id, brid, payload.owner_phone.strip(), now),
                )

        # Create printable store QR + NFC tags
        slug_up = slug.upper()
        conn.execute(
            """INSERT INTO qr_nfc_tags (code, brand_id, branch_id, tag_type, purpose, label, table_number, scan_count, is_active, created_at)
               VALUES (?, ?, ?, 'both', 'branch_checkin', ?, '', 0, 1, ?)""",
            (f"SBQ-{slug_up}-STORE", brand_id, branch_id, f"باركود المحل والولاء — {payload.name_ar.strip()}", now),
        )
        conn.execute(
            """INSERT INTO qr_nfc_tags (code, brand_id, branch_id, tag_type, purpose, label, table_number, scan_count, is_active, created_at)
               VALUES (?, ?, ?, 'both', 'table_order', ?, '1', 0, 1, ?)""",
            (f"SBQ-{slug_up}-T1", brand_id, branch_id, f"طاولة 1 — {payload.name_ar.strip()}", now),
        )

        return {
            "ok": True,
            "brand_id": brand_id,
            "approval_status": "pending",
            "message": "تم استلام طلب تسجيل محلك بنجاح! سيتم مراجعته وتحديد الموقع وتفعيله من الإدارة العليا لسبوق قريباً.",
        }

@app.post("/api/auth/staff/login")
def staff_login(payload: StaffLoginRequest):
    uname = payload.username.strip()
    with db_session() as conn:
        user = conn.execute("SELECT * FROM staff_users WHERE username = ?", (uname,)).fetchone()
        if not user or not verify_secret(payload.pin.strip(), user["pin_hash"]):
            raise HTTPException(status_code=401, detail="اسم المستخدم أو رمز الدخول غير صحيح")
        if not user["is_active"]:
            if user["brand_id"]:
                b_row = conn.execute("SELECT approval_status FROM brands WHERE id = ?", (user["brand_id"],)).fetchone()
                if b_row and b_row["approval_status"] == "pending":
                    raise HTTPException(status_code=403, detail="طلب تسجيل محلك قيد المراجعة من الإدارة العليا لسبوق، سيتم تفعيل حسابك فور الموافقة")
                if b_row and b_row["approval_status"] == "rejected":
                    raise HTTPException(status_code=403, detail="تم رفض طلب تسجيل المحل، يرجى التواصل مع الإدارة العليا لسبوق")
            raise HTTPException(status_code=403, detail="هذا الحساب موقوف حالياً، يرجى مراجعة الإدارة")
        token = create_session(conn, "staff", user["id"], user["role"], user["brand_id"], user["branch_id"], hours=24)
        brand_info = None
        branch_info = None
        if user["brand_id"]:
            br = conn.execute("SELECT id, slug, name_ar, logo_url, theme_ac FROM brands WHERE id = ?", (user["brand_id"],)).fetchone()
            brand_info = dict(br) if br else None
        if user["branch_id"]:
            brc = conn.execute("SELECT id, name FROM branches WHERE id = ?", (user["branch_id"],)).fetchone()
            branch_info = dict(brc) if brc else None
        log_audit(conn, "staff", user["id"], user["role"], "staff_login", {"username": uname}, user["brand_id"], user["branch_id"])
        return {
            "token": token,
            "staff": {
                "id": user["id"],
                "username": user["username"],
                "full_name": user["full_name"],
                "role": user["role"],
                "brand_id": user["brand_id"],
                "branch_id": user["branch_id"],
                "brand": brand_info,
                "branch": branch_info,
            },
        }


@app.get("/api/auth/staff/me")
def staff_me(sess: Dict[str, Any] = Depends(require_staff)):
    with db_session() as conn:
        user = conn.execute("SELECT id, username, full_name, role, brand_id, branch_id, phone FROM staff_users WHERE id = ?", (sess["user_id"],)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="المستخدم غير موجود")
        res = dict(user)
        if res["brand_id"]:
            b = conn.execute("SELECT id, slug, name_ar, logo_url, theme_ac FROM brands WHERE id = ?", (res["brand_id"],)).fetchone()
            res["brand"] = dict(b) if b else None
        if res["branch_id"]:
            br = conn.execute("SELECT id, name FROM branches WHERE id = ?", (res["branch_id"],)).fetchone()
            res["branch"] = dict(br) if br else None
        return {"staff": res}


@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(default=None)):
    tok = _extract_token(authorization)
    if tok:
        with db_session() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (tok,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sabooq Super Admin Dashboard APIs
# ---------------------------------------------------------------------------

@app.get("/api/admin/overview")
def admin_overview(sess: Dict[str, Any] = Depends(require_super_admin)):
    with db_session() as conn:
        totals = conn.execute(
            """SELECT
                (SELECT COUNT(*) FROM brands) AS total_brands,
                (SELECT COUNT(*) FROM brands WHERE is_active = 1) AS active_brands,
                (SELECT COUNT(*) FROM branches) AS total_branches,
                (SELECT COUNT(*) FROM products) AS total_products,
                (SELECT COUNT(*) FROM customers) AS total_customers,
                (SELECT COUNT(*) FROM orders WHERE status != 'cancelled') AS total_orders,
                (SELECT COALESCE(SUM(total), 0) FROM orders WHERE status != 'cancelled') AS total_revenue,
                (SELECT COUNT(*) FROM support_tickets WHERE status = 'open') AS open_tickets,
                (SELECT COUNT(*) FROM brands WHERE approval_status = 'pending') AS pending_brands_count
            """
        ).fetchone()

        per_brand = [
            dict(r)
            for r in conn.execute(
                """SELECT
                    b.id, b.slug, b.name_ar, b.name_en, b.category_ar, b.logo_url, b.theme_ac,
                    b.rating, b.reviews_count, b.commission_rate, b.is_active, b.city, b.approval_status, b.owner_name, b.owner_phone,
                    (SELECT COUNT(*) FROM branches br WHERE br.brand_id = b.id) AS branches_count,
                    (SELECT COUNT(*) FROM products p WHERE p.brand_id = b.id) AS products_count,
                    (SELECT COUNT(*) FROM staff_users s WHERE s.brand_id = b.id) AS staff_count,
                    (SELECT COUNT(*) FROM orders o WHERE o.brand_id = b.id AND o.status != 'cancelled') AS orders_count,
                    (SELECT COALESCE(SUM(o.total), 0) FROM orders o WHERE o.brand_id = b.id AND o.status != 'cancelled') AS revenue
                FROM brands b
                ORDER BY b.id ASC"""
            ).fetchall()
        ]
        recent_orders = [
            _serialize_order(conn, r)
            for r in conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 15").fetchall()
        ]
        tickets = [
            dict(r)
            for r in conn.execute("SELECT * FROM support_tickets ORDER BY id DESC LIMIT 30").fetchall()
        ]
        pending_brands = [
            dict(r)
            for r in conn.execute(
                """SELECT b.*, br.id AS first_branch_id, br.name AS branch_name, br.address AS branch_address, br.lat, br.lng
                   FROM brands b
                   LEFT JOIN branches br ON br.brand_id = b.id
                   WHERE b.approval_status = 'pending'
                   ORDER BY b.id DESC"""
            ).fetchall()
        ]
        return {
            "totals": dict(totals),
            "brands": per_brand,
            "pending_brands": pending_brands,
            "recent_orders": recent_orders,
            "tickets": tickets,
        }


@app.post("/api/admin/brands")
def admin_create_brand(payload: BrandCreate, sess: Dict[str, Any] = Depends(require_super_admin)):
    slug = re.sub(r"[^a-z0-9_-]", "", payload.slug.lower().strip())
    if not slug:
        raise HTTPException(status_code=400, detail="المعرف المختصر (slug) يجب أن يحتوي على حروف إنجليزية أو أرقام")
    now = utc_now_iso()
    with db_session() as conn:
        exists = conn.execute("SELECT id FROM brands WHERE slug = ?", (slug,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="يوجد براند بنفس المعرف المختصر مسبقاً")
        cur = conn.execute(
            """INSERT INTO brands (
                slug, name_ar, name_en, category_ar, category_en, description,
                theme_bg, theme_bg2, theme_card, theme_tx, theme_mut, theme_ac, theme_on,
                commission_rate, loyalty_points_per_sar, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                slug,
                payload.name_ar.strip(),
                payload.name_en.strip() or slug.title(),
                payload.category_ar.strip(),
                payload.category_en.strip(),
                payload.description.strip(),
                payload.theme_bg,
                payload.theme_bg2,
                payload.theme_card,
                payload.theme_tx,
                payload.theme_mut,
                payload.theme_ac,
                payload.theme_on,
                payload.commission_rate,
                payload.loyalty_points_per_sar,
                now,
            ),
        )
        brand_id = cur.lastrowid

        # Create first branch
        bcur = conn.execute(
            """INSERT INTO branches (brand_id, name, address, phone, lat, lng, busy_level, prep_minutes, delivery_fee, is_open, created_at)
               VALUES (?, ?, ?, ?, 24.4672, 39.6111, 'calm', 10, 12.0, 1, ?)""",
            (brand_id, payload.first_branch_name, payload.first_branch_address, payload.first_branch_phone, now),
        )
        branch_id = bcur.lastrowid

        # Create default category
        conn.execute(
            "INSERT INTO categories (brand_id, code, name_ar, name_en, sort_order, is_active) VALUES (?, 'main', 'القائمة الرئيسية', 'Main Menu', 0, 1)",
            (brand_id,),
        )

        # Create default QR & NFC tags
        slug_up = slug.upper()
        conn.execute(
            """INSERT INTO qr_nfc_tags (code, brand_id, branch_id, tag_type, purpose, label, table_number, scan_count, is_active, created_at)
               VALUES (?, ?, ?, 'both', 'table_order', ?, '1', 0, 1, ?)""",
            (f"SBQ-{slug_up}-B1-T1", brand_id, branch_id, f"طاولة 1 — {payload.first_branch_name}", now),
        )
        conn.execute(
            """INSERT INTO qr_nfc_tags (code, brand_id, branch_id, tag_type, purpose, label, table_number, scan_count, is_active, created_at)
               VALUES (?, ?, ?, 'nfc', 'branch_checkin', ?, '', 0, 1, ?)""",
            (f"SBQ-{slug_up}-B1-NFC", brand_id, branch_id, f"نقطة ولاء NFC — {payload.first_branch_name}", now),
        )

        # Create default staff accounts for the new brand
        admin_user = (payload.admin_username or f"{slug}_admin").strip()
        staff_accounts = [
            (admin_user, f"مدير براند {payload.name_ar}", payload.admin_pin, "brand_admin", None),
            (f"{slug}_cashier", f"كاشير {payload.name_ar}", "3333", "cashier", branch_id),
            (f"{slug}_kitchen", f"مطبخ {payload.name_ar}", "4444", "kitchen", branch_id),
        ]
        created_staff = []
        for u, fn, pin, rl, brid in staff_accounts:
            conn.execute(
                """INSERT INTO staff_users (username, full_name, pin_hash, role, brand_id, branch_id, is_active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                (u, fn, hash_secret(pin), rl, brand_id, brid, now),
            )
            created_staff.append({"username": u, "pin": pin, "role": rl})

        log_audit(conn, "staff", sess["user_id"], sess["role"], "create_brand", {"slug": slug, "brand_id": brand_id}, brand_id)
        brand = dict(conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone())
        _cache_clear(); return {"brand": brand, "first_branch_id": branch_id, "created_staff": created_staff}


@app.patch("/api/admin/brands/{brand_id}")
def admin_update_brand(brand_id: int, payload: BrandUpdate, sess: Dict[str, Any] = Depends(require_staff)):
    assert_brand_access(sess, brand_id, allowed_roles=["super_admin", "brand_admin"])
    updates = {k: v for k, v in _dump(payload).items() if v is not None}
    if not updates:
        return {"ok": True}
    if sess["role"] != "super_admin":
        updates.pop("commission_rate", None)
        updates.pop("is_active", None)
    sets = ", ".join([f"{k} = ?" for k in updates.keys()])
    vals = list(updates.values()) + [brand_id]
    with db_session() as conn:
        conn.execute(f"UPDATE brands SET {sets} WHERE id = ?", vals)
        brand = dict(conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone())
        _cache_clear(); return {"brand": brand}



@app.post("/api/admin/brands/{brand_id}/approve")
def admin_approve_brand(brand_id: int, payload: BrandApproveRequest, sess: Dict[str, Any] = Depends(require_super_admin)):
    now = utc_now_iso()
    with db_session() as conn:
        b = conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone()
        if not b:
            raise HTTPException(status_code=404, detail="المحل غير موجود")
        conn.execute(
            """UPDATE brands
               SET city = ?, commission_rate = ?, loyalty_points_per_sar = ?,
                   approval_status = 'approved', is_active = 1, approved_at = ?
               WHERE id = ?""",
            (payload.city.strip(), payload.commission_rate, payload.loyalty_points_per_sar, now, brand_id),
        )
        # Update first branch address & GPS coordinates
        br = conn.execute("SELECT id FROM branches WHERE brand_id = ? ORDER BY id ASC LIMIT 1", (brand_id,)).fetchone()
        if br:
            conn.execute(
                "UPDATE branches SET address = ?, lat = ?, lng = ?, is_open = 1 WHERE id = ?",
                (payload.address.strip() or payload.city.strip(), payload.lat, payload.lng, br["id"]),
            )
        # Activate the store owner's & staff accounts so they can log in immediately
        conn.execute("UPDATE staff_users SET is_active = 1 WHERE brand_id = ?", (brand_id,))
        log_audit(conn, "staff", sess["user_id"], sess["role"], "approve_brand", {"brand_id": brand_id, "city": payload.city}, brand_id)
        updated = dict(conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone())
    _cache_clear()
    return {"ok": True, "brand": updated}


@app.post("/api/admin/brands/{brand_id}/reject")
def admin_reject_brand(brand_id: int, sess: Dict[str, Any] = Depends(require_super_admin)):
    with db_session() as conn:
        conn.execute("UPDATE brands SET approval_status = 'rejected', is_active = 0 WHERE id = ?", (brand_id,))
        conn.execute("UPDATE staff_users SET is_active = 0 WHERE brand_id = ?", (brand_id,))
        log_audit(conn, "staff", sess["user_id"], sess["role"], "reject_brand", {"brand_id": brand_id}, brand_id)
    _cache_clear()
    return {"ok": True}

@app.patch("/api/admin/tickets/{ticket_id}")
def close_support_ticket(ticket_id: int, sess: Dict[str, Any] = Depends(require_super_admin)):
    with db_session() as conn:
        conn.execute("UPDATE support_tickets SET status = 'closed' WHERE id = ?", (ticket_id,))
        return {"ok": True}


# ---------------------------------------------------------------------------
# Brand & Branch Management APIs (Scoped by RBAC)
# ---------------------------------------------------------------------------

@app.get("/api/manage/brands/{brand_id}/dashboard")
def brand_dashboard(brand_id: int, sess: Dict[str, Any] = Depends(require_staff)):
    assert_brand_access(sess, brand_id)
    branch_filter = sess["branch_id"] if sess["role"] not in ("super_admin", "brand_admin") else None
    with db_session() as conn:
        brand = dict(conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone())
        where = "brand_id = ? AND status != 'cancelled'"
        params: List[Any] = [brand_id]
        if branch_filter:
            where += " AND branch_id = ?"
            params.append(branch_filter)

        kpi = dict(
            conn.execute(
                f"""SELECT
                    COUNT(*) AS orders_count,
                    COALESCE(SUM(total), 0) AS total_sales,
                    COALESCE(AVG(total), 0) AS avg_ticket,
                    SUM(CASE WHEN order_mode='delivery' THEN 1 ELSE 0 END) AS delivery_orders,
                    SUM(CASE WHEN order_mode='pickup' THEN 1 ELSE 0 END) AS pickup_orders,
                    SUM(CASE WHEN order_mode='drivethru' THEN 1 ELSE 0 END) AS drivethru_orders,
                    SUM(CASE WHEN order_mode='table' THEN 1 ELSE 0 END) AS table_orders,
                    SUM(CASE WHEN status IN ('new','accepted','preparing','ready') THEN 1 ELSE 0 END) AS active_orders
                FROM orders WHERE {where}""",
                params,
            ).fetchone()
        )
        branches = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM branches WHERE brand_id = ?" + (" AND id = ?" if branch_filter else "") + " ORDER BY id ASC",
                ([brand_id, branch_filter] if branch_filter else [brand_id]),
            ).fetchall()
        ]
        categories = [
            dict(r)
            for r in conn.execute("SELECT * FROM categories WHERE brand_id = ? ORDER BY sort_order ASC, id ASC", (brand_id,)).fetchall()
        ]
        products_raw = conn.execute("SELECT * FROM products WHERE brand_id = ? ORDER BY sort_order ASC, id ASC", (brand_id,)).fetchall()
        products = []
        for p in products_raw:
            pd = dict(p)
            pd["sizes"] = json.loads(pd.get("sizes_json") or "[]")
            products.append(pd)

        staff = [
            dict(r)
            for r in conn.execute(
                """SELECT s.id, s.username, s.full_name, s.role, s.brand_id, s.branch_id, s.phone, s.is_active, s.created_at,
                          br.name AS branch_name
                   FROM staff_users s
                   LEFT JOIN branches br ON br.id = s.branch_id
                   WHERE s.brand_id = ?"""
                + (" AND s.branch_id = ?" if branch_filter else "")
                + " ORDER BY s.id ASC",
                ([brand_id, branch_filter] if branch_filter else [brand_id]),
            ).fetchall()
        ]
        qr_tags = [
            dict(r)
            for r in conn.execute(
                """SELECT t.*, br.name AS branch_name
                   FROM qr_nfc_tags t JOIN branches br ON br.id = t.branch_id
                   WHERE t.brand_id = ?"""
                + (" AND t.branch_id = ?" if branch_filter else "")
                + " ORDER BY t.id DESC",
                ([brand_id, branch_filter] if branch_filter else [brand_id]),
            ).fetchall()
        ]
        customers = [
            dict(r)
            for r in conn.execute(
                """SELECT c.id, c.name, c.phone, c.tier, c.total_points, c.total_visits,
                          COALESCE(cbl.points, 0) AS brand_points, COALESCE(cbl.visits, 0) AS brand_visits,
                          COALESCE((SELECT SUM(o.total) FROM orders o WHERE o.customer_id = c.id AND o.brand_id = ? AND o.status != 'cancelled'), 0) AS brand_spend
                   FROM customers c
                   LEFT JOIN customer_brand_loyalty cbl ON cbl.customer_id = c.id AND cbl.brand_id = ?
                   ORDER BY brand_spend DESC, c.total_points DESC LIMIT 50""",
                (brand_id, brand_id),
            ).fetchall()
        ]
        return {
            "brand": brand,
            "kpi": kpi,
            "branches": branches,
            "categories": categories,
            "products": products,
            "staff": staff,
            "qr_tags": qr_tags,
            "customers": customers,
        }


@app.post("/api/manage/brands/{brand_id}/branches")
def create_branch(brand_id: int, payload: BranchCreate, sess: Dict[str, Any] = Depends(require_staff)):
    assert_brand_access(sess, brand_id, allowed_roles=["super_admin", "brand_admin"])
    now = utc_now_iso()
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO branches (brand_id, name, address, phone, lat, lng, busy_level, prep_minutes, delivery_fee, is_open, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'calm', ?, ?, 1, ?)""",
            (brand_id, payload.name.strip(), payload.address.strip(), payload.phone.strip(), payload.lat, payload.lng, payload.prep_minutes, payload.delivery_fee, now),
        )
        branch = dict(conn.execute("SELECT * FROM branches WHERE id = ?", (cur.lastrowid,)).fetchone())
        _cache_clear(); return {"branch": branch}


@app.patch("/api/manage/branches/{branch_id}")
def update_branch(branch_id: int, payload: BranchUpdate, sess: Dict[str, Any] = Depends(require_staff)):
    with db_session() as conn:
        br = conn.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
        if not br:
            raise HTTPException(status_code=404, detail="الفرع غير موجود")
        assert_branch_access(sess, br["brand_id"], branch_id)
        if sess["role"] not in ("super_admin", "brand_admin", "branch_manager"):
            raise HTTPException(status_code=403, detail="لا تملك صلاحية تعديل إعدادات الفرع")
        updates = {k: v for k, v in _dump(payload).items() if v is not None}
        if updates:
            sets = ", ".join([f"{k} = ?" for k in updates.keys()])
            conn.execute(f"UPDATE branches SET {sets} WHERE id = ?", list(updates.values()) + [branch_id])
        _cache_clear(); return {"branch": dict(conn.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone())}


@app.post("/api/manage/brands/{brand_id}/categories")
def create_category(brand_id: int, payload: CategoryCreate, sess: Dict[str, Any] = Depends(require_staff)):
    assert_brand_access(sess, brand_id, allowed_roles=["super_admin", "brand_admin", "branch_manager"])
    code = re.sub(r"[^a-zA-Z0-9_-]", "", payload.code.strip()) or f"c{secrets.randbelow(9999)}"
    with db_session() as conn:
        cur = conn.execute(
            "INSERT INTO categories (brand_id, code, name_ar, name_en, sort_order, is_active) VALUES (?, ?, ?, ?, ?, 1)",
            (brand_id, code, payload.name_ar.strip(), payload.name_en.strip(), payload.sort_order),
        )
        _cache_clear(); return {"category": dict(conn.execute("SELECT * FROM categories WHERE id = ?", (cur.lastrowid,)).fetchone())}


@app.post("/api/manage/brands/{brand_id}/products")
def create_product(brand_id: int, payload: ProductCreate, sess: Dict[str, Any] = Depends(require_staff)):
    assert_brand_access(sess, brand_id, allowed_roles=["super_admin", "brand_admin", "branch_manager"])
    now = utc_now_iso()
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO products (
                brand_id, category_id, name_ar, name_en, description, price, sizes_json,
                image_url, calories, discount_percent, is_offer, is_available, sort_order, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                brand_id,
                payload.category_id,
                payload.name_ar.strip(),
                payload.name_en.strip(),
                payload.description.strip(),
                payload.price,
                json.dumps(payload.sizes, ensure_ascii=False),
                payload.image_url.strip(),
                payload.calories,
                payload.discount_percent,
                payload.is_offer,
                payload.is_available,
                now,
            ),
        )
        p = dict(conn.execute("SELECT * FROM products WHERE id = ?", (cur.lastrowid,)).fetchone())
        p["sizes"] = json.loads(p["sizes_json"] or "[]")
        _cache_clear(); return {"product": p}


@app.patch("/api/manage/products/{product_id}")
def update_product(product_id: int, payload: ProductUpdate, sess: Dict[str, Any] = Depends(require_staff)):
    with db_session() as conn:
        prod = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail="المنتج غير موجود")
        assert_brand_access(sess, prod["brand_id"], allowed_roles=["super_admin", "brand_admin", "branch_manager"])
        updates = {k: v for k, v in _dump(payload).items() if v is not None}
        if "sizes" in updates:
            updates["sizes_json"] = json.dumps(updates.pop("sizes"), ensure_ascii=False)
        if updates:
            sets = ", ".join([f"{k} = ?" for k in updates.keys()])
            conn.execute(f"UPDATE products SET {sets} WHERE id = ?", list(updates.values()) + [product_id])
        p = dict(conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone())
        p["sizes"] = json.loads(p["sizes_json"] or "[]")
        _cache_clear(); return {"product": p}


@app.delete("/api/manage/products/{product_id}")
def delete_product(product_id: int, sess: Dict[str, Any] = Depends(require_staff)):
    with db_session() as conn:
        prod = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail="المنتج غير موجود")
        assert_brand_access(sess, prod["brand_id"], allowed_roles=["super_admin", "brand_admin"])
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        return {"ok": True}


@app.post("/api/manage/brands/{brand_id}/staff")
def create_staff(brand_id: int, payload: StaffCreate, sess: Dict[str, Any] = Depends(require_staff)):
    assert_brand_access(sess, brand_id, allowed_roles=["super_admin", "brand_admin", "branch_manager"])
    if payload.role == "super_admin":
        raise HTTPException(status_code=403, detail="لا يمكن إنشاء حساب إدارة عليا من هنا")
    if sess["role"] == "branch_manager" and payload.role in ("brand_admin", "branch_manager"):
        raise HTTPException(status_code=403, detail="مدير الفرع يمكنه إضافة الكاشير والمطبخ والمناديب فقط")
    branch_id = payload.branch_id if sess["role"] != "branch_manager" else sess["branch_id"]
    now = utc_now_iso()
    with db_session() as conn:
        exists = conn.execute("SELECT id FROM staff_users WHERE username = ?", (payload.username.strip(),)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="اسم المستخدم مستخدم مسبقاً")
        cur = conn.execute(
            """INSERT INTO staff_users (username, full_name, pin_hash, role, brand_id, branch_id, phone, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (payload.username.strip(), payload.full_name.strip(), hash_secret(payload.pin.strip()), payload.role, brand_id, branch_id, payload.phone.strip(), now),
        )
        row = dict(
            conn.execute(
                "SELECT id, username, full_name, role, brand_id, branch_id, phone, is_active, created_at FROM staff_users WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
        )
        return {"staff": row}


@app.patch("/api/manage/staff/{staff_id}")
def update_staff(staff_id: int, payload: StaffUpdate, sess: Dict[str, Any] = Depends(require_staff)):
    with db_session() as conn:
        st = conn.execute("SELECT * FROM staff_users WHERE id = ?", (staff_id,)).fetchone()
        if not st:
            raise HTTPException(status_code=404, detail="الموظف غير موجود")
        assert_brand_access(sess, st["brand_id"], allowed_roles=["super_admin", "brand_admin", "branch_manager"])
        updates = {k: v for k, v in _dump(payload).items() if v is not None}
        if "pin" in updates:
            updates["pin_hash"] = hash_secret(updates.pop("pin").strip())
        if updates:
            sets = ", ".join([f"{k} = ?" for k in updates.keys()])
            conn.execute(f"UPDATE staff_users SET {sets} WHERE id = ?", list(updates.values()) + [staff_id])
        row = dict(
            conn.execute(
                "SELECT id, username, full_name, role, brand_id, branch_id, phone, is_active FROM staff_users WHERE id = ?",
                (staff_id,),
            ).fetchone()
        )
        return {"staff": row}


@app.post("/api/manage/brands/{brand_id}/qr-tags")
def create_qr_tag(brand_id: int, payload: QrTagCreate, sess: Dict[str, Any] = Depends(require_staff)):
    assert_brand_access(sess, brand_id, allowed_roles=["super_admin", "brand_admin", "branch_manager"])
    now = utc_now_iso()
    with db_session() as conn:
        brand = conn.execute("SELECT slug FROM brands WHERE id = ?", (brand_id,)).fetchone()
        code = (payload.code or f"SBQ-{brand['slug'].upper()}-{secrets.token_hex(3).upper()}").strip().upper()
        cur = conn.execute(
            """INSERT INTO qr_nfc_tags (code, brand_id, branch_id, tag_type, purpose, label, table_number, scan_count, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?)""",
            (code, brand_id, payload.branch_id, payload.tag_type, payload.purpose, payload.label.strip(), payload.table_number.strip(), now),
        )
        tag = dict(conn.execute("SELECT * FROM qr_nfc_tags WHERE id = ?", (cur.lastrowid,)).fetchone())
        return {"tag": tag}


@app.delete("/api/manage/qr-tags/{tag_id}")
def delete_qr_tag(tag_id: int, sess: Dict[str, Any] = Depends(require_staff)):
    with db_session() as conn:
        t = conn.execute("SELECT * FROM qr_nfc_tags WHERE id = ?", (tag_id,)).fetchone()
        if not t:
            raise HTTPException(status_code=404, detail="الرمز غير موجود")
        assert_brand_access(sess, t["brand_id"], allowed_roles=["super_admin", "brand_admin", "branch_manager"])
        conn.execute("DELETE FROM qr_nfc_tags WHERE id = ?", (tag_id,))
        return {"ok": True}


@app.post("/api/manage/brands/{brand_id}/customers/{customer_id}/bonus")
def grant_customer_bonus(brand_id: int, customer_id: int, payload: LoyaltyBonusRequest, sess: Dict[str, Any] = Depends(require_staff)):
    assert_brand_access(sess, brand_id, allowed_roles=["super_admin", "brand_admin", "branch_manager"])
    now = utc_now_iso()
    with db_session() as conn:
        cust = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not cust:
            raise HTTPException(status_code=404, detail="العميل غير موجود")
        conn.execute("UPDATE customers SET total_points = MAX(0, total_points + ?) WHERE id = ?", (payload.points, customer_id))
        new_pts = conn.execute("SELECT total_points FROM customers WHERE id = ?", (customer_id,)).fetchone()["total_points"]
        conn.execute("UPDATE customers SET tier = ? WHERE id = ?", (compute_tier(new_pts), customer_id))
        conn.execute(
            """INSERT INTO customer_brand_loyalty (customer_id, brand_id, points, visits, last_visit_at)
               VALUES (?, ?, MAX(0, ?), 0, ?)
               ON CONFLICT(customer_id, brand_id) DO UPDATE SET points = MAX(0, customer_brand_loyalty.points + ?)""",
            (customer_id, brand_id, payload.points, now, payload.points),
        )
        conn.execute(
            """INSERT INTO loyalty_transactions (customer_id, brand_id, tx_type, points, note, verified_via, created_at)
               VALUES (?, ?, 'bonus', ?, ?, 'manual', ?)""",
            (customer_id, brand_id, payload.points, payload.note, now),
        )
        return {"ok": True, "total_points": new_pts, "tier": compute_tier(new_pts)}


# ---------------------------------------------------------------------------
# Cashier & Kitchen (KDS) Live Operations APIs
# ---------------------------------------------------------------------------

@app.get("/api/ops/orders")
def list_ops_orders(
    brand_id: Optional[int] = None,
    branch_id: Optional[int] = None,
    status: Optional[str] = None,
    sess: Dict[str, Any] = Depends(require_staff),
):
    eff_brand = brand_id if sess["role"] == "super_admin" else sess["brand_id"]
    eff_branch = branch_id if sess["role"] in ("super_admin", "brand_admin") else sess["branch_id"]

    clauses = []
    params: List[Any] = []
    if eff_brand:
        clauses.append("brand_id = ?")
        params.append(eff_brand)
    if eff_branch:
        clauses.append("branch_id = ?")
        params.append(eff_branch)
    if status and status != "all":
        if status == "active":
            clauses.append("status IN ('new','accepted','preparing','ready','out_for_delivery')")
        else:
            clauses.append("status = ?")
            params.append(status)

    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db_session() as conn:
        rows = conn.execute(f"SELECT * FROM orders {where_sql} ORDER BY id DESC LIMIT 100", params).fetchall()
        return {"orders": [_serialize_order(conn, r) for r in rows]}


@app.patch("/api/ops/orders/{order_id}/status")
async def update_ops_order_status(order_id: int, payload: OrderStatusUpdate, sess: Dict[str, Any] = Depends(require_staff)):
    with db_session() as conn:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="الطلب غير موجود")
        assert_branch_access(sess, order["brand_id"], order["branch_id"])

        new_status = payload.status or order["status"]
        new_kstatus = payload.kitchen_status or order["kitchen_status"]
        new_pstatus = payload.payment_status or order["payment_status"]

        # Synchronize kitchen & main order status intelligently
        if payload.kitchen_status == "preparing" and new_status in ("new", "accepted"):
            new_status = "preparing"
        elif payload.kitchen_status == "ready" and new_status in ("new", "accepted", "preparing"):
            new_status = "ready"
        elif payload.status == "preparing" and new_kstatus == "pending":
            new_kstatus = "preparing"
        elif payload.status == "ready":
            new_kstatus = "ready"
        elif payload.status == "completed":
            new_kstatus = "served"
            if order["payment_method"] == "cash":
                new_pstatus = "paid"

        new_est_min = int(order["estimated_minutes"] or 10)
        new_est_at = order["estimated_ready_at"] or ""
        if payload.extra_minutes and payload.extra_minutes > 0:
            new_est_min += int(payload.extra_minutes)
            try:
                base_dt = datetime.fromisoformat(new_est_at) if new_est_at else datetime.now(timezone.utc)
            except Exception:
                base_dt = datetime.now(timezone.utc)
            if base_dt < datetime.now(timezone.utc):
                base_dt = datetime.now(timezone.utc)
            new_est_at = (base_dt + timedelta(minutes=int(payload.extra_minutes))).isoformat()

        conn.execute(
            "UPDATE orders SET status = ?, kitchen_status = ?, payment_status = ?, estimated_minutes = ?, estimated_ready_at = ?, updated_at = ? WHERE id = ?",
            (new_status, new_kstatus, new_pstatus, new_est_min, new_est_at, utc_now_iso(), order_id),
        )
        log_audit(
            conn,
            "staff",
            sess["user_id"],
            sess["role"],
            "update_order_status",
            {"order_id": order_id, "status": new_status, "kitchen_status": new_kstatus, "payment_status": new_pstatus},
            order["brand_id"],
            order["branch_id"],
        )
        updated = _serialize_order(conn, conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())

    await broker.publish(
        "order_updated",
        updated,
        brand_id=updated["brand_id"],
        branch_id=updated["branch_id"],
        order_number=updated["order_number"],
    )
    return {"order": updated}


# ---------------------------------------------------------------------------
# Real-time Server-Sent Events (SSE)
# ---------------------------------------------------------------------------

@app.get("/api/stream")
async def realtime_stream(
    request: Request,
    brand_id: Optional[int] = None,
    branch_id: Optional[int] = None,
    order_number: Optional[str] = None,
):
    q = broker.subscribe(brand_id=brand_id, branch_id=branch_id, order_number=order_number)

    async def event_gen():
        try:
            yield f"event: connected\ndata: {json.dumps({'ok': True, 'ts': utc_now_iso()})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            broker.unsubscribe(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Support Tickets
# ---------------------------------------------------------------------------

@app.post("/api/support/tickets")
def create_support_ticket(payload: SupportTicketCreate):
    with db_session() as conn:
        cur = conn.execute(
            "INSERT INTO support_tickets (name, phone, topic, message, status, created_at) VALUES (?, ?, ?, ?, 'open', ?)",
            (payload.name.strip(), payload.phone.strip(), payload.topic.strip(), payload.message.strip(), utc_now_iso()),
        )
        return {"ticket_id": cur.lastrowid, "ok": True}


# ---------------------------------------------------------------------------
# Static Assets & PWA Frontend Serving
# ---------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/manifest.json")
def serve_manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/{icon_name}.png")
def serve_root_icons(icon_name: str):
    icon_path = STATIC_DIR / "icons" / f"{icon_name}.png"
    if icon_path.exists():
        return FileResponse(icon_path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Not found")


@app.get("/")
def serve_spa():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")
