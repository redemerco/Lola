# Infraestructura RenzoGPT / Lola

> Auditado: 2026-02-09 | Servidor: Raspberry Pi (Linux 6.12, ARM64)

---

## Arquitectura General

```
Internet
   │
Cloudflare CDN/DNS (SSL termination)
   │
cloudflared tunnel (QUIC, auto-reconnect)
   │
   ├── renzogpt.expensetracker.com.uy  → 127.0.0.1:8080  (Lola / RenzoGPT)
   ├── lola.expensetracker.com.uy      → 127.0.0.1:8080  (alias)
   ├── expensetracker.com.uy           → 127.0.0.1:3000  (Expense Tracker)
   └── renzodemarco.com.uy             → 127.0.0.1:8890  (Portfolio)

   Catch-all → 404

Nginx (puerto 80, LAN) → OpenMediaVault admin panel (no involucrado en Lola)
```

---

## 1. Reverse Proxy

**No hay reverse proxy tradicional delante de server.py.**

- **Nginx**: Instalado y corriendo, pero solo sirve el panel de OpenMediaVault en puerto 80. Config en `/etc/nginx/sites-enabled/openmediavault-webgui`. No tiene ningún proxy_pass hacia la app.
- **Caddy**: No instalado.
- **Apache**: Parcialmente instalado (dependencia), no corriendo.
- **Cloudflare Tunnel**: Es el único punto de entrada público. Servicio systemd `cloudflared.service`, corriendo desde 2026-02-08.

**Config del tunnel** (`/etc/cloudflared/config.yml`):
```yaml
tunnel: 7055cdca-07ef-4d07-b772-a243c538110c
credentials-file: /home/pi/.cloudflared/7055cdca-...json

ingress:
  - hostname: expensetracker.com.uy
    service: http://127.0.0.1:3000
  - hostname: www.expensetracker.com.uy
    service: http://127.0.0.1:3000
  - hostname: renzodemarco.com.uy
    service: http://127.0.0.1:8890
  - hostname: www.renzodemarco.com.uy
    service: http://127.0.0.1:8890
  - hostname: renzogpt.expensetracker.com.uy
    service: http://127.0.0.1:8080
  - hostname: lola.expensetracker.com.uy
    service: http://127.0.0.1:8080
  - service: http_status:404
```

---

## 2. Firewall

**No hay firewall host-level configurado.**

- **UFW**: No instalado.
- **iptables**: Policy ACCEPT en INPUT. Solo hay reglas de Docker (FORWARD/NAT) y Tailscale (`ts-input`, `ts-forward`).
- **Consecuencia**: Todos los puertos abiertos en la Pi son accesibles desde la LAN sin restricción. La seguridad depende del router (no forwardea puertos) y del Cloudflare Tunnel (solo expone hostnames configurados).

---

## 3. PM2

**No existe ecosystem.config.js.** Todos los procesos fueron iniciados manualmente y guardados con `pm2 save`.

### Procesos activos

| ID | Nombre | Intérprete | Puerto | Status | Restarts | Uptime |
|----|--------|-----------|--------|--------|----------|--------|
| 0 | expense-tracker-production | node | 3000 | online | 1 | 3 días |
| 1 | micheque-bot | node | — | online | 0 | 2 meses |
| 7 | renzo-portfolio | python3 | 8890 | online | 0 | 2 meses |
| **8** | **renzogpt** | **python3** | **8080** | **online** | **113** | **6 horas** |

### Config de renzogpt (PM2)

- **Script**: `/srv/.../renzogpt/server.py`
- **Interpreter**: `python3`
- **Exec mode**: `fork_mode`
- **Working directory**: `/srv/.../Remoto/Proyectos` (⚠️ padre del proyecto, no el directorio del proyecto)
- **Autorestart**: `true`
- **Watch**: `false`
- **Max memory restart**: no configurado
- **Log date format**: no configurado
- **Error log**: `~/.pm2/logs/renzogpt-error.log`
- **Output log**: `~/.pm2/logs/renzogpt-out.log`

