import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import qrcode
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


HOST = os.getenv("IMAGE_BILLING_HOST", "0.0.0.0")
PORT = int(os.getenv("IMAGE_BILLING_PORT", "8791"))
ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("IMAGE_BILLING_DB", str(ROOT / "data" / "billing.db")))
CACHE_DIR = Path(os.getenv("IMAGE_BILLING_CACHE", str(ROOT / "data" / "responses")))
WEB_DIR = Path(os.getenv("IMAGE_BILLING_WEB", str(ROOT / "web")))
SERVICE_API_KEY = os.getenv("IMAGE_BILLING_SERVICE_API_KEY", "").strip()
WORKER_API_KEY = os.getenv("IMAGE_OPTIMIZER_API_KEY", "").strip()
WORKER_URL = os.getenv("IMAGE_OPTIMIZER_WORKER_URL", "http://127.0.0.1:8789/v1/images/optimize").strip()
PASSWORD_HASH = os.getenv("IMAGE_BILLING_PASSWORD_HASH", "").strip()
PUBLIC_BASE_URL = os.getenv("IMAGE_BILLING_PUBLIC_BASE_URL", "https://image-optimizer.tietiezhi.xyz").rstrip("/")
PAYMENT_CONFIG_PATH = Path(os.getenv("IMAGE_BILLING_PAYMENT_CONFIG", str(ROOT / "payment.json")))
MAX_BODY_BYTES = 48 * 1024 * 1024
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
MAX_EDGE = 3_840
DB_LOCK = threading.RLock()
VMQ_SIGN_TYPE = "HMAC_SHA256"
VMQ_CALLBACK_MAX_SKEW_SECONDS = 20 * 60


def env_int(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def env_float(name, default, minimum, maximum):
    try:
        value = float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


# HTTP requests may run concurrently; the worker/GPU limit remains separately configurable.
OPTIMIZE_MAX_CONCURRENCY = env_int("IMAGE_OPTIMIZER_MAX_CONCURRENCY", 16, 1, 32)
OPTIMIZE_MAX_QUEUE = env_int("IMAGE_OPTIMIZER_MAX_QUEUE", 64, 0, 4096)
OPTIMIZE_QUEUE_TIMEOUT_SECONDS = env_float(
    "IMAGE_OPTIMIZER_QUEUE_TIMEOUT_SECONDS", 30.0, 0.0, 600.0
)
OPTIMIZE_WORKER_TIMEOUT_SECONDS = env_float(
    "IMAGE_OPTIMIZER_WORKER_TIMEOUT_SECONDS", 180.0, 0.0, 900.0
)
OPTIMIZE_HTTP_IO_TIMEOUT_SECONDS = env_float(
    "IMAGE_OPTIMIZER_HTTP_IO_TIMEOUT_SECONDS", 60.0, 5.0, 600.0
)
OPTIMIZE_RESPONSE_WRITE_TIMEOUT_SECONDS = env_float(
    "IMAGE_OPTIMIZER_RESPONSE_WRITE_TIMEOUT_SECONDS", 300.0, 5.0, 600.0
)
OPTIMIZE_INGRESS_TIMEOUT_SECONDS = env_float(
    "IMAGE_OPTIMIZER_INGRESS_TIMEOUT_SECONDS", 300.0, 5.0, 600.0
)
OPTIMIZE_SLOTS = threading.BoundedSemaphore(OPTIMIZE_MAX_CONCURRENCY)
OPTIMIZE_METRICS_LOCK = threading.Lock()
OPTIMIZE_ACTIVE = 0
OPTIMIZE_QUEUED = 0


class RequestBodyTimeout(TimeoutError):
    """The complete optimizer request body exceeded its bounded ingress budget."""


def acquire_optimize_slot():
    """Reserve one bounded optimizer slot, rejecting an overloaded queue quickly."""
    global OPTIMIZE_QUEUED, OPTIMIZE_ACTIVE
    with OPTIMIZE_METRICS_LOCK:
        if OPTIMIZE_ACTIVE + OPTIMIZE_QUEUED >= OPTIMIZE_MAX_CONCURRENCY + OPTIMIZE_MAX_QUEUE:
            return False
        OPTIMIZE_QUEUED += 1
    try:
        if OPTIMIZE_QUEUE_TIMEOUT_SECONDS > 0:
            acquired = OPTIMIZE_SLOTS.acquire(timeout=OPTIMIZE_QUEUE_TIMEOUT_SECONDS)
        else:
            acquired = OPTIMIZE_SLOTS.acquire()
    except Exception:
        acquired = False
    with OPTIMIZE_METRICS_LOCK:
        OPTIMIZE_QUEUED = max(0, OPTIMIZE_QUEUED - 1)
        if acquired:
            OPTIMIZE_ACTIVE += 1
    return acquired


def release_optimize_slot():
    """Release one optimizer slot and keep runtime metrics consistent."""
    global OPTIMIZE_ACTIVE
    OPTIMIZE_SLOTS.release()
    with OPTIMIZE_METRICS_LOCK:
        OPTIMIZE_ACTIVE = max(0, OPTIMIZE_ACTIVE - 1)


def optimizer_runtime_status():
    """Return non-sensitive concurrency counters for health and operations checks."""
    with OPTIMIZE_METRICS_LOCK:
        admission_capacity = OPTIMIZE_MAX_CONCURRENCY + OPTIMIZE_MAX_QUEUE
        admission_in_flight = OPTIMIZE_ACTIVE + OPTIMIZE_QUEUED
        return {
            "concurrency": OPTIMIZE_MAX_CONCURRENCY,
            "active": OPTIMIZE_ACTIVE,
            "queued": OPTIMIZE_QUEUED,
            "queue_limit": OPTIMIZE_MAX_QUEUE,
            "admission_capacity": admission_capacity,
            "admission_available": admission_in_flight < admission_capacity,
            "saturated": admission_in_flight >= admission_capacity,
            "ingress_timeout_seconds": OPTIMIZE_INGRESS_TIMEOUT_SECONDS,
        }


def now_ts():
    return int(time.time())


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                balance_micro INTEGER NOT NULL DEFAULT 0,
                reserved_micro INTEGER NOT NULL DEFAULT 0,
                total_topup_micro INTEGER NOT NULL DEFAULT 0,
                total_spend_micro INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO account(id, updated_at) VALUES(1, unixepoch());
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                tier TEXT NOT NULL,
                price_micro INTEGER NOT NULL,
                source_width INTEGER NOT NULL DEFAULT 0,
                source_height INTEGER NOT NULL DEFAULT 0,
                target_width INTEGER NOT NULL,
                target_height INTEGER NOT NULL,
                operation TEXT NOT NULL DEFAULT '',
                route TEXT NOT NULL DEFAULT '',
                queue_ms INTEGER NOT NULL DEFAULT 0,
                process_ms INTEGER NOT NULL DEFAULT 0,
                total_ms INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                response_path TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                finished_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                amount_micro INTEGER NOT NULL,
                balance_after_micro INTEGER NOT NULL,
                ref_type TEXT NOT NULL,
                ref_id TEXT NOT NULL,
                remark TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                UNIQUE(ref_type, ref_id, kind)
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_created ON ledger(created_at DESC);
            CREATE TABLE IF NOT EXISTS topup_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                amount_micro INTEGER NOT NULL,
                pay_amount_cents INTEGER NOT NULL,
                status INTEGER NOT NULL DEFAULT 0,
                provider_txn_id TEXT NOT NULL DEFAULT '',
                qr_code TEXT NOT NULL DEFAULT '',
                pay_url TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                paid_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )


