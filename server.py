#!/usr/bin/env python3
"""
RenzoGPT Server - Sirve el frontend y conecta con Gemini via el router.
Sin dependencias externas, solo stdlib.

Uso:
    python3 server.py              # puerto 8080
    python3 server.py 3000         # puerto custom
    PORT=8080 pm2 start server.py --interpreter python3 --name renzogpt
"""

import sys
import os
import json
import base64
import hashlib
import hmac
import re
import sqlite3
import shlex
import subprocess
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from cryptography.fernet import Fernet

# Importar el router
sys.path.insert(0, os.path.expanduser("~"))
from importlib import import_module
gemini_router = import_module("gemini-router")
GeminiRouter = gemini_router.GeminiRouter

router = GeminiRouter()

# ═══════════════ SQLITE + ENCRYPTION ═══════════════

_DB_PATH = os.path.expanduser("~/.lola-db.sqlite")
_MASTER_KEY_PATH = os.path.expanduser("~/.lola-master.key")
_db_lock = threading.Lock()


def _load_or_create_master_key():
    """Carga la master key de LOLA_ENCRYPTION_KEY env var o ~/.lola-master.key.
    Si no existe ninguna, genera una nueva y la guarda."""
    env_key = os.environ.get("LOLA_ENCRYPTION_KEY", "").strip()
    if env_key:
        # Asegurar que es una Fernet key válida (44 bytes base64)
        try:
            Fernet(env_key.encode())
            return env_key.encode()
        except Exception:
            print("[DB] LOLA_ENCRYPTION_KEY inválida, ignorando")

    # Intentar cargar de archivo
    if os.path.exists(_MASTER_KEY_PATH):
        with open(_MASTER_KEY_PATH, "rb") as f:
            key = f.read().strip()
        try:
            Fernet(key)
            print(f"[DB] Master key cargada desde {_MASTER_KEY_PATH}")
            return key
        except Exception:
            print(f"[DB] Master key corrupta en {_MASTER_KEY_PATH}, generando nueva")

    # Generar nueva
    key = Fernet.generate_key()
    with open(_MASTER_KEY_PATH, "wb") as f:
        f.write(key)
    os.chmod(_MASTER_KEY_PATH, 0o600)
    print(f"[DB] Master key generada y guardada en {_MASTER_KEY_PATH}")
    print(f"[DB] IMPORTANTE: Hacé backup de este archivo. Sin él no se pueden leer los datos encriptados.")
    return key


_fernet = Fernet(_load_or_create_master_key())


def _encrypt(plaintext):
    """Encripta texto con Fernet (AES-128-CBC + HMAC). Retorna str base64."""
    if not plaintext:
        return ""
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def _decrypt(ciphertext):
    """Desencripta texto Fernet. Retorna str."""
    if not ciphertext:
        return ""
    return _fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def _db_conn():
    """Crea una conexión a SQLite con WAL mode."""
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _hash_key(value):
    """Hash determinístico para usar como clave de búsqueda (no reversible)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _db_init():
    """Crea la DB y tablas si no existen."""
    with _db_lock:
        conn = _db_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tenants (
                    phone_hash TEXT PRIMARY KEY,
                    phone TEXT,
                    email TEXT,
                    plan TEXT,
                    business_data TEXT,
                    system_prompt TEXT,
                    created TEXT,
                    updated TEXT
                );
                CREATE TABLE IF NOT EXISTS subscribers (
                    email_hash TEXT PRIMARY KEY,
                    email TEXT,
                    plan TEXT,
                    status TEXT,
                    mp_id TEXT,
                    phone TEXT,
                    updated TEXT
                );
            """)
            conn.commit()
            print(f"[DB] Inicializada: {_DB_PATH}")
        finally:
            conn.close()


def _db_tenant_load(phone):
    """Carga un tenant desde SQLite. Retorna dict o None."""
    ph = _hash_key(phone)
    with _db_lock:
        conn = _db_conn()
        try:
            row = conn.execute("SELECT * FROM tenants WHERE phone_hash = ?", (ph,)).fetchone()
            if not row:
                return None
            return {
                "phone": _decrypt(row["phone"]) if row["phone"] else phone,
                "email": _decrypt(row["email"]) if row["email"] else "",
                "plan": row["plan"] or "",
                "data": json.loads(_decrypt(row["business_data"])) if row["business_data"] else {},
                "system_prompt": _decrypt(row["system_prompt"]) if row["system_prompt"] else "",
                "created": row["created"] or "",
                "updated": row["updated"] or "",
            }
        except Exception as e:
            print(f"[DB] Error cargando tenant {phone}: {e}")
            return None
        finally:
            conn.close()


def _db_tenant_save(phone, data):
    """Guarda un tenant en SQLite con campos sensibles encriptados."""
    ph = _hash_key(phone)
    with _db_lock:
        conn = _db_conn()
        try:
            business_data = data.get("data", {})
            conn.execute("""
                INSERT OR REPLACE INTO tenants (phone_hash, phone, email, plan, business_data, system_prompt, created, updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ph,
                _encrypt(phone),
                _encrypt(data.get("email", "")),
                data.get("plan", ""),
                _encrypt(json.dumps(business_data, ensure_ascii=False)) if business_data else "",
                _encrypt(data.get("system_prompt", "")),
                data.get("created", time.strftime("%Y-%m-%d %H:%M")),
                time.strftime("%Y-%m-%d %H:%M"),
            ))
            conn.commit()
            print(f"[DB] Tenant guardado: {phone}")
        except Exception as e:
            print(f"[DB] Error guardando tenant {phone}: {e}")
        finally:
            conn.close()


def _db_subscribers_load():
    """Carga todos los subscribers desde SQLite. Retorna dict {email: info}."""
    with _db_lock:
        conn = _db_conn()
        try:
            rows = conn.execute("SELECT * FROM subscribers").fetchall()
            subs = {}
            for row in rows:
                email = _decrypt(row["email"]) if row["email"] else ""
                if not email:
                    continue
                subs[email] = {
                    "plan": row["plan"] or "",
                    "status": row["status"] or "",
                    "mp_id": _decrypt(row["mp_id"]) if row["mp_id"] else "",
                    "phone": _decrypt(row["phone"]) if row["phone"] else "",
                    "updated": row["updated"] or "",
                }
            return subs
        except Exception as e:
            print(f"[DB] Error cargando subscribers: {e}")
            return {}
        finally:
            conn.close()


def _db_subscribers_save(subs):
    """Guarda todos los subscribers (reemplaza la tabla completa)."""
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute("DELETE FROM subscribers")
            for email, info in subs.items():
                eh = _hash_key(email)
                conn.execute("""
                    INSERT INTO subscribers (email_hash, email, plan, status, mp_id, phone, updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    eh,
                    _encrypt(email),
                    info.get("plan", ""),
                    info.get("status", ""),
                    _encrypt(info.get("mp_id", "")),
                    _encrypt(info.get("phone", "")),
                    info.get("updated", ""),
                ))
            conn.commit()
        except Exception as e:
            print(f"[DB] Error guardando subscribers: {e}")
        finally:
            conn.close()


def _db_subscriber_upsert(email, info):
    """Inserta o actualiza un subscriber individual."""
    eh = _hash_key(email)
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO subscribers (email_hash, email, plan, status, mp_id, phone, updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                eh,
                _encrypt(email),
                info.get("plan", ""),
                info.get("status", ""),
                _encrypt(info.get("mp_id", "")),
                _encrypt(info.get("phone", "")),
                info.get("updated", ""),
            ))
            conn.commit()
        except Exception as e:
            print(f"[DB] Error upserting subscriber {email}: {e}")
        finally:
            conn.close()


