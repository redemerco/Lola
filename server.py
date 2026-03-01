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
import random
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
LOLA_BASE = "/srv/dev-disk-by-uuid-540a82c6-6a24-41c4-9779-5f4a8e1634ce/lola"
sys.path.insert(0, LOLA_BASE)
from gemini_router import GeminiRouter

router = GeminiRouter()

# ═══════════════ PATHS ═══════════════
LOLA_SECRETS = os.path.join(LOLA_BASE, "secrets")
LOLA_DATA = os.path.join(LOLA_BASE, "data")

# ═══════════════ SQLITE + ENCRYPTION ═══════════════

_DB_PATH = os.path.join(LOLA_DATA, "lola-db.sqlite")
_MASTER_KEY_PATH = os.path.join(LOLA_SECRETS, ".lola-master.key")
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

_SERVER_START = time.time()


# ═══════════════ ADMIN AUTH TOKEN ═══════════════

_ADMIN_TOKEN_PATH = os.path.join(LOLA_SECRETS, ".lola-admin-token")


def _load_or_create_admin_token():
    """Carga el admin token de LOLA_ADMIN_TOKEN env var o ~/.lola-admin-token.
    Si no existe ninguno, genera uno y lo guarda."""
    env_token = os.environ.get("LOLA_ADMIN_TOKEN", "").strip()
    if env_token:
        return env_token

    if os.path.exists(_ADMIN_TOKEN_PATH):
        with open(_ADMIN_TOKEN_PATH) as f:
            token = f.read().strip()
        if token:
            return token

    # Generar nuevo token
    token = os.urandom(24).hex()
    with open(_ADMIN_TOKEN_PATH, "w") as f:
        f.write(token)
    os.chmod(_ADMIN_TOKEN_PATH, 0o600)
    print(f"[Auth] Admin token generado y guardado en {_ADMIN_TOKEN_PATH}")
    return token


_ADMIN_TOKEN = _load_or_create_admin_token()
print(f"[Auth] Admin token: {_ADMIN_TOKEN[:4]}{'*' * (len(_ADMIN_TOKEN) - 4)}")


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


def _require_admin(handler):
    """Verifica Authorization: Bearer <token>. Retorna True si OK, False si mandó 401."""
    #auth = handler.headers.get("Authorization", "")
    if auth == f"Bearer {_ADMIN_TOKEN}":
        return True
    handler._json_response({"error": "No autorizado"}, 401)
    return False


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
                CREATE TABLE IF NOT EXISTS wa_numbers (
                    phone_number_id TEXT PRIMARY KEY,
                    tenant_phone_hash TEXT,
                    access_token TEXT,
                    business_account_id TEXT,
                    label TEXT,
                    status TEXT DEFAULT 'active',
                    created TEXT,
                    updated TEXT
                );
                CREATE TABLE IF NOT EXISTS conversation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    transport TEXT DEFAULT '',
                    ts TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_convlog_phone_ts ON conversation_log(phone_hash, ts);
                CREATE TABLE IF NOT EXISTS wacli_stores (
                    phone_hash TEXT PRIMARY KEY,
                    store_dir TEXT NOT NULL,
                    state TEXT DEFAULT 'syncing',
                    trial_expires TEXT,
                    tenant_phone TEXT,
                    created TEXT,
                    updated TEXT
                );
            """)
            # Migrate: add trial_expires and tenant_phone columns if missing
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(wacli_stores)").fetchall()]
                if "trial_expires" not in cols:
                    conn.execute("ALTER TABLE wacli_stores ADD COLUMN trial_expires TEXT")
                if "tenant_phone" not in cols:
                    conn.execute("ALTER TABLE wacli_stores ADD COLUMN tenant_phone TEXT")
                conn.commit()
            except Exception:
                pass
            conn.commit()
            print(f"[DB] Inicializada: {_DB_PATH}")
        finally:
            conn.close()


def _db_log_message(phone_hash, role, text, transport=""):
    """Guarda un mensaje en conversation_log."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute(
                "INSERT INTO conversation_log (phone_hash, role, text, transport, ts) VALUES (?, ?, ?, ?, ?)",
                (phone_hash, role, _encrypt(text), transport, ts)
            )
            conn.commit()
        except Exception as e:
            print(f"[DB] Error guardando mensaje en log: {e}")
        finally:
            conn.close()


def _db_load_recent_messages(phone_hash, limit=20):
    """Carga los últimos N mensajes de un número. Retorna lista de dicts."""
    with _db_lock:
        conn = _db_conn()
        try:
            rows = conn.execute(
                "SELECT role, text, ts FROM conversation_log WHERE phone_hash=? ORDER BY id DESC LIMIT ?",
                (phone_hash, limit)
            ).fetchall()
            result = []
            for row in reversed(rows):  # revertir para orden cronológico
                result.append({
                    "role": row["role"],
                    "text": _decrypt(row["text"]) if row["text"] else "",
                })
            return result
        except Exception as e:
            print(f"[DB] Error cargando mensajes: {e}")
            return []
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


def _db_wa_number_load(phone_number_id):
    """Carga un wa_number desde SQLite. Retorna dict o None."""
    with _db_lock:
        conn = _db_conn()
        try:
            row = conn.execute("SELECT * FROM wa_numbers WHERE phone_number_id = ? AND status = 'active'",
                               (phone_number_id,)).fetchone()
            if not row:
                return None
            return {
                "phone_number_id": row["phone_number_id"],
                "tenant_phone_hash": row["tenant_phone_hash"] or "",
                "access_token": _decrypt(row["access_token"]) if row["access_token"] else "",
                "business_account_id": row["business_account_id"] or "",
                "label": row["label"] or "",
                "status": row["status"] or "active",
            }
        except Exception as e:
            print(f"[DB] Error cargando wa_number {phone_number_id}: {e}")
            return None
        finally:
            conn.close()


def _db_wa_number_save(phone_number_id, data):
    """Guarda un wa_number en SQLite con access_token encriptado."""
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO wa_numbers
                (phone_number_id, tenant_phone_hash, access_token, business_account_id, label, status, created, updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                phone_number_id,
                data.get("tenant_phone_hash", ""),
                _encrypt(data.get("access_token", "")),
                data.get("business_account_id", ""),
                data.get("label", ""),
                data.get("status", "active"),
                data.get("created", time.strftime("%Y-%m-%d %H:%M")),
                time.strftime("%Y-%m-%d %H:%M"),
            ))
            conn.commit()
            print(f"[DB] wa_number guardado: {phone_number_id} ({data.get('label', '')})")
        except Exception as e:
            print(f"[DB] Error guardando wa_number {phone_number_id}: {e}")
        finally:
            conn.close()


def _db_wa_numbers_list():
    """Lista todos los wa_numbers. Retorna lista de dicts (sin access_token desencriptado)."""
    with _db_lock:
        conn = _db_conn()
        try:
            rows = conn.execute("SELECT * FROM wa_numbers ORDER BY created").fetchall()
            result = []
            for row in rows:
                result.append({
                    "phone_number_id": row["phone_number_id"],
                    "tenant_phone_hash": row["tenant_phone_hash"] or "",
                    "business_account_id": row["business_account_id"] or "",
                    "label": row["label"] or "",
                    "status": row["status"] or "active",
                    "created": row["created"] or "",
                    "updated": row["updated"] or "",
                })
            return result
        except Exception as e:
            print(f"[DB] Error listando wa_numbers: {e}")
            return []
        finally:
            conn.close()


def _db_wa_numbers_count():
    """Retorna la cantidad de wa_numbers activos en la DB."""
    with _db_lock:
        conn = _db_conn()
        try:
            row = conn.execute("SELECT COUNT(*) FROM wa_numbers WHERE status = 'active'").fetchone()
            return row[0] if row else 0
        except Exception:
            return 0
        finally:
            conn.close()


def _db_tenant_load_by_hash(phone_hash):
    """Carga un tenant por su phone_hash (sin saber el phone original)."""
    with _db_lock:
        conn = _db_conn()
        try:
            row = conn.execute("SELECT * FROM tenants WHERE phone_hash = ?", (phone_hash,)).fetchone()
            if not row:
                return None
            return {
                "phone": _decrypt(row["phone"]) if row["phone"] else "",
                "email": _decrypt(row["email"]) if row["email"] else "",
                "plan": row["plan"] or "",
                "data": json.loads(_decrypt(row["business_data"])) if row["business_data"] else {},
                "system_prompt": _decrypt(row["system_prompt"]) if row["system_prompt"] else "",
                "created": row["created"] or "",
                "updated": row["updated"] or "",
            }
        except Exception as e:
            print(f"[DB] Error cargando tenant by hash {phone_hash}: {e}")
            return None
        finally:
            conn.close()


def _db_tenants_count():
    """Retorna la cantidad de tenants en la DB."""
    with _db_lock:
        conn = _db_conn()
        try:
            row = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()
            return row[0] if row else 0
        except Exception:
            return 0
        finally:
            conn.close()


def _db_migrate_from_json():
    """Migra datos desde archivos JSON viejos a SQLite. Renombra originales a .bak."""
    tenants_dir = os.path.join(LOLA_DATA, "lola-tenants")
    subscribers_path = os.path.join(LOLA_DATA, "lola-subscribers.json")
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
_wa_config_path = os.path.join(LOLA_SECRETS, ".whatsapp-config.json")
try:
    with open(_wa_config_path) as f:
        WA_CONFIG = json.load(f)
    print(f"[WhatsApp] Config cargada desde {_wa_config_path}")
except FileNotFoundError:
    WA_CONFIG = None
    print(f"[WhatsApp] No se encontró {_wa_config_path}, webhook desactivado")

# Instagram Messaging API config
_ig_config_path = os.path.join(LOLA_SECRETS, ".instagram-config.json")
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