### Auto-start

- Servicio systemd: `pm2-pi.service` (enabled, activo desde 2025-12-03)
- En boot: `pm2 resurrect` restaura todos los procesos del dump

### Alertas PM2

- ⚠️ **113 restarts** — el proceso crashea frecuentemente
- ⚠️ **exec cwd incorrecto** — debería ser `.../renzogpt`, no `.../Proyectos`
- ⚠️ **Sin max_memory_restart** — riesgo de OOM en Raspberry Pi
- ⚠️ **Sin log_date_format** — logs sin timestamps

---

## 4. DNS / Cloudflare

- **Método**: Cloudflare Tunnel (conexión saliente QUIC, no necesita IP pública ni puertos abiertos)
- **Edge**: GRU (São Paulo, Brasil)
- **SSL**: Terminado en Cloudflare (certificado Cloudflare → usuario). Tunnel encripta el tráfico Pi ↔ Cloudflare.
- **No hay DDNS** — no se necesita con el tunnel

---

## 5. Autenticación de Endpoints Admin

**Protección únicamente por código custom** en `server.py`:

```python
def _require_admin(handler):
    # Verifica Authorization: Bearer <token>
    # Token desde ~/.lola-admin-token o LOLA_ADMIN_TOKEN env var
    # Retorna 401 si inválido
```

**Endpoints protegidos:**
- `GET /api/mp/subscribers`
- `POST /api/mp/cancel`
- `POST /api/mp/setup-plans`
- `POST /api/execute`
- `GET/POST /api/admin/wa-numbers`

**No hay protección a nivel:**
- ❌ Nginx (no es reverse proxy de la app)
- ❌ Cloudflare (no hay reglas WAF/Access configuradas desde el servidor)
- ❌ IP whitelist

---

## 6. Modificaciones Locales (sin commitear)

**Branch**: `main` (up to date con `origin/main`)

### server.py — cambios principales

1. **Reescritura de `LOLA_SALES_PROMPT`**:
   - Lola ya no se identifica como IA ("Trabajas atendiendo clientes" en vez de "Sos una IA")
   - Reglas nuevas: nunca decir que es IA, nunca inventar URLs, siempre verificar pagos con `{{estado_pago}}`
   - Tags consolidados en una sección única
   - Sección "QUE ES LOLA" → "QUE HAGO" en primera persona

2. **Nueva función `_wa_react(to, msg_id, emoji, wa_ctx=None)`**:
   - Envía reaction emojis en WhatsApp
   - Constante `_WA_REACTIONS = ["👍", "👌", "🙌", "😊"]`

3. **Nueva función `_time_period()`**:
   - Retorna período del día en Uruguay (UTC-3): mañana/tarde/noche/madrugada
   - Se inyecta en el system prompt para saludos contextuales

4. **`_process_lola_tags()` — pausas simuladas**:
   - `{{estado_pago}}` y `{{estado_suscripcion}}` ahora prefijan con `{{PAUSA:5}}`
   - Simula que Lola "está chequeando" antes de responder

5. **`_handle_wa_message()` — overhaul**:
   - Delay random 1-3s antes del typing indicator (simula persona)
   - Procesamiento de `{{react:emoji}}` tags
   - Split de respuesta por `{{PAUSA:N}}` markers con delays
   - Delay dinámico entre chunks: `min(0.5 + len(chunk) * 0.02, 3.0)`
   - Guard contra respuestas vacías post-tags

6. **OTP endpoint**:
   - Antes: HTTP 404 con `{"error": "No encontramos un pago..."}`
   - Ahora: HTTP 403 con `{"error": "no_plan"}`

### index.html — cambio menor

- Cuando el server devuelve `"no_plan"`, muestra botón verde "Ver planes" linkando a `/lola-landing.html`

