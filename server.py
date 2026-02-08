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
import subprocess
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Importar el router
sys.path.insert(0, os.path.expanduser("~"))
from importlib import import_module
gemini_router = import_module("gemini-router")
GeminiRouter = gemini_router.GeminiRouter

router = GeminiRouter()

# WhatsApp Business API config
_wa_config_path = os.path.expanduser("~/.whatsapp-config.json")
try:
    with open(_wa_config_path) as f:
        WA_CONFIG = json.load(f)
    print(f"[WhatsApp] Config cargada desde {_wa_config_path}")
except FileNotFoundError:
    WA_CONFIG = None
    print(f"[WhatsApp] No se encontró {_wa_config_path}, webhook desactivado")

STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

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
            cmd, shell=True, capture_output=True, text=True, timeout=30,
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
    "- Cierra ventas y manda link de pago (MercadoPago, transferencia, etc).\n"
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
    "- **Básico** ($29 USD/mes): Catálogo manual, chat inteligente, respuestas 24/7, hasta 500 conversaciones/mes.\n"
    "- **Pro** ($79 USD/mes): Todo lo del Básico + integración con tu sistema (Shopify, WooCommerce, Google Sheets), "
    "stock en tiempo real, creación de pedidos, links de pago automáticos, conversaciones ilimitadas.\n"
    "- Setup: sin costo de instalación. Te lo configuramos nosotros.\n"
    "- Prueba gratis: 7 días para que la pruebes con tu negocio.\n"
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
    "REGLAS:\n"
    "- La meta es que el interesado quiera probar Lola. Ofrecé la prueba gratis de 7 días.\n"
    "- Si preguntan algo técnico que no sabés, decí que el equipo técnico se los explica en el onboarding.\n"
    "- Nunca inventes features que no existen.\n"
    "- Si piden contacto, deciles que pueden escribir a hola@lola.uy o agendar una demo.\n"
    "- Sos Lola. La mejor prueba de que funciona sos vos misma hablando con ellos ahora.\n"
)

WA_SYSTEM_PROMPT = LOLA_SALES_PROMPT


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
    """Muestra 'escribiendo...' y opcionalmente marca el mensaje como leído."""
    if not WA_CONFIG:
        return
    url = f"https://graph.facebook.com/v23.0/{WA_CONFIG['phone_number_id']}/messages"
    data = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "typing_indicator": {"type": "text"}}
    if msg_id:
        data["status"] = "read"
        data["message_id"] = msg_id
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {WA_CONFIG['access_token']}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        print(f"[WhatsApp] Error typing indicator: {e}")


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
            _wa_append(from_number, "model", reply)
            model = result.get("model", "?")
            key = result.get("key", "?")
            rpd = router.rpd_counts.get(key - 1, {}).get(model, "?") if isinstance(key, int) else "?"
            print(f"[WhatsApp] Respondido con K{key}/{model} (RPD usado: {rpd}): {reply[:120]}")
            # Dividir en varios mensajes para parecer natural
            chunks = [c.strip() for c in reply.split("\n") if c.strip()]
            if len(chunks) <= 1:
                _send_whatsapp(from_number, reply)
            else:
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
        # lola.expensetracker.com.uy → sirve landing como index
        host = self.headers.get("Host", "")
        if path in ("/", "") and "lola." in host:
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
        elif path == "/webhook":
            self._handle_webhook_incoming()
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
                        t = threading.Thread(
                            target=_handle_wa_message,
                            args=(from_number, full_text, msg_id),
                            daemon=True,
                        )
                        t.start()
                    elif msg_type in ("audio", "image"):
                        media_info = msg.get(msg_type, {})
                        media_id = media_info.get("id", "")
                        if not media_id:
                            continue
                        caption = media_info.get("caption", "")
                        print(f"[WhatsApp] {msg_type.capitalize()} de {from_number} (media_id: {media_id})")
                        t = threading.Thread(
                            target=_handle_wa_media,
                            args=(from_number, media_id, msg_id, msg_type, caption),
                            daemon=True,
                        )
                        t.start()
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
                        t = threading.Thread(
                            target=_handle_wa_message,
                            args=(from_number, f"(el usuario compartió su ubicación: {loc_text})", msg_id),
                            daemon=True,
                        )
                        t.start()

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
        """Chat web de Lola vendiendo Lola."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            text = body.get("message", "").strip()
            session_id = body.get("session_id", "default")
            if not text:
                self._json_response({"error": "No message"}, 400)
                return

            # Obtener/crear historial de sesión
            now = time.time()
            entry = _lola_web_history.get(session_id)
            if entry and (now - entry["ts"]) > _LOLA_WEB_HISTORY_TTL:
                del _lola_web_history[session_id]
                entry = None

            if not entry:
                _lola_web_history[session_id] = {"messages": [], "ts": now}
                entry = _lola_web_history[session_id]

            entry["ts"] = now
            history = entry["messages"]

            # Construir mensajes para ask_chat
            messages = history + [{"role": "user", "text": text}]

            result = router.ask_chat(messages, system=LOLA_SALES_PROMPT, timeout=30)

            if result["ok"]:
                reply = result["text"]
                # Guardar en historial
                entry["messages"].append({"role": "user", "text": text})
                entry["messages"].append({"role": "model", "text": reply})
                # Recortar historial
                while len(entry["messages"]) > _LOLA_WEB_HISTORY_MAX:
                    entry["messages"].pop(0)
                self._json_response({
                    "text": reply,
                    "model": result.get("model", ""),
                    "key": result.get("key", ""),
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
    server = HTTPServer(("0.0.0.0", port), RenzoHandler)
    print(f"🚀 RenzoGPT corriendo en http://0.0.0.0:{port}")
    print(f"   Router: {len(router.keys)} keys × {len(router.models)} modelos")
    print(f"   WhatsApp: {'habilitado' if WA_CONFIG else 'deshabilitado'}")
    print(f"   Ctrl+C para frenar")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 RenzoGPT apagado.")
        server.server_close()


if __name__ == "__main__":
    main()