# ═══════════════ WACLI BRIDGE ═══════════════
# wacli: WhatsApp CLI via whatsmeow (no Meta API needed)
# Corre wacli sync --follow --cmd-socket como subprocess.
# Envía mensajes y typing via Unix socket (instantáneo, sin reconexión).
LOLA_WACLI = os.path.join(LOLA_BASE, "wacli")
WACLI_BIN = os.path.join(LOLA_WACLI, "wacli")
WACLI_STORE = os.path.join(LOLA_WACLI, "store-lola")
WACLI_DB = os.path.join(WACLI_STORE, "wacli.db")
WACLI_SOCKET = os.path.join(WACLI_STORE, "cmd.sock")
_wacli_enabled = os.path.isfile(WACLI_DB)
_wacli_last_ts = None
_wacli_sync_proc = None

if _wacli_enabled:
    print(f"[wacli] Store encontrado en {WACLI_STORE}")
else:
    print(f"[wacli] No se encontró {WACLI_DB}, bridge desactivado")


def _wacli_stderr_reader(proc):
    """Lee stderr del wacli sync y lo loguea."""
    try:
        for line in proc.stderr:
            msg = line.decode("utf-8", errors="replace").rstrip()
            if msg:
                print(f"[wacli-sync] {msg}", flush=True)
    except Exception:
        pass


def _wacli_start_sync():
    """Arranca wacli sync --follow con cmd-socket."""
    global _wacli_sync_proc
    _wacli_sync_proc = subprocess.Popen(
        [WACLI_BIN, "--store", WACLI_STORE, "sync", "--follow", "--cmd-socket", WACLI_SOCKET],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    print(f"[wacli] Sync iniciado (PID {_wacli_sync_proc.pid})")
    # Loguear stderr en background
    t = threading.Thread(target=_wacli_stderr_reader, args=(_wacli_sync_proc,), daemon=True)
    t.start()
    # Esperar a que el socket esté disponible
    for _ in range(20):
        if os.path.exists(WACLI_SOCKET):
            print(f"[wacli] Socket listo: {WACLI_SOCKET}")
            return
        time.sleep(0.5)
    print(f"[wacli] WARN: socket no apareció después de 10s")


def _wacli_cmd(cmd_dict, socket_path=None):
    """Envía un comando al sync via Unix socket. Retorna respuesta dict."""
    import socket as _socket
    sp = socket_path or WACLI_SOCKET
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(15)
        sock.connect(sp)
        payload = json.dumps(cmd_dict) + "\n"
        sock.sendall(payload.encode("utf-8"))
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        sock.close()
        return json.loads(data.strip())
    except Exception as e:
        print(f"[wacli] Error cmd socket ({sp}): {e}")
        return {"ok": False, "error": str(e)}


def _wacli_send(to, text, socket_path=None):
    """Envía un mensaje de texto via socket al sync."""
    resp = _wacli_cmd({"action": "send_text", "to": to, "message": text}, socket_path=socket_path)
    if resp.get("ok"):
        print(f"[wacli] Mensaje enviado a {to}")
    else:
        print(f"[wacli] Error enviando a {to}: {resp.get('error', '?')}")


def _wacli_typing(to, socket_path=None):
    """Envía indicador de 'escribiendo...' via socket."""
    _wacli_cmd({"action": "typing", "to": to}, socket_path=socket_path)


def _wacli_send_file(to, file_path, caption="", store_dir=None):
    """Envía un archivo (imagen/video/doc) via wacli send file subprocess.
    Requiere que no haya otro wacli corriendo con el mismo store."""
    sd = store_dir or WACLI_STORE
    cmd = [WACLI_BIN, "--store", sd, "send", "file", "--to", to, "--file", file_path]
    if caption:
        cmd.extend(["--caption", caption])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"[wacli] Archivo enviado a {to}: {file_path}")
        else:
            print(f"[wacli] Error enviando archivo a {to}: {result.stderr[:200]}")
    except Exception as e:
        print(f"[wacli] Error enviando archivo: {e}")


def _wacli_poll_loop():
    """Thread que pollea wacli.db buscando mensajes nuevos entrantes."""
    global _wacli_last_ts
    import sqlite3 as _sq3

    # Arrancar sync como subprocess propio
    _wacli_start_sync()

    # Arrancar con el timestamp actual para no procesar historial viejo
    try:
        conn = _sq3.connect(f"file:{WACLI_DB}?mode=ro", uri=True)
        conn.row_factory = _sq3.Row
        row = conn.execute("SELECT MAX(ts) as max_ts FROM messages").fetchone()
        if row and row["max_ts"]:
            _wacli_last_ts = row["max_ts"]
        else:
            _wacli_last_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.close()
        print(f"[wacli] Polling iniciado desde ts={_wacli_last_ts}")
    except Exception as e:
        print(f"[wacli] Error inicializando poll: {e}")
        return

    while True:
        try:
            time.sleep(2)
            conn = _sq3.connect(f"file:{WACLI_DB}?mode=ro", uri=True)
            conn.row_factory = _sq3.Row
            rows = conn.execute(
                "SELECT chat_jid, msg_id, sender_jid, ts, from_me, text, media_type, media_caption, mime_type "
                "FROM messages WHERE ts > ? AND from_me = 0 ORDER BY ts ASC LIMIT 20",
                (_wacli_last_ts,)
            ).fetchall()
            conn.close()

            for row in rows:
                _wacli_last_ts = row["ts"]
                chat_jid = row["chat_jid"]
                text = (row["text"] or "").strip()
                msg_id = row["msg_id"] or ""
                media_type = (row["media_type"] or "").strip()

                # Ignorar grupos por ahora, solo DMs
                if "@g.us" in chat_jid:
                    continue

                # Extraer número limpio del JID
                from_number = chat_jid.split("@")[0]
                if not from_number.isdigit():
                    sender = (row["sender_jid"] or "").split("@")[0]
                    if sender.isdigit():
                        from_number = sender
                    else:
                        print(f"[wacli] JID no numérico ignorado: {chat_jid}")
                        continue

                if not text and not media_type:
                    continue

                # Check if this number is in onboarding mode
                if from_number in _onboarding_sessions:
                    wa_ctx = {
                        "transport": "wacli",
                        "system_prompt": LOLA_ONBOARDING_PROMPT,
                        "is_lola_sales": False,
                        "is_onboarding": True,
                        "chat_jid": chat_jid,
                    }
                else:
                    wa_ctx = {
                        "transport": "wacli",
                        "system_prompt": LOLA_SALES_PROMPT,
                        "is_lola_sales": True,
                        "chat_jid": chat_jid,
                    }

                if media_type in ("audio", "image"):
                    caption = (row["media_caption"] or "").strip()
                    print(f"[wacli] {media_type.capitalize()} de {from_number} (msg_id: {msg_id})")
                    _wa_queue_message(from_number, msg_id, {
                        "type": media_type,
                        "wacli_msg_id": msg_id,
                        "wacli_chat_jid": chat_jid,
                        "caption": caption,
                        "mime_type": (row["mime_type"] or ""),
                    }, wa_ctx)
                else:
                    print(f"[wacli] Mensaje de {from_number}: {text[:80]}")
                    _wa_queue_message(from_number, msg_id, {"type": "text", "text": text}, wa_ctx)

        except Exception as e:
            print(f"[wacli] Error en poll loop: {e}")
            time.sleep(5)

# ═══════════════ TENANT WACLI MANAGER ═══════════════
# Multi-tenant wacli: cada comerciante tiene su propio wacli sync process.
# Flujo: MP pago → QR por WhatsApp → comerciante escanea → sync arranca.

_MAX_TENANT_WACLI = 10  # límite para no saturar la RPi
_tenant_wacli = {}  # phone_hash → {"state": "authenticating"|"syncing"|"error", "proc": Popen, "store_dir": str, "socket_path": str, "last_ts": str}
_tenant_wacli_lock = threading.Lock()

# Onboarding sessions: from_number → {"phone_hash": str, "state": "onboarding"|"done"}
_onboarding_sessions = {}

# Track trial expiry notices already sent (avoid spamming)
_trial_expiry_notified = set()  # phone_hash


def _db_wacli_store_save(phone_hash, store_dir, state="syncing", trial_expires=None, tenant_phone=None):
    """Guarda o actualiza un wacli store en la DB."""
    now = time.strftime("%Y-%m-%d %H:%M")
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute(
                "INSERT INTO wacli_stores (phone_hash, store_dir, state, trial_expires, tenant_phone, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(phone_hash) DO UPDATE SET "
                "store_dir=excluded.store_dir, state=excluded.state, trial_expires=excluded.trial_expires, "
                "tenant_phone=COALESCE(excluded.tenant_phone, wacli_stores.tenant_phone), updated=excluded.updated",
                (phone_hash, store_dir, state, trial_expires,
                 _encrypt(tenant_phone) if tenant_phone else None,
                 now, now)
            )
            conn.commit()
        finally:
            conn.close()


def _db_wacli_store_get(phone_hash):
    """Carga un wacli store por phone_hash. Retorna dict o None."""
    with _db_lock:
        conn = _db_conn()
        try:
            row = conn.execute("SELECT * FROM wacli_stores WHERE phone_hash=?", (phone_hash,)).fetchone()
            if not row:
                return None
            return {
                "phone_hash": row["phone_hash"],
                "store_dir": row["store_dir"],
                "state": row["state"] or "syncing",
                "trial_expires": row["trial_expires"] or "",
                "tenant_phone": _decrypt(row["tenant_phone"]) if row["tenant_phone"] else "",
                "created": row["created"] or "",
                "updated": row["updated"] or "",
            }
        except Exception as e:
            print(f"[DB] Error cargando wacli_store {phone_hash[:8]}: {e}")
            return None
        finally:
            conn.close()


def _db_wacli_store_set_trial(phone_hash, trial_expires):
    """Actualiza trial_expires de un wacli store."""
    now = time.strftime("%Y-%m-%d %H:%M")
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute(
                "UPDATE wacli_stores SET trial_expires=?, updated=? WHERE phone_hash=?",
                (trial_expires, now, phone_hash)
            )
            conn.commit()
        finally:
            conn.close()


def _db_wacli_store_update_state(phone_hash, state):
    """Actualiza solo el estado de un wacli store."""
    now = time.strftime("%Y-%m-%d %H:%M")
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute(
                "UPDATE wacli_stores SET state=?, updated=? WHERE phone_hash=?",
                (state, now, phone_hash)
            )
            conn.commit()
        finally:
            conn.close()