def payment_config():
    if not PAYMENT_CONFIG_PATH.is_file():
        return {}
    return json.loads(PAYMENT_CONFIG_PATH.read_text(encoding="utf-8"))


def parse_cny_cents(value):
    raw = str(value).strip()
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", raw):
        raise ValueError("支付金额格式非法")
    whole, _, fraction = raw.partition(".")
    return int(whole) * 100 + int(fraction.ljust(2, "0") or "0")


def format_cny_cents(cents):
    return f"{int(cents) // 100}.{int(cents) % 100:02d}"


def vmq_hmac(params, secret):
    canonical = "&".join(f"{key}={params[key]}" for key in sorted(params) if key.lower() != "sign")
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def vmq_pay_type(provider):
    if provider == "wechat":
        return 1
    if provider == "alipay":
        return 2
    raise ValueError("不支持的支付方式")


def require_vmq_config(provider):
    cfg = payment_config()
    base_url = str(cfg.get("vmq_base_url", "")).strip().rstrip("/")
    communication_key = str(cfg.get("vmq_communication_key", "")).strip()
    enabled = bool(cfg.get(f"vmq_{provider}_enabled", False))
    parsed = urllib.parse.urlparse(base_url)
    if not enabled or parsed.scheme != "https" or not parsed.netloc or not communication_key:
        raise ValueError(("支付宝" if provider == "alipay" else "微信") + "支付暂未启用")
    if len(communication_key) < 32 or len(communication_key) > 256:
        raise ValueError("VMQ 通信密钥配置非法")
    return base_url, communication_key


def request_vmq_order(order_no, provider, amount_cents):
    base_url, communication_key = require_vmq_config(provider)
    pay_type = vmq_pay_type(provider)
    params = {
        "payId": order_no,
        "param": order_no,
        "type": str(pay_type),
        "price": format_cny_cents(amount_cents),
        "signType": VMQ_SIGN_TYPE,
        "timestamp": str(int(time.time() * 1000)),
        "nonce": secrets.token_urlsafe(24)[:32],
    }
    params["sign"] = vmq_hmac(params, communication_key)
    form = dict(params)
    form.update({
        "notifyUrl": PUBLIC_BASE_URL + "/pay/vmq/notify",
        "returnUrl": PUBLIC_BASE_URL + "/",
        "isHtml": "0",
    })
    request = urllib.request.Request(
        base_url + "/createOrder",
        data=urllib.parse.urlencode(form).encode(),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "terln-image-optimizer/VMQ",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read(1024 * 1024))
    if int(payload.get("code", 0)) != 1:
        raise ValueError("VMQ 创建订单失败: " + str(payload.get("msg", "未知错误"))[:160])
    data = payload.get("data") or {}
    if data.get("payId") != order_no or int(data.get("payType", 0)) != pay_type or int(data.get("state", -1)) != 0:
        raise ValueError("VMQ 返回的订单信息不匹配")
    if parse_cny_cents(data.get("price", "")) != amount_cents:
        raise ValueError("VMQ 返回的订单原始金额不匹配")
    really_cents = parse_cny_cents(data.get("reallyPrice", ""))
    pay_url = str(data.get("payUrl", "")).strip()
    provider_order_id = str(data.get("orderId", "")).strip()
    if really_cents < amount_cents or not pay_url or not provider_order_id:
        raise ValueError("VMQ 未返回可用收款码")
    return really_cents, pay_url, provider_order_id