---

## 7. SSL / HTTPS

**100% manejado por Cloudflare:**
- Usuario ↔ Cloudflare: HTTPS con certificado Cloudflare
- Cloudflare ↔ Pi: Tunnel QUIC (encriptado)
- No hay certbot, no hay certificados locales, no hay SSL en la Pi
- server.py escucha HTTP plano en puerto 8080

---

## 8. Archivos de Configuración

### `~/.whatsapp-config.json`
```json
{
  "phone_number_id": "968052939731213",
  "business_account_id": "615390944902194",
  "access_token": "xxx",
  "app_secret": "xxx",
  "verify_token": "xxx"
}
```
⚠️ El `access_token` es temporal (expira cada 24hs). Renovar en developers.facebook.com/tools/explorer/

### `~/.mercadopago-config.json`
```json
{
  "access_token": "xxx",
  "public_key": "xxx",
  "webhook_secret": "xxx",
  "plans": {
    "basico": { "id": "d6a5511b...", "init_point": "https://mercadopago.com.uy/..." },
    "pro": { "id": "e00fe13c...", "init_point": "https://mercadopago.com.uy/..." }
  }
}
```

### `~/.instagram-config.json`
```json
{
  "ig_user_id": "xxx",
  "access_token": "xxx",
  "verify_token": "xxx"
}
```
⚠️ El `access_token` se genera desde Meta App > API de Instagram > Generar token. El `ig_user_id` aparece al lado del nombre de la cuenta.

### `~/.lola-master.key`
- Existe, 44 bytes (Fernet key), permisos `600`
- Respaldada en `~/.lola-backups/lola-master.key`

### `~/.lola-admin-token`
- Existe, 48 bytes, permisos `600`

### `~/.gemini-keys`
- **6 keys** (una por línea)
- Con 3 modelos × 6 keys × 20 RPD = **~360 RPD** en free tier

### Variables de entorno
- No hay `LOLA_*` ni `GEMINI*` en el entorno
- Todo se lee de archivos al startup

---

## 9. Gemini Router (`~/gemini-router.py`)

Router inteligente de API keys para Google Gemini. Funciona como módulo importable y como CLI.

### Funciones públicas

| Función | Descripción |
|---------|-------------|
| `ask(prompt, image_path=None)` | Single-turn con imagen opcional |
| `ask_chat(messages, system=None)` | Multi-turn (la que usa Lola) |
| `ask_multimodal(prompt, extra_parts=None, tools=None)` | Con audio/imagen inline |
| `status()` | Dashboard de estado (texto) |
| `status_json()` | Estado en dict (para web) |

### Modelos y pools

| Modelo | Pool | RPD/key | RPM/key |
|--------|------|---------|---------|
| gemini-2.5-flash | flash | 20 | 5 |
| gemini-3-flash-preview | flash (compartido) | 20 | 5 |
| gemini-2.5-flash-lite | lite (independiente) | 20 | 10 |

### Lógica de fallback

1. `_find_best_combo()` evalúa todos los combos key×model
2. Ordena por: menos requests recientes → más RPD restante
3. Si todo está en cooldown, espera el tiempo mínimo
4. Parsea el retry time exacto de los 429 de Google
5. RPM learning adaptativo: baja el soft limit cuando recibe 429

### Archivos

| Archivo | Descripción |
|---------|-------------|
| `~/.gemini-keys` | API keys (una por línea) |
| `~/.gemini-router-state.json` | Estado persistido (RPD counters, stats) |

---

## 10. Red

### Interfaces

| Interfaz | IP | Propósito |
|----------|-----|---------|
| eth0 | 192.168.1.6/24 (DHCP) | LAN principal |
| tailscale0 | 100.121.47.10/32 | VPN remota |
| docker0 | 172.17.0.1/16 | Docker (sin containers) |
| br-7d55fd3f4cea | 172.18.0.1/16 | Docker (Wazuh stack) |
| br-40c01a5dfe9d | 172.19.0.1/16 | Docker (n8n) |