def _db_migrate_from_json():
    """Migra datos desde archivos JSON viejos a SQLite. Renombra originales a .bak."""
    tenants_dir = os.path.expanduser("~/.lola-tenants")
    subscribers_path = os.path.expanduser("~/.lola-subscribers.json")
    migrated_tenants = 0
    migrated_subs = 0

    # Migrar tenants
    if os.path.isdir(tenants_dir):
        json_files = [f for f in os.listdir(tenants_dir) if f.endswith(".json")]
        for fname in json_files:
            fpath = os.path.join(tenants_dir, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                phone = data.get("phone", fname.replace(".json", ""))
                _db_tenant_save(phone, data)
                os.rename(fpath, fpath + ".bak")
                migrated_tenants += 1
            except Exception as e:
                print(f"[DB] Error migrando tenant {fname}: {e}")

    # Migrar subscribers
    if os.path.isfile(subscribers_path):
        try:
            with open(subscribers_path) as f:
                subs = json.load(f)
            if subs:
                _db_subscribers_save(subs)
                migrated_subs = len(subs)
            os.rename(subscribers_path, subscribers_path + ".bak")
        except Exception as e:
            print(f"[DB] Error migrando subscribers: {e}")

    if migrated_tenants or migrated_subs:
        print(f"[DB] Migrados {migrated_tenants} tenants y {migrated_subs} subscribers desde JSON")


# Inicializar DB al arrancar
_db_init()
_db_migrate_from_json()

# WhatsApp Business API config
_wa_config_path = os.path.expanduser("~/.whatsapp-config.json")
try:
    with open(_wa_config_path) as f:
        WA_CONFIG = json.load(f)
    print(f"[WhatsApp] Config cargada desde {_wa_config_path}")
except FileNotFoundError:
    WA_CONFIG = None
    print(f"[WhatsApp] No se encontró {_wa_config_path}, webhook desactivado")

# Instagram Messaging API config
_ig_config_path = os.path.expanduser("~/.instagram-config.json")
try:
    with open(_ig_config_path) as f:
        IG_CONFIG = json.load(f)
    if IG_CONFIG.get("access_token") and IG_CONFIG.get("ig_user_id"):
        print(f"[Instagram] Config cargada desde {_ig_config_path}")
    else:
        print(f"[Instagram] Config encontrada pero incompleta, webhook desactivado")
        IG_CONFIG = None
except FileNotFoundError:
    IG_CONFIG = None
    print(f"[Instagram] No se encontró {_ig_config_path}, webhook desactivado")

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

# ═══════════════ OTP / AUTH / TENANTS ═══════════════

# OTP pendientes: phone → {code, created, attempts}
_otp_pending = {}
_OTP_EXPIRE_SECS = 300       # 5 min
_OTP_MAX_ATTEMPTS = 3
_otp_send_log = {}            # phone → [timestamps]
_OTP_MAX_SENDS_PER_HOUR = 3

# Sesiones autenticadas: token → {phone, email, plan, created, last_active, onboarding_complete}
_auth_sessions = {}
_AUTH_SESSION_TTL = 7200      # 2 horas

# Tenants (datos de cada comerciante) — ahora en SQLite via _db_tenant_load/_db_tenant_save


def _normalize_phone(phone):
    """Normaliza un teléfono a solo dígitos, agrega 598 si falta."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 8:  # número uruguayo sin código de país
        digits = "598" + digits
    elif len(digits) == 9 and digits.startswith("9"):
        digits = "598" + digits
    return digits


def _tenant_load(phone):
    """Carga datos de un tenant. Wrapper → SQLite."""
    return _db_tenant_load(phone)


def _tenant_save(phone, data):
    """Guarda datos de un tenant. Wrapper → SQLite."""
    _db_tenant_save(phone, data)


def _cleanup_otp_and_sessions():
    """Limpieza lazy de OTPs expirados y sesiones viejas."""
    now = time.time()
    # OTPs
    expired = [p for p, v in _otp_pending.items() if now - v["created"] > _OTP_EXPIRE_SECS]
    for p in expired:
        del _otp_pending[p]
    # Sesiones
    expired = [t for t, v in _auth_sessions.items() if now - v["last_active"] > _AUTH_SESSION_TTL]
    for t in expired:
        del _auth_sessions[t]
    # Rate limit log: limpiar timestamps viejos
    hour_ago = now - 3600
    for phone in list(_otp_send_log.keys()):
        _otp_send_log[phone] = [ts for ts in _otp_send_log[phone] if ts > hour_ago]
        if not _otp_send_log[phone]:
            del _otp_send_log[phone]

# MercadoPago config
_mp_config_path = os.path.expanduser("~/.mercadopago-config.json")
try:
    with open(_mp_config_path) as f:
        MP_CONFIG = json.load(f)
    if MP_CONFIG.get("access_token"):
        print(f"[MercadoPago] Config cargada desde {_mp_config_path}")
    else:
        print(f"[MercadoPago] Config encontrada pero sin access_token, endpoints desactivados")
        MP_CONFIG = None
except FileNotFoundError:
    MP_CONFIG = None
    print(f"[MercadoPago] No se encontró {_mp_config_path}, endpoints desactivados")


def _mp_save_config():
    """Guarda la config de MP a disco."""
    with open(_mp_config_path, "w") as f:
        json.dump(MP_CONFIG, f, indent=2, ensure_ascii=False)


def _mp_load_subscribers():
    """Carga suscriptores. Wrapper → SQLite."""
    return _db_subscribers_load()


def _mp_save_subscribers(subs):
    """Guarda suscriptores. Wrapper → SQLite."""
    _db_subscribers_save(subs)


def _mp_api(method, path, data=None):
    """Hace una request a la API de MercadoPago."""
    url = f"https://api.mercadopago.com{path}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {MP_CONFIG['access_token']}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"ok": True, "data": json.loads(resp.read()), "status": resp.status}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"[MercadoPago] API error {e.code}: {err_body[:500]}")
        return {"ok": False, "error": err_body, "status": e.code}
    except Exception as e:
        print(f"[MercadoPago] API exception: {e}")
        return {"ok": False, "error": str(e), "status": 0}


def _mp_create_preference(amount, description, phone):
    """Crea una preferencia de pago en MercadoPago y retorna el init_point (link de pago)."""
    if not MP_CONFIG:
        return None
    data = {
        "items": [{
            "title": description,
            "quantity": 1,
            "unit_price": amount,
            "currency_id": "UYU",
        }],
        "external_reference": phone,
        "back_urls": {
            "success": "https://lola.expensetracker.com.uy/pago-ok",
            "failure": "https://lola.expensetracker.com.uy/pago-error",
            "pending": "https://lola.expensetracker.com.uy/pago-pendiente",
        },
        "notification_url": "https://lola.expensetracker.com.uy/mp-webhook",
        "auto_return": "approved",
    }
    resp = _mp_api("POST", "/checkout/preferences", data)
    if resp["ok"]:
        link = resp["data"].get("init_point", "")
        print(f"[MercadoPago] Preference creada: ${amount} - {description} - phone={phone} → {link}")
        return link
    else:
        print(f"[MercadoPago] Error creando preference: {resp.get('error', '?')}")
        return None


def _mp_check_payment(phone):
    """Busca el último pago reciente por external_reference (phone) y retorna info."""
    if not MP_CONFIG:
        return None
    resp = _mp_api("GET", f"/v1/payments/search?external_reference={phone}&sort=date_created&criteria=desc&limit=1")
    if not resp["ok"]:
        print(f"[MercadoPago] Error buscando pagos para {phone}: {resp.get('error', '?')}")
        return None
    results = resp["data"].get("results", [])
    if not results:
        return {"found": False}
    pay = results[0]
    return {
        "found": True,
        "status": pay.get("status", ""),
        "status_detail": pay.get("status_detail", ""),
        "amount": pay.get("transaction_amount", 0),
        "description": pay.get("description", ""),
        "date": pay.get("date_created", ""),
    }


def _mp_check_subscription(phone):
    """Busca si hay una suscripción activa para este teléfono en los subscribers locales."""
    subs = _mp_load_subscribers()
    for email, info in subs.items():
        if info.get("phone", "").endswith(phone[-8:]):  # comparar últimos 8 dígitos
            return {
                "found": True,
                "plan": info.get("plan", "desconocido"),
                "status": info.get("status", ""),
                "email": email,
            }
    return {"found": False}


# Whitelist de prefijos de comandos permitidos
COMMAND_WHITELIST = [
    "pm2 ",
    "pm2",
    "systemctl status ",
    "df ",
    "df",
    "free ",
    "free",
    "uptime",
    "top -bn1",
    "cat /tmp/",
    "tail ",
    "head ",
    "ls ",
    "ls",
    "pwd",
    "whoami",
    "hostname",
    "ip addr",
    "ping -c",
    "python3 ~/gemini-router.py --status",
    "docker ps",
    "docker stats --no-stream",
]


SHELL_OPERATORS = ["&&", "||", ";", "|", "`", "$(", "${", ">", "<", "\n"]


def _validate_command(cmd):
    """Valida que el comando esté en la whitelist y no tenga operadores de shell."""
    cmd = cmd.strip()
    # Bloquear encadenamiento de comandos
    for op in SHELL_OPERATORS:
        if op in cmd:
            return False
    for prefix in COMMAND_WHITELIST:
        if cmd == prefix or cmd.startswith(prefix):
            return True
    return False


def _execute_command(cmd):
    """Ejecuta un comando validado con timeout."""
    try:
        result = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=30,
        )
        output = result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        return {"ok": True, "output": output.strip() or "(sin output)", "exit_code": result.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "Timeout: el comando tardó más de 30 segundos."}
    except Exception as e:
        return {"ok": False, "output": f"Error: {e}"}


LOLA_SALES_PROMPT = (
    "Sos Lola. Sos una IA que atiende clientes por WhatsApp para negocios, las 24hs. "
    "Estás hablando con dueños de tiendas/negocios que quieren contratarte. "
    "SIEMPRE hablá en primera persona: \"yo atiendo\", \"yo respondo\", \"yo cierro ventas\". "
    "NUNCA hables de vos misma en tercera persona como \"Lola puede\" o \"ella hace\". Vos SOS Lola.\n"
    "\n"
    "CÓMO HABLAR:\n"
    "- Escribís como una persona REAL en WhatsApp. Nada de texto perfecto.\n"
    "- Mensajes CORTOS, 1-2 oraciones máximo. Como un chat entre amigos.\n"
    "- NUNCA uses signos de exclamación invertidos (¡). Nadie escribe así en WhatsApp.\n"
    "- NUNCA uses signos de interrogación invertidos (¿). Tampoco.\n"
    "- NUNCA uses asteriscos ni negritas (**texto**). Es WhatsApp, no markdown.\n"
    "- Minúsculas. No capitalices todo perfecto. \"si claro\" en vez de \"Sí, claro!\".\n"
    "- Hablás en uruguayo: \"dale\", \"ta\", \"bárbaro\", \"de una\", \"re\", \"posta\".\n"
    "- NO hagas listas. Si querés mencionar varias cosas, contalo en una oración.\n"
    "- NO repitas info que ya dijiste.\n"
    "- Sos directa y copada, como hablando con un conocido.\n"
    "\n"
    "QUÉ ES LOLA:\n"
    "- Chatbot de IA que se conecta al WhatsApp del negocio y atiende clientes automáticamente.\n"
    "- Responde consultas, muestra catálogo y precios, consulta stock en tiempo real.\n"
    "- Cobra por vos: genera y manda el link de pago de MercadoPago directo al cliente.\n"
    "- Informa estado de pedidos y pagos.\n"
    "- Entiende texto, audios, fotos y ubicación.\n"
    "- Funciona 24/7, atiende múltiples clientes a la vez, nunca se cansa.\n"
    "- Habla como una persona real, no como un bot genérico.\n"
    "\n"
    "PARA QUIÉN:\n"
    "- Tiendas de ropa, accesorios, electrónica.\n"
    "- Restaurantes, cafeterías, delivery.\n"
    "- Peluquerías, centros de estética.\n"
    "- Inmobiliarias, talleres, cualquier negocio que venda por WhatsApp.\n"
    "\n"
    "PLANES:\n"
    "- **Básico** ($1.290 UYU/mes): Catálogo manual, chat inteligente, respuestas 24/7, hasta 500 conversaciones/mes.\n"
    "- **Pro** ($3.490 UYU/mes): Todo lo del Básico + integración con tu sistema (Shopify, WooCommerce, Google Sheets), "
    "stock en tiempo real, creación de pedidos, links de pago automáticos, conversaciones ilimitadas.\n"
    "- Setup: sin costo de instalación. Te lo configuramos nosotros.\n"
    "\n"
    "CÓMO FUNCIONA:\n"
    "1. Nos pasás tu catálogo (productos, precios, stock).\n"
    "2. Conectamos Lola a tu número de WhatsApp Business.\n"
    "3. Lola empieza a atender clientes. Vos solo mirás las ventas.\n"
    "\n"
    "VENTAJAS:\n"
    "- No perdés más ventas por no responder a tiempo.\n"
    "- Atiende de noche, fines de semana, feriados.\n"
    "- Maneja múltiples clientes al mismo tiempo.\n"
    "- Se integra con tu sistema para datos reales (stock, pedidos, pagos).\n"
    "- Cuesta menos que un empleado y trabaja 24/7.\n"
    "\n"
    "COBROS Y PAGOS:\n"
    "- Cuando el cliente quiera pagar algo, usá el tag {{cobrar:MONTO:DESCRIPCION}} y yo lo reemplazo por el link de pago.\n"
    "- Ejemplo: \"dale, te paso el link {{cobrar:2500:Remera azul talle M}}\"\n"
    "- El MONTO va sin signo de pesos, solo el número. La DESCRIPCION es lo que compra.\n"
    "- Cuando el cliente pregunte si llegó su pago o diga \"ya pagué\", usá el tag {{estado_pago}} y yo te digo el estado.\n"
    "- Ejemplo: \"dejame chequear {{estado_pago}}\"\n"
    "- NUNCA muestres los tags al cliente, yo los proceso antes de que lleguen.\n"
    "\n"
    "CONTRATACIÓN:\n"
    "- Cuando el interesado quiera contratar o pagar, mandá el link de suscripción usando estos tags:\n"
    "  - Plan Básico ($1.290/mes): {{plan:basico}}\n"
    "  - Plan Pro ($3.490/mes): {{plan:pro}}\n"
    "- Ejemplo: \"dale, te paso el link para el básico {{plan:basico}}\"\n"
    "- NUNCA muestres los tags al cliente, yo los reemplazo por el link de MercadoPago automáticamente.\n"
    "- Si el cliente dice que quiere contratar pero no eligió plan, preguntale cuál prefiere y mandá el link.\n"
    "- Si el cliente quiere contratar, mandá el link sin vueltas.\n"
    "- Si el cliente pregunta si ya está suscripto o si le llegó el pago de la suscripción, usá {{estado_suscripcion}} para chequear.\n"
    "- Ejemplo: \"dejame ver {{estado_suscripcion}}\"\n"
    "\n"
    "REGLAS:\n"
    "- La meta es que el interesado quiera contratar Lola.\n"
    "- Si preguntan algo técnico que no sabés, decí que el equipo técnico se los explica en el onboarding.\n"
    "- Nunca inventes features que no existen.\n"
    "- Si piden contacto, deciles que pueden escribir a hola@lola.uy o agendar una demo.\n"
    "- Sos Lola. La mejor prueba de que funciona sos vos misma hablando con ellos ahora.\n"
)

WA_SYSTEM_PROMPT = LOLA_SALES_PROMPT

LOLA_ONBOARDING_PROMPT = (
    "Sos Lola. Estás en modo de configuración con un comerciante que acaba de contratar tu servicio. "
    "Tu objetivo es recopilar toda la información necesaria para armar el bot que atenderá a los CLIENTES de su negocio.\n"
    "\n"
    "CÓMO HABLAR:\n"
    "- Mensajes CORTOS, 1-2 oraciones. Como un chat entre conocidos.\n"
    "- Hablás en uruguayo: 'dale', 'ta', 'bárbaro', 'de una', 'genial'.\n"
    "- Sos directa, copada y eficiente.\n"
    "- NUNCA uses signos invertidos. NUNCA uses asteriscos ni negritas.\n"
    "- NO hagas listas. Contá las cosas en oraciones naturales.\n"
    "\n"
    "QUÉ NECESITÁS SABER (preguntá de a una cosa, en orden natural):\n"
    "1. Nombre del negocio\n"
    "2. Rubro (ropa, comida, servicios, etc.)\n"
    "3. Productos o servicios principales. Si el comerciante dice que tiene muchos o un catálogo grande, "
    "pedile solo las CATEGORÍAS principales (ej: 'medicamentos, perfumería, dermocosmetica'). "
    "NO insistas en precios ni en detallar cada producto. Aceptá lo que te diga y seguí adelante.\n"
    "4. Horarios de atención\n"
    "5. Ubicación / zona de cobertura\n"
    "6. Políticas de envío (si aplica)\n"
    "7. Políticas de cambio/devolución\n"
    "8. Tono/personalidad que quiere para el bot (formal, relajado, gracioso, etc.)\n"
    "9. Cualquier info extra que quiera que el bot sepa (ej: 'aceptamos débito y crédito', 'tenemos descuentos para jubilados')\n"
    "\n"
    "PROCESO:\n"
    "- Arrancá saludando y diciendo que vas a hacerle unas preguntas para configurar su Lola.\n"
    "- Hacé UNA pregunta a la vez. No bombardees con muchas.\n"
    "- Si el comerciante da info parcial, está bien, usá lo que te dio y avanzá.\n"
    "- Si dice que tiene un archivo, catálogo, o que 'son muchos', NO insistas. "
    "Decile que con las categorías o rubros principales alcanza y que después se puede agregar más detalle. Seguí con la próxima pregunta.\n"
    "- Si algo no aplica (ej: no hace envíos), está bien, seguí con lo siguiente.\n"
    "- Cuando tengas TODO, hacé un resumen corto de lo que entendiste y pedí confirmación.\n"
    "- Cuando el comerciante confirme que está todo bien, escribí EXACTAMENTE el tag {{onboarding_complete}} al final de tu mensaje.\n"
    "- SOLO escribí {{onboarding_complete}} cuando el comerciante haya confirmado explícitamente.\n"
    "\n"
    "REGLAS:\n"
    "- No inventes datos. Solo usá lo que el comerciante te diga.\n"
    "- NUNCA insistas si el comerciante dice que no puede detallar algo. Aceptá y seguí.\n"
    "- Si el comerciante quiere cambiar algo del resumen, ajustalo y pedí confirmación de nuevo.\n"
    "- Sé paciente y amable. Es su primera vez configurando esto.\n"
)

TENANT_EXTRACTION_PROMPT = (
    "Analizá la siguiente conversación entre Lola y un comerciante durante el onboarding. "
    "Extraé toda la información del negocio en formato JSON con estos campos:\n"
    "{\n"
    '  "nombre_negocio": "",\n'
    '  "rubro": "",\n'
    '  "categorias": ["categoria1", "categoria2"],\n'
    '  "productos_destacados": [{"nombre": "", "precio": ""}],\n'
    '  "horarios": "",\n'
    '  "ubicacion": "",\n'
    '  "envio": "",\n'
    '  "cambios_devoluciones": "",\n'
    '  "tono": "",\n'
    '  "info_extra": ""\n'
    "}\n"
    "NOTAS:\n"
    "- 'categorias' son los rubros/categorías de productos que maneja (ej: medicamentos, perfumería).\n"
    "- 'productos_destacados' solo si el comerciante mencionó productos específicos con precio. Si no, dejalo vacío [].\n"
    "- Respondé SOLO con el JSON, sin explicación ni markdown.\n"
    "\nConversación:\n"
)


def _generate_tenant_prompt(data):
    """Genera el system prompt que el bot del comerciante usará para atender a sus clientes."""
    nombre = data.get("nombre_negocio", "el negocio")
    rubro = data.get("rubro", "")
    categorias = data.get("categorias", [])
    productos = data.get("productos_destacados", data.get("productos", []))
    horarios = data.get("horarios", "")
    ubicacion = data.get("ubicacion", "")
    envio = data.get("envio", "")
    cambios = data.get("cambios_devoluciones", "")
    tono = data.get("tono", "amable y natural")
    extra = data.get("info_extra", "")

    cat_text = ", ".join(categorias) if categorias else ""
    prod_text = ""
    if productos:
        items = []
        for p in productos:
            n = p.get("nombre", "")
            pr = p.get("precio", "")
            items.append(f"- {n}: ${pr}" if pr else f"- {n}")
        prod_text = "\n".join(items)

    prompt = (
        f"Sos el asistente virtual de {nombre}."
    )
    if rubro:
        prompt += f" Es un negocio de {rubro}."
    prompt += (
        "\nAtendés clientes por WhatsApp. Hablás como una persona real, no como un robot.\n"
        "\nCÓMO HABLAR:\n"
        f"- Tu tono es: {tono}\n"
        "- Mensajes CORTOS, como en un chat real.\n"
        "- NUNCA uses signos invertidos. NUNCA uses asteriscos ni negritas.\n"
        "- Hablás en español rioplatense/uruguayo.\n"
    )
    if cat_text:
        prompt += f"\nCATEGORÍAS DE PRODUCTOS: {cat_text}\n"
    if prod_text:
        prompt += f"\nPRODUCTOS DESTACADOS:\n{prod_text}\n"
    if horarios:
        prompt += f"\nHORARIOS: {horarios}\n"
    if ubicacion:
        prompt += f"\nUBICACIÓN: {ubicacion}\n"
    if envio:
        prompt += f"\nENVÍOS: {envio}\n"
    if cambios:
        prompt += f"\nCAMBIOS/DEVOLUCIONES: {cambios}\n"
    if extra:
        prompt += f"\nINFO ADICIONAL: {extra}\n"
    prompt += (
        "\nREGLAS:\n"
        "- Respondé consultas sobre productos, precios, horarios y envíos.\n"
        "- Si preguntan por algo que no tenés en el catálogo, decí que no lo manejás.\n"
        "- Nunca inventes datos que no te dieron.\n"
        "- Sé amable y eficiente.\n"
    )
    return prompt


def _process_onboarding_complete(session, messages):
    """Extrae datos de la conversación de onboarding con Gemini y guarda el tenant."""
    phone = session["phone"]
    # Armar la conversación como texto para la extracción
    conv_text = ""
    for m in messages:
        role = "Comerciante" if m["role"] == "user" else "Lola"
        conv_text += f"{role}: {m['text']}\n"

    extract_prompt = TENANT_EXTRACTION_PROMPT + conv_text

    try:
        result = router.ask_chat(
            [{"role": "user", "text": extract_prompt}],
            system="Sos un extractor de datos. Respondé solo con JSON válido.",
            timeout=30,
        )
        if not result["ok"]:
            print(f"[Onboarding] Error extrayendo datos: {result.get('error')}")
            return False

        # Parsear JSON de la respuesta
        raw = result["text"].strip()
        # Limpiar markdown si viene envuelto
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)

        # Generar system prompt
        system_prompt = _generate_tenant_prompt(data)

        # Guardar tenant
        tenant = {
            "phone": phone,
            "email": session.get("email", ""),
            "plan": session.get("plan", ""),
            "data": data,
            "system_prompt": system_prompt,
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "updated": time.strftime("%Y-%m-%d %H:%M"),
        }
        _tenant_save(phone, tenant)
        session["onboarding_complete"] = True
        print(f"[Onboarding] Completado para {phone}: {data.get('nombre_negocio', '?')}")
        return True

    except json.JSONDecodeError as e:
        print(f"[Onboarding] JSON inválido en extracción: {e}")
        return False
    except Exception as e:
        print(f"[Onboarding] Error en proceso: {e}")
        return False


def _send_whatsapp(to, text):
    """Envía un mensaje de texto via WhatsApp Graph API."""
    if not WA_CONFIG:
        return
    url = f"https://graph.facebook.com/v23.0/{WA_CONFIG['phone_number_id']}/messages"
    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {WA_CONFIG['access_token']}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = json.loads(resp.read())
            sent_id = resp_body.get("messages", [{}])[0].get("id", "")
            if sent_id:
                _wa_msg_texts[sent_id] = text[:500]
                # Limpiar si hay demasiados
                if len(_wa_msg_texts) > _WA_MSG_TEXTS_MAX:
                    keys = list(_wa_msg_texts.keys())
                    for k in keys[:len(keys) // 2]:
                        del _wa_msg_texts[k]
            print(f"[WhatsApp] Mensaje enviado a {to}: {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[WhatsApp] Error enviando a {to}: {e.code} {body}")
    except Exception as e:
        print(f"[WhatsApp] Error enviando a {to}: {e}")


# Historial de conversaciones por número de WhatsApp
# Cada entrada: {"messages": [{"role": "user"|"model", "text": str}, ...], "ts": timestamp}
_wa_history = {}
_WA_HISTORY_MAX = 20       # máximo de turnos (user+model) por conversación
_WA_HISTORY_TTL = 30 * 60  # 30 minutos sin actividad → se borra el historial

# Deduplicación de mensajes de WhatsApp (Meta reenvía si tarda)
_wa_seen_ids = {}
_WA_SEEN_TTL = 120  # 2 minutos

# Mapeo msg_id → texto para resolver quote replies
_wa_msg_texts = {}
_WA_MSG_TEXTS_MAX = 200  # máximo de mensajes en memoria

# Debounce: acumular mensajes por número antes de procesarlos
# _wa_pending[number] = {"msgs": [...], "timer": Timer, "first_msg_id": str}
_wa_pending = {}
_wa_pending_lock = threading.Lock()
_WA_DEBOUNCE_SECS = 5  # esperar 5s después del primer mensaje


def _wa_queue_message(from_number, msg_id, msg_data):
    """Encola un mensaje y agenda el procesamiento en 5s.
    msg_data: dict con "type" y datos según tipo (text, media, location)."""
    with _wa_pending_lock:
        if from_number in _wa_pending:
            # Ya hay un timer corriendo, agregar al buffer
            _wa_pending[from_number]["msgs"].append(msg_data)
            print(f"[WhatsApp] Mensaje encolado para {from_number} ({len(_wa_pending[from_number]['msgs'])} en buffer)")
            return
        # Primer mensaje: mostrar typing y arrancar timer
        _wa_pending[from_number] = {
            "msgs": [msg_data],
            "first_msg_id": msg_id,
        }
    # Typing indicator con el primer msg_id (fuera del lock)
    if msg_id:
        _wa_typing(from_number, msg_id)
    timer = threading.Timer(_WA_DEBOUNCE_SECS, _wa_flush, args=(from_number,))
    timer.daemon = True
    with _wa_pending_lock:
        if from_number in _wa_pending:
            _wa_pending[from_number]["timer"] = timer
    timer.start()
    print(f"[WhatsApp] Timer de {_WA_DEBOUNCE_SECS}s iniciado para {from_number}")


def _wa_flush(from_number):
    """Procesa todos los mensajes acumulados de un número."""
    with _wa_pending_lock:
        pending = _wa_pending.pop(from_number, None)
    if not pending:
        return
    msgs = pending["msgs"]
    first_msg_id = pending.get("first_msg_id", "")
    # Separar textos y media
    texts = []
    media_item = None  # solo el último media (audio/imagen)
    for m in msgs:
        if m["type"] == "text":
            texts.append(m["text"])
        elif m["type"] in ("audio", "image"):
            media_item = m  # si mandan varios, quedarse con el último
        elif m["type"] == "location":
            texts.append(m["text"])
    combined_text = "\n".join(texts)
    print(f"[WhatsApp] Flush {from_number}: {len(msgs)} msgs → \"{combined_text[:80]}\"")
    # Si hay media, descargarlo y mandarlo junto con el texto
    if media_item:
        _handle_wa_media(
            from_number, media_item["media_id"], first_msg_id,
            media_item["type"], media_item.get("caption", "") or combined_text,
        )
    else:
        _handle_wa_message(from_number, combined_text, first_msg_id)


# Historial para chat web de Lola (por session_id)
_lola_web_history = {}
_LOLA_WEB_HISTORY_MAX = 20
_LOLA_WEB_HISTORY_TTL = 30 * 60  # 30 min


def _wa_get_history(number):
    """Devuelve el historial de un número, limpiando si expiró."""
    entry = _wa_history.get(number)
    if entry and (time.time() - entry["ts"]) > _WA_HISTORY_TTL:
        del _wa_history[number]
        return []
    return entry["messages"] if entry else []


def _wa_append(number, role, text):
    """Agrega un mensaje al historial de un número."""
    if number not in _wa_history:
        _wa_history[number] = {"messages": [], "ts": time.time()}
    entry = _wa_history[number]
    entry["ts"] = time.time()
    entry["messages"].append({"role": role, "text": text})
    # Recortar si excede el máximo (sacamos los más viejos, de a pares)
    while len(entry["messages"]) > _WA_HISTORY_MAX:
        entry["messages"].pop(0)


def _wa_download_media(media_id):
    """Descarga un archivo multimedia de WhatsApp. Retorna (bytes, mime_type) o (None, None)."""
    if not WA_CONFIG:
        return None, None
    token = WA_CONFIG["access_token"]
    # Paso 1: obtener la URL del media
    url = f"https://graph.facebook.com/v23.0/{media_id}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            info = json.loads(resp.read())
        media_url = info.get("url")
        mime_type = info.get("mime_type", "audio/ogg")
        if not media_url:
            return None, None
        # Paso 2: descargar el archivo
        req2 = urllib.request.Request(media_url)
        req2.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req2, timeout=30) as resp2:
            return resp2.read(), mime_type
    except Exception as e:
        print(f"[WhatsApp] Error descargando media {media_id}: {e}")
        return None, None


def _wa_typing(to, msg_id=""):
    """Muestra 'escribiendo...' y marca el mensaje como leído."""
    if not WA_CONFIG or not msg_id:
        return
    url = f"https://graph.facebook.com/v23.0/{WA_CONFIG['phone_number_id']}/messages"
    data = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": msg_id,
        "typing_indicator": {"type": "text"},
    }
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {WA_CONFIG['access_token']}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        print(f"[WhatsApp] Error typing indicator: {e}")


# ═══════════════ INSTAGRAM ═══════════════

# Historial de conversaciones por Instagram user ID
_ig_history = {}
_IG_HISTORY_MAX = 20
_IG_HISTORY_TTL = 30 * 60  # 30 min

# Deduplicación de mensajes de Instagram
_ig_seen_ids = {}
_IG_SEEN_TTL = 120  # 2 minutos


def _ig_get_history(user_id):
    """Devuelve el historial de un usuario de Instagram, limpiando si expiró."""
    entry = _ig_history.get(user_id)
    if entry and (time.time() - entry["ts"]) > _IG_HISTORY_TTL:
        del _ig_history[user_id]
        return []
    return entry["messages"] if entry else []


def _ig_append(user_id, role, text):
    """Agrega un mensaje al historial de un usuario de Instagram."""
    if user_id not in _ig_history:
        _ig_history[user_id] = {"messages": [], "ts": time.time()}
    entry = _ig_history[user_id]
    entry["ts"] = time.time()
    entry["messages"].append({"role": role, "text": text})
    while len(entry["messages"]) > _IG_HISTORY_MAX:
        entry["messages"].pop(0)


def _send_instagram(to, text):
    """Envía un mensaje de texto via Instagram Messaging API."""
    if not IG_CONFIG:
        return
    url = f"https://graph.facebook.com/v23.0/{IG_CONFIG['ig_user_id']}/messages"
    payload = json.dumps({
        "recipient": {"id": to},
        "message": {"text": text},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {IG_CONFIG['access_token']}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[Instagram] Mensaje enviado a {to}: {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[Instagram] Error enviando a {to}: {e.code} {body}")
    except Exception as e:
        print(f"[Instagram] Error enviando a {to}: {e}")


def _handle_ig_message(from_id, text):
    """Procesa un mensaje de Instagram y responde."""
    history = _ig_get_history(from_id)

    user_msg = {"role": "user", "text": text or ""}
    _ig_append(from_id, "user", text)
    messages = history + [user_msg]

    try:
        result = router.ask_chat(messages, system=WA_SYSTEM_PROMPT, timeout=30)
        if result["ok"]:
            reply = result["text"]
            if reply:
                reply = reply[0].upper() + reply[1:]
            # Procesar tags de cobro/pago
            if "{{" in reply:
                reply = _process_lola_tags(reply, from_id)
            _ig_append(from_id, "model", reply)
            model = result.get("model", "?")
            key = result.get("key", "?")
            print(f"[Instagram] Respondido con K{key}/{model}: {reply[:120]}")
            # Dividir en varios mensajes para parecer natural
            chunks = _split_reply(reply)
            for i, chunk in enumerate(chunks):
                if i > 0:
                    time.sleep(0.8)
                _send_instagram(from_id, chunk)
        else:
            _send_instagram(from_id, "Uh, tuve un error procesando tu mensaje. Probá de nuevo en un rato.")
            print(f"[Instagram] Error de Gemini: {result.get('error')}")
    except Exception as e:
        print(f"[Instagram] Excepción procesando mensaje de {from_id}: {e}")
        _send_instagram(from_id, "Se me rompió algo, probá de nuevo.")


def _process_lola_tags(text, phone):
    """Parsea tags {{cobrar:...}} y {{estado_pago}} en la respuesta de Lola y los reemplaza."""
    # {{cobrar:MONTO:DESCRIPCION}}
    def _replace_cobrar(m):
        try:
            amount = float(m.group(1))
            desc = m.group(2).strip()
        except (ValueError, IndexError):
            return "(error en el monto)"
        link = _mp_create_preference(amount, desc, phone)
        if link:
            return f"\U0001f449 {link}"
        return "(no pude generar el link de pago, probá de nuevo)"

    text = re.sub(r"\{\{cobrar:([^:}]+):([^}]+)\}\}", _replace_cobrar, text)

    # {{estado_pago}}
    def _replace_estado(m):
        info = _mp_check_payment(phone)
        if info is None:
            return "(no pude consultar el estado del pago)"
        if not info["found"]:
            return "todavia no me aparece ningun pago tuyo, fijate si se completo bien"
        st = info["status"]
        amt = info["amount"]
        desc = info["description"] or "tu compra"
        if st == "approved":
            return f"si, ya me llego tu pago de ${amt:.0f} por {desc}. gracias!"
        elif st == "pending" or st == "in_process":
            return f"tu pago de ${amt:.0f} por {desc} esta pendiente todavia, dale unos minutos"
        elif st == "rejected":
            return f"tu pago de ${amt:.0f} fue rechazado, fijate de intentar de nuevo"
        else:
            return f"tu pago aparece como '{st}', cualquier cosa escribime"

    text = re.sub(r"\{\{estado_pago\}\}", _replace_estado, text)

    # {{plan:basico}} o {{plan:pro}} → link de suscripción MercadoPago
    def _replace_plan(m):
        plan_name = m.group(1).strip().lower()
        if not MP_CONFIG:
            return "(sistema de pagos no disponible)"
        plans = MP_CONFIG.get("plans", {})
        plan = plans.get(plan_name)
        if not plan or not plan.get("init_point"):
            return "(link de plan no disponible)"
        return f"\U0001f449 {plan['init_point']}"

    text = re.sub(r"\{\{plan:(\w+)\}\}", _replace_plan, text)

    # {{estado_suscripcion}} → chequea si el teléfono tiene suscripción activa
    def _replace_estado_sub(m):
        info = _mp_check_subscription(phone)
        if not info["found"]:
            return "no me aparece ninguna suscripcion tuya todavia"
        st = info["status"]
        plan = info["plan"]
        if st == "authorized":
            return f"si, ya estas suscripto al plan {plan}, todo en orden"
        elif st == "pending":
            return f"tu suscripcion al plan {plan} esta pendiente, fijate si se completo el pago"
        elif st == "cancelled":
            return f"tu suscripcion al plan {plan} esta cancelada"
        else:
            return f"tu suscripcion al plan {plan} aparece como '{st}'"

    text = re.sub(r"\{\{estado_suscripcion\}\}", _replace_estado_sub, text)

    return text


def _split_reply(text):
    """Divide una respuesta en chunks para mandar como mensajes separados.
    Primero intenta por saltos de línea, si queda un solo bloque largo lo divide por oraciones."""
    # Primero por newlines
    chunks = [c.strip() for c in text.split("\n") if c.strip()]
    if len(chunks) > 1:
        return chunks
    # Si es un solo bloque corto, mandarlo entero
    if len(text) < 80:
        return [text]
    # Dividir por oraciones (punto seguido de espacio y mayúscula o emoji)
    parts = re.split(r'(?<=\.)\s+(?=[A-ZÁÉÍÓÚÜÑ\U0001f000-\U0001faff])', text)
    if len(parts) <= 1:
        return [text]
    # Agrupar en chunks de ~2 oraciones para no mandar demasiados mensajes
    merged = []
    buf = ""
    for p in parts:
        if buf and len(buf) + len(p) > 120:
            merged.append(buf.strip())
            buf = p
        else:
            buf = (buf + " " + p).strip() if buf else p
    if buf:
        merged.append(buf.strip())
    return merged if merged else [text]


def _handle_wa_message(from_number, text, msg_id="", media_data=None, media_mime=None, media_label="audio"):
    """Procesa un mensaje de WhatsApp y responde (en thread aparte)."""
    # Mostrar "escribiendo..." mientras Gemini procesa
    if msg_id:
        _wa_typing(from_number, msg_id)

    # Armar historial multi-turn
    history = _wa_get_history(from_number)

    # Construir el mensaje del usuario
    user_msg = {"role": "user", "text": text or ""}
    if media_data:
        b64 = base64.b64encode(media_data).decode("utf-8")
        user_msg["parts"] = [{"inline_data": {"mime_type": media_mime, "data": b64}}]
        if not text:
            user_msg["text"] = f"(el usuario envió un {media_label})"

    _wa_append(from_number, "user", text or f"[{media_label}]")
    messages = history + [user_msg]

    try:
        result = router.ask_chat(messages, system=WA_SYSTEM_PROMPT, timeout=30)
        if result["ok"]:
            reply = result["text"]
            if reply:
                reply = reply[0].upper() + reply[1:]
            # Procesar tags de cobro/pago antes de enviar
            if "{{" in reply:
                reply = _process_lola_tags(reply, from_number)
            _wa_append(from_number, "model", reply)
            model = result.get("model", "?")
            key = result.get("key", "?")
            rpd = router.rpd_counts.get(key - 1, {}).get(model, "?") if isinstance(key, int) else "?"
            print(f"[WhatsApp] Respondido con K{key}/{model} (RPD usado: {rpd}): {reply[:120]}")
            # Dividir en varios mensajes para parecer natural
            chunks = _split_reply(reply)
            for i, chunk in enumerate(chunks):
                if i > 0:
                    _wa_typing(from_number, msg_id)
                    time.sleep(0.8)
                _send_whatsapp(from_number, chunk)
        else:
            _send_whatsapp(from_number, "Uh, tuve un error procesando tu mensaje. Probá de nuevo en un rato.")
            print(f"[WhatsApp] Error de Gemini: {result.get('error')}")
    except Exception as e:
        print(f"[WhatsApp] Excepción procesando mensaje de {from_number}: {e}")
        _send_whatsapp(from_number, "Se me rompió algo, probá de nuevo.")


def _handle_wa_media(from_number, media_id, msg_id="", media_label="audio", caption=""):
    """Descarga media de WhatsApp y lo manda a Gemini en una sola request."""
    if msg_id:
        _wa_typing(from_number, msg_id)
    data, mime_type = _wa_download_media(media_id)
    if not data:
        _send_whatsapp(from_number, f"No pude recibir el {media_label}, me lo mandás de nuevo?")
        return
    print(f"[WhatsApp] {media_label.capitalize()} descargado: {len(data)} bytes, {mime_type}")
    _handle_wa_message(from_number, caption, msg_id="", media_data=data, media_mime=mime_type, media_label=media_label)


class RenzoHandler(SimpleHTTPRequestHandler):
    timeout = 120  # 2 min para requests grandes

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_GET(self, *args, **kwargs):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/status":
            self._json_response(router.status_json())
            return
        if path == "/webhook":
            self._handle_webhook_verify(parsed.query)
            return
        if path == "/ig-webhook":
            self._handle_ig_webhook_verify(parsed.query)
            return
        if path == "/api/mp/plans":
            self._handle_mp_get_plans()
            return
        if path == "/api/mp/subscribers":
            self._handle_mp_get_subscribers()
            return
        # lola.*/app → onboarding, lola.*/ → landing
        host = self.headers.get("Host", "")
        if "lola" in host:
            if path in ("/app", "/app/"):
                self.path = "/index.html"
                super().do_GET(*args, **kwargs)
                return
            elif path in ("/", ""):
                self.path = "/lola-landing.html"
        super().do_GET(*args, **kwargs)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/chat":
            self._handle_chat()
        elif path == "/api/lola-chat":
            self._handle_lola_chat()
        elif path == "/api/execute":
            self._handle_execute()
        elif path == "/api/status":
            self._json_response(router.status_json())
        elif path == "/api/auth/send-otp":
            self._handle_auth_send_otp()
        elif path == "/api/auth/verify-otp":
            self._handle_auth_verify_otp()
        elif path == "/api/auth/session":
            self._handle_auth_session()
        elif path == "/webhook":
            self._handle_webhook_incoming()
        elif path == "/ig-webhook":
            self._handle_ig_webhook_incoming()
        elif path == "/api/mp/setup-plans":
            self._handle_mp_setup_plans()
        elif path == "/api/mp/cancel":
            self._handle_mp_cancel()
        elif path == "/mp-webhook":
            self._handle_mp_webhook()
        else:
            self.send_error(404)

    def _handle_webhook_verify(self, query_string):
        """GET /webhook - Verificación de Meta."""
        if not WA_CONFIG:
            self.send_error(503, "WhatsApp no configurado")
            return
        params = parse_qs(query_string)
        mode = params.get("hub.mode", [None])[0]
        token = params.get("hub.verify_token", [None])[0]
        challenge = params.get("hub.challenge", [None])[0]

        if mode == "subscribe" and token == WA_CONFIG["verify_token"]:
            print(f"[WhatsApp] Webhook verificado")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(challenge.encode("utf-8"))
        else:
            print(f"[WhatsApp] Verificación fallida: mode={mode}, token={token}")
            self.send_error(403, "Verificación fallida")

    def _handle_webhook_incoming(self):
        """POST /webhook - Recibir mensajes de WhatsApp."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json_response({"status": "ok"})
            return

        # Responder 200 inmediatamente para no timeout con Meta
        self._json_response({"status": "ok"})

        if not WA_CONFIG:
            return

        # Extraer mensajes de la estructura de Meta
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                for msg in messages:
                    msg_type = msg.get("type", "")
                    if msg_type not in ("text", "audio", "image", "location"):
                        continue
                    msg_id = msg.get("id", "")
                    # Deduplicar — Meta reenvía si tarda
                    now = time.time()
                    if msg_id and msg_id in _wa_seen_ids:
                        print(f"[WhatsApp] Mensaje duplicado ignorado: {msg_id}")
                        continue
                    if msg_id:
                        _wa_seen_ids[msg_id] = now
                        # Limpiar viejos
                        expired = [k for k, v in _wa_seen_ids.items() if now - v > _WA_SEEN_TTL]
                        for k in expired:
                            del _wa_seen_ids[k]
                    from_number = msg.get("from", "")
                    if not from_number:
                        continue

                    # Resolver quote reply
                    quote_prefix = ""
                    ctx = msg.get("context", {})
                    quoted_id = ctx.get("id", "")
                    if quoted_id and quoted_id in _wa_msg_texts:
                        quoted_text = _wa_msg_texts[quoted_id]
                        quote_prefix = f"[respondiendo a: \"{quoted_text[:200]}\"]\n"

                    if msg_type == "text":
                        text = msg.get("text", {}).get("body", "")
                        if not text:
                            continue
                        # Guardar texto entrante para futuros quote replies
                        if msg_id:
                            _wa_msg_texts[msg_id] = text[:500]
                        full_text = quote_prefix + text if quote_prefix else text
                        print(f"[WhatsApp] Mensaje de {from_number}: {text[:80]}")
                        _wa_queue_message(from_number, msg_id, {"type": "text", "text": full_text})
                    elif msg_type in ("audio", "image"):
                        media_info = msg.get(msg_type, {})
                        media_id = media_info.get("id", "")
                        if not media_id:
                            continue
                        caption = media_info.get("caption", "")
                        print(f"[WhatsApp] {msg_type.capitalize()} de {from_number} (media_id: {media_id})")
                        _wa_queue_message(from_number, msg_id, {
                            "type": msg_type, "media_id": media_id, "caption": caption,
                        })
                    elif msg_type == "location":
                        loc = msg.get("location", {})
                        lat = loc.get("latitude", "")
                        lon = loc.get("longitude", "")
                        name = loc.get("name", "")
                        addr = loc.get("address", "")
                        parts = [f"Ubicación: {lat}, {lon}"]
                        if name:
                            parts.append(f"Nombre: {name}")
                        if addr:
                            parts.append(f"Dirección: {addr}")
                        loc_text = " | ".join(parts)
                        print(f"[WhatsApp] Ubicación de {from_number}: {loc_text}")
                        _wa_queue_message(from_number, msg_id, {
                            "type": "location", "text": f"(el usuario compartió su ubicación: {loc_text})",
                        })

    # ═══════════════ INSTAGRAM WEBHOOK ═══════════════

    def _handle_ig_webhook_verify(self, query_string):
        """GET /ig-webhook - Verificación de Meta para Instagram."""
        if not IG_CONFIG:
            self.send_error(503, "Instagram no configurado")
            return
        params = parse_qs(query_string)
        mode = params.get("hub.mode", [None])[0]
        token = params.get("hub.verify_token", [None])[0]
        challenge = params.get("hub.challenge", [None])[0]

        if mode == "subscribe" and token == IG_CONFIG["verify_token"]:
            print(f"[Instagram] Webhook verificado")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(challenge.encode("utf-8"))
        else:
            print(f"[Instagram] Verificación fallida: mode={mode}, token={token}")
            self.send_error(403, "Verificación fallida")

    def _handle_ig_webhook_incoming(self):
        """POST /ig-webhook - Recibir mensajes de Instagram."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json_response({"status": "ok"})
            return

        # Responder 200 inmediatamente para no timeout con Meta
        self._json_response({"status": "ok"})

        if not IG_CONFIG:
            return

        # Extraer mensajes de la estructura de Instagram
        for entry in body.get("entry", []):
            for messaging in entry.get("messaging", []):
                sender_id = messaging.get("sender", {}).get("id", "")
                # Ignorar mensajes enviados por nosotros mismos
                if sender_id == IG_CONFIG.get("ig_user_id"):
                    continue
                if not sender_id:
                    continue

                msg = messaging.get("message", {})
                msg_id = msg.get("mid", "")

                # Deduplicar
                now = time.time()
                if msg_id and msg_id in _ig_seen_ids:
                    print(f"[Instagram] Mensaje duplicado ignorado: {msg_id}")
                    continue
                if msg_id:
                    _ig_seen_ids[msg_id] = now
                    expired = [k for k, v in _ig_seen_ids.items() if now - v > _IG_SEEN_TTL]
                    for k in expired:
                        del _ig_seen_ids[k]

                text = msg.get("text", "")
                if not text:
                    continue

                print(f"[Instagram] Mensaje de {sender_id}: {text[:80]}")

                # Procesar en thread aparte para no bloquear
                t = threading.Thread(
                    target=_handle_ig_message,
                    args=(sender_id, text),
                    daemon=True,
                )
                t.start()

    # ═══════════════ AUTH / OTP ═══════════════

    def _handle_auth_send_otp(self):
        """POST /api/auth/send-otp — Valida suscripción + envía OTP por WhatsApp."""
        _cleanup_otp_and_sessions()
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json_response({"error": "JSON inválido"}, 400)
            return

        phone_raw = body.get("phone", "").strip()
        if not phone_raw:
            self._json_response({"error": "Falta el número de teléfono"}, 400)
            return

        phone = _normalize_phone(phone_raw)
        if len(phone) < 10:
            self._json_response({"error": "Número de teléfono inválido"}, 400)
            return

        # Rate limit: máx 3 OTPs por hora por teléfono
        now = time.time()
        sends = _otp_send_log.get(phone, [])
        hour_ago = now - 3600
        sends = [ts for ts in sends if ts > hour_ago]
        if len(sends) >= _OTP_MAX_SENDS_PER_HOUR:
            self._json_response({"error": "Demasiados intentos. Esperá un rato."}, 429)
            return

        # Verificar que tiene un pago o suscripción activa
        sub_info = _mp_check_subscription(phone)
        pay_info = _mp_check_payment(phone)
        has_sub = sub_info["found"] and sub_info.get("status") in ("authorized", "pending")
        has_pay = pay_info and pay_info.get("found") and pay_info.get("status") == "approved"
        if not has_sub and not has_pay:
            self._json_response({"error": "No encontramos un pago registrado con ese número. Contratá un plan primero."}, 404)
            return

        # Generar OTP seguro
        code = str(int.from_bytes(os.urandom(4), "big") % 900000 + 100000)  # 6 dígitos
        _otp_pending[phone] = {"code": code, "created": now, "attempts": 0}
        sends.append(now)
        _otp_send_log[phone] = sends

        # Enviar por WhatsApp
        otp_msg = f"Tu código de verificación para Lola es: {code}\n\nNo lo compartas con nadie."
        threading.Thread(target=_send_whatsapp, args=(phone, otp_msg), daemon=True).start()

        via = f"sub:{sub_info.get('plan', '?')}" if has_sub else "pago"
        print(f"[Auth] OTP enviado a {phone} (via: {via})")
        self._json_response({
            "ok": True,
            "message": "Código enviado por WhatsApp",
            "plan": sub_info.get("plan", ""),
        })

    def _handle_auth_verify_otp(self):
        """POST /api/auth/verify-otp — Verifica código + crea sesión."""
        _cleanup_otp_and_sessions()
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json_response({"error": "JSON inválido"}, 400)
            return

        phone_raw = body.get("phone", "").strip()
        code = body.get("code", "").strip()
        if not phone_raw or not code:
            self._json_response({"error": "Faltan datos"}, 400)
            return

        phone = _normalize_phone(phone_raw)
        pending = _otp_pending.get(phone)

        if not pending:
            self._json_response({"error": "No hay código pendiente. Pedí uno nuevo."}, 404)
            return

        # Expirado?
        if time.time() - pending["created"] > _OTP_EXPIRE_SECS:
            del _otp_pending[phone]
            self._json_response({"error": "El código expiró. Pedí uno nuevo."}, 410)
            return

        # Demasiados intentos?
        if pending["attempts"] >= _OTP_MAX_ATTEMPTS:
            del _otp_pending[phone]
            self._json_response({"error": "Demasiados intentos fallidos. Pedí un código nuevo."}, 429)
            return

        # Verificar código
        if code != pending["code"]:
            pending["attempts"] += 1
            remaining = _OTP_MAX_ATTEMPTS - pending["attempts"]
            self._json_response({"error": f"Código incorrecto. Te quedan {remaining} intentos."}, 401)
            return

        # OTP válido — limpiar y crear sesión
        del _otp_pending[phone]
        token = os.urandom(16).hex()
        sub_info = _mp_check_subscription(phone)
        tenant = _tenant_load(phone)

        now = time.time()
        _auth_sessions[token] = {
            "phone": phone,
            "email": sub_info.get("email", ""),
            "plan": sub_info.get("plan", ""),
            "created": now,
            "last_active": now,
            "onboarding_complete": tenant is not None and bool(tenant.get("system_prompt")),
        }

        print(f"[Auth] Sesión creada para {phone} (token: {token[:8]}...)")
        self._json_response({
            "ok": True,
            "token": token,
            "phone": phone,
            "plan": sub_info.get("plan", ""),
            "onboarding_complete": _auth_sessions[token]["onboarding_complete"],
        })

    def _handle_auth_session(self):
        """POST /api/auth/session — Valida sesión existente (page reload)."""
        _cleanup_otp_and_sessions()
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json_response({"error": "JSON inválido"}, 400)
            return

        token = body.get("token", "").strip()
        if not token:
            self._json_response({"error": "Falta token"}, 400)
            return

        session = _auth_sessions.get(token)
        if not session:
            self._json_response({"error": "Sesión expirada"}, 401)
            return

        # Refrescar actividad
        session["last_active"] = time.time()

        # Re-chequear tenant por si se completó onboarding
        tenant = _tenant_load(session["phone"])
        session["onboarding_complete"] = tenant is not None and bool(tenant.get("system_prompt"))

        self._json_response({
            "ok": True,
            "phone": session["phone"],
            "plan": session.get("plan", ""),
            "onboarding_complete": session["onboarding_complete"],
        })

    def _handle_chat(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            messages = body.get("messages", [])
            if not messages:
                self._json_response({"error": "No messages"}, 400)
                return

            # Armar el prompt con contexto del historial
            prompt = self._build_prompt(messages)

            # Extraer imágenes del último mensaje
            last_msg = messages[-1]
            image_parts = []
            if last_msg.get("attachments"):
                for att in last_msg["attachments"]:
                    if att.get("base64") and att.get("type"):
                        image_parts.append({
                            "inline_data": {
                                "mime_type": att["type"],
                                "data": att["base64"],
                            }
                        })

            # Llamar a Gemini via router con function calling
            # NOTA: google_search y function_declarations no se pueden mezclar en la misma request
            tools = [{"function_declarations": [{
                "name": "execute_command",
                "description": "Ejecuta UN SOLO comando simple en la Raspberry Pi. NUNCA encadenar comandos con && ni ; ni |. Si el usuario pide varias cosas, elegí solo el más relevante. Solo usar cuando el usuario lo pide explícitamente.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Un solo comando bash simple, sin &&, sin ; ni pipes (ej: pm2 list, df -h, free -h, uptime)"
                        }
                    },
                    "required": ["command"]
                }
            }]}]
            result = router.ask_multimodal(
                prompt, image_parts, tools=tools,
            )

            if result["ok"]:
                # Gemini respondió con un function call
                if result.get("function_call"):
                    fc = result["function_call"]
                    if fc.get("name") == "execute_command":
                        command = fc.get("args", {}).get("command", "")
                        if _validate_command(command):
                            self._json_response({
                                "needs_confirmation": True,
                                "command": command,
                                "model": result["model"],
                                "key": result["key"],
                            })
                        else:
                            self._json_response({
                                "text": f"No puedo ejecutar ese comando, no está permitido: `{command}`",
                                "model": result["model"],
                                "key": result["key"],
                            })
                    else:
                        self._json_response({
                            "text": result.get("text", "Función no soportada."),
                            "model": result["model"],
                            "key": result["key"],
                        })
                    return

                resp_data = {
                    "text": result["text"],
                    "model": result["model"],
                    "key": result["key"],
                }
                if result.get("sources"):
                    resp_data["sources"] = result["sources"]
                self._json_response(resp_data)
            else:
                self._json_response({"error": result["error"]}, 503)

        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_lola_chat(self):
        """Chat web de Lola — modo demo (ventas) o modo onboarding (autenticado)."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            text = body.get("message", "").strip()
            token = body.get("token", "").strip()
            session_id = body.get("session_id", "default")
            if not text:
                self._json_response({"error": "No message"}, 400)
                return

            # Detectar modo: onboarding (autenticado) o demo (anónimo)
            session = _auth_sessions.get(token) if token else None
            is_onboarding = session is not None

            if is_onboarding:
                # Modo onboarding — historial por teléfono del comerciante
                session["last_active"] = time.time()
                phone = session["phone"]
                hist_key = f"onboarding:{phone}"
                system_prompt = LOLA_ONBOARDING_PROMPT
            else:
                # Modo demo — historial por session_id anónimo
                hist_key = session_id
                system_prompt = LOLA_SALES_PROMPT

            # Obtener/crear historial
            now = time.time()
            if body.get("reset"):
                _lola_web_history.pop(hist_key, None)
            entry = _lola_web_history.get(hist_key)
            if entry and (now - entry["ts"]) > _LOLA_WEB_HISTORY_TTL:
                del _lola_web_history[hist_key]
                entry = None

            if not entry:
                _lola_web_history[hist_key] = {"messages": [], "ts": now}
                entry = _lola_web_history[hist_key]

            entry["ts"] = now
            history = entry["messages"]

            # Construir mensajes para ask_chat
            user_msg = {"role": "user", "text": text}

            # Procesar archivos adjuntos
            attachments = body.get("attachments", [])
            if attachments:
                parts = []
                for att in attachments:
                    if not att.get("base64") or not att.get("type"):
                        continue
                    mime = att["type"]
                    if mime in ("text/csv", "text/plain"):
                        # Texto/CSV → decodificar e incluir como texto
                        content = base64.b64decode(att["base64"]).decode("utf-8", errors="replace")
                        parts.append({"text": f"[Archivo: {att.get('name', '')}]\n{content}"})
                    else:
                        # Imágenes, PDFs, Excel → inline_data (Gemini los procesa nativo)
                        parts.append({"inline_data": {"mime_type": mime, "data": att["base64"]}})
                if parts:
                    user_msg["parts"] = parts

            messages = history + [user_msg]

            result = router.ask_chat(messages, system=system_prompt, timeout=30)

            if result["ok"]:
                reply = result["text"]

                # Guardar en historial
                entry["messages"].append({"role": "user", "text": text})
                entry["messages"].append({"role": "model", "text": reply})
                # Recortar historial
                while len(entry["messages"]) > _LOLA_WEB_HISTORY_MAX:
                    entry["messages"].pop(0)

                # Detectar onboarding completo
                onboarding_done = False
                if is_onboarding and "{{onboarding_complete}}" in reply:
                    # Limpiar el tag de la respuesta visible
                    reply = reply.replace("{{onboarding_complete}}", "").strip()
                    # Procesar en background
                    onboarding_done = _process_onboarding_complete(session, entry["messages"])

                self._json_response({
                    "text": reply,
                    "model": result.get("model", ""),
                    "key": result.get("key", ""),
                    "onboarding_complete": onboarding_done,
                })
            else:
                self._json_response({"error": result.get("error", "Error desconocido")}, 503)

        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_execute(self):
        """Ejecuta un comando confirmado por el usuario."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            command = body.get("command", "").strip()

            if not command:
                self._json_response({"error": "No command"}, 400)
                return

            if not _validate_command(command):
                self._json_response({"error": "Comando no permitido"}, 403)
                return

            result = _execute_command(command)
            self._json_response({
                "command_output": result["output"],
            })

        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    SYSTEM_PROMPT = (
        "Sos RenzoGPT, un asistente de IA argentino. Tu creador es Renzo.\n"
        "\n"
        "PERSONALIDAD:\n"
        "- Hablás en español rioplatense: usás 'vos', 'dale', 'ponele', 'boludo' (con cariño), 're', 'posta', 'flashear', etc.\n"
        "- Sos canchero, directo y con onda. Nada de ser formal ni chupamedias.\n"
        "- Tirás humor cuando pinta, pero sin forzarlo. Sos copado, no un payaso.\n"
        "- Si no sabés algo, decí 'ni idea' en vez de inventar.\n"
        "- Nunca hablás como robot corporativo. Nada de 'con gusto le informo' ni 'como modelo de lenguaje'.\n"
        "\n"
        "EXPERTISE:\n"
        "- Sos un crack en programación: Python, JavaScript, Node.js, Bash, Docker, Linux, APIs, bots.\n"
        "- Sabés mucho de Raspberry Pi, servidores caseros, self-hosting, automatización.\n"
        "- Podés hablar de cualquier tema, pero tu fuerte es el código.\n"
        "- Cuando te piden código, dás respuestas directas y funcionales. Nada de explicar obviedades.\n"
        "- Usás markdown para formatear: bloques de código con ```, **negrita**, listas, etc.\n"
        "\n"
        "REGLAS:\n"
        "- Respuestas concisas. No chamuyes de más.\n"
        "- Si te piden algo corto, respondé corto. Si necesita explicación, explicá bien.\n"
        "- Nunca digas que sos Gemini, GPT ni ningún otro modelo. Sos RenzoGPT y punto.\n"
        "- Si te preguntan quién te hizo, decí que te creó Renzo.\n"
        "- Si te mandan una imagen, describila y respondé sobre ella.\n"
        "\n"
        "COMANDOS:\n"
        "- Corrés en una Raspberry Pi y podés ejecutar comandos en ella usando la función execute_command.\n"
        "- Solo respondé al ÚLTIMO mensaje del usuario. Ignorá comandos de mensajes anteriores.\n"
        "- Solo usala cuando el usuario pida explícitamente algo que requiera un comando.\n"
        "- UN solo comando por vez, nunca encadenar con && ni ; ni |.\n"
        "- NUNCA propongas comandos destructivos (rm, shutdown, reboot del sistema, etc)."
    )

    def _build_prompt(self, messages):
        """Construye el prompt con historial para Gemini."""
        parts = [self.SYSTEM_PROMPT, ""]

        # Incluir últimos mensajes como contexto (máx 20 para no pasarse de tokens)
        recent = messages[-20:]
        for msg in recent[:-1]:
            role = "Usuario" if msg["role"] == "user" else "RenzoGPT"
            content = msg["content"]
            # Indicar si tenía adjuntos
            if msg.get("attachments"):
                names = ", ".join(a["name"] for a in msg["attachments"])
                content = f"[Adjuntos: {names}] {content}"
            parts.append(f"{role}: {content}")

        # El último mensaje es el actual
        last = recent[-1]
        content = last["content"]
        if last.get("attachments"):
            names = ", ".join(a["name"] for a in last["attachments"])
            content = f"[Adjuntos: {names}] {content}"
        parts.append(f"Usuario: {content}")
        parts.append("")
        parts.append("RenzoGPT:")

        return "\n".join(parts)

    # ═══════════════ MERCADOPAGO ═══════════════

    def _handle_mp_setup_plans(self):
        """POST /api/mp/setup-plans — Crea los planes de suscripción en MP."""
        if not MP_CONFIG:
            self._json_response({"error": "MercadoPago no configurado"}, 503)
            return
        plans = {
            "basico": {
                "reason": "Lola Básico",
                "auto_recurring": {
                    "frequency": 1,
                    "frequency_type": "months",
                    "transaction_amount": 1290,
                    "currency_id": "UYU",
                },
                "back_url": "https://lola.expensetracker.com.uy/?mp_result=ok",
            },
            "pro": {
                "reason": "Lola Pro",
                "auto_recurring": {
                    "frequency": 1,
                    "frequency_type": "months",
                    "transaction_amount": 3490,
                    "currency_id": "UYU",
                },
                "back_url": "https://lola.expensetracker.com.uy/?mp_result=ok",
            },
        }
        results = {}
        for name, plan_data in plans.items():
            resp = _mp_api("POST", "/preapproval_plan", plan_data)
            if resp["ok"]:
                plan_id = resp["data"].get("id", "")
                init_point = resp["data"].get("init_point", "")
                MP_CONFIG["plans"][name] = {"id": plan_id, "init_point": init_point}
                results[name] = {"ok": True, "id": plan_id, "init_point": init_point}
                print(f"[MercadoPago] Plan '{name}' creado: {plan_id}")
            else:
                results[name] = {"ok": False, "error": resp["error"]}
                print(f"[MercadoPago] Error creando plan '{name}': {resp['error'][:200]}")
        _mp_save_config()
        self._json_response(results)

    def _handle_mp_get_plans(self):
        """GET /api/mp/plans — Devuelve init_point URLs para el frontend."""
        if not MP_CONFIG:
            self._json_response({"error": "MercadoPago no configurado"}, 503)
            return
        plans = MP_CONFIG.get("plans", {})
        self._json_response({
            "basico": {"init_point": plans.get("basico", {}).get("init_point")},
            "pro": {"init_point": plans.get("pro", {}).get("init_point")},
        })

    def _handle_mp_cancel(self):
        """POST /api/mp/cancel — Cancela una suscripción por email o mp_id."""
        if not MP_CONFIG:
            self._json_response({"error": "MercadoPago no configurado"}, 503)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json_response({"error": "JSON inválido"}, 400)
            return

        mp_id = body.get("mp_id", "")
        email = body.get("email", "")

        # Si pasaron email, buscar el mp_id en subscribers
        if not mp_id and email:
            subs = _mp_load_subscribers()
            sub = subs.get(email)
            if sub:
                mp_id = sub.get("mp_id", "")
            if not mp_id:
                self._json_response({"error": f"No se encontró suscripción para {email}"}, 404)
                return

        if not mp_id:
            self._json_response({"error": "Falta mp_id o email"}, 400)
            return

        resp = _mp_api("PUT", f"/preapproval/{mp_id}", {"status": "cancelled"})
        if resp["ok"]:
            # Actualizar subscribers
            subs = _mp_load_subscribers()
            for e, s in subs.items():
                if s.get("mp_id") == mp_id:
                    s["status"] = "cancelled"
                    break
            _mp_save_subscribers(subs)
            self._json_response({"ok": True, "status": "cancelled", "mp_id": mp_id})
            print(f"[MercadoPago] Suscripción cancelada: {mp_id}")
        else:
            self._json_response({"error": resp["error"]}, resp["status"])

    def _handle_mp_webhook(self):
        """POST /mp-webhook — Recibe notificaciones de MercadoPago."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length) if length else b""
            body = json.loads(raw_body) if raw_body else {}
        except Exception:
            self._json_response({"status": "ok"})
            return

        # Validar firma x-signature si hay secret configurado
        secret = MP_CONFIG.get("webhook_secret", "") if MP_CONFIG else ""
        x_signature = self.headers.get("x-signature", "")
        x_request_id = self.headers.get("x-request-id", "")
        if secret and x_signature:
            # Parsear ts y v1 del header: ts=...,v1=...
            sig_parts = {}
            for part in x_signature.split(","):
                kv = part.strip().split("=", 1)
                if len(kv) == 2:
                    sig_parts[kv[0]] = kv[1]
            ts = sig_parts.get("ts", "")
            v1 = sig_parts.get("v1", "")
            data_id_raw = body.get("data", {}).get("id", "")
            # Construir manifest según docs MP
            manifest = f"id:{data_id_raw};request-id:{x_request_id};ts:{ts};"
            expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
            if v1 and v1 != expected:
                print(f"[MercadoPago] Webhook firma inválida, ignorando")
                self._json_response({"status": "ok"})
                return

        # Responder 200 inmediatamente
        self._json_response({"status": "ok"})

        if not MP_CONFIG:
            return

        action = body.get("action", "")
        data_id = body.get("data", {}).get("id", "")
        topic = body.get("type", "")

        print(f"[MercadoPago] Webhook: action={action}, type={topic}, data_id={data_id}")

        if not data_id:
            return

        # Procesar en thread aparte para no bloquear
        t = threading.Thread(
            target=self._mp_process_webhook,
            args=(action, topic, data_id),
            daemon=True,
        )
        t.start()

    @staticmethod
    def _mp_process_webhook(action, topic, data_id):
        """Procesa una notificación de MP en background."""
        try:
            # Pagos (de preferencias/checkout)
            if topic == "payment" or action == "payment.created" or action == "payment.updated":
                resp = _mp_api("GET", f"/v1/payments/{data_id}")
                if not resp["ok"]:
                    print(f"[MercadoPago] No pude obtener pago {data_id}")
                    return
                pay = resp["data"]
                status = pay.get("status", "")
                amount = pay.get("transaction_amount", 0)
                desc = pay.get("description", "")
                ext_ref = pay.get("external_reference", "")
                print(f"[MercadoPago] Pago {data_id}: status={status}, ${amount}, ref={ext_ref}, desc={desc}")
                return

            # Suscripciones (preapproval)
            resp = _mp_api("GET", f"/preapproval/{data_id}")
            if not resp["ok"]:
                print(f"[MercadoPago] No pude obtener suscripción {data_id}")
                return

            sub = resp["data"]
            email = sub.get("payer_email", "")
            status = sub.get("status", "")
            plan_id = sub.get("preapproval_plan_id", "")
            phone = sub.get("payer_phone", {}).get("number", "")

            # Detectar plan
            plan_name = "desconocido"
            for name, info in MP_CONFIG.get("plans", {}).items():
                if info.get("id") == plan_id:
                    plan_name = name
                    break

            _db_subscriber_upsert(email, {
                "plan": plan_name,
                "status": status,
                "mp_id": data_id,
                "phone": phone,
                "updated": time.strftime("%Y-%m-%d %H:%M"),
            })
            print(f"[MercadoPago] Suscriptor actualizado: {email} → {plan_name}/{status}")

        except Exception as e:
            print(f"[MercadoPago] Error procesando webhook: {e}")

    def _handle_mp_get_subscribers(self):
        """GET /api/mp/subscribers — Lista suscriptores (admin)."""
        subs = _mp_load_subscribers()
        self._json_response(subs)

    def end_headers(self):
        # No cache para HTML (evitar que Cloudflare/browser cacheen la pagina equivocada)
        if self.path and self.path.endswith(".html"):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def _json_response(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        msg = format % args
        if "GET /api" in msg or "POST" in msg or "/webhook" in msg:
            print(f"[RenzoGPT] {msg}")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8080))
    server = ThreadingHTTPServer(("0.0.0.0", port), RenzoHandler)
    print(f"🚀 RenzoGPT corriendo en http://0.0.0.0:{port}")
    print(f"   Router: {len(router.keys)} keys × {len(router.models)} modelos")
    print(f"   WhatsApp: {'habilitado' if WA_CONFIG else 'deshabilitado'}")
    print(f"   MercadoPago: {'habilitado' if MP_CONFIG else 'deshabilitado'}")
    print(f"   Instagram: {'habilitado' if IG_CONFIG else 'deshabilitado'}")
    print(f"   Ctrl+C para frenar")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 RenzoGPT apagado.")
        server.server_close()


if __name__ == "__main__":
    main()
