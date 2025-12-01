from __future__ import annotations
import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, make_response, session, abort
)
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
import urllib.request, urllib.parse, html

# ----------------------------------------------------------------------------
# Paths & app
# ----------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app = Flask(__name__, template_folder=str(TEMPLATES_DIR), static_folder=str(STATIC_DIR))
# SECRET_KEY: если не задан, генерируем новый при каждом запуске, чтобы инвалидировать старые сессии
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    _secret = os.urandom(32)
app.secret_key = _secret
# Сессия только на время браузерной сессии (не "запоминать" логин)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # В проде включи True (HTTPS):
    SESSION_COOKIE_SECURE=False,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

MENU_PATH = DATA_DIR / "menu.json"
BOOKINGS_CSV = DATA_DIR / "bookings.csv"
ORDERS_CSV = DATA_DIR / "orders.csv"
MENU_IMAGES_ORDER_PATH = DATA_DIR / "menu_images.json"
MENU_ICONS_PATH = DATA_DIR / "menu_icons.json"
PROMO_CODES_PATH = DATA_DIR / "promocodes.json"

# ----------------------------------------------------------------------------
# Media / uploads
# ----------------------------------------------------------------------------
ALLOWED_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
HERO_DIR = STATIC_DIR / "hero"
HERO_ORDER_PATH = DATA_DIR / "hero_order.json"
GALLERY_DIR = STATIC_DIR / "gallery"
BRAND_DIR = STATIC_DIR / "branding"
MENU_DIR = STATIC_DIR / "menu"
MENU_ITEMS_DIR = STATIC_DIR / "menu_items"
CONTENT_UPLOAD_DIR = STATIC_DIR / "uploads" / "content"
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", 8)) * 1024 * 1024

# ----------------------------------------------------------------------------
# Admin credentials: ONLY from data/admin.json (username + password_hash)
# ----------------------------------------------------------------------------
ADMIN_CONFIG_PATH = DATA_DIR / "admin.json"