def _db_wacli_stores_list(state=None):
    """Lista wacli stores. Si state, filtra por ese estado."""
    with _db_lock:
        conn = _db_conn()
        try:
            if state:
                rows = conn.execute("SELECT * FROM wacli_stores WHERE state=?", (state,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM wacli_stores").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def _db_wacli_store_delete(phone_hash):
    """Elimina un wacli store de la DB."""
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute("DELETE FROM wacli_stores WHERE phone_hash=?", (phone_hash,))
            conn.commit()
        finally:
            conn.close()


def _send_whatsapp_image(to, image_path, caption=""):
    """Sube imagen a Meta media endpoint y la envía como mensaje de imagen.
    Usa credenciales de Lola (WA_CONFIG)."""
    if not WA_CONFIG:
        print("[WhatsApp] No hay WA_CONFIG para enviar imagen")
        return False
    phone_number_id = WA_CONFIG["phone_number_id"]
    access_token = WA_CONFIG["access_token"]

    # 1. Subir imagen a Media API
    upload_url = f"https://graph.facebook.com/v23.0/{phone_number_id}/media"
    boundary = f"----WaQrBoundary{int(time.time())}"
    with open(image_path, "rb") as f:
        image_data = f.read()
    body_parts = []
    body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"messaging_product\"\r\n\r\nwhatsapp".encode())
    body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"type\"\r\n\r\nimage/png".encode())
    body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"qr.png\"\r\nContent-Type: image/png\r\n\r\n".encode() + image_data)
    body_parts.append(f"--{boundary}--\r\n".encode())
    body = b"\r\n".join(body_parts)

    req = urllib.request.Request(upload_url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            media_resp = json.loads(resp.read())
        media_id = media_resp.get("id")
        if not media_id:
            print(f"[WhatsApp] Upload de imagen falló: {media_resp}")
            return False
        print(f"[WhatsApp] Imagen subida: media_id={media_id}")
    except Exception as e:
        print(f"[WhatsApp] Error subiendo imagen: {e}")
        return False

    # 2. Enviar mensaje de imagen
    msg_url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
    msg_payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {"id": media_id},
    }
    if caption:
        msg_payload["image"]["caption"] = caption
    req2 = urllib.request.Request(msg_url, data=json.dumps(msg_payload).encode(), method="POST")
    req2.add_header("Content-Type", "application/json")
    req2.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req2, timeout=30) as resp:
            print(f"[WhatsApp] Imagen enviada a {to}: {resp.status}")
        return True
    except Exception as e:
        print(f"[WhatsApp] Error enviando imagen a {to}: {e}")
        return False


def _wacli_tenant_auth(phone_hash, phone, wa_ctx=None):
    """Thread: ejecuta wacli --json auth para un tenant, lee QR codes de stdout,
    los convierte a PNG y los manda por WhatsApp. Cuando auth OK, arranca sync."""
    import tempfile

    store_dir = os.path.join(LOLA_WACLI, f"tenant-{phone_hash[:8]}")
    os.makedirs(store_dir, exist_ok=True)
    socket_path = os.path.join(store_dir, "cmd.sock")

    # Helper: send message via the right channel
    def _send(text):
        _send_whatsapp(phone, text, wa_ctx)

    # Helper: send image via the right channel
    def _send_image(path, caption=""):
        if (wa_ctx or {}).get("transport") == "wacli":
            chat_jid = (wa_ctx or {}).get("chat_jid") or phone
            _wacli_send_file(chat_jid, path, caption=caption)
        else:
            _send_whatsapp_image(phone, path, caption=caption)

    with _tenant_wacli_lock:
        # Verificar límite
        active = sum(1 for v in _tenant_wacli.values() if v["state"] in ("authenticating", "syncing"))
        if active >= _MAX_TENANT_WACLI:
            print(f"[wacli-tenant] Límite de {_MAX_TENANT_WACLI} tenants alcanzado, no se puede agregar {phone_hash[:8]}")
            _send("No pudimos configurar tu WhatsApp ahora porque estamos al limite de capacidad. Contacta soporte para que te ayudemos.")
            return
        # Cancelar auth previo si existe
        existing = _tenant_wacli.get(phone_hash)
        if existing and existing.get("proc"):
            try:
                existing["proc"].terminate()
            except Exception:
                pass
        _tenant_wacli[phone_hash] = {
            "state": "authenticating",
            "proc": None,
            "store_dir": store_dir,
            "socket_path": socket_path,
            "last_ts": None,
            "phone": phone,
        }

    print(f"[wacli-tenant] Iniciando auth para {phone_hash[:8]} (phone={phone})")

    # Mensaje intro
    _send(
        "te voy a mandar un codigo QR para vincular tu WhatsApp con Lola.\n\n"
        "cuando lo recibas, anda a WhatsApp > Configuracion > Dispositivos vinculados > Vincular dispositivo y escanea el QR.\n\n"
        "tenes 60 segundos para escanearlo, si se vence te mando otro."
    )

    try:
        proc = subprocess.Popen(
            [WACLI_BIN, "--json", "--store", store_dir, "auth", "--follow"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with _tenant_wacli_lock:
            _tenant_wacli[phone_hash]["proc"] = proc

        # Loguear stderr
        def _log_stderr():
            try:
                for line in proc.stderr:
                    msg = line.decode("utf-8", errors="replace").rstrip()
                    if msg:
                        print(f"[wacli-tenant-{phone_hash[:8]}] {msg}", flush=True)
            except Exception:
                pass
        threading.Thread(target=_log_stderr, daemon=True).start()

        # Leer stdout línea por línea buscando QR events y auth success
        qr_count = 0
        for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            if evt.get("event") == "qr":
                qr_count += 1
                qr_code = evt["code"]
                print(f"[wacli-tenant-{phone_hash[:8]}] QR #{qr_count} recibido")

                # Generar PNG con qrencode
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    subprocess.run(
                        ["qrencode", "-o", tmp_path, "-s", "10", "-m", "4", qr_code],
                        check=True, timeout=10,
                    )
                    cap = "Escanea este QR desde WhatsApp > Dispositivos vinculados" if qr_count == 1 else f"QR actualizado (intento {qr_count})"
                    _send_image(tmp_path, caption=cap)
                except Exception as e:
                    print(f"[wacli-tenant-{phone_hash[:8]}] Error generando/enviando QR: {e}")
                finally:
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass

            elif evt.get("authenticated"):
                # Auth exitoso! Guardar en DB y ya está en sync mode (--follow)
                print(f"[wacli-tenant-{phone_hash[:8]}] Auth exitoso!")
                _send("tu WhatsApp quedo vinculado con Lola! ya estoy lista para atender a tus clientes.")
                # Set trial_expires = now + 14 days
                trial_expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 14 * 86400))
                _db_wacli_store_save(phone_hash, store_dir, state="syncing", trial_expires=trial_expires, tenant_phone=phone)
                print(f"[wacli-tenant-{phone_hash[:8]}] Trial expires: {trial_expires}")
                with _tenant_wacli_lock:
                    _tenant_wacli[phone_hash]["state"] = "syncing"
                # El proc sigue corriendo en modo --follow, arrancamos el poll loop
                _wacli_tenant_start_poll(phone_hash)
                return

        # Si llegamos acá, el proceso terminó sin autenticar
        exit_code = proc.wait()
        print(f"[wacli-tenant-{phone_hash[:8]}] Auth terminó sin éxito (exit={exit_code})")
        _send("no pudimos vincular tu WhatsApp. Contacta soporte o intenta de nuevo desde la app.")
        with _tenant_wacli_lock:
            _tenant_wacli[phone_hash]["state"] = "error"
        _db_wacli_store_save(phone_hash, store_dir, state="error")

    except Exception as e:
        print(f"[wacli-tenant-{phone_hash[:8]}] Error en auth: {e}")
        with _tenant_wacli_lock:
            _tenant_wacli[phone_hash]["state"] = "error"
        _db_wacli_store_save(phone_hash, store_dir, state="error")