def handle_vmq_notify(query):
    required = {"payId", "param", "type", "price", "reallyPrice", "eventId", "timestamp", "nonce", "signType", "sign"}
    if set(query) != required or any(len(query[key]) != 1 or not query[key][0].strip() for key in required):
        raise ValueError("VMQ 回调参数集合不合法")
    values = {key: query[key][0] for key in required}
    if values["signType"].upper() != VMQ_SIGN_TYPE:
        raise ValueError("VMQ 回调签名类型不支持")
    if not 16 <= len(values["nonce"]) <= 128 or len(values["eventId"]) > 128:
        raise ValueError("VMQ 回调事件标识非法")
    timestamp = int(values["timestamp"])
    if abs(int(time.time() * 1000) - timestamp) > VMQ_CALLBACK_MAX_SKEW_SECONDS * 1000:
        raise ValueError("VMQ 回调时间戳无效")
    cfg = payment_config()
    communication_key = str(cfg.get("vmq_communication_key", "")).strip()
    signed = {key: value for key, value in values.items() if key != "sign"}
    if not communication_key or not hmac.compare_digest(values["sign"].lower(), vmq_hmac(signed, communication_key)):
        raise ValueError("VMQ 回调签名无效")
    order_no = values["payId"]
    if values["param"] != order_no:
        raise ValueError("VMQ 回调透传参数不匹配")
    with connect_db() as db:
        order = db.execute("SELECT * FROM topup_orders WHERE order_no=?", (order_no,)).fetchone()
    if not order:
        raise ValueError("充值订单不存在")
    if int(values["type"]) != vmq_pay_type(order["provider"]):
        raise ValueError("VMQ 回调支付方式不匹配")
    original_cents = (int(order["amount_micro"]) + 5_000) // 10_000
    really_cents = parse_cny_cents(values["reallyPrice"])
    if parse_cny_cents(values["price"]) != original_cents:
        raise ValueError("VMQ 回调原始金额不匹配")
    if really_cents != int(order["pay_amount_cents"]):
        raise ValueError("VMQ 回调实际支付金额不匹配")
    settle_topup(order["provider"], order_no, values["eventId"], really_cents)


def size_price(width, height):
    if width <= 0 or height <= 0:
        raise ValueError("图片宽高必须大于 0")
    pixels = width * height
    if pixels < MIN_PIXELS:
        raise ValueError("图片总像素不能小于 655360")
    if pixels > MAX_PIXELS:
        raise ValueError("图片总像素不能大于 8294400")
    if max(width, height) > MAX_EDGE:
        raise ValueError("图片最大边不能超过 3840")
    if width > height * 3 or height > width * 3:
        raise ValueError("图片比例必须在 1:3 到 3:1 之间")
    if pixels > 2_480 * 2_480:
        return "4K", 64_000
    if pixels > 1_240 * 1_240:
        return "2K", 48_000
    return "1K", 32_000


def password_matches(password):
    try:
        algorithm, iterations, salt_b64, digest_b64 = PASSWORD_HASH.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def create_session():
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = now_ts()
    with DB_LOCK, connect_db() as db:
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        db.execute("INSERT INTO sessions(token_hash,expires_at,created_at) VALUES(?,?,?)", (digest, now + SESSION_TTL_SECONDS, now))
    return token


def valid_session(token):
    if not token:
        return False
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connect_db() as db:
        return db.execute("SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>?", (digest, now_ts())).fetchone() is not None


def delete_session(token):
    if not token:
        return
    with DB_LOCK, connect_db() as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))


def reserve_job(request_id, width, height, route):
    tier, price = size_price(width, height)
    now = now_ts()
    with DB_LOCK, connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
        if existing:
            db.execute("COMMIT")
            return dict(existing), True
        account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
        if account["balance_micro"] < price:
            db.execute("ROLLBACK")
            raise PermissionError("余额不足")
        db.execute(
            "UPDATE account SET balance_micro=balance_micro-?,reserved_micro=reserved_micro+?,updated_at=? WHERE id=1",
            (price, price, now),
        )
        cursor = db.execute(
            "INSERT INTO jobs(request_id,status,tier,price_micro,target_width,target_height,route,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (request_id, "queued", tier, price, width, height, route, now),
        )
        job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (cursor.lastrowid,)).fetchone())
        db.execute("COMMIT")
        return job, False


def begin_job(job_id, queued_at):
    started = now_ts()
    queue_ms = max(0, int((time.monotonic() - queued_at) * 1000))
    with DB_LOCK, connect_db() as db:
        db.execute("UPDATE jobs SET status='processing',started_at=?,queue_ms=? WHERE id=?", (started, queue_ms, job_id))