def _load_admin_file() -> tuple[Optional[str], Optional[str]]:
    try:
        if ADMIN_CONFIG_PATH.exists():
            with open(ADMIN_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("username"), data.get("password_hash")
    except Exception:
        pass
    return None, None

ADMIN_USER, ADMIN_HASH = _load_admin_file()

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
SITE_CONFIG_PATH = DATA_DIR / "site.json"

def load_site() -> Dict:
    """Load site-wide settings from data/site.json with safe defaults."""
    defaults: Dict = {
        "name": "Название Паба",
        "tagline": "лучший бар города",
        "contacts": {
            "address": "Улица, 1, Город",
            "phone": "+48 000 000 000",
            "email": "info@example.com",
            "hours": "Пн–Вс: 12:00–00:00",
            "map_url": "https://yandex.ru/maps/-/CLGCJK3x",
        },
        "socials": {"instagram": "", "facebook": "", "vk": "", "tiktok": ""},
        "branding": {"logo_url": "/static/logo.jpg"},
        "theme": {"accent": "#E6C160", "max_width": "max-w-6xl"},
        "cart": {"delivery_price": 200, "free_from": 1500, "pickup_discount": 0},
        "notifications": {"yandex_metrika": {"id": os.environ.get("YANDEX_METRIKA_ID", "")}},
    }
    if SITE_CONFIG_PATH.exists():
        try:
            with open(SITE_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in defaults.items():
                if k not in data:
                    data[k] = v
            # ensure nested defaults
            data.setdefault("cart", {})
            for k, v in defaults["cart"].items():
                data["cart"].setdefault(k, v)
            data.setdefault("notifications", {}).setdefault("yandex_metrika", {})
            ym_env = os.environ.get("YANDEX_METRIKA_ID")
            if ym_env and not data["notifications"]["yandex_metrika"].get("id"):
                data["notifications"]["yandex_metrika"]["id"] = ym_env
            return data
        except Exception:
            return defaults
    return defaults
def save_site(data: Dict) -> None:
    SITE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SITE_CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SITE_CONFIG_PATH)

# ------------------------ CONTENT WRAPPER HELPERS ------------------------
CONTENT_PATH = DATA_DIR / "content_wrapper.json"

def load_content() -> List[Dict]:
    if CONTENT_PATH.exists():
        try:
            with open(CONTENT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []

def save_content(blocks: List[Dict]) -> None:
    CONTENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONTENT_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(blocks, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONTENT_PATH)


# ------------------------ CONTENT CLEANUP HELPERS ------------------------
def _collect_content_images(blocks: List[Dict]) -> set[str]:
    """Extract relative content image paths from content blocks."""
    imgs: set[str] = set()
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        val = b.get("value") if isinstance(b.get("value"), dict) else b
        src = (val or {}).get("src") or ""
        if not isinstance(src, str) or not src:
            continue
        rel = src.strip()
        # Strip query/hash
        for sep in ("?", "#"):
            if sep in rel:
                rel = rel.split(sep, 1)[0]
        # Find uploads/content anywhere in the path (handles absolute URLs)
        marker = "/uploads/content/"
        idx = rel.find(marker)
        if idx != -1:
            rel = rel[idx + 1 :] if rel.startswith("/") else rel[idx:]
        if rel.startswith("/static/"):
            rel = rel[len("/static/") :]
        elif rel.startswith("static/"):
            rel = rel[len("static/") :]
        rel = rel.lstrip("/")
        if rel.startswith("uploads/content/"):
            imgs.add(rel)
    return imgs


def _cleanup_unused_content_images(old_blocks: List[Dict], new_blocks: List[Dict]) -> None:
    """Delete files in static/uploads/content that are no longer referenced."""
    try:
        new_imgs = _collect_content_images(new_blocks)
        to_remove = _collect_content_images(old_blocks) - new_imgs if old_blocks is not None else set()
        # sweep directory for any orphan not in new_imgs
        if CONTENT_UPLOAD_DIR.exists():
            for p in CONTENT_UPLOAD_DIR.iterdir():
                if p.is_file() and p.suffix.lower() in ALLOWED_IMG_EXT:
                    rel = f"uploads/content/{p.name}"
                    if rel not in new_imgs:
                        to_remove.add(rel)
        for rel in to_remove:
            p = (STATIC_DIR / rel).resolve()
            try:
                if CONTENT_UPLOAD_DIR.resolve() in p.parents and p.exists():
                    p.unlink()
            except Exception:
                continue
    except Exception:
        pass

# ------------------------ PROMO HELPERS ------------------------
PROMO_SESSION_KEY = "promo_code"


def _normalize_promo(item: Dict) -> Optional[Dict]:
    if not isinstance(item, dict):
        return None
    code = str(item.get("code", "")).strip().upper()
    if not code:
        return None
    raw_type = str(item.get("type") or item.get("kind") or "percent").lower()
    promo_type = "percent" if raw_type not in ("percent", "fixed", "amount") else ("fixed" if raw_type in ("fixed", "amount") else "percent")
    try:
        value = abs(float(item.get("value", 0)))
    except Exception:
        value = 0.0
    try:
        min_subtotal = max(0.0, float(item.get("min_subtotal", item.get("min_total", 0) or 0)))
    except Exception:
        min_subtotal = 0.0
    expires = str(item.get("expires_at") or "").strip() or None
    if expires:
        try:
            datetime.fromisoformat(expires).date()
        except Exception:
            expires = None
    try:
        max_uses = max(0, int(item.get("max_uses", 0) or 0))
    except Exception:
        max_uses = 0
    try:
        used = max(0, int(item.get("used", 0) or 0))
    except Exception:
        used = 0
    active_raw = item.get("active", True)
    active = active_raw if isinstance(active_raw, bool) else (str(active_raw).lower() not in ("false", "0", "off", "no", ""))
    comment = str(item.get("comment", "") or "")
    return {
        "code": code,
        "type": promo_type,
        "value": value,
        "min_subtotal": min_subtotal,
        "expires_at": expires,
        "max_uses": max_uses,
        "used": used,
        "active": active,
        "comment": comment,
    }


def load_promocodes() -> List[Dict]:
    try:
        if PROMO_CODES_PATH.exists():
            with open(PROMO_CODES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                seen = {}
                for row in data:
                    norm = _normalize_promo(row)
                    if norm:
                        seen[norm["code"]] = norm
                return sorted(seen.values(), key=lambda x: x["code"])
    except Exception:
        pass
    return []


def save_promocodes(items: List[Dict]) -> None:
    cleaned: List[Dict] = []
    seen = set()
    for it in items:
        norm = _normalize_promo(it)
        if not norm or norm["code"] in seen:
            continue
        cleaned.append(norm)
        seen.add(norm["code"])
    PROMO_CODES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROMO_CODES_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROMO_CODES_PATH)


def set_promo_code(code: Optional[str]) -> None:
    if code:
        session[PROMO_SESSION_KEY] = str(code).strip().upper()
    else:
        session.pop(PROMO_SESSION_KEY, None)
    session.modified = True


def _promo_status(promo: Dict, subtotal: float) -> tuple[str, Optional[str]]:
    if not promo.get("active", True):
        return "inactive", "Промокод выключен"
    expires = promo.get("expires_at")
    if expires:
        try:
            exp_date = datetime.fromisoformat(str(expires)).date()
            if datetime.now().date() > exp_date:
                return "expired", "Срок действия промокода истек"
        except Exception:
            pass
    try:
        used = int(promo.get("used", 0) or 0)
    except Exception:
        used = 0
    try:
        max_uses = int(promo.get("max_uses", 0) or 0)
    except Exception:
        max_uses = 0
    if max_uses > 0 and used >= max_uses:
        return "limit", "Лимит использования промокода исчерпан"
    try:
        min_subtotal = float(promo.get("min_subtotal", 0) or 0.0)
    except Exception:
        min_subtotal = 0.0
    if subtotal < min_subtotal:
        return "pending", f"Скидка действует от {min_subtotal:.2f} руб."
    return "ok", None


def _calc_promo_discount(subtotal: float, promo: Dict) -> float:
    try:
        kind = promo.get("type")
        value = float(promo.get("value", 0) or 0.0)
        if subtotal <= 0 or value <= 0:
            return 0.0
        disc = value if kind == "fixed" else subtotal * (value / 100.0)
        disc = max(0.0, disc)
        return min(subtotal, disc)
    except Exception:
        return 0.0


def get_applied_promo(subtotal: float) -> Dict:
    code = (session.get(PROMO_SESSION_KEY) or "").strip().upper()
    state = {"code": code, "promo": None, "discount": 0.0, "status": "none", "message": None}
    if not code:
        return state
    promo = None
    for p in load_promocodes():
        if p.get("code") == code:
            promo = p
            break
    if not promo:
        set_promo_code(None)
        state["status"] = "invalid"
        state["message"] = "Промокод не найден"
        return state
    status, message = _promo_status(promo, subtotal)
    state.update({"promo": promo, "status": status, "message": message})
    if status == "ok":
        state["discount"] = _calc_promo_discount(subtotal, promo)
    elif status in ("inactive", "expired", "limit"):
        set_promo_code(None)
    return state


def _increment_promo_usage(code: str) -> None:
    if not code:
        return
    promos = load_promocodes()
    changed = False
    for p in promos:
        if p.get("code") == code:
            try:
                p["used"] = max(0, int(p.get("used", 0) or 0)) + 1
            except Exception:
                p["used"] = (p.get("used") or 0) + 1
            changed = True
            break
    if changed:
        save_promocodes(promos)

# ------------------------ CART HELPERS ------------------------

def get_cart() -> List[Dict]:
    return session.get("cart", [])


def set_cart(cart: List[Dict]) -> None:
    session["cart"] = cart
    session.modified = True


def calc_subtotal(cart: Optional[List[Dict]] = None) -> float:
    c = cart if cart is not None else get_cart()
    try:
        return sum(float(r.get("unit_price", 0)) * int(r.get("qty", 0)) for r in c)
    except Exception:
        return 0.0


def calc_cart_count(cart: Optional[List[Dict]] = None) -> int:
    c = cart if cart is not None else get_cart()
    try:
        return sum(int(item.get("qty", 0)) for item in c)
    except Exception:
        return len(c)


def compute_shipping(subtotal: float, cfg: Optional[Dict] = None) -> float:
    """Return delivery cost based on site cart settings.
    Free delivery if subtotal >= free_from (>0), else flat delivery_price.
    Defaults: delivery_price=200, free_from=1500.
    """
    try:
        cart_cfg = (cfg or load_site()).get("cart", {})
        delivery = float(cart_cfg.get("delivery_price", 200))
        free_from = float(cart_cfg.get("free_from", 1500))
        return 0.0 if (subtotal > 0 and subtotal >= free_from) else delivery
    except Exception:
        return 0.0 if (subtotal > 0 and subtotal >= 1500) else 200.0

# ------------------------ Telegram notify helpers ------------------------

def _tg_send(text: str) -> bool:
    """Send a Telegram message using site.json settings or env vars.
    Returns True on HTTP 2xx, False otherwise. Silent on errors (logged)."""
    try:
        cfg = load_site()
        tcfg = (cfg.get("notifications") or {}).get("telegram") or {}
        token = (tcfg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (tcfg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        enabled = bool(tcfg.get("enabled") or (token and chat_id))
        if not enabled or not token or not chat_id:
            return False
        api_url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(api_url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=6) as r:
            return 200 <= getattr(r, 'status', 200) < 300
    except Exception as e:
        try:
            app.logger.warning("Telegram notify failed: %s", e)
        except Exception:
            pass
        return False


# override with localized/discount-aware formatter (kept separate to avoid breaking legacy text above)
def _format_order_for_tg(customer: dict, cart: list[dict], subtotal: float, delivery: float, total: float,
                         discount: float = 0.0, promo_code: Optional[str] = None,
                         payment_method: Optional[str] = None, change_from: Optional[float] = None,
                         delivery_method: Optional[str] = None, pickup_discount: float = 0.0, pickup_discount_pct: float = 0.0) -> str:
    """Return a nicely formatted HTML message for Telegram (parse_mode=HTML)."""

    def esc(s):
        try:
            return html.escape(str(s or ""))
        except Exception:
            return ""

    def money(v) -> str:
        try:
            return f"{float(v):.2f} руб."
        except Exception:
            return f"{v} руб."

    lines: list[str] = []
    if delivery_method:
        dm = "Самовывоз" if delivery_method == "pickup" else "Доставка"
    lines.append("📦 Новый заказ")
    try:
        lines.append(f"🕒 {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    except Exception:
        pass
    lines.append("──────────")

    lines.append(f"👤 Имя: {esc(customer.get('name'))}")
    lines.append(f"📞 Телефон: {esc(customer.get('phone'))}")
    if dm == "Доставка":
        lines.append(f"🏠 Адрес: {esc(customer.get('address'))}")
    if customer.get("comment"):
        lines.append(f"💬 Комментарий: {esc(customer.get('comment'))}")

    lines.append(f"🚚 Способ: {dm}")

    pay_map = {"card": "Картой при получении", "cash": "Наличными"}
    pay_txt = pay_map.get(str(payment_method).lower(), "") if payment_method else ""
    if dm == 'Доставка':
        if pay_txt:
            if payment_method == "cash" and change_from is not None:
                lines.append(f"💳 Оплата: {pay_txt} (сдача с {money(change_from)})")
            else:
                lines.append(f"💳 Оплата: {pay_txt}")

    lines.append("──────────")
    lines.append("🛒 Позиции:")
    for row in cart or []:
        try:
            name = esc(row.get("name", ""))
            variant = row.get("variant") or ""
            qty = int(row.get("qty", 1))
            unit = float(row.get("unit_price", 0))
            line_total = unit * qty
            var_txt = f" ({esc(variant)})" if variant else ""
            lines.append(f"• {name}{var_txt} x {qty} = {money(line_total)}")
        except Exception:
            continue

    lines.append("──────────")
    try:
        subtotal_after = max(0.0, float(subtotal) - float(discount or 0.0))
    except Exception:
        subtotal_after = subtotal
    lines.append(f"🧾 Сумма: {money(subtotal)}")
    if discount and discount > 0:
        label = f" ({esc(promo_code)})" if promo_code else ""
        lines.append(f"🎁 Скидка{label}: -{money(discount)}")
        lines.append(f"🧮 После скидки: {money(subtotal_after)}")
    if pickup_discount and pickup_discount > 0:
        lines.append(f"🚶 Самовывоз: -{money(pickup_discount)}")
    if dm == "Доставка":
        lines.append(f"🚚 Доставка: {money(delivery)}")
    lines.append(f"✅ Итог к оплате: {money(total)}")
    return "\n".join(lines)


@app.context_processor
def inject_site():
    # make `site` available in all templates + cart counter for шапки
    return {"site": load_site(), "cart_count": calc_cart_count()}


def load_menu():
    if MENU_PATH.exists():
        with open(MENU_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    # fallback demo
    return {"Пиво": [{"name": "Pale Ale", "desc": "цитрусовый, 5.2%", "price": 18}]}

# --- icons map helpers ---

def load_menu_icons() -> Dict[str, str]:
    try:
        if MENU_ICONS_PATH.exists():
            with open(MENU_ICONS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def save_menu_icons(mapping: Dict[str, str]) -> None:
    MENU_ICONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MENU_ICONS_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MENU_ICONS_PATH)


def _find_menu_item(category: str, subname: Optional[str], item_idx: int) -> Optional[Dict]:
    """Return the menu item dict (by index) from category or its subsection."""
    menu = load_menu()
    cat = menu.get(category)
    if cat is None:
        return None
    # two shapes supported: list OR {subsections: {name: [items]}}
    if isinstance(cat, dict) and "subsections" in cat:
        subs = cat.get("subsections") or {}
        items = subs.get(subname) if subname else None
        if items is None:
            return None
        try:
            return items[item_idx]
        except Exception:
            return None
    # plain list
    try:
        return cat[item_idx]
    except Exception:
        return None


def _resolve_price_variant(item: Dict, variant_idx: int) -> tuple[Optional[str], Optional[float]]:
    if variant_idx is None or variant_idx < 0:
        # single-price item
        return None, float(item.get("price")) if item.get("price") is not None else None
    variants = item.get("variants") or []
    try:
        v = variants[variant_idx]
        return v.get("label"), float(v.get("price"))
    except Exception:
        return None, None


# ----------------------------------------------------------------------------
# Hero images collector
# ----------------------------------------------------------------------------

def get_hero_images() -> List[Dict[str, str]]:
    """Collect images from static/hero.
    Supports pairs base-mobile.* / base-desktop.*. If only one exists, use it for both.
    Returns list of dicts: {mobile, desktop} with URLS.
    """
    items: List[Dict[str, str]] = []
    if not HERO_DIR.exists():
        return items

    # apply saved order if present
    order_index: dict[str, float] = {}
    try:
        if HERO_ORDER_PATH.exists():
            data = json.load(open(HERO_ORDER_PATH, "r", encoding="utf-8"))
            if isinstance(data, list):
                order_index = {str(Path(name).name): idx for idx, name in enumerate(data)}
    except Exception:
        order_index = {}

    files = [p for p in HERO_DIR.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_IMG_EXT]
    files.sort(key=lambda x: (order_index.get(x.name, 10_000_000), x.name.lower()))
    by_name = {p.name: p for p in files}
    used = set()

    def find_variant(base: str, var: str):
        for sep in ("-", "_"):
            for ext in ALLOWED_IMG_EXT:
                name = f"{base}{sep}{var}{ext}"
                if name in by_name:
                    return by_name[name]
        return None

    for p in files:
        if p.name in used:
            continue
        stem = p.stem
        base = stem
        mobile = desktop = p
        if stem.endswith("-mobile") or stem.endswith("_mobile"):
            base = stem.rsplit("-mobile", 1)[0] if "-mobile" in stem else stem.rsplit("_mobile", 1)[0]
            mobile = p
            desktop = find_variant(base, "desktop") or p
        elif stem.endswith("-desktop") or stem.endswith("_desktop"):
            base = stem.rsplit("-desktop", 1)[0] if "-desktop" in stem else stem.rsplit("_desktop", 1)[0]
            desktop = p
            mobile = find_variant(base, "mobile") or p
        items.append({
            "mobile": url_for("static", filename=f"hero/{mobile.name}"),
            "desktop": url_for("static", filename=f"hero/{desktop.name}")
        })
        used.update({p.name, mobile.name if mobile else "", desktop.name if desktop else ""})
    return items

# ----------------------------------------------------------------------------
# Menu images (for mobile-only slider after HERO)
# ----------------------------------------------------------------------------

def get_menu_images() -> List[str]:
    """Return menu images (URLs) respecting saved order in data/menu_images.json.
    Files not listed are appended sorted by name.
    """
    return [url_for("static", filename=f"menu/{p.name}") for p in _menu_ordered_files()]

# ----------------------------------------------------------------------------
# Auth helpers
# ----------------------------------------------------------------------------

def is_admin() -> bool:
    return session.get("is_admin") is True


def check_admin_password(username: str, password: str) -> bool:
    # Only JSON-based creds
    if not ADMIN_USER or not ADMIN_HASH:
        return False
    if username != ADMIN_USER:
            return False
    try:
        return check_password_hash(ADMIN_HASH, password)
    except Exception:
        return False


def login_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)

    return wrapper


def _save_admin_file(username: str, password_hash: str) -> None:
    """Atomically write admin.json and update in-memory creds."""
    global ADMIN_USER, ADMIN_HASH
    ADMIN_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ADMIN_CONFIG_PATH.with_suffix(".tmp")
    data = {"username": username, "password_hash": password_hash}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ADMIN_CONFIG_PATH)
    ADMIN_USER, ADMIN_HASH = username, password_hash

# ----------------------------------------------------------------------------
# Extra admin guards (defense-in-depth)
# ----------------------------------------------------------------------------
ADMIN_OPEN_ENDPOINTS = {"admin_login", "admin_logout"}

@app.before_request
def _admin_guard_before_request():
    """Protect all /admin/* routes even if a decorator is missing somewhere.
    Allows only /admin/login and /admin/logout for anonymous users.
    """
    p = request.path.rstrip("/")
    if p.startswith("/admin"):
        if request.endpoint in ADMIN_OPEN_ENDPOINTS:
            return None
        if not is_admin():
            return redirect(url_for("admin_login", next=request.path))
    return None

@app.after_request
def _admin_no_cache_after_request(resp):
    """Disable caching for admin pages to avoid stale content being shown from cache."""
    p = request.path.rstrip("/")
    if p.startswith("/admin"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

# ----------------------------------------------------------------------------
# Routes: public
# ----------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template(
        "index.html",
        hero_images=get_hero_images(),
        menu_images=get_menu_images(),  # моб. слайдер картинок меню
        content_blocks=load_content(),
    )


@app.get("/menu")
def menu():
    return render_template("menu.html", menu=load_menu())


@app.route("/booking", methods=["GET", "POST"])
def booking():
    if request.method == "POST":
        form = {
            "name": request.form.get("name", "").strip(),
            "phone": request.form.get("phone", "").strip(),
            "email": request.form.get("email", "").strip(),  # поле на странице убрано, в CSV остаётся пустым
            "date": request.form.get("date", "").strip(),
            "time": request.form.get("time", "").strip(),
            "size": request.form.get("size", "").strip(),
            "comment": request.form.get("comment", "").strip(),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
            "ua": request.headers.get("User-Agent"),
        }
        # Серверная валидация: обязательны имя, телефон, дата, время и количество гостей ≥ 1
        try:
            size_int = int(form["size"]) if form["size"] != "" else 0
        except Exception:
            size_int = 0
        if (not form["name"] or not form["phone"] or not form["date"] or not form["time"] or size_int < 1):
            flash("Пожалуйста, заполните имя, телефон, дату, время и количество гостей (минимум 1).", "error")
            return redirect(url_for("booking"))
        form["size"] = str(size_int)

        BOOKINGS_CSV.parent.mkdir(parents=True, exist_ok=True)
        new_file = not BOOKINGS_CSV.exists()
        with open(BOOKINGS_CSV, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(form.keys()))
            if new_file:
                writer.writeheader()
            writer.writerow(form)
        app.logger.info("New booking: %s", form)
        flash("Спасибо! Мы получили вашу заявку и свяжемся для подтверждения.", "success")
        return redirect(url_for("booking"))

    return render_template("booking.html")



@app.get("/contact")
def contact():
    return render_template("contact.html")

@app.get("/gallery")
def gallery():
    images = []
    if GALLERY_DIR.exists():
        for p in sorted(GALLERY_DIR.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and p.suffix.lower() in ALLOWED_IMG_EXT:
                images.append({"name": p.name, "url": url_for("static", filename=f"gallery/{p.name}")})
    return render_template("gallery.html", images=images)


@app.get("/api/menu")
def api_menu():
    return jsonify(load_menu())

@app.get("/api/menu_icons")
def api_menu_icons():
    return jsonify(load_menu_icons())


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/robots.txt")
def robots():
    """Plaintext robots.txt with explicit Content-Type and trailing newline."""
    lines = [
        "User-agent: *",
        "Allow: /",
        "Sitemap: " + url_for("sitemap", _external=True),
    ]
    text = "\n".join(lines) + "\n"
    resp = make_response(text)
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    return resp


@app.get("/sitemap.xml")
def sitemap():
    """Minimal XML sitemap without indentation/whitespace issues."""
    urls = [
        url_for("index", _external=True),
        url_for("menu", _external=True),
        url_for("booking", _external=True),
        url_for("contact", _external=True),
        url_for("cart", _external=True),
    ]
    items = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{items}"  # noqa: E231 (compact)
        '</urlset>'
    )
    resp = make_response(xml)
    resp.headers["Content-Type"] = "application/xml; charset=utf-8"
    return resp

# ----------------------------------------------------------------------------
# CART & ORDER ROUTES
# ----------------------------------------------------------------------------
@app.post("/cart/add")
def cart_add():
    category = request.form.get("category")
    subname = request.form.get("subname") or None
    try:
        item_idx = int(request.form.get("item_idx", -1))
    except Exception:
        item_idx = -1
    try:
        variant_idx = int(request.form.get("variant_idx", -1))
    except Exception:
        variant_idx = -1
    try:
        qty = max(1, int(request.form.get("qty", 1)))
    except Exception:
        qty = 1

    if not category or item_idx < 0:
        flash("Не удалось добавить в корзину (некорректные данные)", "error")
        return redirect(request.referrer or url_for("menu"))

    src_item = _find_menu_item(category, subname, item_idx)
    if not src_item:
        flash("Позиция не найдена в меню", "error")
        return redirect(request.referrer or url_for("menu"))

    variant_label, unit_price = _resolve_price_variant(src_item, variant_idx)
    if unit_price is None:
        flash("Не удалось определить цену позиции", "error")
        return redirect(request.referrer or url_for("menu"))

    cart = get_cart()
    sku = {
        "category": category,
        "subname": subname or "",
        "name": src_item.get("name", ""),
        "variant": variant_label or "",
    }

    # merge if same sku exists
    for row in cart:
        if all(row.get(k) == v for k, v in sku.items()):
            row["qty"] = int(row.get("qty", 1)) + qty
            set_cart(cart)
            # если форма отправлена в невидимый iframe — вернём короткий HTML, который обновит счётчик в родителе
            if request.headers.get('Sec-Fetch-Dest') == 'iframe' or request.form.get('silent') == '1':
                count = calc_cart_count()
                html = ('<!doctype html><meta charset="utf-8"><script>(function(){try{var c=' + str(count) + ';var d=parent.document;var sels=["[data-cart-count]","#cart-count",".cart-count"];for(var i=0;i<sels.length;i++){d.querySelectorAll(sels[i]).forEach(function(el){el.textContent=String(c);try{el.dataset.cartCount=String(c);}catch(_e){}try{el.classList.add("animate-bump");setTimeout(function(){el.classList.remove("animate-bump")},500);}catch(_e){}});}if(parent.postMessage){parent.postMessage({type:"cart_count",count:c},"*");}}catch(e){}})();</script>OK')
                return html
            return redirect(request.referrer or url_for("menu"))

    cart.append({
        **sku,
        "unit_price": unit_price,
        "qty": qty,
    })
    set_cart(cart)
    if not cart:
        set_promo_code(None)
    # если форма отправлена в невидимый iframe — вернём короткий HTML, который обновит счётчик в родителе
    if request.headers.get('Sec-Fetch-Dest') == 'iframe' or request.form.get('silent') == '1':
                count = calc_cart_count()
                html = ('<!doctype html><meta charset="utf-8"><script>(function(){try{var c=' + str(count) + ';var d=parent.document;var sels=["[data-cart-count]","#cart-count",".cart-count"];for(var i=0;i<sels.length;i++){d.querySelectorAll(sels[i]).forEach(function(el){el.textContent=String(c);try{el.dataset.cartCount=String(c);}catch(_e){}try{el.classList.add("animate-bump");setTimeout(function(){el.classList.remove("animate-bump")},500);}catch(_e){}});}if(parent.postMessage){parent.postMessage({type:"cart_count",count:c},"*");}}catch(e){}})();</script>OK')
                return html
    return redirect(request.referrer or url_for("menu"))


@app.get("/cart")
def cart():
    cart = get_cart()
    subtotal = calc_subtotal(cart)
    promo_state = get_applied_promo(subtotal)
    discount = promo_state.get("discount", 0.0) if promo_state else 0.0
    subtotal_after = max(0.0, subtotal - discount)
    sitecfg = load_site()
    delivery = compute_shipping(subtotal_after, sitecfg)
    total = subtotal_after + delivery
    return render_template(
        "cart.html",
        cart=cart,
        subtotal=subtotal,
        subtotal_after=subtotal_after,
        discount=discount,
        delivery=delivery,
        total=total,
        promo_state=promo_state,
    )


@app.post("/cart/promo")
def cart_apply_promo():
    action = request.form.get("action")
    if action == "clear":
        set_promo_code(None)
        flash("Промокод удален", "success")
        return redirect(url_for("cart"))

    code = (request.form.get("code") or "").strip()
    if not code:
        flash("Введите промокод", "error")
        return redirect(url_for("cart"))

    code = code.upper()
    cart = get_cart()
    subtotal = calc_subtotal(cart)
    promo = None
    for p in load_promocodes():
        if p.get("code") == code:
            promo = p
            break
    if not promo:
        set_promo_code(None)
        flash("Промокод не найден", "error")
        return redirect(url_for("cart"))

    status, message = _promo_status(promo, subtotal)
    if status in ("inactive", "expired", "limit"):
        set_promo_code(None)
        flash(message or "Промокод недоступен", "error")
        return redirect(url_for("cart"))

    set_promo_code(code)
    if status == "pending":
        flash(message or "Промокод сохранен, добавьте товаров на нужную сумму", "info")
    else:
        flash("Промокод применен", "success")
    return redirect(url_for("cart"))


@app.post("/cart/remove")
def cart_remove():
    try:
        idx = int(request.form.get("i", -1))
    except Exception:
        idx = -1
    cart = get_cart()
    if 0 <= idx < len(cart):
        removed = cart.pop(idx)
        set_cart(cart)
        if not cart:
            set_promo_code(None)
        flash(f"Удалено: {removed.get('name')}", "success")
    else:
        flash("Позиция не найдена", "error")
    return redirect(url_for("cart"))


@app.post("/cart/update")
def cart_update():
    """Update quantity for a cart line by index. qty=0 removes the line.
    Supports relative actions via action=inc|dec for graceful no-JS.
    """
    try:
        idx = int(request.form.get("i", -1))
    except Exception:
        idx = -1
    action = request.form.get("action")
    cart = get_cart()
    if not (0 <= idx < len(cart)):
        flash("Позиция не найдена", "error")
        return redirect(url_for("cart"))

    try:
        if action in ("inc", "dec"):
            cur = int(cart[idx].get("qty", 1))
            new_q = cur + (1 if action == "inc" else -1)
        else:
            new_q = int(request.form.get("qty", 1))
    except Exception:
        new_q = 1

    new_q = max(0, new_q)
    if new_q == 0:
        cart.pop(idx)
    else:
        cart[idx]["qty"] = new_q
    set_cart(cart)

    # Silent mode: if submitted via hidden iframe, return tiny HTML that bumps cart badge
    if request.headers.get('Sec-Fetch-Dest') == 'iframe' or request.form.get('silent') == '1':
        count = calc_cart_count()
        html = ('<!doctype html><meta charset="utf-8"><script>(function(){try{var c=' + str(count) + ';var d=parent.document;var sels=["[data-cart-count]","#cart-count",".cart-count"];for(var i=0;i<sels.length;i++){d.querySelectorAll(sels[i]).forEach(function(el){el.textContent=String(c);try{el.dataset.cartCount=String(c);}catch(_e){}try{el.classList.add("animate-bump");setTimeout(function(){el.classList.remove("animate-bump")},500);}catch(_e){}});}if(parent.postMessage){parent.postMessage({type:"cart_count",count:c},"*");}}catch(e){}})();<\/script>OK')
        return html

    flash("Корзина обновлена", "success")
    return redirect(url_for("cart"))


@app.post("/cart/clear")
def cart_clear():
    set_cart([])
    set_promo_code(None)
    flash("Корзина очищена", "success")
    return redirect(url_for("cart"))


def _append_order_row(row: dict) -> None:
    """Append order row to CSV preserving existing data and extending header when needed."""
    ORDERS_CSV.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(row.keys())
    needs_header = not ORDERS_CSV.exists()
    existing_rows = None
    existing_fields: list[str] = []

    if ORDERS_CSV.exists():
        try:
            with open(ORDERS_CSV, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                existing_fields = reader.fieldnames or []
                if not existing_fields:
                    needs_header = True
                missing = [k for k in fieldnames if k not in existing_fields]
                if missing and existing_fields:
                    fieldnames = existing_fields + missing
                    existing_rows = list(reader)
                elif existing_fields:
                    fieldnames = existing_fields
                    needs_header = False
        except Exception:
            needs_header = True

    if existing_rows is not None:
        existing_rows.append(row)
        tmp = ORDERS_CSV.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in existing_rows:
                writer.writerow(r)
        os.replace(tmp, ORDERS_CSV)
        return

    with open(ORDERS_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)


@app.post("/order")
def order_submit():
    cart = get_cart()
    if not cart:
        flash("Корзина пуста", "error")
        return redirect(url_for("cart"))

    customer = {
        "name": request.form.get("name", "").strip(),
        "phone": request.form.get("phone", "").strip(),
        "address": request.form.get("address", "").strip(),
        "comment": request.form.get("comment", "").strip(),
    }
    delivery_method = (request.form.get("delivery_method") or "delivery").strip().lower()
    if delivery_method not in ("delivery", "pickup"):
        flash("Неизвестный способ получения", "error"); return redirect(url_for("cart"))
    if not customer["name"] or not customer["phone"]:
        flash("Заполните имя и телефон", "error"); return redirect(url_for("cart"))
    if delivery_method == "delivery" and not customer["address"]:
        flash("Введите адрес для доставки", "error"); return redirect(url_for("cart"))
    if delivery_method == "pickup":
        customer["address"] = ""

    payment_method = (request.form.get("payment_method") or "card").strip().lower()
    if payment_method not in ("card", "cash"):
        payment_method = "card"

    change_from_val = None
    subtotal = calc_subtotal(cart)
    promo_state = get_applied_promo(subtotal)
    discount = promo_state.get("discount", 0.0) if promo_state else 0.0
    subtotal_after = max(0.0, subtotal - discount)
    sitecfg = load_site()
    cart_cfg = (sitecfg.get("cart") or {}) if isinstance(sitecfg, dict) else {}
    pickup_pct = float(cart_cfg.get("pickup_discount", 0) or 0)

    if delivery_method == "delivery":
        delivery = compute_shipping(subtotal_after, sitecfg)
        pickup_discount = 0.0
        total = subtotal_after + delivery
    else:
        delivery = 0.0
        pickup_discount = max(0.0, subtotal_after * (pickup_pct / 100.0))
        total = max(0.0, subtotal_after - pickup_discount) + delivery

    if payment_method == "cash":
        change_raw = (request.form.get("change_from") or "").strip()
        if not change_raw:
            flash("Введите сумму, с которой нужна сдача", "error")
            return redirect(url_for("cart"))
        try:
            change_from_val = float(change_raw.replace(",", "."))
        except Exception:
            change_from_val = -1.0
        if change_from_val < total:
            flash("Сумма для сдачи меньше суммы заказа", "error")
            return redirect(url_for("cart"))

    promo_code = ""
    promo_status = ""
    if promo_state and promo_state.get("promo"):
        promo_code = promo_state["promo"].get("code", "")
        promo_status = promo_state.get("status") or ""

    row = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "ua": request.headers.get("User-Agent"),
        "customer": json.dumps(customer, ensure_ascii=False),
        "cart": json.dumps(cart, ensure_ascii=False),
        "subtotal": f"{subtotal:.2f}",
        "discount": f"{discount:.2f}",
        "discounted_subtotal": f"{subtotal_after:.2f}",
        "delivery": f"{delivery:.2f}",
        "total": f"{total:.2f}",
        "promo_code": promo_code,
        "promo_status": promo_status,
        "payment_method": payment_method,
        "change_from": f"{change_from_val:.2f}" if change_from_val is not None else "",
        "delivery_method": delivery_method,
        "pickup_discount_pct": f"{pickup_pct:.2f}",
        "pickup_discount_value": f"{pickup_discount:.2f}",
    }

    _append_order_row(row)
    if promo_code and discount > 0:
        _increment_promo_usage(promo_code)
    set_promo_code(None)

    try:
        msg = _format_order_for_tg(
            customer,
            cart,
            subtotal,
            delivery,
            total,
            discount=discount,
            promo_code=promo_code,
            payment_method=payment_method,
            change_from=change_from_val,
            delivery_method=delivery_method,
            pickup_discount=pickup_discount,
            pickup_discount_pct=pickup_pct,
        )
        _tg_send(msg)
    except Exception as e:
        try:
            app.logger.warning("Telegram notify error: %s", e)
        except Exception:
            pass

    set_cart([])
    flash("Заказ оформлен! Мы свяжемся с вами в ближайшее время.", "success")
    return redirect(url_for("menu"))
# ----------------------------------------------------------------------------
# ------------------------ MINI ADMIN ------------------------
# ----------------------------------------------------------------------------
@app.get("/admin")
@login_required
def admin_dashboard():
    return render_template("admin_dashboard.html")

# helper: list images from static/menu for admin UI

def _load_menu_images_meta() -> tuple[list[str], dict[str, int]]:
    """Load ordering metadata for static/menu.
    Supports legacy format (list) and new format ({order:[], indices:{}}).
    """
    try:
        with open(MENU_IMAGES_ORDER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            order = data.get("order") or []
            indices = data.get("indices") or {}
            # normalize
            order = [str(Path(x).name) for x in order if isinstance(x, str)]
            indices = {str(Path(k).name): int(v) for k, v in indices.items() if isinstance(k, str)}
            return order, indices
        elif isinstance(data, list):
            # legacy: only order list
            order = [str(Path(x).name) for x in data if isinstance(x, str)]
            return order, {}
    except Exception:
        pass
    return [], {}


def _save_menu_images_meta(order: list[str], indices: dict[str, int]) -> None:
    MENU_IMAGES_ORDER_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"order": order, "indices": indices}
    tmp = MENU_IMAGES_ORDER_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MENU_IMAGES_ORDER_PATH)

# Back-compat shims (not used directly, оставлены на случай старых вызовов)

def _load_menu_order() -> list[str]:
    o, _ = _load_menu_images_meta()
    return o


def _save_menu_order(names: list[str]) -> None:
    _save_menu_images_meta(names, {})


def _menu_ordered_files() -> list[Path]:
    MENU_DIR.mkdir(parents=True, exist_ok=True)
    files = [p for p in MENU_DIR.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_IMG_EXT]
    by_name = {p.name: p for p in files}

    order, indices = _load_menu_images_meta()
    used = set()
    result: list[Path] = []

    if order:
        for name in order:
            p = by_name.get(name)
            if p and p.name not in used:
                result.append(p)
                used.add(p.name)
    elif indices:
        # sort by provided indices, then name
        for _, name in sorted(((indices.get(n, float('inf')), n) for n in by_name.keys()), key=lambda t: (t[0], t[1].lower())):
            p = by_name.get(name)
            if p and p.name not in used:
                result.append(p)
                used.add(p.name)
    # append remaining by name
    for p in sorted(files, key=lambda x: x.name.lower()):
        if p.name not in used:
            result.append(p)
            used.add(p.name)
    return result


def _list_menu_images():
    files = _menu_ordered_files()
    _, indices = _load_menu_images_meta()
    return [{"name": p.name, "url": url_for("static", filename=f"menu/{p.name}"), "index": indices.get(p.name)} for p in files]

@app.route("/admin/settings", methods=["GET", "POST"])
@login_required
def admin_settings():
    cfg = load_site()
    if request.method == "POST":
        # Basic fields
        cfg["name"] = request.form.get("name", cfg.get("name", "")).strip() or cfg.get("name", "")
        cfg["tagline"] = request.form.get("tagline", cfg.get("tagline", "")).strip()
        # Contacts
        cfg.setdefault("contacts", {})
        cfg["contacts"]["address"] = request.form.get("address", cfg["contacts"].get("address", ""))
        cfg["contacts"]["phone"] = request.form.get("phone", cfg["contacts"].get("phone", ""))
        cfg["contacts"]["email"] = request.form.get("email", cfg["contacts"].get("email", ""))
        cfg["contacts"]["hours"] = request.form.get("hours", cfg["contacts"].get("hours", ""))
        cfg["contacts"]["map_url"] = request.form.get("map_url", cfg["contacts"].get("map_url", "https://yandex.ru/maps/-/CLGCJK3x"))
        # Socials
        cfg.setdefault("socials", {})
        for k in ("instagram", "facebook", "vk", "tiktok"):
            cfg["socials"][k] = request.form.get(k, cfg["socials"].get(k, ""))
        # Theme
        cfg.setdefault("theme", {})
        cfg["theme"]["accent"] = request.form.get("accent", cfg["theme"].get("accent", "#E6C160"))
        cfg["theme"]["max_width"] = request.form.get("max_width", cfg["theme"].get("max_width", "max-w-6xl"))
        # Cart (delivery settings)
        cfg.setdefault("cart", {})
        try:
            cfg["cart"]["delivery_price"] = float(request.form.get("delivery_price", cfg["cart"].get("delivery_price", 200)))
        except Exception:
            pass
        try:
            cfg["cart"]["free_from"] = float(request.form.get("free_from", cfg["cart"].get("free_from", 1500)))
        except Exception:
            pass
        try:
            cfg["cart"]["pickup_discount"] = float(request.form.get("pickup_discount", cfg["cart"].get("pickup_discount", 0)))
        except Exception:
            pass
        # Notifications: Telegram
        cfg.setdefault("notifications", {})
        cfg["notifications"].setdefault("telegram", {})
        tg = cfg["notifications"]["telegram"]
        tg_enabled = request.form.get("tg_enabled")
        tg["enabled"] = True if tg_enabled in ("on", "1", "true", "True") else False
        token_val = request.form.get("tg_token", tg.get("bot_token", ""))
        chat_val = request.form.get("tg_chat_id", tg.get("chat_id", ""))
        tg["bot_token"] = (token_val or "").strip()
        tg["chat_id"] = (chat_val or "").strip()
        # Notifications: Yandex Metrika
        cfg["notifications"].setdefault("yandex_metrika", {})
        ym = cfg["notifications"]["yandex_metrika"]
        ym["id"] = (request.form.get("ym_id", ym.get("id", "")) or "").strip()
        # Logo upload (optional)
        cfg.setdefault("branding", {})
        file = request.files.get("logo")
        if file and file.filename:
            ext = Path(file.filename).suffix.lower()
            if ext in ALLOWED_IMG_EXT:
                BRAND_DIR.mkdir(parents=True, exist_ok=True)
                safe = secure_filename(Path(file.filename).name)
                target = BRAND_DIR / safe
                n = 1
                while target.exists():
                    target = BRAND_DIR / f"{Path(safe).stem}-{n}{Path(safe).suffix}"
                    n += 1
                file.save(str(target))
                cfg["branding"]["logo_url"] = url_for("static", filename=f"branding/{target.name}")
            else:
                flash("Недопустимый формат логотипа", "error")
        save_site(cfg)
        flash("Настройки сохранены", "success")
        return redirect(url_for("admin_settings"))
    return render_template("admin_settings.html", cfg=cfg)


@app.route("/admin/promos", methods=["GET", "POST"])
@login_required
def admin_promos():
    promos = load_promocodes()
    if request.method == "POST":
        action = (request.form.get("action") or "create").strip().lower()
        code = (request.form.get("code") or "").strip().upper()
        if action == "delete":
            before = len(promos)
            promos = [p for p in promos if p.get("code") != code]
            save_promocodes(promos)
            flash("Промокод удален" if len(promos) < before else "Промокод не найден", "success")
            return redirect(url_for("admin_promos"))

        if action == "toggle":
            changed = False
            for p in promos:
                if p.get("code") == code:
                    p["active"] = not p.get("active", True)
                    changed = True
                    break
            if changed:
                save_promocodes(promos)
                flash("Статус промокода обновлен", "success")
            else:
                flash("Промокод не найден", "error")
            return redirect(url_for("admin_promos"))

        if action == "reset_used":
            changed = False
            for p in promos:
                if p.get("code") == code:
                    p["used"] = 0
                    changed = True
                    break
            if changed:
                save_promocodes(promos)
                flash("Счетчик использований сброшен", "success")
            else:
                flash("Промокод не найден", "error")
            return redirect(url_for("admin_promos"))

        # create / upsert
        if not code:
            flash("Введите код промокода", "error")
            return redirect(url_for("admin_promos"))
        promo_type = (request.form.get("type") or "percent").lower()
        try:
            value = abs(float(request.form.get("value", 0) or 0))
        except Exception:
            value = 0.0
        try:
            min_subtotal = max(0.0, float(request.form.get("min_subtotal", 0) or 0))
        except Exception:
            min_subtotal = 0.0
        expires = (request.form.get("expires_at") or "").strip() or None
        max_uses = request.form.get("max_uses", "").strip()
        try:
            max_uses_int = max(0, int(max_uses)) if max_uses != "" else 0
        except Exception:
            max_uses_int = 0
        comment = request.form.get("comment", "").strip()
        active = True if request.form.get("active") in ("on", "1", "true", "True") else False

        new_item = {
            "code": code,
            "type": "fixed" if promo_type in ("fixed", "amount") else "percent",
            "value": value,
            "min_subtotal": min_subtotal,
            "expires_at": expires,
            "max_uses": max_uses_int,
            "used": 0,
            "active": active,
            "comment": comment,
        }
        # replace if exists
        promos = [p for p in promos if p.get("code") != code]
        promos.append(new_item)
        save_promocodes(promos)
        flash("Промокод сохранен", "success")
        return redirect(url_for("admin_promos"))

    return render_template("admin_promos.html", promos=promos)

@app.route("/admin/menu", methods=["GET", "POST"])
@login_required
def admin_menu_edit():
    if request.method == "POST":
        raw = request.form.get("menu_json", "").strip()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Ожидается JSON-объект (словарь категорий)")
        except Exception as e:
            flash(f"Ошибка JSON: {e}", "error")
            return redirect(url_for("admin_menu_edit"))
        MENU_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # save icons map if provided
        icons_raw = (request.form.get("menu_icons_json") or "").strip()
        if icons_raw != "":
            try:
                icons = json.loads(icons_raw)
                if isinstance(icons, dict):
                    save_menu_icons({str(k): str(v) for k, v in icons.items()})
            except Exception as e:
                flash(f"Иконки не сохранены: {e}", "error")
        flash("Меню сохранено", "success")
        return redirect(url_for("admin_menu_edit"))
    # prepare pretty json for textarea
    try:
        menu_text = json.dumps(load_menu(), ensure_ascii=False, indent=2)
    except Exception:
        menu_text = "{}"
    try:
        menu_icons_text = json.dumps(load_menu_icons(), ensure_ascii=False, indent=2)
    except Exception:
        menu_icons_text = "{}"
    return render_template("admin_menu.html", menu_text=menu_text, menu_icons_text=menu_icons_text, menu_images=_list_menu_images())

# Upload images to static/menu
@app.post("/admin/menu/upload")
@login_required
def admin_menu_upload():
    files = request.files.getlist("images")
    saved = 0
    MENU_DIR.mkdir(parents=True, exist_ok=True)
    for f in files:
        if not f or not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_IMG_EXT:
            flash(f"Файл {f.filename} пропущен: недопустимый формат", "error")
            continue
        fname = secure_filename(Path(f.filename).name)
        target = MENU_DIR / fname
        n = 1
        while target.exists():
            target = MENU_DIR / f"{Path(fname).stem}-{n}{Path(fname).suffix}"
            n += 1
        f.save(str(target))
        saved += 1
    if saved:
        flash(f"Загружено файлов: {saved}", "success")
    return redirect(url_for("admin_menu_edit"))

# Persist order of images for static/menu
@app.post("/admin/menu/order")
@login_required
def admin_menu_order():
    """Persist order/indices for images in static/menu/ to data/menu_images.json.
    Accepts either:
      - form['order']   = JSON list of names ["a.jpg", "b.jpg", ...]
      - form['indices'] = JSON object {"a.jpg": 10, "b.jpg": 20, ...}
    """
    raw_order = (request.form.get("order") or "").strip()
    raw_indices = (request.form.get("indices") or "").strip()

    names_from_order: list[str] | None = None
    if raw_order:
        try:
            data = json.loads(raw_order)
            if isinstance(data, list):
                names_from_order = [str(Path(x).name) for x in data if isinstance(x, str)]
        except Exception:
            names_from_order = None  # ignore malformed 'order'

    indices_map: dict[str, float] | None = None
    if names_from_order is None:  # try indices
        if raw_indices == "":
            indices_map = {}
        else:
            try:
                data = json.loads(raw_indices)
                if isinstance(data, dict):
                    indices_map = {str(Path(k).name): float(v) for k, v in data.items()}
                else:
                    flash("Неверные данные порядка: 'indices' должен быть объектом JSON", "error")
                    return redirect(url_for("admin_menu_edit"))
            except Exception:
                flash("Неверные данные порядка: не удалось разобрать JSON 'indices'", "error")
                return redirect(url_for("admin_menu_edit"))

    MENU_DIR.mkdir(parents=True, exist_ok=True)

    all_files = [p for p in MENU_DIR.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_IMG_EXT]
    by_name = {p.name: p for p in all_files}

    ordered: list[str] = []
    indices_final: dict[str, int] = {}

    if names_from_order is not None:  # explicit order dominates
        seen = set()
        for name in names_from_order:
            if name in by_name and name not in seen:
                ordered.append(name)
                seen.add(name)
        # append the rest alphabetically
        for name in sorted(by_name.keys(), key=lambda s: s.lower()):
            if name not in seen:
                ordered.append(name)
        # keep indices if provided (optional)
        if raw_indices:
            try:
                data = json.loads(raw_indices)
                if isinstance(data, dict):
                    indices_final = {str(Path(k).name): int(v) for k, v in data.items()}
            except Exception:
                pass
    else:  # indices_map is not None
        present = []
        missing = []
        for name in by_name.keys():
            if name in indices_map:
                present.append((indices_map[name], name))
            else:
                missing.append(name)
        present.sort(key=lambda t: (t[0], t[1].lower()))
        ordered = [name for value, name in present]
        ordered.extend(sorted(missing, key=lambda s: s.lower()))
        indices_final = {name: int(value) for value, name in present}

    _save_menu_images_meta(ordered, indices_final)
    flash("Порядок изображений сохранён", "success")
    return redirect(url_for("admin_menu_edit"))

@app.post("/admin/menu/delete")
@login_required
def admin_menu_delete():
    name = request.form.get("name", "")
    if not name:
        abort(400)
    p = (MENU_DIR / name).resolve()
    if MENU_DIR.resolve() not in p.parents or p.suffix.lower() not in ALLOWED_IMG_EXT:
        abort(400)
    if p.exists():
        p.unlink()
        flash(f"Удалено: {name}", "success")
    return redirect(url_for("admin_menu_edit"))

# ===== Item-level image upload/delete (admin) =====

def _menu_read() -> Dict:
    try:
        with open(MENU_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _menu_write(data: Dict) -> None:
    MENU_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MENU_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MENU_PATH)


def _update_item_image(data: Dict, section: str, subsection: Optional[str], item_name: str,
                        section_idx: int, sub_idx: int, item_idx: int,
                        new_path: Optional[str]) -> bool:
    """Set or clear image for a specific item. Works for flat sections and sections with subsections."""
    if not isinstance(data, dict):
        return False
    cat = data.get(section)
    if cat is None:
        return False

    # Resolve list of items in the targeted container
    items = None
    if isinstance(cat, dict) and "subsections" in cat:
        subs = cat.get("subsections") or {}
        if subsection and subsection in subs:
            items = subs.get(subsection)
        elif isinstance(sub_idx, int) and 0 <= sub_idx < len(subs):
            key = list(subs.keys())[sub_idx]
            items = subs.get(key)
        if not isinstance(items, list):
            return False
    elif isinstance(cat, list):
        items = cat
    else:
        return False

    # Find item by name, fallback by index
    idx = None
    if item_name:
        for i, it in enumerate(items):
            if isinstance(it, dict) and it.get("name") == item_name:
                idx = i
                break
    if idx is None and isinstance(item_idx, int) and 0 <= item_idx < len(items):
        idx = item_idx
    if idx is None:
        return False

    it = items[idx]
    if not isinstance(it, dict):
        return False

    if new_path:
        it["image"] = new_path
    else:
        it.pop("image", None)
    return True


@app.post("/admin/menu/item-image/upload")
@login_required
def admin_menu_item_image_upload():
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "no file"}), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_IMG_EXT:
        return jsonify({"ok": False, "error": "unsupported format"}), 400

    # Save file to static/menu_items
    MENU_ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    safe = secure_filename(Path(file.filename).name)
    target = MENU_ITEMS_DIR / safe
    n = 1
    while target.exists():
        target = MENU_ITEMS_DIR / f"{Path(safe).stem}-{n}{Path(safe).suffix}"
        n += 1
    file.save(str(target))
    rel_path = f"menu_items/{target.name}"

    # Identify item context
    section = request.form.get("section", "")
    subsection = request.form.get("subsection") or None
    item_name = request.form.get("item_name", "")
    def _to_int(v, default=-1):
        try:
            return int(v)
        except Exception:
            return default
    section_idx = _to_int(request.form.get("section_idx"), -1)
    sub_idx = _to_int(request.form.get("sub_idx"), -1)
    item_idx = _to_int(request.form.get("item_idx"), -1)

    data = _menu_read()
    if not _update_item_image(data, section, subsection, item_name, section_idx, sub_idx, item_idx, rel_path):
        # rollback file if mapping failed
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({"ok": False, "error": "item not found"}), 400

    _menu_write(data)
    return jsonify({"ok": True, "path": rel_path})


@app.post("/admin/menu/item-image/delete")
@login_required
def admin_menu_item_image_delete():
    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        payload = {}
    section = payload.get("section", "")
    subsection = payload.get("subsection") or None
    item_name = payload.get("item_name", "")
    image_path = payload.get("image", "")

    data = _menu_read()
    if not _update_item_image(data, section, subsection, item_name, -1, -1, -1, None):
        return jsonify({"ok": False, "error": "item not found"}), 400

    # Delete file only if it is inside static/menu_items
    if isinstance(image_path, str) and image_path.startswith("menu_items/"):
        p = (STATIC_DIR / image_path).resolve()
        try:
            if MENU_ITEMS_DIR.resolve() in p.parents and p.exists():
                p.unlink()
        except Exception:
            pass

    _menu_write(data)
    return jsonify({"ok": True})

@app.route("/admin/content", methods=["GET", "POST"])
@login_required
def admin_content():
    if request.method == "POST":
        old_blocks = load_content()
        raw = (request.form.get("content_json", "") or "").strip()
        try:
            data = json.loads(raw) if raw else []
            if not isinstance(data, list):
                raise ValueError("Ожидается JSON-массив блоков")
        except Exception as e:
            flash(f"Ошибка JSON: {e}", "error")
            return redirect(url_for("admin_content"))
        save_content(data)
        _cleanup_unused_content_images(old_blocks, data)
        flash("Контент сохранён", "success")
        return redirect(url_for("admin_content"))
    try:
        content_text = json.dumps(load_content(), ensure_ascii=False, indent=2)
    except Exception:
        content_text = "[]"
    return render_template("admin_content.html", content_text=content_text)

@app.post("/admin/content/upload")
@login_required
def admin_content_upload():
    f = request.files.get("image")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "no file"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_IMG_EXT:
        return jsonify({"ok": False, "error": "unsupported format"}), 400
    CONTENT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fname = secure_filename(Path(f.filename).name)
    target = CONTENT_UPLOAD_DIR / fname
    n = 1
    while target.exists():
        target = CONTENT_UPLOAD_DIR / f"{Path(fname).stem}-{n}{Path(fname).suffix}"
        n += 1
    f.save(str(target))
    web_path = url_for("static", filename=f"uploads/content/{target.name}")
    return jsonify({"ok": True, "path": web_path})

@app.route("/admin/gallery", methods=["GET", "POST"])
@login_required
def admin_gallery():
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    if request.method == "POST":
        files = request.files.getlist("images")
        saved = 0
        for f in files:
            if not f or not f.filename:
                continue
            ext = Path(f.filename).suffix.lower()
            if ext not in ALLOWED_IMG_EXT:
                flash(f"Файл {f.filename} пропущен: недопустимый формат", "error")
                continue
            fname = secure_filename(Path(f.filename).name)
            target = GALLERY_DIR / fname
            n = 1
            while target.exists():
                target = GALLERY_DIR / f"{Path(fname).stem}-{n}{Path(fname).suffix}"
                n += 1
            f.save(str(target))
            saved += 1
        if saved:
            flash(f"Загружено файлов: {saved}", "success")
        return redirect(url_for("admin_gallery"))
    files = []
    if GALLERY_DIR.exists():
        for p in sorted(GALLERY_DIR.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and p.suffix.lower() in ALLOWED_IMG_EXT:
                files.append({"name": p.name, "url": url_for("static", filename=f"gallery/{p.name}")})
    return render_template("admin_gallery.html", files=files)

@app.post("/admin/gallery/delete")
@login_required
def admin_gallery_delete():
    name = request.form.get("name", "")
    if not name:
        abort(400)
    p = (GALLERY_DIR / name).resolve()
    if GALLERY_DIR.resolve() not in p.parents or p.suffix.lower() not in ALLOWED_IMG_EXT:
        abort(400)
    if p.exists():
        p.unlink()
        flash(f"Удалено: {name}", "success")
    return redirect(url_for("admin_gallery"))

@app.get("/api/site")
def api_site():
    return jsonify(load_site())

@app.get("/api/content")
def api_content():
    return jsonify(load_content())

@app.get("/api/cart_count")
def api_cart_count():
    return jsonify({"count": calc_cart_count()})

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if check_admin_password(username, password):
            # Начинаем чистую сессию и не делаем её постоянной
            session.clear()
            session["is_admin"] = True
            session["admin_user"] = username
            session.permanent = False
            flash("Добро пожаловать!", "success")
            return redirect(request.args.get("next") or url_for("admin_hero"))
        flash(
            "Неверные логин или пароль. Убедитесь, что настроен файл data/admin.json с полями username и password_hash.",
            "error",
        )
    return render_template("admin_login.html")


@app.get("/admin/logout")
def admin_logout():
    # Полностью очищаем сессию, чтобы в следующий раз обязательно вводить логин/пароль
    session.clear()
    flash("Вы вышли.", "success")
    resp = redirect(url_for("admin_login"))
    return resp


@app.route("/admin/password", methods=["GET", "POST"])
@login_required
def admin_password():
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        confirm = request.form.get("confirm", "")
        username = request.form.get("username", ADMIN_USER or "admin").strip() or (ADMIN_USER or "admin")

        # Validate current password
        if not ADMIN_HASH or not check_password_hash(ADMIN_HASH, current):
            flash("Неверный текущий пароль", "error")
            return redirect(url_for("admin_password"))
        # New password checks
        if len(new) < 6:
            flash("Новый пароль должен быть не короче 6 символов", "error")
            return redirect(url_for("admin_password"))
        if new != confirm:
            flash("Пароли не совпадают", "error")
            return redirect(url_for("admin_password"))
        # Save
        new_hash = generate_password_hash(new)
        _save_admin_file(username, new_hash)
        flash("Пароль успешно изменён", "success")
        return redirect(url_for("admin_hero"))

    return render_template("admin_password.html", username=ADMIN_USER or "admin")


@app.route("/admin/hero", methods=["GET", "POST"])
@login_required
def admin_hero():
    HERO_DIR.mkdir(parents=True, exist_ok=True)

    def _load_order() -> list[str]:
        try:
            with open(HERO_ORDER_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(Path(x).name) for x in data if isinstance(x, str)]
        except Exception:
            pass
        return []

    def _save_order(order: list[str]) -> None:
        try:
            HERO_ORDER_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(HERO_ORDER_PATH, "w", encoding="utf-8") as f:
                json.dump(order, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    if request.method == "POST":
        files = request.files.getlist("images")
        saved = 0
        for f in files:
            if not f or not f.filename:
                continue
            ext = Path(f.filename).suffix.lower()
            if ext not in ALLOWED_IMG_EXT:
                flash(f"Недопустимое расширение: {f.filename}", "error")
                continue
            fname = secure_filename(Path(f.filename).name)
            base = Path(fname).stem
            ext = Path(fname).suffix
            target = HERO_DIR / fname
            n = 1
            while target.exists():
                target = HERO_DIR / f"{base}-{n}{ext}"
                n += 1
            f.save(str(target))
            saved += 1
            order = _load_order()
            if target.name not in order:
                order.append(target.name)
                _save_order(order)
        if saved:
            flash(f"Загружено файлов: {saved}", "success")
        return redirect(url_for("admin_hero"))

    order_list = _load_order()
    order_index = {name: idx for idx, name in enumerate(order_list)}
    files = []
    if HERO_DIR.exists():
        for p in sorted(HERO_DIR.iterdir(), key=lambda x: order_index.get(x.name, 10_000_000)):
            if p.is_file() and p.suffix.lower() in ALLOWED_IMG_EXT:
                files.append({"name": p.name, "url": url_for("static", filename=f"hero/{p.name}")})
    return render_template("admin_hero.html", files=files)


@app.post("/admin/hero/delete")
@login_required
def admin_hero_delete():
    name = request.form.get("name", "")
    if not name:
        abort(400)
    p = (HERO_DIR / name).resolve()
    if HERO_DIR.resolve() not in p.parents or p.suffix.lower() not in ALLOWED_IMG_EXT:
        abort(400)
    if p.exists():
        p.unlink()
        flash(f"Удалено: {name}", "success")
        try:
            if HERO_ORDER_PATH.exists():
                with open(HERO_ORDER_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    new_data = [x for x in data if x != name]
                    with open(HERO_ORDER_PATH, "w", encoding="utf-8") as f:
                        json.dump(new_data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return redirect(url_for("admin_hero"))


@app.post("/admin/hero/order")
@login_required
def admin_hero_order():
    """Persist manual ordering of hero images."""
    HERO_DIR.mkdir(parents=True, exist_ok=True)
    try:
        payload = request.get_json(force=True, silent=True) or {}
        raw_order = payload.get("order")
        if not isinstance(raw_order, list):
            return jsonify(ok=False, error="bad payload"), 400
        names: list[str] = []
        for item in raw_order:
            name = item.get("name") if isinstance(item, dict) else item
            if not isinstance(name, str):
                return jsonify(ok=False, error="bad item"), 400
            fname = Path(name).name
            p = HERO_DIR / fname
            if not p.exists() or p.suffix.lower() not in ALLOWED_IMG_EXT:
                return jsonify(ok=False, error="file missing"), 400
            names.append(fname)
        HERO_ORDER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HERO_ORDER_PATH, "w", encoding="utf-8") as f:
            json.dump(names, f, ensure_ascii=False, indent=2)
        return jsonify(ok=True, order=names)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
