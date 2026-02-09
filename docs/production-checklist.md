# Lola — Checklist de Producción

## Estado actual

| Servicio | Estado | Qué falta |
|---|---|---|
| WhatsApp Business API | App en modo Development, token permanente listo | App Review + Business Verification |
| MercadoPago | Verificar si es test o producción | Credenciales de producción si es test |
| Gemini API | 4 keys free tier (~240 RPD) | Opcional: 1 key paga como fallback |
| Dominio | lola.expensetracker.com.uy | Dominio propio (lola.uy o similar) |
| Número de WhatsApp | Usando número personal | Comprar chip dedicado para Lola |
| Facebook Business Manager | Cuenta de desarrollador | Verificar negocio |

---

## 1. Facebook App Review (BLOQUEANTE)

Sin esto, solo podés mandar mensajes a números registrados como testers en tu app. Con el App Review aprobado, podés mandar a cualquier número.

### Qué necesitás

1. **Política de privacidad** publicada en una URL pública (ej: `https://lola.expensetracker.com.uy/privacidad`). Tiene que cubrir:
   - Qué datos recopilás (número de teléfono, mensajes, datos del negocio)
   - Para qué los usás (brindar el servicio de chatbot)
   - Cómo los almacenás (encriptados con AES-128)
   - Cómo pueden pedir que se borren sus datos
   - Contacto (email)

2. **Permisos a solicitar**:
   - `whatsapp_business_messaging` — para enviar y recibir mensajes
   - `whatsapp_business_management` — para gestionar números de teléfono

3. **Para cada permiso**, Meta pide:
   - Descripción de cómo lo usás (1-2 párrafos)
   - Video o screenshots mostrando el uso en tu app
   - El video puede ser: abrir WhatsApp → mandarle un mensaje a Lola → mostrar que responde automáticamente

### Cómo hacerlo

1. Ir a [developers.facebook.com](https://developers.facebook.com) → Tu App → App Review → Permissions and Features
2. Solicitar `whatsapp_business_messaging` y `whatsapp_business_management`
3. Completar el formulario con la descripción y el video
4. Subir la URL de la política de privacidad en Settings → Basic → Privacy Policy URL
5. Enviar para revisión

### Tiempos

- Meta tarda entre 1-5 días hábiles
- Si rechazan, te dicen por qué y podés resubmitir
- El rechazo más común es: política de privacidad faltante o incompleta

---

## 2. Business Verification (BLOQUEANTE)

Sin esto tenés un límite de 250 mensajes por día. Con la verificación se levanta.

### Qué necesitás

- Documentos del negocio (RUT, certificado de DGI, o extracto bancario)
- Que el nombre del negocio en Business Manager coincida con los documentos
- Número de teléfono y dirección verificables

### Cómo hacerlo

1. Ir a [business.facebook.com](https://business.facebook.com) → Settings → Business Info → Start Verification
2. Subir los documentos
3. Meta puede llamar al teléfono del negocio o mandar un código por correo

### Tiempos

- 1-5 días hábiles
- Se puede hacer en paralelo con el App Review

---

## 3. MercadoPago Producción

### Verificar credenciales actuales

Las credenciales de test tienen el prefijo `TEST-` en el access_token. Si tu token empieza con `APP_USR-`, ya es producción.

Verificar en `~/.mercadopago-config.json`:
```bash
cat ~/.mercadopago-config.json | grep access_token
```

### Si es test, migrar a producción

1. Ir a [mercadopago.com.uy/developers](https://www.mercadopago.com.uy/developers) → Tus aplicaciones → Tu app
2. En Credenciales → Producción, copiar el Access Token de producción
3. Actualizar `~/.mercadopago-config.json` con el token nuevo
4. Recrear los planes de suscripción (los de test no sirven en prod):
   ```bash
   TOKEN=$(cat ~/.lola-admin-token)
   curl -X POST -H "Authorization: Bearer $TOKEN" https://renzogpt.expensetracker.com.uy/api/mp/setup-plans
   ```
5. Verificar que los nuevos `init_point` funcionen

---

## 4. Número de WhatsApp para Lola

Lola necesita su propio número dedicado para atender clientes de ventas. Opciones:

### Opción A: Chip prepago (recomendado para Uruguay)
- Comprar un chip Antel/Movistar/Claro
- Activarlo en un celular, verificar que reciba SMS
- Registrarlo en WhatsApp Business API via Facebook Business Manager
- Costo: ~$100 UYU el chip + recarga mínima

### Opción B: Número virtual
- Proveedores como Twilio, MessageBird ofrecen números virtuales
- Más caro pero no necesitás chip físico
- Útil si querés un número de otro país

### Registrar el número en la API
1. Facebook Business Manager → WhatsApp Manager → Phone Numbers → Add Phone Number
2. Verificar con código SMS o llamada
3. Anotar el `phone_number_id` que genera
4. Actualizar `~/.whatsapp-config.json` con el nuevo `phone_number_id`

---

## 5. Dominio propio (NO BLOQUEANTE)

Para quedar más profesional. Opciones:
- `lola.uy` — verificar disponibilidad en nic.com.uy
- `lola.com.uy` — idem
- `holalola.uy` — alternativa

### Setup
1. Comprar el dominio
2. En Cloudflare: agregar el dominio, apuntar un CNAME al tunnel existente
3. Actualizar URLs en el código (back_urls de MP, links en el system prompt, etc.)

---

## 6. Gemini Key Paga (NO BLOQUEANTE)

Hoy con 4 keys gratis tenés ~240 RPD. Para pocos clientes sobra. Cuando escales:

1. Ir a [aistudio.google.com](https://aistudio.google.com) → Get API Key
2. Activar billing en Google Cloud Console
3. Crear una key con billing habilitado
4. Agregarla como la última key en `~/gemini-router.py` (el router la usa como fallback cuando las gratis se agotan)

Costo: ~$0.10 por 1M tokens de input con Flash. Muy barato.

---

## Orden recomendado

1. **Verificar MP** (5 min) — ver si el token es test o prod
2. **Comprar chip** (1 hora) — ir a Antel, comprar, activar
3. **Política de privacidad** (1 hora) — redactar y publicar en el sitio
4. **App Review + Business Verification** (enviar y esperar 1-5 días) — se hacen en paralelo
5. **Dominio propio** (cuando quieras) — cosmético, no bloquea nada
6. **Key paga de Gemini** (cuando escales) — no urgente
