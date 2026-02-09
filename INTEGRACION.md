# Lola - Guía de Integración para Comerciantes

---

## Parte 1: Lo que el cliente tiene que hacer ANTES de la visita

Mandarle esto al comerciante antes de ir al local. Sin estas cuentas no se puede hacer la integración.

### 1. Cuenta de Facebook

El comerciante necesita una cuenta de Facebook (personal o de empresa). Se usa para acceder a la plataforma de WhatsApp Business API.

- Si ya tiene Facebook: no necesita hacer nada
- Si no tiene: crear una en https://www.facebook.com/

### 2. WhatsApp Business en el celular

El número del negocio tiene que estar en **WhatsApp Business** (la app de tapa verde), no en WhatsApp normal.

- Descargar desde: https://www.whatsapp.com/business/
- Si ya usa WhatsApp normal con el número del negocio, la app lo migra automáticamente al instalar WhatsApp Business
- **IMPORTANTE**: el número del negocio va a quedar vinculado a la API. Si el comerciante usa ese número para hablar con clientes a mano, esos mensajes van a pasar por Lola

### 3. Cuenta de MercadoPago

El comerciante necesita una cuenta de MercadoPago **verificada** (con cédula y datos bancarios cargados). Es la cuenta donde le van a caer los pagos.

- Si ya tiene: no necesita hacer nada
- Si no tiene: crear una en https://www.mercadopago.com.uy/
- Verificar identidad en: https://www.mercadopago.com.uy/settings/account

### 4. Shopify (solo Plan Pro, si aplica)

Si el comerciante tiene tienda en Shopify, necesitamos acceso de administrador.

- El comerciante tiene que saber su URL de admin: `https://{su-tienda}.myshopify.com/admin`
- Necesita tener rol de **Owner** o **Admin** en la tienda (para poder crear apps custom)
- Si no tiene Shopify, se saltea este paso

### Checklist para mandarle al cliente

```
Antes de nuestra visita, necesitamos que tengas:

[ ] Cuenta de Facebook activa
[ ] WhatsApp Business instalado con el número del negocio
[ ] Cuenta de MercadoPago verificada (con cédula y datos bancarios)
[ ] (Si tenés Shopify) Acceso de administrador a tu tienda
```

---

## Parte 2: En el local (presencial)

Estas cosas necesitan que estemos con el comerciante porque requieren su celular, sus contraseñas, o verificación por SMS.

### 2.1 Verificar el número de WhatsApp en Meta

1. Entrar con la cuenta de Facebook del comerciante a:
   **https://developers.facebook.com/apps/**
2. Click **"Create App"** → tipo **"Business"**
3. Nombre: "Lola - [Nombre del Negocio]"
4. En el dashboard de la app → **"Add Products"** → buscar **"WhatsApp"** → **"Set Up"**
5. En **WhatsApp** → **API Setup**:
   https://developers.facebook.com/apps/`{APP_ID}`/whatsapp-business/wa-dev-console/
6. Click **"Add phone number"**
7. Ingresar el número del negocio → **el comerciante va a recibir un SMS o llamada en su celular** con un código de verificación
8. Ingresar el código
9. Anotar:
   - **Phone Number ID**
   - **WhatsApp Business Account ID**

### 2.2 Crear app de MercadoPago

Esto necesita que el comerciante inicie sesión con su cuenta de MP:

1. Entrar a: **https://www.mercadopago.com.uy/developers/panel/app**
2. Click **"Create application"**
3. Tipo: **"Online payments"** → **"Checkout Pro"**
4. Nombre: "Lola - [Nombre del Negocio]"
5. Ir a **"Production credentials"**
   (si dice "Test credentials", hay que activar las de producción primero)
6. Anotar el **Access Token** (empieza con `APP_USR-`)

### 2.3 Shopify: habilitar apps custom (solo Plan Pro)

Si el comerciante tiene Shopify, necesitamos que entre a su admin:

1. Entrar a: **https://`{tienda}`.myshopify.com/admin**
2. Ir a **Settings** → **Apps and sales channels**:
   https://`{tienda}`.myshopify.com/admin/settings/apps
3. Click **"Develop apps"** (arriba a la derecha)
4. Si es la primera vez: click **"Allow custom app development"** → confirmar
   (esto necesita que el comerciante sea Owner o Admin de la tienda)
5. Click **"Create an app"** → Nombre: "Lola" → **Create app**
6. Pestaña **"Configuration"** → **"Admin API integration"** → **"Configure"**
7. Marcar estos permisos:
   - `read_products`
   - `read_inventory`
   - `read_orders`
   - `write_orders`
   - `read_customers`
8. **"Save"**
9. Pestaña **"API credentials"** → **"Install app"** → confirmar
10. Copiar el **Admin API access token** (se muestra una sola vez)
11. Anotar también la **API key** y **API secret key** de la misma página

### 2.4 Onboarding

Con el comerciante presente, hacer el onboarding:

1. Entrar a **https://lola.expensetracker.com.uy/app**
2. Ingresar el número del negocio → recibe código OTP por WhatsApp → ingresarlo
3. Lola le hace preguntas sobre el negocio (nombre, rubro, productos, horarios, etc.)
4. El comerciante responde → Lola arma el bot automáticamente

