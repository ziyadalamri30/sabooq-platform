import hashlib
import hmac
import json
import os
import secrets
from typing import Any, Dict, Tuple


PAYMENT_MODE = os.environ.get("SABOOQ_PAYMENT_MODE", "sandbox")  # sandbox | live
PAYMENT_PROVIDER = os.environ.get("SABOOQ_PAYMENT_PROVIDER", "moyasar")  # moyasar | tap | hyperpay
MOYASAR_SECRET_KEY = os.environ.get("MOYASAR_SECRET_KEY", "")
TAP_SECRET_KEY = os.environ.get("TAP_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("SABOOQ_PAYMENT_WEBHOOK_SECRET", "sabooq-dev-webhook-secret")


def initiate_payment(order_number: str, amount_sar: float, method: str, customer_name: str = "") -> Dict[str, Any]:
    """
    Prepares a payment session for Moyasar / Tap / HyperPay (Mada, Apple Pay, Credit Card),
    Wallet, or Cash. In sandbox mode, returns an instant-confirmable payment intent.
    """
    tx_id = f"PAY-{secrets.token_hex(6).upper()}"
    amount_halalas = int(round(amount_sar * 100))

    if method == "cash":
        return {
            "provider": "cash",
            "method": "cash",
            "gateway_tx_id": tx_id,
            "status": "pending",
            "requires_action": False,
            "message": "سيتم التحصيل نقداً عند الاستلام أو من الكاشير",
            "amount_sar": amount_sar,
            "currency": "SAR",
        }

    if method == "wallet":
        return {
            "provider": "wallet",
            "method": "wallet",
            "gateway_tx_id": tx_id,
            "status": "paid",
            "requires_action": False,
            "message": "تم الخصم من محفظة سبوق بنجاح",
            "amount_sar": amount_sar,
            "currency": "SAR",
        }

    # Electronic gateway (Apple Pay / Mada / Credit Card)
    if PAYMENT_MODE == "sandbox" or not (MOYASAR_SECRET_KEY or TAP_SECRET_KEY):
        return {
            "provider": f"{PAYMENT_PROVIDER}_sandbox",
            "method": method,
            "gateway_tx_id": tx_id,
            "status": "authorized",
            "requires_action": False,
            "sandbox": True,
            "amount_sar": amount_sar,
            "amount_halalas": amount_halalas,
            "currency": "SAR",
            "callback_url": f"/api/payments/webhook/{PAYMENT_PROVIDER}",
            "message": f"تم تفويض الدفع الإلكتروني ({method.upper()}) عبر بوابة {PAYMENT_PROVIDER} التجريبية",
        }

    # Live configuration metadata for frontend SDK (Moyasar / Tap)
    return {
        "provider": PAYMENT_PROVIDER,
        "method": method,
        "gateway_tx_id": tx_id,
        "status": "initiated",
        "requires_action": True,
        "sandbox": False,
        "amount_sar": amount_sar,
        "amount_halalas": amount_halalas,
        "currency": "SAR",
        "description": f"Sabooq Order {order_number} - {customer_name}",
        "callback_url": f"/api/payments/webhook/{PAYMENT_PROVIDER}",
        "message": "جاهز لإتمام الدفع عبر البوابة المعتمدة",
    }


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> Tuple[bool, str]:
    """Verifies HMAC-SHA256 webhook signature from payment gateway."""
    if not signature_header:
        return (PAYMENT_MODE == "sandbox"), "sandbox_bypass" if PAYMENT_MODE == "sandbox" else "missing_signature"
    expected = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    ok = hmac.compare_digest(expected, signature_header.strip())
    return ok, "verified" if ok else "invalid_signature"


def payment_gateway_status() -> Dict[str, Any]:
    return {
        "mode": PAYMENT_MODE,
        "default_provider": PAYMENT_PROVIDER,
        "supported_providers": ["moyasar", "tap", "hyperpay"],
        "supported_methods": [
            {"id": "apple_pay", "label_ar": "Apple Pay", "electronic": True},
            {"id": "mada", "label_ar": "مدى (Mada)", "electronic": True},
            {"id": "card", "label_ar": "بطاقة ائتمانية (Visa / Mastercard)", "electronic": True},
            {"id": "wallet", "label_ar": "محفظة سبوق", "electronic": True},
            {"id": "cash", "label_ar": "نقدي عند الاستلام", "electronic": False},
        ],
        "webhook_endpoint": "/api/payments/webhook/{provider}",
        "live_keys_configured": bool(MOYASAR_SECRET_KEY or TAP_SECRET_KEY),
    }