def settle_job(job, worker_result, response_bytes, process_ms, total_ms):
    cache_path = CACHE_DIR / f"{job['request_id']}.json"
    cache_path.write_bytes(response_bytes)
    now = now_ts()
    with DB_LOCK, connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if current and current["status"] == "success":
            db.execute("COMMIT")
            return
        account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
        balance_after = account["balance_micro"]
        db.execute(
            "UPDATE account SET reserved_micro=MAX(0,reserved_micro-?),total_spend_micro=total_spend_micro+?,updated_at=? WHERE id=1",
            (job["price_micro"], job["price_micro"], now),
        )
        db.execute(
            "INSERT OR IGNORE INTO ledger(kind,amount_micro,balance_after_micro,ref_type,ref_id,remark,created_at) VALUES('charge',?,?,?,?,?,?)",
            (-job["price_micro"], balance_after, "job", job["request_id"], f"{job['tier']} 图片优化", now),
        )
        db.execute(
            """UPDATE jobs SET status='success',source_width=?,source_height=?,operation=?,process_ms=?,total_ms=?,response_path=?,finished_at=? WHERE id=?""",
            (
                int(worker_result.get("source_width", 0)),
                int(worker_result.get("source_height", 0)),
                str(worker_result.get("operation", "")),
                process_ms,
                total_ms,
                str(cache_path),
                now,
                job["id"],
            ),
        )
        db.execute("COMMIT")


def fail_job(job, message, process_ms, total_ms):
    now = now_ts()
    with DB_LOCK, connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if current and current["status"] not in ("success", "failed"):
            db.execute(
                "UPDATE account SET balance_micro=balance_micro+?,reserved_micro=MAX(0,reserved_micro-?),updated_at=? WHERE id=1",
                (job["price_micro"], job["price_micro"], now),
            )
            db.execute(
                "UPDATE jobs SET status='failed',error=?,process_ms=?,total_ms=?,finished_at=? WHERE id=?",
                (message[:1000], process_ms, total_ms, now, job["id"]),
            )
        db.execute("COMMIT")


def call_worker(payload):
    req = urllib.request.Request(WORKER_URL, data=payload, method="POST")
    req.add_header("Authorization", "Bearer " + WORKER_API_KEY)
    req.add_header("Content-Type", "application/json")
    try:
        urlopen_options = {}
        if OPTIMIZE_WORKER_TIMEOUT_SECONDS > 0:
            urlopen_options["timeout"] = OPTIMIZE_WORKER_TIMEOUT_SECONDS
        with urllib.request.urlopen(req, **urlopen_options) as response:
            body = response.read(64 * 1024 * 1024)
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(1024 * 1024)


def clamp_page(value, default=1):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def clamp_page_size(value, default=10):
    try:
        return min(50, max(5, int(value)))
    except (TypeError, ValueError):
        return default