### 2.5 Datos a llevarse del local

Al salir del local tenés que tener todos estos datos:

**WhatsApp (obligatorio):**
```
Phone Number ID:           _______________
Business Account ID:       _______________
App Secret:                _______________
```
El App Secret está en: https://developers.facebook.com/apps/`{APP_ID}`/settings/basic/ → "App Secret" → "Show"

**MercadoPago (obligatorio):**
```
Access Token (APP_USR-...):    _______________
```

**Shopify (solo Plan Pro):**
```
URL de la tienda:              _______________.myshopify.com
Admin API access token:        shpat_xxxxxxxxxxxxxxxxxxxxx
API key:                       _______________
API secret key:                _______________
```

---

## Parte 3: Remoto (desde nuestra oficina)

Todo esto se puede hacer sin el comerciante presente, con los datos que nos llevamos del local.

### 3.1 Token permanente de WhatsApp

El token temporal que genera el Graph API Explorer dura 24hs. Para producción hay que crear uno permanente:

1. Ir a https://business.facebook.com/settings/system-users
2. **"Add"** → crear un System User tipo **Admin**
3. **"Add Assets"** → asignarle la app del comerciante con **Full Control**
4. **"Generate New Token"** → seleccionar la app → marcar:
   - `whatsapp_business_messaging`
   - `whatsapp_business_management`
5. Copiar el token (se muestra una sola vez)

### 3.2 Configurar webhook de WhatsApp

1. En la app → **WhatsApp** → **Configuration**:
   https://developers.facebook.com/apps/`{APP_ID}`/whatsapp-business/wa-settings/
2. En **"Webhook"**:
   - **Callback URL**: `https://lola.expensetracker.com.uy/webhook`
   - **Verify token**: pedir el verify token al equipo de Lola
3. Click **"Verify and Save"**
4. Suscribirse al campo **`messages`** (click en "Subscribe")

### 3.3 Configurar webhook de MercadoPago

1. En https://www.mercadopago.com.uy/developers/panel/app → la app del comerciante
2. Sección **"Webhooks"** → **"Configure notifications"**
   O: https://www.mercadopago.com.uy/developers/panel/app/`{APP_ID}`/webhooks
3. **URL**: `https://lola.expensetracker.com.uy/mp-webhook`
4. **Eventos**: marcar `payment` y `subscription_preapproval`
5. Guardar y copiar el **Secret key**

### 3.4 Cargar credenciales en el servidor

Cargar las credenciales del comerciante en el servidor de Lola (WhatsApp config, MP config, Shopify config si aplica).

### 3.5 Verificar Shopify (solo Plan Pro)

Si el comerciante tiene Shopify, verificar que el token funciona:

```bash
curl -s https://{tienda}.myshopify.com/admin/api/2025-01/products/count.json \
  -H "X-Shopify-Access-Token: shpat_xxxxx" | python3 -m json.tool
```

Debería devolver algo como `{"count": 42}`.

### 3.6 Test final

1. Mandar un mensaje al número de WhatsApp del comerciante desde otro teléfono
2. Lola debería responder automáticamente con info del negocio
3. Probar preguntar por productos, precios, horarios
4. Probar un cobro: decirle a Lola que querés comprar algo → debe mandar link de MercadoPago
5. Si tiene Shopify: preguntar por stock de un producto → Lola debe consultarlo en tiempo real

---

## Troubleshooting

| Problema | Causa probable | Solución |
|----------|---------------|----------|
| Lola no responde en WhatsApp | Token expirado (si es temporal) | Regenerar en https://developers.facebook.com/tools/explorer/ |
| "Webhook verification failed" | Verify token no coincide | Verificar que el verify token sea el correcto |
| No llegan mensajes al webhook | Webhook no suscrito a `messages` | Ir a WhatsApp → Configuration → suscribirse a `messages` |
| Link de pago no se genera | Access token de MP inválido | Verificar credenciales de producción (no test) en MP |
| OTP no llega por WhatsApp | Número no registrado o token malo | Verificar que el phone_number_id es correcto |
| Lola no consulta stock de Shopify | Token inválido o sin permisos | Verificar que la Custom App tiene los scopes correctos y está instalada |
| "401 Unauthorized" en Shopify | Token expirado o app desinstalada | Reinstalar la app en https://`{tienda}`.myshopify.com/admin/settings/apps |

---

## Links rápidos

| Qué | URL |
|-----|-----|
| Apps de Meta | https://developers.facebook.com/apps/ |
| Token temporal WhatsApp | https://developers.facebook.com/tools/explorer/ |
| Token permanente (System Users) | https://business.facebook.com/settings/system-users |
| Config webhook WhatsApp | https://developers.facebook.com/apps/`{APP_ID}`/whatsapp-business/wa-settings/ |
| Apps de MercadoPago | https://www.mercadopago.com.uy/developers/panel/app |
| Shopify Admin | https://`{tienda}`.myshopify.com/admin |
| Shopify Apps custom | https://`{tienda}`.myshopify.com/admin/settings/apps |
| Shopify API docs | https://shopify.dev/docs/api/admin-rest |
| Shopify scopes | https://shopify.dev/docs/api/usage/access-scopes |
| Panel de Lola | https://lola.expensetracker.com.uy/app |