- **IP dinámica** — DHCP, netplan config en `/etc/netplan/20-openmediavault-eth0.yaml`
- ⚠️ Recomendado: configurar reserva DHCP en el router

### Puertos TCP abiertos (LAN)

| Puerto | Proceso | Propósito |
|--------|---------|---------|
| 22 | sshd | SSH |
| 80 | nginx | OpenMediaVault admin |
| 111 | rpcbind | RPC/NFS |
| 139, 445 | smbd | Samba/CIFS |
| 443 | docker-proxy | Kibana (Wazuh) |
| 1514, 1515 | docker-proxy | Wazuh agent/registro |
| 3000 | node | Expense Tracker |
| 3001, 3002 | python3 | Otros servicios |
| 5432 | postgres | PostgreSQL (localhost only) |
| 5678 | docker-proxy | n8n |
| **8080** | **python3** | **RenzoGPT / Lola** |
| 8890 | python3 | Portfolio |
| 9200 | docker-proxy | Elasticsearch |
| 55000 | docker-proxy | Wazuh API |

### Puertos expuestos a Internet

Solo via Cloudflare Tunnel:
- `renzogpt.expensetracker.com.uy` → :8080
- `lola.expensetracker.com.uy` → :8080
- `expensetracker.com.uy` → :3000
- `renzodemarco.com.uy` → :8890

### Restricciones de acceso

- ❌ No hay firewall host-level
- ❌ No hay IP whitelist
- ❌ No hay geo-restricción a nivel servidor
- Posible: reglas WAF en Cloudflare dashboard (no verificable desde acá)

---

## 11. Backups

| Qué | Dónde | Frecuencia | Retención |
|-----|-------|-----------|-----------|
| SQLite DB | `~/.lola-backups/lola-db-YYYYMMDD-HHMM.sqlite` | Cada 6 horas (cron) | 7 días |
| Master key | `~/.lola-backups/lola-master.key` | Cada 6 horas (cron) | Permanente |

Script: `~/.lola-backup.sh` — usa `sqlite3 .backup` (atómico, WAL-safe).

---

## 12. Docker Containers

| Container | Puertos | Propósito |
|-----------|---------|---------|
| Wazuh Manager | 514/udp, 1514, 1515, 55000 | SIEM |
| Elasticsearch | 9200 | Search engine (Wazuh) |
| Kibana | 443 | Dashboard (Wazuh) |
| n8n | 5678 | Workflow automation |

---

## 13. Servicios adicionales

- **Tailscale VPN**: Activo en 100.121.47.10 — acceso remoto a la Pi
- **OpenMediaVault**: NAS admin en puerto 80
- **Wazuh**: SIEM/IDS (Elasticsearch + Kibana + Manager)
- **n8n**: Workflow automation en puerto 5678

---

## Recomendaciones de Seguridad

1. **Configurar UFW** — permitir solo 22 (SSH), 80 (OMV admin si necesario), y los puertos de Docker. Bloquear todo lo demás en LAN.
2. **Cloudflare Access** — poner WAF rules o Zero Trust Access delante de los endpoints admin.
3. **Reserva DHCP** — fijar IP 192.168.1.6 en el router para evitar cambios de lease.
4. **PM2 ecosystem config** — crear archivo declarativo y versionado para reproducibilidad.
5. **max_memory_restart** — configurar límite en PM2 para renzogpt (ej: 200M).
6. **Investigar 113 restarts** — `pm2 logs renzogpt --err --lines 100`.
7. **Corregir exec cwd** — debería ser `.../renzogpt`, no `.../Proyectos`.
8. **Rate limiting** — considerar rate limit en Cloudflare para los endpoints públicos.
9. **Backup off-site** — los backups están en el mismo disco. Considerar copia a la nube.