def _wacli_tenant_start_sync(phone_hash, store_dir):
    """Arranca wacli sync --follow --cmd-socket para un tenant ya autenticado."""
    socket_path = os.path.join(store_dir, "cmd.sock")
    db_path = os.path.join(store_dir, "wacli.db")

    # Limpiar socket viejo si existe
    if os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except Exception:
            pass

    proc = subprocess.Popen(
        [WACLI_BIN, "--store", store_dir, "sync", "--follow", "--cmd-socket", socket_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    print(f"[wacli-tenant-{phone_hash[:8]}] Sync iniciado (PID {proc.pid})")

    # Loguear stderr
    def _log_stderr():
        try:
            for line in proc.stderr:
                msg = line.decode("utf-8", errors="replace").rstrip()
                if msg:
                    print(f"[wacli-tenant-{phone_hash[:8]}] {msg}", flush=True)
        except Exception:
            pass
    threading.Thread(target=_log_stderr, daemon=True).start()

    # Esperar socket
    for _ in range(20):
        if os.path.exists(socket_path):
            print(f"[wacli-tenant-{phone_hash[:8]}] Socket listo: {socket_path}")
            break
        time.sleep(0.5)
    else:
        print(f"[wacli-tenant-{phone_hash[:8]}] WARN: socket no apareció después de 10s")

    with _tenant_wacli_lock:
        _tenant_wacli[phone_hash] = {
            "state": "syncing",
            "proc": proc,
            "store_dir": store_dir,
            "socket_path": socket_path,
            "last_ts": None,
            "phone": "",
        }

    _db_wacli_store_save(phone_hash, store_dir, state="syncing")
    _wacli_tenant_start_poll(phone_hash)


def _wacli_tenant_start_poll(phone_hash):
    """Arranca el poll loop para un tenant en un thread."""
    t = threading.Thread(target=_wacli_tenant_poll_loop, args=(phone_hash,), daemon=True)
    t.start()


def _wacli_tenant_poll_loop(phone_hash):
    """Thread que pollea wacli.db de un tenant buscando mensajes nuevos."""
    import sqlite3 as _sq3

    with _tenant_wacli_lock:
        info = _tenant_wacli.get(phone_hash)
    if not info:
        print(f"[wacli-tenant-{phone_hash[:8]}] No hay info de tenant, abortando poll")
        return

    store_dir = info["store_dir"]
    socket_path = info["socket_path"]
    db_path = os.path.join(store_dir, "wacli.db")

    # Cargar tenant info para system_prompt
    tenant = _db_tenant_load_by_hash(phone_hash)
    system_prompt = (tenant or {}).get("system_prompt") or LOLA_SALES_PROMPT
    tenant_phone = (tenant or {}).get("phone", "")

    # Arrancar con el timestamp actual para no procesar historial viejo
    last_ts = None
    for _ in range(30):  # esperar hasta 30s a que aparezca el DB
        if os.path.isfile(db_path):
            break
        time.sleep(1)
    else:
        print(f"[wacli-tenant-{phone_hash[:8]}] DB no apareció: {db_path}")
        return

    try:
        conn = _sq3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = _sq3.Row
        row = conn.execute("SELECT MAX(ts) as max_ts FROM messages").fetchone()
        if row and row["max_ts"]:
            last_ts = row["max_ts"]
        else:
            last_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.close()
        print(f"[wacli-tenant-{phone_hash[:8]}] Polling iniciado desde ts={last_ts}")
    except Exception as e:
        print(f"[wacli-tenant-{phone_hash[:8]}] Error inicializando poll: {e}")
        return

    while True:
        try:
            # Verificar que el tenant sigue activo
            with _tenant_wacli_lock:
                tinfo = _tenant_wacli.get(phone_hash)
            if not tinfo or tinfo["state"] not in ("syncing",):
                print(f"[wacli-tenant-{phone_hash[:8]}] Tenant ya no está en syncing, parando poll")
                return

            # Check trial/subscription validity
            store = _db_wacli_store_get(phone_hash)
            if store and store["state"] == "expired":
                print(f"[wacli-tenant-{phone_hash[:8]}] Store expired, parando poll")
                return
            if store and store.get("trial_expires"):
                try:
                    trial_ts = time.mktime(time.strptime(store["trial_expires"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
                except (ValueError, OverflowError):
                    trial_ts = 0
                if trial_ts and time.time() > trial_ts:
                    # Trial expired — check for payment
                    tenant = _db_tenant_load_by_hash(phone_hash)
                    sub = _mp_check_subscription(tenant["phone"]) if tenant and tenant.get("phone") else None
                    if sub and sub.get("status") == "authorized":
                        # Has payment — clear trial, keep syncing
                        _db_wacli_store_set_trial(phone_hash, "")
                        print(f"[wacli-tenant-{phone_hash[:8]}] Trial expirado pero tiene pago, sigue activo")
                    else:
                        # No payment — expire
                        _db_wacli_store_update_state(phone_hash, "expired")
                        with _tenant_wacli_lock:
                            if phone_hash in _tenant_wacli:
                                _tenant_wacli[phone_hash]["state"] = "expired"
                        _send_trial_expired_notice(phone_hash, store.get("tenant_phone", ""))
                        print(f"[wacli-tenant-{phone_hash[:8]}] Trial expirado sin pago, parando")
                        return

            time.sleep(2)
            conn = _sq3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = _sq3.Row
            rows = conn.execute(
                "SELECT chat_jid, msg_id, sender_jid, ts, from_me, text, media_type, media_caption, mime_type "
                "FROM messages WHERE ts > ? AND from_me = 0 ORDER BY ts ASC LIMIT 20",
                (last_ts,)
            ).fetchall()
            conn.close()

            for row in rows:
                last_ts = row["ts"]
                chat_jid = row["chat_jid"]
                text = (row["text"] or "").strip()
                msg_id = row["msg_id"] or ""
                media_type = (row["media_type"] or "").strip()

                # Solo DMs
                if "@g.us" in chat_jid:
                    continue

                from_number = chat_jid.split("@")[0]
                if not from_number.isdigit():
                    sender = (row["sender_jid"] or "").split("@")[0]
                    if sender.isdigit():
                        from_number = sender
                    else:
                        continue

                if not text and not media_type:
                    continue

                wa_ctx = {
                    "transport": "wacli",
                    "system_prompt": system_prompt,
                    "is_lola_sales": False,
                    "chat_jid": chat_jid,
                    "socket_path": socket_path,
                }

                if media_type in ("audio", "image"):
                    caption = (row["media_caption"] or "").strip()
                    print(f"[wacli-tenant-{phone_hash[:8]}] {media_type.capitalize()} de {from_number}")
                    _wa_queue_message(from_number, msg_id, {
                        "type": media_type,
                        "wacli_msg_id": msg_id,
                        "wacli_chat_jid": chat_jid,
                        "caption": caption,
                        "mime_type": (row["mime_type"] or ""),
                    }, wa_ctx)
                else:
                    print(f"[wacli-tenant-{phone_hash[:8]}] Mensaje de {from_number}: {text[:80]}")
                    _wa_queue_message(from_number, msg_id, {"type": "text", "text": text}, wa_ctx)

        except Exception as e:
            print(f"[wacli-tenant-{phone_hash[:8]}] Error en poll loop: {e}")
            time.sleep(5)


def _db_tenant_load_by_hash(phone_hash):
    """Carga un tenant desde SQLite usando el hash directamente."""
    with _db_lock:
        conn = _db_conn()
        try:
            row = conn.execute("SELECT * FROM tenants WHERE phone_hash = ?", (phone_hash,)).fetchone()
            if not row:
                return None
            return {
                "phone": _decrypt(row["phone"]) if row["phone"] else "",
                "email": _decrypt(row["email"]) if row["email"] else "",
                "plan": row["plan"] or "",
                "business_data": json.loads(_decrypt(row["business_data"])) if row["business_data"] else {},
                "system_prompt": _decrypt(row["system_prompt"]) if row["system_prompt"] else "",
            }
        except Exception as e:
            print(f"[DB] Error cargando tenant por hash {phone_hash[:8]}: {e}")
            return None
        finally:
            conn.close()


def _wacli_tenant_boot_restore():
    """Al iniciar, restaura los tenants con wacli activo de la DB."""
    stores = _db_wacli_stores_list(state="syncing")
    if not stores:
        return
    print(f"[wacli-tenant] Restaurando {len(stores)} tenant(s) wacli...")
    for st in stores:
        phone_hash = st["phone_hash"]
        store_dir = st["store_dir"]
        # Check if trial expired before restoring
        trial_exp = st.get("trial_expires", "")
        if trial_exp:
            try:
                trial_ts = time.mktime(time.strptime(trial_exp, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
                if time.time() > trial_ts:
                    # Check payment before marking expired
                    tenant = _db_tenant_load_by_hash(phone_hash)
                    sub = _mp_check_subscription(tenant["phone"]) if tenant and tenant.get("phone") else None
                    if sub and sub.get("status") == "authorized":
                        _db_wacli_store_set_trial(phone_hash, "")
                        print(f"[wacli-tenant-{phone_hash[:8]}] Trial expirado pero tiene pago, limpiando trial")
                    else:
                        _db_wacli_store_update_state(phone_hash, "expired")
                        tenant_phone = _decrypt(st["tenant_phone"]) if st.get("tenant_phone") else ""
                        _send_trial_expired_notice(phone_hash, tenant_phone)
                        print(f"[wacli-tenant-{phone_hash[:8]}] Trial expirado al restaurar, marcando expired")
                        continue
            except (ValueError, OverflowError):
                pass
        db_path = os.path.join(store_dir, "wacli.db")
        if not os.path.isfile(db_path):
            print(f"[wacli-tenant-{phone_hash[:8]}] Store no encontrado ({store_dir}), marcando error")
            _db_wacli_store_update_state(phone_hash, "error")
            continue
        print(f"[wacli-tenant-{phone_hash[:8]}] Restaurando sync desde {store_dir}")
        _wacli_tenant_start_sync(phone_hash, store_dir)
        time.sleep(2)  # No saturar


# ═══════════════ OTP / AUTH / TENANTS ═══════════════

# OTP pendientes: phone → {code, created, attempts}
# Rate limit por IP para /api/lola-chat
_ip_rate = {}  # ip → [timestamps]
_IP_RATE_MAX = 20       # máx requests por hora
_IP_RATE_WINDOW = 3600  # 1 hora

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
_mp_config_path = os.path.join(LOLA_SECRETS, ".mercadopago-config.json")
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
    f"python3 {LOLA_BASE}/gemini_router.py --status",
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
    "Sos Lola. Trabajás atendiendo clientes por WhatsApp para negocios, las 24hs. "
    "Estás hablando con dueños de tiendas/negocios que quieren contratarte. "
    "SIEMPRE hablá en primera persona: \"yo atiendo\", \"yo respondo\", \"yo cierro ventas\". "
    "NUNCA hables de vos misma en tercera persona como \"Lola puede\" o \"ella hace\". Vos SOS Lola.\n"
    "\n"
    "CÓMO HABLAR:\n"
    "- MÁXIMO 2-3 líneas por mensaje. Si vas a decir más, pará y preguntá algo.\n"
    "- Escribís como una persona REAL en WhatsApp. Nada de texto perfecto ni prolijo.\n"
    "- NUNCA uses signos invertidos (¡ ¿), asteriscos ni negritas. Es WhatsApp, no markdown.\n"
    "- Minúsculas siempre. \"si claro\" en vez de \"Sí, claro!\".\n"
    "- Tono: cercana y simpática pero PROFESIONAL. Como una vendedora joven que te cae bien, no como una amiga del liceo.\n"
    "- Hablás en uruguayo suave: \"dale\", \"ta\", \"bárbaro\", \"de una\", \"re\". NO abuses del lunfardo ni uses \"bo\", \"posta\", \"onda\" ni jerga excesiva.\n"
    "- NO hagas listas ni enumeres cosas. Contá todo en oraciones sueltas, como hablando.\n"
    "- NO repitas info que ya dijiste.\n"
    "- Podés usar alguna muletilla natural de vez en cuando: \"mirá\", \"ponele\". Pero sin exagerar.\n"
    "- NUNCA hagas preguntas genéricas de coach/IA tipo: \"qué es lo que más valorás?\", \"cómo te sentís con eso?\", \"qué te parece importante?\". Esas frases te delatan como IA al instante. Preguntá cosas CONCRETAS sobre su negocio: \"cuántos mensajes recibís por día?\", \"tenés catálogo armado?\", \"usás mercadopago?\".\n"
    "- Cuando el cliente se despide (chau, nos vemos, gracias, etc), reaccioná con {{react:🙌}} Y TAMBIÉN respondé despidiéndote (ej: 'dale, hablamos!' o 'chau, cualquier cosa acá estoy'). NUNCA mandes solo la reacción sin texto.\n"
    "\n"
    "ESTRATEGIA DE CONVERSACIÓN (MUY IMPORTANTE):\n"
    "- PRIMERO preguntá, DESPUÉS explicá. Nunca largues el pitch de una.\n"
    "- Siempre terminá con una pregunta para mantener la charla.\n"
    "- Dá la info DE A POCO. Solo respondé lo que te preguntan, no te adelantes.\n"
    "- Si preguntan qué hacés: resumilo en 1 oración y preguntá qué tipo de negocio tienen.\n"
    "- Si preguntan precios: tirá solo el rango (\"arranca en 1290 por mes\") y preguntá qué necesitan para recomendar.\n"
    "- Si te dicen su rubro: engancháte con eso, mostrá que entendés su problema específico. Ej peluquería: \"uh mal, los turnos por whatsapp son un quilombo no? yo te puedo manejar todo eso\".\n"
    "- Generá confianza con experiencia: \"ya tengo negocios parecidos andando\" (no inventes nombres específicos).\n"
    "- Si sentís que están tibios, no empujes. Preguntá qué los frena.\n"
    "\n"
    "QUÉ SÉ HACER (usá esta info cuando sea relevante, NUNCA la tires toda junta):\n"
    "- Me conecto al whatsapp del negocio y atiendo clientes 24/7.\n"
    "- Respondo consultas, muestro catálogo, precios y stock.\n"
    "- Cobro: mando link de pago de MercadoPago directo al cliente.\n"
    "- Informo estado de pedidos y pagos.\n"
    "- Entiendo texto, audios, fotos y ubicación.\n"
    "- Atiendo varios clientes a la vez, de noche, findes y feriados.\n"
    "\n"
    "PRUEBA GRATIS:\n"
    "- Ofrecés 2 semanas gratis, sin pagar nada, sin compromiso.\n"
    "- Si el comerciante acepta probar o dice que quiere, escribí {{trial_start}} al final de tu mensaje de confirmación.\n"
    "- SOLO usá {{trial_start}} cuando el comerciante diga claramente que quiere probar.\n"
    "- Después del trial arranca la suscripción si quieren seguir.\n"
    "- Ejemplo: 'dale, te lo dejo andando 2 semanas gratis y vos me decis {{trial_start}}'\n"
    "\n"
    "PLANES (mencioná solo si preguntan, el foco es la prueba gratis):\n"
    "- Básico ($1.290 UYU/mes): catálogo, chat inteligente 24/7, hasta 500 conversaciones/mes.\n"
    "- Pro ($3.490 UYU/mes): todo lo del básico + integración con su sistema, stock en tiempo real, pedidos y cobros automáticos, sin límite de conversaciones.\n"
    "- Pero primero que prueben gratis. El precio viene después.\n"
    "\n"
    "CÓMO FUNCIONA (explicá solo si preguntan, en máximo 2 líneas):\n"
    "- Me pasan su catálogo, conecto a su whatsapp, y arranco a atender. re simple.\n"
    "\n"
    "TAGS (yo los proceso, NUNCA los muestres como texto visible):\n"
    "- {{trial_start}} → activa la prueba gratis. SOLO cuando el comerciante acepta probar.\n"
    "- {{cobrar:MONTO:DESCRIPCION}} → genera link de pago. Ej: \"dale, te paso el link {{cobrar:2500:Remera azul talle M}}\"\n"
    "- {{estado_pago}} → consulta el estado del último pago. Ej: \"dejame chequear {{estado_pago}}\"\n"
    "- {{plan:basico}} o {{plan:pro}} → link de suscripción. Ej: \"te paso el link {{plan:basico}}\"\n"
    "- {{estado_suscripcion}} → chequea si ya está suscripto. Ej: \"dejame ver {{estado_suscripcion}}\"\n"
    "- {{react:🙌}} → reacciona al mensaje del cliente con un emoji. Solo para despedidas.\n"
    "\n"
    "CONTRATACIÓN:\n"
    "- El foco principal es ofrecer la prueba gratis de 2 semanas.\n"
    "- Si aceptan, usá {{trial_start}} y listo.\n"
    "- Solo hablá de planes y precios si preguntan explícitamente.\n"
    "\n"
    "REGLAS:\n"
    "- La meta es que el interesado quiera contratar, pero sin ser pesada. Vendé conversando, no empujando.\n"
    "- Si preguntan algo técnico que no sabés, decí que se ve todo en el setup y es re simple.\n"
    "- NUNCA inventes features que no existen.\n"
    "- NUNCA inventes URLs ni links. Solo usá los tags, yo los reemplazo por los links reales.\n"
    "- SIEMPRE usá {{estado_pago}} cuando hablen de un pago. NUNCA confirmes un pago sin verificar con el tag. Si el cliente dice 'ya pagué' o insiste, volvé a chequear con {{estado_pago}}.\n"
    "- Si piden contacto: hola@lola.uy\n"
    "- Sos Lola. La mejor prueba de que funciona sos vos misma hablando con ellos ahora.\n"
    "- NUNCA digas que sos una IA, inteligencia artificial, chatbot, bot o programa. Si te preguntan, decí que sos Lola y que trabajás atendiendo clientes por WhatsApp. Cambiá de tema naturalmente.\n"
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
    "- Cuando el comerciante confirme que está todo bien, decile que ya quedó la configuración y que ahora le vas a mandar un código QR para vincular su WhatsApp con Lola. Le va a llegar en unos segundos.\n"
    "- Escribí EXACTAMENTE el tag {{onboarding_complete}} al final de ese mensaje.\n"
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


def _send_whatsapp(to, text, wa_ctx=None):
    """Envía un mensaje de texto via WhatsApp Graph API o wacli."""
    # Si el transporte es wacli, usar CLI
    if (wa_ctx or {}).get("transport") == "wacli":
        chat_jid = (wa_ctx or {}).get("chat_jid") or to
        _wacli_send(chat_jid, text, socket_path=(wa_ctx or {}).get("socket_path"))
        return
    phone_number_id = (wa_ctx or {}).get("phone_number_id") or (WA_CONFIG or {}).get("phone_number_id")
    access_token = (wa_ctx or {}).get("access_token") or (WA_CONFIG or {}).get("access_token")
    if not phone_number_id or not access_token:
        return
    url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {access_token}")
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


def _wa_queue_message(from_number, msg_id, msg_data, wa_ctx=None):
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
            "wa_ctx": wa_ctx,
        }
    # Typing indicator con el primer msg_id (fuera del lock)
    if msg_id:
        _wa_typing(from_number, msg_id, wa_ctx)
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
    wa_ctx = pending.get("wa_ctx")
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
        if media_item.get("wacli_msg_id"):
            # Media via wacli — descargar con CLI
            _handle_wacli_media(
                from_number, media_item, first_msg_id,
                combined_text, wa_ctx=wa_ctx,
            )
        else:
            _handle_wa_media(
                from_number, media_item["media_id"], first_msg_id,
                media_item["type"], media_item.get("caption", "") or combined_text,
                wa_ctx=wa_ctx,
            )
    else:
        _handle_wa_message(from_number, combined_text, first_msg_id, wa_ctx=wa_ctx)


# Historial para chat web de Lola (por session_id)
_lola_web_history = {}
_LOLA_WEB_HISTORY_MAX = 20
_LOLA_WEB_HISTORY_TTL = 30 * 60  # 30 min


def _wa_get_history(number):
    """Devuelve el historial de un número, limpiando si expiró.
    Si no hay nada en memoria, carga los últimos turnos de la DB."""
    entry = _wa_history.get(number)
    if entry and (time.time() - entry["ts"]) > _WA_HISTORY_TTL:
        del _wa_history[number]
        entry = None
    if entry:
        return entry["messages"]
    # Intentar cargar de DB
    phone_hash = _hash_key(number)
    db_msgs = _db_load_recent_messages(phone_hash, limit=_WA_HISTORY_MAX)
    if db_msgs:
        _wa_history[number] = {"messages": db_msgs, "ts": time.time()}
        return db_msgs
    return []


def _wa_append(number, role, text):
    """Agrega un mensaje al historial de un número y lo persiste en DB."""
    if number not in _wa_history:
        _wa_history[number] = {"messages": [], "ts": time.time()}
    entry = _wa_history[number]
    entry["ts"] = time.time()
    entry["messages"].append({"role": role, "text": text})
    # Recortar si excede el máximo (sacamos los más viejos, de a pares)
    while len(entry["messages"]) > _WA_HISTORY_MAX:
        entry["messages"].pop(0)
    # Persistir en DB (async para no bloquear)
    phone_hash = _hash_key(number)
    threading.Thread(target=_db_log_message, args=(phone_hash, role, text), daemon=True).start()


def _wa_download_media(media_id, wa_ctx=None):
    """Descarga un archivo multimedia de WhatsApp. Retorna (bytes, mime_type) o (None, None)."""
    token = (wa_ctx or {}).get("access_token") or (WA_CONFIG or {}).get("access_token")
    if not token:
        return None, None
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


def _wa_typing(to, msg_id="", wa_ctx=None):
    """Muestra 'escribiendo...' y marca el mensaje como leído."""
    if (wa_ctx or {}).get("transport") == "wacli":
        chat_jid = (wa_ctx or {}).get("chat_jid") or to
        _wacli_typing(chat_jid, socket_path=(wa_ctx or {}).get("socket_path"))
        return
    phone_number_id = (wa_ctx or {}).get("phone_number_id") or (WA_CONFIG or {}).get("phone_number_id")
    access_token = (wa_ctx or {}).get("access_token") or (WA_CONFIG or {}).get("access_token")
    if not phone_number_id or not access_token or not msg_id:
        return
    url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
    data = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": msg_id,
        "typing_indicator": {"type": "text"},
    }
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        print(f"[WhatsApp] Error typing indicator: {e}")


def _wa_react(to, msg_id, emoji, wa_ctx=None):
    """Reacciona a un mensaje con un emoji."""
    if (wa_ctx or {}).get("transport") == "wacli":
        return  # wacli no soporta reacciones aún
    phone_number_id = (wa_ctx or {}).get("phone_number_id") or (WA_CONFIG or {}).get("phone_number_id")
    access_token = (wa_ctx or {}).get("access_token") or (WA_CONFIG or {}).get("access_token")
    if not phone_number_id or not access_token or not msg_id:
        return
    url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "reaction",
        "reaction": {"message_id": msg_id, "emoji": emoji},
    }
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        print(f"[WhatsApp] Error reacción: {e}")


# Reacciones — se elige una al azar
_WA_REACTIONS = ["👍", "👌", "🙌", "😊"]


def _time_period():
    """Retorna el período del día actual (Uruguay, UTC-3)."""
    hour = (time.gmtime().tm_hour - 3) % 24
    if 5 <= hour < 12:
        return "mañana"
    elif 12 <= hour < 19:
        return "tarde"
    elif 19 <= hour < 24:
        return "noche"
    else:
        return "madrugada"


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
    url = f"https://graph.instagram.com/v23.0/{IG_CONFIG['ig_user_id']}/messages"
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
            resultado = "(no pude consultar el estado del pago)"
        elif not info["found"]:
            resultado = "todavia no me aparece ningun pago tuyo, fijate si se completo bien"
        else:
            st = info["status"]
            amt = info["amount"]
            desc = info["description"] or "tu compra"
            if st == "approved":
                resultado = f"si, ya me llego tu pago de ${amt:.0f} por {desc}. gracias!"
            elif st == "pending" or st == "in_process":
                resultado = f"tu pago de ${amt:.0f} por {desc} esta pendiente todavia, dale unos minutos"
            elif st == "rejected":
                resultado = f"tu pago de ${amt:.0f} fue rechazado, fijate de intentar de nuevo"
            else:
                resultado = f"tu pago aparece como '{st}', cualquier cosa escribime"
        return f"{{{{PAUSA:5}}}}{resultado}"

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
            resultado = "no me aparece ninguna suscripcion tuya todavia"
        else:
            st = info["status"]
            plan = info["plan"]
            if st == "authorized":
                resultado = f"si, ya estas suscripto al plan {plan}, todo en orden"
            elif st == "pending":
                resultado = f"tu suscripcion al plan {plan} esta pendiente, fijate si se completo el pago"
            elif st == "cancelled":
                resultado = f"tu suscripcion al plan {plan} esta cancelada"
            else:
                resultado = f"tu suscripcion al plan {plan} aparece como '{st}'"
        return f"{{{{PAUSA:5}}}}{resultado}"

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


def _trial_expiry_checker():
    """Daemon thread: runs every hour, checks trial expirations and sends notices."""
    while True:
        try:
            time.sleep(3600)  # every hour
            stores = _db_wacli_stores_list(state="syncing")
            now = time.time()
            for st in stores:
                trial_exp = st.get("trial_expires", "")
                if not trial_exp:
                    continue
                phone_hash = st["phone_hash"]
                try:
                    trial_ts = time.mktime(time.strptime(trial_exp, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
                except (ValueError, OverflowError):
                    continue
                tenant_phone = _decrypt(st["tenant_phone"]) if st.get("tenant_phone") else ""
                remaining = trial_ts - now
                if remaining < 0:
                    # Trial expired — check payment
                    tenant = _db_tenant_load_by_hash(phone_hash)
                    sub = _mp_check_subscription(tenant["phone"]) if tenant and tenant.get("phone") else None
                    if sub and sub.get("status") == "authorized":
                        _db_wacli_store_set_trial(phone_hash, "")
                        print(f"[trial-checker] {phone_hash[:8]} trial expirado pero tiene pago, limpiando trial")
                    else:
                        _db_wacli_store_update_state(phone_hash, "expired")
                        with _tenant_wacli_lock:
                            if phone_hash in _tenant_wacli:
                                _tenant_wacli[phone_hash]["state"] = "expired"
                        _send_trial_expired_notice(phone_hash, tenant_phone)
                elif remaining < 86400:
                    # Less than 24 hours left
                    _send_trial_reminder(phone_hash, tenant_phone)
        except Exception as e:
            print(f"[trial-checker] Error: {e}")


def _send_trial_expired_notice(phone_hash, tenant_phone):
    """Sends a trial expired notice to the tenant, once."""
    if phone_hash in _trial_expiry_notified:
        return
    _trial_expiry_notified.add(phone_hash)
    if not tenant_phone:
        store = _db_wacli_store_get(phone_hash)
        tenant_phone = (store or {}).get("tenant_phone", "")
    if not tenant_phone:
        return
    # Build payment link
    plan_link = ""
    if MP_CONFIG:
        plans = MP_CONFIG.get("plans", {})
        basic = plans.get("basico", {})
        if basic.get("init_point"):
            plan_link = f"\n\npara seguir con Lola, suscribite aca: {basic['init_point']}"
    msg = (
        "hola! tu periodo de prueba gratis de 2 semanas termino."
        f"{plan_link}\n\n"
        "si tenes alguna duda escribime aca."
    )
    _send_whatsapp(tenant_phone, msg)
    print(f"[trial] Aviso de expiración enviado a {tenant_phone}")


def _send_trial_reminder(phone_hash, tenant_phone):
    """Sends a trial expiring soon reminder (< 24h left)."""
    reminder_key = f"reminder_{phone_hash}"
    if reminder_key in _trial_expiry_notified:
        return
    _trial_expiry_notified.add(reminder_key)
    if not tenant_phone:
        return
    plan_link = ""
    if MP_CONFIG:
        plans = MP_CONFIG.get("plans", {})
        basic = plans.get("basico", {})
        if basic.get("init_point"):
            plan_link = f"\n\npara seguir con Lola despues del trial, suscribite aca: {basic['init_point']}"
    msg = (
        "hola! te queda menos de un dia de prueba gratis."
        f"{plan_link}\n\n"
        "cualquier duda escribime."
    )
    _send_whatsapp(tenant_phone, msg)
    print(f"[trial] Recordatorio de expiración enviado a {tenant_phone}")


def _handle_trial_start(from_number, wa_ctx=None):
    """Procesa {{trial_start}}: crea registro onboarding y cambia a modo onboarding."""
    phone_hash = _hash_key(from_number)
    # Create wacli_stores entry with state=onboarding
    _db_wacli_store_save(phone_hash, "", state="onboarding", tenant_phone=from_number)
    # Add to onboarding sessions so next messages use LOLA_ONBOARDING_PROMPT
    _onboarding_sessions[from_number] = {"phone_hash": phone_hash, "state": "onboarding"}
    print(f"[trial] Onboarding iniciado para {from_number} (hash={phone_hash[:8]})")


def _handle_onboarding_complete(from_number, wa_ctx=None):
    """Procesa {{onboarding_complete}}: extrae datos, guarda tenant, dispara QR auth."""
    session_info = _onboarding_sessions.get(from_number)
    if not session_info:
        print(f"[onboarding] No hay sesión de onboarding para {from_number}, ignorando")
        return

    phone_hash = session_info["phone_hash"]
    history = _wa_get_history(from_number)

    # Build conversation text for extraction
    conv_text = ""
    for m in history:
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
            print(f"[onboarding] Error extrayendo datos: {result.get('error')}")
            return

        raw = result["text"].strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)

        # Generate system prompt
        system_prompt = _generate_tenant_prompt(data)

        # Save tenant
        tenant = {
            "phone": from_number,
            "email": "",
            "plan": "trial",
            "data": data,
            "system_prompt": system_prompt,
            "created": time.strftime("%Y-%m-%d %H:%M"),
            "updated": time.strftime("%Y-%m-%d %H:%M"),
        }
        _tenant_save(from_number, tenant)

        # Update wacli_stores state to pending_qr
        _db_wacli_store_update_state(phone_hash, "pending_qr")

        print(f"[onboarding] Completado para {from_number}: {data.get('nombre_negocio', '?')}")

        # Remove from onboarding sessions
        _onboarding_sessions.pop(from_number, None)

        # Dispatch QR auth in a new thread
        t = threading.Thread(target=_wacli_tenant_auth, args=(phone_hash, from_number, wa_ctx), daemon=True)
        t.start()
        print(f"[onboarding] QR auth dispatched para {from_number}")

    except json.JSONDecodeError as e:
        print(f"[onboarding] JSON inválido en extracción: {e}")
    except Exception as e:
        print(f"[onboarding] Error en proceso: {e}")


def _handle_wa_message(from_number, text, msg_id="", media_data=None, media_mime=None, media_label="audio", wa_ctx=None):
    """Procesa un mensaje de WhatsApp y responde (en thread aparte)."""
    # Delay variable antes de empezar a tipear (1-3s, como una persona)
    time.sleep(random.uniform(1.0, 3.0))

    # Mostrar "escribiendo..." mientras Gemini procesa
    if msg_id:
        _wa_typing(from_number, msg_id, wa_ctx)

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

    # Determinar system prompt: del tenant (wa_ctx) o default de Lola ventas
    system_prompt = (wa_ctx or {}).get("system_prompt") or WA_SYSTEM_PROMPT

    # Agregar contexto de horario al system prompt
    period = _time_period()
    time_ctx = f"\n(Contexto: ahora es de {period} en Uruguay. Saludá acorde si es el primer mensaje.)\n"
    system_prompt = system_prompt + time_ctx

    try:
        result = router.ask_chat(messages, system=system_prompt, timeout=30)
        if result["ok"]:
            reply = result["text"]
            if reply:
                reply = reply[0].upper() + reply[1:]
            # Procesar trial/onboarding tags before other tags
            if "{{trial_start}}" in reply and (wa_ctx or {}).get("is_lola_sales"):
                reply = reply.replace("{{trial_start}}", "").strip()
                _handle_trial_start(from_number, wa_ctx)
            if "{{onboarding_complete}}" in reply and (wa_ctx or {}).get("is_onboarding"):
                reply = reply.replace("{{onboarding_complete}}", "").strip()
                _handle_onboarding_complete(from_number, wa_ctx)
            # Procesar tags antes de enviar
            react_emoji = ""
            if "{{" in reply:
                # Extraer reacción si la hay ({{react:🙌}})
                react_match = re.search(r"\{\{react:(.+?)\}\}", reply)
                if react_match:
                    react_emoji = react_match.group(1).strip()
                    reply = re.sub(r"\{\{react:.+?\}\}", "", reply).strip()
                reply = _process_lola_tags(reply, from_number)
            # Reaccionar al último mensaje del usuario si Lola lo indicó
            if react_emoji and msg_id:
                _wa_react(from_number, msg_id, react_emoji, wa_ctx)
            # Guard: si reply quedó vacío después de procesar tags, no enviar
            if not reply:
                print(f"[WhatsApp] Reply vacío después de procesar tags, no se envía mensaje a {from_number}")
                return
            # Guardar en historial SIN marcadores internos ({{PAUSA:N}})
            clean_reply = re.sub(r"\{\{PAUSA:\d+\}\}", "\n", reply).strip()
            _wa_append(from_number, "model", clean_reply)
            model = result.get("model", "?")
            key = result.get("key", "?")
            rpd = router.rpd_counts.get(key - 1, {}).get(model, "?") if isinstance(key, int) else "?"
            print(f"[WhatsApp] Respondido con K{key}/{model} (RPD usado: {rpd}): {reply[:120]}")
            # Separar por {{PAUSA:N}} para simular espera (ej: chequeo de pagos)
            pausa_parts = re.split(r"\{\{PAUSA:(\d+)\}\}", reply)
            # pausa_parts: [texto_antes, segundos, texto_despues, ...]
            segments = []  # lista de (texto, delay_antes)
            i = 0
            while i < len(pausa_parts):
                text_part = pausa_parts[i].strip()
                if i == 0:
                    if text_part:
                        segments.append((text_part, 0))
                else:
                    # pausa_parts[i] es el delay, pausa_parts[i+1] es el texto
                    delay_secs = int(pausa_parts[i])
                    i += 1
                    text_part = pausa_parts[i].strip() if i < len(pausa_parts) else ""
                    if text_part:
                        segments.append((text_part, delay_secs))
                i += 1

            for seg_text, seg_delay in segments:
                if seg_delay > 0:
                    _wa_typing(from_number, msg_id, wa_ctx)
                    time.sleep(seg_delay)
                # Dividir en varios mensajes para parecer natural
                chunks = _split_reply(seg_text)
                for j, chunk in enumerate(chunks):
                    if j > 0:
                        delay = min(0.5 + len(chunks[j - 1]) * 0.02, 3.0)
                        _wa_typing(from_number, msg_id, wa_ctx)
                        time.sleep(delay)
                    _send_whatsapp(from_number, chunk, wa_ctx)
        else:
            _send_whatsapp(from_number, "Uh, tuve un error procesando tu mensaje. Probá de nuevo en un rato.", wa_ctx)
            print(f"[WhatsApp] Error de Gemini: {result.get('error')}")
    except Exception as e:
        print(f"[WhatsApp] Excepción procesando mensaje de {from_number}: {e}")
        _send_whatsapp(from_number, "Se me rompió algo, probá de nuevo.", wa_ctx)


def _handle_wacli_media(from_number, media_item, msg_id="", caption="", wa_ctx=None):
    """Descarga media via wacli cmd-socket y lo manda a Gemini."""
    media_type = media_item["type"]  # "audio" o "image"
    wacli_msg_id = media_item["wacli_msg_id"]
    wacli_chat_jid = media_item["wacli_chat_jid"]
    mime_type = media_item.get("mime_type", "")
    item_caption = media_item.get("caption", "")
    final_caption = item_caption or caption or ""
    if msg_id:
        _wa_typing(from_number, msg_id, wa_ctx)
    try:
        # Descargar via cmd-socket (no lockea el store)
        import socket as _socket
        _sp = (wa_ctx or {}).get("socket_path") or WACLI_SOCKET
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(60)
        sock.connect(_sp)
        cmd = json.dumps({
            "action": "download_media",
            "chat": wacli_chat_jid,
            "msg_id": wacli_msg_id,
        }) + "\n"
        sock.sendall(cmd.encode("utf-8"))
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        sock.close()
        resp = json.loads(data.strip())
        if not resp.get("ok"):
            print(f"[wacli] Error descargando media: {resp.get('error', '?')}")
            _send_whatsapp(from_number, f"No pude recibir el {media_type}, me lo mandás de nuevo?", wa_ctx)
            return
        local_path = resp.get("path", "")
        if not local_path or not os.path.isfile(local_path):
            print(f"[wacli] Media descargado pero archivo no encontrado: {local_path}")
            _send_whatsapp(from_number, f"No pude recibir el {media_type}, me lo mandás de nuevo?", wa_ctx)
            return
        with open(local_path, "rb") as f:
            file_data = f.read()
        # Inferir mime si no lo tenemos
        if not mime_type:
            if media_type == "audio":
                mime_type = "audio/ogg"
            elif media_type == "image":
                mime_type = "image/jpeg"
        print(f"[wacli] {media_type.capitalize()} descargado: {len(file_data)} bytes, {mime_type}")
        _handle_wa_message(from_number, final_caption, msg_id="", media_data=file_data, media_mime=mime_type, media_label=media_type, wa_ctx=wa_ctx)
    except Exception as e:
        print(f"[wacli] Error procesando media: {e}")
        _send_whatsapp(from_number, f"No pude recibir el {media_type}, me lo mandás de nuevo?", wa_ctx)


def _handle_wa_media(from_number, media_id, msg_id="", media_label="audio", caption="", wa_ctx=None):
    """Descarga media de WhatsApp y lo manda a Gemini en una sola request."""
    if msg_id:
        _wa_typing(from_number, msg_id, wa_ctx)
    data, mime_type = _wa_download_media(media_id, wa_ctx)
    if not data:
        _send_whatsapp(from_number, f"No pude recibir el {media_label}, me lo mandás de nuevo?", wa_ctx)
        return
    print(f"[WhatsApp] {media_label.capitalize()} descargado: {len(data)} bytes, {mime_type}")
    _handle_wa_message(from_number, caption, msg_id="", media_data=data, media_mime=mime_type, media_label=media_label, wa_ctx=wa_ctx)


class RenzoHandler(SimpleHTTPRequestHandler):
    timeout = 120  # 2 min para requests grandes

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def do_GET(self, *args, **kwargs):
        parsed = urlparse(self.path)
        path = parsed.path
        # Block access to hidden/sensitive files
        if path.startswith("/.") or "/.." in path or path.startswith("/server.py"):
            self.send_error(404)
            return
        if path == "/health":
            uptime = int(time.time() - _SERVER_START)
            self._json_response({
                "status": "ok",
                "uptime_seconds": uptime,
                "gemini_keys": len(router.keys),
                "whatsapp": WA_CONFIG is not None,
                "mercadopago": MP_CONFIG is not None,
                "wa_numbers": _db_wa_numbers_count(),
                "tenants": _db_tenants_count(),
                "wacli_tenants": len([v for v in _tenant_wacli.values() if v["state"] == "syncing"]),
            })
            return
        if path == "/api/status":
            self._json_response(router.status_json())
            return
        if path == "/webhook":
            self._handle_webhook_verify(parsed.query)
            return
        if path == "/ig-webhook":
            self._handle_ig_webhook_verify(parsed.query)
            return
        if path == "/privacy":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Lola - Privacy Policy</title>
<style>body{font-family:system-ui,sans-serif;max-width:700px;margin:40px auto;padding:0 20px;color:#333;line-height:1.6}h1{font-size:1.5em}h2{font-size:1.1em;margin-top:2em}p{margin:0.8em 0}</style></head><body>
<h1>Lola - Privacy Policy</h1>
<p><strong>Last updated:</strong> February 2026</p>
<h2>1. What we collect</h2>
<p>When you interact with Lola through WhatsApp or Instagram, we process the messages you send (text, audio, images, location) solely to generate a response. We also store your phone number or user ID to maintain conversation context during your session.</p>
<h2>2. How we use your data</h2>
<p>Your messages are sent to an AI language model to generate replies. We do not use your data for advertising, profiling, or any purpose other than providing the chat service.</p>
<h2>3. Data retention</h2>
<p>Conversation history is kept in memory for 30 minutes of inactivity and then automatically deleted. Business subscriber data (name, email, plan) is stored encrypted and retained while the subscription is active.</p>
<h2>4. Third parties</h2>
<p>We use the following third-party services to operate: Meta Platforms (WhatsApp & Instagram APIs), Google (Gemini AI API), and MercadoPago (payment processing). Each has its own privacy policy.</p>
<h2>5. Your rights</h2>
<p>You can request deletion of your data at any time by contacting us at <a href="mailto:hola@lola.uy">hola@lola.uy</a>.</p>
<h2>6. Contact</h2>
<p>For privacy-related questions: <a href="mailto:hola@lola.uy">hola@lola.uy</a></p>
</body></html>""")
            return
        if path == "/api/mp/plans":
            self._handle_mp_get_plans()
            return
        if path == "/api/mp/subscribers":
            self._handle_mp_get_subscribers()
            return
        if path == "/api/admin/wa-numbers":
            self._handle_admin_wa_numbers_get()
            return
        if path == "/api/admin/wacli-status":
            self._handle_admin_wacli_status()
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
        elif path == "/api/admin/wa-numbers":
            self._handle_admin_wa_numbers_post()
        elif path == "/api/admin/wacli-auth":
            self._handle_admin_wacli_auth()
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
        """POST /webhook - Recibir mensajes de WhatsApp (multi-tenant)."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json_response({"status": "ok"})
            return

        # Responder 200 inmediatamente para no timeout con Meta
        self._json_response({"status": "ok"})

        # Extraer mensajes de la estructura de Meta
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # Identificar a qué tenant va este mensaje
                phone_number_id = value.get("metadata", {}).get("phone_number_id", "")

                # Buscar config del tenant en wa_numbers
                wa_number = _db_wa_number_load(phone_number_id) if phone_number_id else None
                if wa_number:
                    tenant = _db_tenant_load_by_hash(wa_number["tenant_phone_hash"])
                    wa_ctx = {
                        "phone_number_id": phone_number_id,
                        "access_token": wa_number["access_token"],
                        "system_prompt": tenant["system_prompt"] if tenant and tenant.get("system_prompt") else LOLA_SALES_PROMPT,
                        "tenant_phone": tenant["phone"] if tenant else "",
                        "is_lola_sales": False,
                    }
                elif WA_CONFIG and phone_number_id == WA_CONFIG.get("phone_number_id", ""):
                    # Es el número de ventas de Lola
                    wa_ctx = {
                        "phone_number_id": WA_CONFIG["phone_number_id"],
                        "access_token": WA_CONFIG["access_token"],
                        "system_prompt": LOLA_SALES_PROMPT,
                        "tenant_phone": "",
                        "is_lola_sales": True,
                    }
                else:
                    if phone_number_id:
                        print(f"[WhatsApp] phone_number_id desconocido: {phone_number_id}")
                    continue

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
                        _wa_queue_message(from_number, msg_id, {"type": "text", "text": full_text}, wa_ctx)
                    elif msg_type in ("audio", "image"):
                        media_info = msg.get(msg_type, {})
                        media_id = media_info.get("id", "")
                        if not media_id:
                            continue
                        caption = media_info.get("caption", "")
                        print(f"[WhatsApp] {msg_type.capitalize()} de {from_number} (media_id: {media_id})")
                        _wa_queue_message(from_number, msg_id, {
                            "type": msg_type, "media_id": media_id, "caption": caption,
                        }, wa_ctx)
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
                        }, wa_ctx)

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
            self._json_response({"error": "no_plan"}, 403)
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
        if not _require_admin(self):
            return
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

    def _get_client_ip(self):
        """Obtiene la IP real del cliente (respeta X-Forwarded-For de Cloudflare)."""
        return (self.headers.get("CF-Connecting-IP")
                or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or self.client_address[0])

    def _check_ip_rate(self):
        """Verifica rate limit por IP. Retorna True si OK, False si excedido (manda 429)."""
        ip = self._get_client_ip()
        now = time.time()
        cutoff = now - _IP_RATE_WINDOW
        hits = _ip_rate.get(ip, [])
        hits = [t for t in hits if t > cutoff]
        if len(hits) >= _IP_RATE_MAX:
            _ip_rate[ip] = hits
            self._json_response({"error": "Demasiadas solicitudes. Esperá un rato."}, 429)
            return False
        hits.append(now)
        _ip_rate[ip] = hits
        # Limpiar IPs viejas cada ~100 requests
        if len(_ip_rate) > 200:
            _ip_rate.clear()
        return True

    def _handle_lola_chat(self):
        """Chat web de Lola — modo demo (ventas) o modo onboarding (autenticado)."""
        if not self._check_ip_rate():
            return
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
        if not _require_admin(self):
            return
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
        if not _require_admin(self):
            return
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
        if not _require_admin(self):
            return
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

            # Si no vino phone de MP, buscar en subscribers existentes
            if not phone and email:
                existing = _db_subscribers_load()
                ex = existing.get(email, {})
                phone = ex.get("phone", "")

            _db_subscriber_upsert(email, {
                "plan": plan_name,
                "status": status,
                "mp_id": data_id,
                "phone": phone,
                "updated": time.strftime("%Y-%m-%d %H:%M"),
            })
            print(f"[MercadoPago] Suscriptor actualizado: {email} → {plan_name}/{status}")

            # Si es suscripción authorized y hay un wacli_store expired, reactivar
            if status == "authorized" and phone:
                wa_phone = _normalize_phone(phone)
                phone_hash = _hash_key(wa_phone)
                store = _db_wacli_store_get(phone_hash)
                if store and store["state"] == "expired":
                    # Reactivate: clear trial, set syncing, restart sync
                    _db_wacli_store_update_state(phone_hash, "syncing")
                    _db_wacli_store_set_trial(phone_hash, "")
                    _trial_expiry_notified.discard(phone_hash)
                    print(f"[MercadoPago] Reactivando tenant {phone_hash[:8]} (pago recibido)")
                    _send_whatsapp(wa_phone, "tu pago fue confirmado! Lola ya esta de vuelta atendiendo a tus clientes.")
                    # Restart sync if store_dir exists
                    if store.get("store_dir") and os.path.isfile(os.path.join(store["store_dir"], "wacli.db")):
                        _wacli_tenant_start_sync(phone_hash, store["store_dir"])
                elif not store:
                    # No wacli store exists yet — this is a direct payment without trial
                    print(f"[MercadoPago] Pago recibido para {wa_phone} pero no hay wacli store, iniciando auth")
                    t = threading.Thread(
                        target=_wacli_tenant_auth,
                        args=(phone_hash, wa_phone),
                        daemon=True,
                    )
                    t.start()
                else:
                    print(f"[MercadoPago] Pago recibido para {wa_phone}, store state={store['state']}")

        except Exception as e:
            print(f"[MercadoPago] Error procesando webhook: {e}")

    def _handle_admin_wa_numbers_get(self):
        """GET /api/admin/wa-numbers — Lista números de WhatsApp registrados."""
        if not _require_admin(self):
            return
        numbers = _db_wa_numbers_list()
        self._json_response({"wa_numbers": numbers})

    def _handle_admin_wa_numbers_post(self):
        """POST /api/admin/wa-numbers — Registra un número de WhatsApp para un tenant."""
        if not _require_admin(self):
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json_response({"error": "JSON inválido"}, 400)
            return

        phone_number_id = body.get("phone_number_id", "").strip()
        tenant_phone = body.get("tenant_phone", "").strip()
        access_token = body.get("access_token", "").strip()
        business_account_id = body.get("business_account_id", "").strip()
        label = body.get("label", "").strip()

        if not phone_number_id or not access_token:
            self._json_response({"error": "Faltan phone_number_id y/o access_token"}, 400)
            return

        tenant_phone_hash = ""
        if tenant_phone:
            tenant_phone = _normalize_phone(tenant_phone)
            tenant_phone_hash = _hash_key(tenant_phone)

        _db_wa_number_save(phone_number_id, {
            "tenant_phone_hash": tenant_phone_hash,
            "access_token": access_token,
            "business_account_id": business_account_id,
            "label": label,
            "status": "active",
        })

        self._json_response({
            "ok": True,
            "phone_number_id": phone_number_id,
            "label": label,
            "tenant_phone": tenant_phone,
        })

    def _handle_admin_wacli_status(self):
        """GET /api/admin/wacli-status — Estado de todos los procesos wacli de tenants."""
        if not _require_admin(self):
            return
        with _tenant_wacli_lock:
            tenants = {}
            for ph, info in _tenant_wacli.items():
                proc_alive = info.get("proc") and info["proc"].poll() is None
                tenants[ph[:8]] = {
                    "state": info["state"],
                    "store_dir": info["store_dir"],
                    "proc_alive": proc_alive,
                    "pid": info["proc"].pid if info.get("proc") else None,
                }
        db_stores = _db_wacli_stores_list()
        self._json_response({
            "active": tenants,
            "db_stores": db_stores,
            "max_tenants": _MAX_TENANT_WACLI,
        })

    def _handle_admin_wacli_auth(self):
        """POST /api/admin/wacli-auth — Trigger manual de QR auth para un tenant."""
        if not _require_admin(self):
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self._json_response({"error": "JSON inválido"}, 400)
            return

        phone = body.get("phone", "").strip()
        if not phone:
            self._json_response({"error": "Falta phone"}, 400)
            return

        phone = _normalize_phone(phone)
        phone_hash = _hash_key(phone)

        # Verificar si ya está en proceso
        with _tenant_wacli_lock:
            existing = _tenant_wacli.get(phone_hash)
            if existing and existing["state"] == "authenticating":
                self._json_response({"error": "Ya hay un auth en proceso para este número"}, 409)
                return

        t = threading.Thread(target=_wacli_tenant_auth, args=(phone_hash, phone), daemon=True)
        t.start()
        self._json_response({"ok": True, "phone": phone, "phone_hash": phone_hash[:8], "message": "Auth iniciado, QR será enviado por WhatsApp"})

    def _handle_mp_get_subscribers(self):
        """GET /api/mp/subscribers — Lista suscriptores (admin)."""
        if not _require_admin(self):
            return
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
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
    wa_num_count = _db_wa_numbers_count()
    print(f"   WhatsApp: {'habilitado' if WA_CONFIG else 'deshabilitado'} ({wa_num_count} números de tenants)")
    print(f"   MercadoPago: {'habilitado' if MP_CONFIG else 'deshabilitado'}")
    print(f"   Instagram: {'habilitado' if IG_CONFIG else 'deshabilitado'}")
    print(f"   wacli: {'habilitado' if _wacli_enabled else 'deshabilitado'}")
    if _wacli_enabled:
        wacli_thread = threading.Thread(target=_wacli_poll_loop, daemon=True)
        wacli_thread.start()
    # Restaurar wacli tenants de la DB
    wacli_stores = _db_wacli_stores_list(state="syncing")
    print(f"   wacli-tenants: {len(wacli_stores)} activos (max {_MAX_TENANT_WACLI})")
    if wacli_stores:
        restore_thread = threading.Thread(target=_wacli_tenant_boot_restore, daemon=True)
        restore_thread.start()
    # Start trial expiry checker thread
    trial_checker = threading.Thread(target=_trial_expiry_checker, daemon=True)
    trial_checker.start()
    print(f"   trial-checker: activo (cada 1h)")
    print(f"   Ctrl+C para frenar")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 RenzoGPT apagado.")
        server.server_close()


if __name__ == "__main__":
    main()