def dashboard_data(query):
    jobs_page = clamp_page(query.get("jobs_page", ["1"])[0])
    ledger_page = clamp_page(query.get("ledger_page", ["1"])[0])
    orders_page = clamp_page(query.get("orders_page", ["1"])[0])
    page_size = clamp_page_size(query.get("page_size", ["10"])[0])
    today_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with connect_db() as db:
        account = dict(db.execute("SELECT * FROM account WHERE id=1").fetchone())
        stats = dict(
            db.execute(
                """SELECT
                SUM(CASE WHEN created_at>=? AND status='success' THEN 1 ELSE 0 END) AS today_success,
                SUM(CASE WHEN created_at>=? AND status='failed' THEN 1 ELSE 0 END) AS today_failed,
                SUM(CASE WHEN created_at>=? AND status='success' THEN price_micro ELSE 0 END) AS today_spend_micro,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS total_success,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS total_failed,
                COALESCE(AVG(CASE WHEN status='success' THEN queue_ms END),0) AS avg_queue_ms,
                COALESCE(AVG(CASE WHEN status='success' THEN process_ms END),0) AS avg_process_ms,
                SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued,
                SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END) AS processing
                FROM jobs""",
                (today_start, today_start, today_start),
            ).fetchone()
        )
        stats = {key: value if value is not None else 0 for key, value in stats.items()}
        totals = {
            "jobs": db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "ledger": db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],
            "orders": db.execute("SELECT COUNT(*) FROM topup_orders").fetchone()[0],
        }
        jobs = [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ? OFFSET ?", (page_size, (jobs_page - 1) * page_size))]
        ledger = [dict(row) for row in db.execute("SELECT * FROM ledger ORDER BY id DESC LIMIT ? OFFSET ?", (page_size, (ledger_page - 1) * page_size))]
        orders = [dict(row) for row in db.execute("SELECT * FROM topup_orders ORDER BY id DESC LIMIT ? OFFSET ?", (page_size, (orders_page - 1) * page_size))]
        trend = [
            dict(row)
            for row in db.execute(
                """WITH RECURSIVE days(day, n) AS (
                    SELECT date('now','localtime','-6 days'), 0
                    UNION ALL SELECT date(day,'+1 day'), n+1 FROM days WHERE n<6
                )
                SELECT days.day,
                    SUM(CASE WHEN jobs.status='success' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN jobs.status='failed' THEN 1 ELSE 0 END) AS failed,
                    COALESCE(SUM(CASE WHEN jobs.status='success' THEN jobs.price_micro ELSE 0 END),0) AS spend_micro,
                    COALESCE(AVG(CASE WHEN jobs.status='success' THEN jobs.process_ms END),0) AS avg_process_ms
                FROM days LEFT JOIN jobs ON date(jobs.created_at,'unixepoch','localtime')=days.day
                GROUP BY days.day ORDER BY days.day"""
            )
        ]
        tiers = [dict(row) for row in db.execute("SELECT tier,COUNT(*) AS count FROM jobs WHERE status='success' GROUP BY tier ORDER BY tier")]
        routes = [dict(row) for row in db.execute("SELECT route,COUNT(*) AS count FROM jobs WHERE status='success' GROUP BY route ORDER BY count DESC")]
    attempts = int(stats.get("total_success") or 0) + int(stats.get("total_failed") or 0)
    stats["success_rate"] = round((int(stats.get("total_success") or 0) * 100 / attempts), 1) if attempts else 0
    return {
        "account": account,
        "stats": stats,
        "jobs": jobs,
        "ledger": ledger,
        "orders": orders,
        "charts": {"trend": trend, "tiers": tiers, "routes": routes},
        "pagination": {
            "page_size": page_size,
            "jobs": {"page": jobs_page, "total": totals["jobs"]},
            "ledger": {"page": ledger_page, "total": totals["ledger"]},
            "orders": {"page": orders_page, "total": totals["orders"]},
        },
        "prices": {"1K": 32000, "2K": 48000, "4K": 64000},
    }


def load_private_key(value):
    raw = value.strip().encode()
    if b"BEGIN" not in raw:
        raw = b"-----BEGIN PRIVATE KEY-----\n" + raw + b"\n-----END PRIVATE KEY-----"
    return serialization.load_pem_private_key(raw, password=None)


def load_public_key(value):
    raw = value.strip().encode()
    if b"BEGIN" not in raw:
        raw = b"-----BEGIN PUBLIC KEY-----\n" + raw + b"\n-----END PUBLIC KEY-----"
    return serialization.load_pem_public_key(raw)


def alipay_sign(params, private_key):
    content = "&".join(f"{key}={params[key]}" for key in sorted(params) if params[key] != "")
    signature = load_private_key(private_key).sign(content.encode(), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode()


def alipay_verify(form, public_key):
    signature = form.get("sign", "")
    content = "&".join(f"{key}={form[key]}" for key in sorted(form) if key not in ("sign", "sign_type") and form[key] != "")
    load_public_key(public_key).verify(base64.b64decode(signature), content.encode(), padding.PKCS1v15(), hashes.SHA256())


def create_alipay_checkout(order_no, amount_cents, cfg):
    params = {
        "app_id": cfg["alipay_app_id"],
        "method": "alipay.trade.page.pay",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": PUBLIC_BASE_URL + "/pay/alipay/notify",
        "return_url": PUBLIC_BASE_URL + "/?pay_return=1&order_no=" + order_no,
        "biz_content": json.dumps(
            {
                "out_trade_no": order_no,
                "product_code": "FAST_INSTANT_TRADE_PAY",
                "total_amount": f"{amount_cents / 100:.2f}",
                "subject": "Image Optimizer 余额充值",
                "qr_pay_mode": "4",
                "qrcode_width": 200,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    }
    params["sign"] = alipay_sign(params, cfg["alipay_private_key"])
    url = cfg.get("alipay_gateway", "https://openapi.alipay.com/gateway.do") + "?" + urllib.parse.urlencode(params)
    qr = ""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            html = response.read(2 * 1024 * 1024).decode("utf-8", "ignore")
        match = re.search(r'<input[^>]*\bname="qrCode"[^>]*\bvalue="(https://qr\.alipay\.com/[^"]+)"', html)
        if match:
            qr = match.group(1)
    except Exception:
        pass
    return qr, url


def wechat_authorization(method, path, body, cfg):
    timestamp = str(now_ts())
    nonce = secrets.token_urlsafe(24)
    message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body.decode()}\n"
    signature = load_private_key(cfg["wechat_private_key"]).sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
    return (
        'WECHATPAY2-SHA256-RSA2048 '
        f'mchid="{cfg["wechat_mch_id"]}",nonce_str="{nonce}",signature="{base64.b64encode(signature).decode()}",'
        f'timestamp="{timestamp}",serial_no="{cfg["wechat_cert_serial"]}"'
    )


def create_wechat_checkout(order_no, amount_cents, cfg):
    path = "/v3/pay/transactions/native"
    body = json.dumps(
        {
            "appid": cfg["wechat_app_id"],
            "mchid": cfg["wechat_mch_id"],
            "description": "Image Optimizer 余额充值",
            "out_trade_no": order_no,
            "notify_url": PUBLIC_BASE_URL + "/pay/wechat/notify",
            "amount": {"total": amount_cents, "currency": "CNY"},
        },
        separators=(",", ":"),
    ).encode()
    req = urllib.request.Request("https://api.mch.weixin.qq.com" + path, data=body, method="POST")
    req.add_header("Authorization", wechat_authorization("POST", path, body, cfg))
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "terln-image-optimizer/1.0")
    with urllib.request.urlopen(req, timeout=10) as response:
        result = json.loads(response.read())
    if not result.get("code_url"):
        raise RuntimeError("微信响应缺少二维码")
    return result["code_url"]


def create_topup(provider, amount_micro):
    cfg = payment_config()
    if provider not in ("alipay", "wechat"):
        raise ValueError("不支持的支付方式")
    if amount_micro < 1_000_000 or amount_micro > 500_000_000:
        raise ValueError("充值金额须在 ¥1 到 ¥500 之间")
    amount_cents = (amount_micro + 5_000) // 10_000
    order_no = "IO" + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(6).upper()
    with DB_LOCK, connect_db() as db:
        db.execute(
            "INSERT INTO topup_orders(order_no,provider,amount_micro,pay_amount_cents,status,created_at) VALUES(?,?,?,?,0,?)",
            (order_no, provider, amount_micro, amount_cents, now_ts()),
        )
    try:
        if str(cfg.get("payment_backend", "official")).lower() == "vmq":
            really_cents, qr, provider_order_id = request_vmq_order(order_no, provider, amount_cents)
            pay_url = ""
            amount_cents = really_cents
        elif provider == "alipay":
            qr, pay_url = create_alipay_checkout(order_no, amount_cents, cfg)
            provider_order_id = ""
        else:
            qr, pay_url = create_wechat_checkout(order_no, amount_cents, cfg), ""
            provider_order_id = ""
        with DB_LOCK, connect_db() as db:
            db.execute(
                "UPDATE topup_orders SET pay_amount_cents=?,provider_txn_id=?,qr_code=?,pay_url=? WHERE order_no=?",
                (amount_cents, provider_order_id, qr, pay_url, order_no),
            )
        return {"order_no": order_no, "provider": provider, "amount_micro": amount_micro, "pay_amount_cents": amount_cents, "qr_code": qr, "pay_url": pay_url}
    except Exception:
        with DB_LOCK, connect_db() as db:
            db.execute("UPDATE topup_orders SET status=2 WHERE order_no=?", (order_no,))
        raise


def settle_topup(provider, order_no, txn_id, pay_cents):
    with DB_LOCK, connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        order = db.execute("SELECT * FROM topup_orders WHERE order_no=? AND provider=?", (order_no, provider)).fetchone()
        if not order:
            db.execute("ROLLBACK")
            raise ValueError("充值订单不存在")
        if order["pay_amount_cents"] != pay_cents:
            db.execute("ROLLBACK")
            raise ValueError("充值订单金额不匹配")
        if order["status"] == 1:
            db.execute("COMMIT")
            return
        if order["status"] != 0:
            db.execute("ROLLBACK")
            raise ValueError("充值订单状态不可入账")
        original_cents = (int(order["amount_micro"]) + 5_000) // 10_000
        # VMQ 在待支付阶段已保存其 orderId；官方直连订单直到支付前该字段为空。
        payment_adjustment_micro = max(pay_cents - original_cents, 0) * 10_000 if order["provider_txn_id"] else 0
        credited_micro = int(order["amount_micro"]) + payment_adjustment_micro
        now = now_ts()
        db.execute("UPDATE topup_orders SET status=1,provider_txn_id=?,paid_at=? WHERE id=?", (txn_id, now, order["id"]))
        db.execute(
            "UPDATE account SET balance_micro=balance_micro+?,total_topup_micro=total_topup_micro+?,updated_at=? WHERE id=1",
            (credited_micro, credited_micro, now),
        )
        balance = db.execute("SELECT balance_micro FROM account WHERE id=1").fetchone()[0]
        db.execute(
            "INSERT OR IGNORE INTO ledger(kind,amount_micro,balance_after_micro,ref_type,ref_id,remark,created_at) VALUES('topup',?,?,?,?,?,?)",
            (credited_micro, balance, "order", order_no, ("支付宝" if provider == "alipay" else "微信") + "充值", now),
        )
        db.execute("COMMIT")


class Handler(BaseHTTPRequestHandler):
    server_version = "terln-image-billing/1.0"

    def setup(self):
        super().setup()
        # 账单创建发生在完整请求体到达之后。没有读写超时的情况下，FRP/Caddy
        # 半开连接会让 rfile.read 或大图回写永久卡住，却不进入队列指标。
        self.connection.settimeout(OPTIMIZE_HTTP_IO_TIMEOUT_SECONDS)

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} {fmt % args}", flush=True)

    def send_json(self, status, payload, headers=None):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.write_response_bytes(raw)

    def write_response_bytes(self, raw):
        # Base64 PNG responses are several MiB. Keep the short idle deadline
        # for inbound HTTP work, but grant the outbound transfer the same
        # finite 5-minute budget as the gateway. Previously a 60-second
        # socket timeout could cut a 200 JSON response in the middle, which
        # the gateway then saw as an "unexpected end of JSON input".
        self.connection.settimeout(OPTIMIZE_RESPONSE_WRITE_TIMEOUT_SECONDS)
        try:
            self.wfile.write(raw)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError) as exc:
            self.close_connection = True
            print(
                f"response write failed bytes={len(raw)} error={exc!r}",
                flush=True,
            )
            return False
        finally:
            if not self.close_connection:
                self.connection.settimeout(OPTIMIZE_HTTP_IO_TIMEOUT_SECONDS)

    def error(self, status, message, code):
        self.send_json(status, {"error": {"message": message, "type": "invalid_request_error", "param": "", "code": code}})

    def read_body(self, limit=MAX_BODY_BYTES, deadline=None):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > limit:
            raise ValueError("请求体大小非法")
        # socket timeout only bounds a single idle read. Optimizer requests can
        # contain large Base64 images and may keep trickling through FRP/Caddy,
        # so enforce an independent wall-clock ingress budget as well.
        remaining = length
        chunks = []
        while remaining:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RequestBodyTimeout("图片优化请求上传超时")
                self.connection.settimeout(min(OPTIMIZE_HTTP_IO_TIMEOUT_SECONDS, left))
            try:
                chunk = self.rfile.read(min(64 * 1024, remaining))
            except socket.timeout as exc:
                raise RequestBodyTimeout("图片优化请求上传超时") from exc
            if not chunk:
                raise ValueError("请求体不完整")
            chunks.append(chunk)
            remaining -= len(chunk)
        self.connection.settimeout(OPTIMIZE_HTTP_IO_TIMEOUT_SECONDS)
        return b"".join(chunks)

    def session_token(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return jar.get("optimizer_session").value if jar.get("optimizer_session") else ""

    def require_session(self):
        if not valid_session(self.session_token()):
            self.error(401, "请先登录", "unauthorized")
            return False
        return True

    def route_name(self):
        host = self.headers.get("X-Forwarded-Host", self.headers.get("Host", ""))
        return "singapore" if host.endswith("tietiezhi.xyz") else "domestic"

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        if path == "/pay/vmq/notify":
            try:
                handle_vmq_notify(urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True))
                raw = b"success"
            except Exception as exc:
                print("vmq notify failed", repr(exc), flush=True)
                raw = b"fail"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path == "/health":
            worker_ok = False
            try:
                with urllib.request.urlopen("http://127.0.0.1:8789/health", timeout=2) as response:
                    worker_ok = response.status == 200
            except Exception:
                pass
            self.send_json(
                200 if worker_ok else 503,
                {
                    "status": "ok" if worker_ok else "degraded",
                    "worker": worker_ok,
                    "billing": True,
                    "queue_timeout_seconds": OPTIMIZE_QUEUE_TIMEOUT_SECONDS,
                    "worker_timeout_seconds": OPTIMIZE_WORKER_TIMEOUT_SECONDS,
                    "http_io_timeout_seconds": OPTIMIZE_HTTP_IO_TIMEOUT_SECONDS,
                    "response_write_timeout_seconds": OPTIMIZE_RESPONSE_WRITE_TIMEOUT_SECONDS,
                    **optimizer_runtime_status(),
                },
            )
            return
        if path == "/api/dashboard":
            if self.require_session():
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                header_params = {
                    "jobs_page": self.headers.get("X-Dashboard-Jobs-Page"),
                    "ledger_page": self.headers.get("X-Dashboard-Ledger-Page"),
                    "orders_page": self.headers.get("X-Dashboard-Orders-Page"),
                    "page_size": self.headers.get("X-Dashboard-Page-Size"),
                }
                for key, value in header_params.items():
                    if value is not None:
                        query[key] = [value]
                self.send_json(200, {"success": True, "data": dashboard_data(query)})
            return
        if path.startswith("/api/topup/orders/"):
            if not self.require_session():
                return
            order_no = path.rsplit("/", 1)[-1]
            with connect_db() as db:
                order = db.execute("SELECT * FROM topup_orders WHERE order_no=?", (order_no,)).fetchone()
            if not order:
                self.error(404, "订单不存在", "not_found")
            else:
                self.send_json(200, {"success": True, "data": dict(order)})
            return
        if path.startswith("/api/topup/qr/"):
            if not self.require_session():
                return
            order_no = path.rsplit("/", 1)[-1]
            with connect_db() as db:
                order = db.execute("SELECT qr_code FROM topup_orders WHERE order_no=?", (order_no,)).fetchone()
            if not order or not order["qr_code"]:
                self.error(404, "二维码不存在", "not_found")
                return
            output = io.BytesIO()
            qrcode.make(order["qr_code"]).save(output, "PNG")
            raw = output.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if path in ("/", "/index.html", "/login"):
            self.serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self.serve_file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
            return
        if path == "/style.css":
            self.serve_file(WEB_DIR / "style.css", "text/css; charset=utf-8")
            return
        self.error(404, "not found", "not_found")

    def serve_file(self, path, content_type):
        if not path.is_file():
            self.error(404, "not found", "not_found")
            return
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            try:
                body = json.loads(self.read_body(64 * 1024))
                if not password_matches(str(body.get("password", ""))):
                    self.error(401, "密码错误", "invalid_password")
                    return
                token = create_session()
                cookie = f"optimizer_session={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; HttpOnly; Secure; SameSite=Strict"
                self.send_json(200, {"success": True}, {"Set-Cookie": cookie})
            except Exception as exc:
                self.error(400, str(exc), "invalid_request")
            return
        if path == "/api/logout":
            delete_session(self.session_token())
            self.send_json(200, {"success": True}, {"Set-Cookie": "optimizer_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})
            return
        if path == "/api/topup":
            if not self.require_session():
                return
            try:
                body = json.loads(self.read_body(64 * 1024))
                checkout = create_topup(str(body.get("provider", "")), int(body.get("amount_micro", 0)))
                self.send_json(200, {"success": True, "data": checkout})
            except Exception as exc:
                self.error(400, str(exc), "topup_create_failed")
            return
        if path == "/pay/alipay/notify":
            try:
                form = {key: value[0] for key, value in urllib.parse.parse_qs(self.read_body(1024 * 1024).decode()).items()}
                cfg = payment_config()
                alipay_verify(form, cfg["alipay_public_key"])
                if form.get("app_id") != cfg["alipay_app_id"]:
                    raise ValueError("支付宝 app_id 不匹配")
                if form.get("trade_status") in ("TRADE_SUCCESS", "TRADE_FINISHED"):
                    cents = int(round(float(form.get("total_amount", "0")) * 100))
                    settle_topup("alipay", form.get("out_trade_no", ""), form.get("trade_no", ""), cents)
                self.send_response(200); self.end_headers(); self.wfile.write(b"success")
            except Exception as exc:
                print("alipay notify failed", repr(exc), flush=True)
                self.send_response(200); self.end_headers(); self.wfile.write(b"fail")
            return
        if path == "/pay/wechat/notify":
            try:
                envelope = json.loads(self.read_body(1024 * 1024))
                resource = envelope["resource"]
                cfg = payment_config()
                plain = AESGCM(cfg["wechat_api_v3_key"].encode()).decrypt(
                    resource["nonce"].encode(), base64.b64decode(resource["ciphertext"]), resource.get("associated_data", "").encode()
                )
                txn = json.loads(plain)
                if txn.get("mchid") != cfg["wechat_mch_id"] or txn.get("appid") not in ("", cfg["wechat_app_id"]):
                    raise ValueError("微信商户信息不匹配")
                if txn.get("trade_state") == "SUCCESS":
                    settle_topup("wechat", txn["out_trade_no"], txn.get("transaction_id", ""), int(txn["amount"]["total"]))
                self.send_json(200, {"code": "SUCCESS", "message": "成功"})
            except Exception as exc:
                print("wechat notify failed", repr(exc), flush=True)
                self.send_json(500, {"code": "FAIL", "message": str(exc)})
            return
        if path == "/v1/images/optimize":
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not SERVICE_API_KEY or not hmac.compare_digest(supplied, SERVICE_API_KEY):
                self.error(401, "unauthorized", "unauthorized")
                return
            request_id = self.headers.get("X-Request-ID", "").strip() or secrets.token_hex(16)
            started = time.monotonic()
            job = None
            slot_acquired = False
            try:
                # Apply backpressure before consuming a large body. Previously
                # every HTTP handler read the whole Base64 image before it hit
                # the bounded optimizer queue, so concurrent slow uploads could
                # saturate the FRP path while health still reported active=0.
                if not acquire_optimize_slot():
                    self.close_connection = True
                    self.error(503, "优化服务繁忙，请稍后重试", "optimizer_busy")
                    return
                slot_acquired = True
                payload = self.read_body(
                    deadline=time.monotonic() + OPTIMIZE_INGRESS_TIMEOUT_SECONDS
                )
                body = json.loads(payload)
                width, height = int(body.get("target_width", 0)), int(body.get("target_height", 0))
                job, duplicate = reserve_job(request_id, width, height, self.route_name())
                if duplicate:
                    if job["status"] == "success" and job["response_path"] and Path(job["response_path"]).is_file():
                        raw = Path(job["response_path"]).read_bytes()
                        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers()
                        self.write_response_bytes(raw)
                        return
                    self.error(409, "相同请求正在处理或已失败", "duplicate_request")
                    return
                queued_at = time.monotonic()
                begin_job(job["id"], queued_at)
                processing = time.monotonic()
                status, raw = call_worker(payload)
                process_ms = int((time.monotonic() - processing) * 1000)
                if status < 200 or status >= 300:
                    raise RuntimeError(f"worker HTTP {status}: {raw[:500].decode('utf-8','ignore')}")
                result = json.loads(raw)
                if int(result.get("width", 0)) != width or int(result.get("height", 0)) != height:
                    raise RuntimeError("worker 返回尺寸不匹配")
                total_ms = int((time.monotonic() - started) * 1000)
                result["billing"] = {"tier": job["tier"], "price_micro": job["price_micro"], "request_id": request_id}
                response_bytes = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
                settle_job(job, result, response_bytes, process_ms, total_ms)
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(response_bytes))); self.end_headers()
                self.write_response_bytes(response_bytes)
            except RequestBodyTimeout:
                if job:
                    fail_job(job, "图片优化请求上传超时", 0, int((time.monotonic() - started) * 1000))
                self.close_connection = True
                self.error(408, "图片优化请求上传超时，请重试", "request_body_timeout")
            except PermissionError:
                self.error(402, "图片优化账户余额不足", "insufficient_balance")
            except Exception as exc:
                if job:
                    fail_job(job, str(exc), 0, int((time.monotonic() - started) * 1000))
                self.error(500, str(exc), "optimizer_failed")
            finally:
                if slot_acquired:
                    release_optimize_slot()
            return
        self.error(404, "not found", "not_found")


if __name__ == "__main__":
    if not SERVICE_API_KEY or not WORKER_API_KEY or not PASSWORD_HASH:
        raise SystemExit("service API key, worker API key and password hash are required")
    init_db()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
