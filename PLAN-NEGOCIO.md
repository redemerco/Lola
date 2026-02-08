# RenzoGPT - Plan de Negocio

## Resumen

Bot de WhatsApp para tiendas e-commerce (Shopify/Fenicio) que atiende clientes 24/7, responde preguntas sobre productos, stock y precios en tiempo real, y cierra ventas automáticamente generando links de pago. El dueño de la tienda solo despacha.

---

## Producto

### Qué hace

- Responde consultas de clientes por WhatsApp automáticamente
- Consulta stock y precios en tiempo real via API de Shopify/Fenicio
- Arma pedidos y envía links de pago al cliente
- Confirma el pago y notifica al cliente
- El dueño de la tienda no interviene en ningún paso

### Ejemplo de conversación

```
👤 Cliente:  Hola, tienen zapatillas Nike?
🤖 Bot:      Sí! Tenemos estas:
             - Nike Air Max 90 talle 40-44 → $4.500
             - Nike Court Vision talle 38-43 → $3.200
             ¿Te interesa alguna?

👤 Cliente:  La Air Max en 42
🤖 Bot:      Nike Air Max 90 talle 42, $4.500. ¿Te armo el pedido?

👤 Cliente:  Dale
🤖 Bot:      Acá tenés el link para pagar: https://tienda.com/checkout/...
             Apenas pagues te confirmo el pedido

👤 Cliente:  *paga*
🤖 Bot:      Listo! Pedido #1234 confirmado.
             Te llega en 24-48hs. Cualquier cosa escribime.
```

---

## Arquitectura técnica

### Stack

| Componente | Tecnología | Costo |
|-----------|-----------|-------|
| Servidor | Raspberry Pi (ya disponible) | $0 |
| IA | Gemini API (free tier + fallback pago) | ~$0 |
| E-commerce | Shopify GraphQL API | $0 |
| WhatsApp | Meta Cloud API / Baileys | $0-7/mes |
| Process manager | PM2 | $0 |

### Flujo técnico

```
WhatsApp del cliente
       ↓
  Raspberry Pi (server.py)
       ↓
  Gemini (interpreta qué quiere el cliente)
       ↓
  Shopify API (consulta stock, precios, crea pedido)
       ↓
  Link de pago al cliente via WhatsApp
       ↓
  Webhook de Shopify confirma pago → bot notifica al cliente
```

### Arquitectura multi-tenant

Cada tienda tiene su propia configuración:

```
/clientes/
  ├── don-carlo/
  │     ├── config.json    (prompt, reglas, credenciales Shopify)
  │     ├── catalogo/      (productos cacheados)
  │     └── session/       (conexión WhatsApp)
  │
  ├── pizzeria-napo/
  │     ├── config.json
  │     ├── catalogo/
  │     └── session/
  │
  └── barberia-tito/
        ├── config.json
        ├── catalogo/
        └── session/
```

Ejemplo de `config.json`:

```json
{
  "nombre": "Pizzería Don Carlo",
  "telefono": "+598991234567",
  "prompt": "Sos el asistente de Pizzería Don Carlo...",
  "horarios": "Lunes a sábado 19-00hs",
  "shopify_store": "don-carlo.myshopify.com",
  "shopify_token": "shpat_xxx",
  "webhook_secret": "xxx"
}
```

---

## Integración Shopify

### Permisos requeridos (Admin API Access Token)

```
- read_products       → buscar productos y stock
- write_draft_orders  → crear pedidos
- read_orders         → verificar pagos
```

### 1. Buscar productos

```graphql
query {
  products(first: 5, query: "Nike") {
    nodes {
      title
      variants(first: 10) {
        nodes {
          title
          price
          inventoryQuantity
          id
        }
      }
    }
  }
}
```

### 2. Crear Draft Order (pedido)

```graphql
mutation {
  draftOrderCreate(input: {
    lineItems: [{
      variantId: "gid://shopify/ProductVariant/123456"
      quantity: 1
    }]
  }) {
    draftOrder {
      id
      invoiceUrl    # link de checkout para el cliente
    }
  }
}
```

### 3. Recibir confirmación de pago (Webhook)

Shopify envía un POST al servidor cuando se confirma el pago:

```json
{
  "topic": "orders/paid",
  "order": {
    "id": 789,
    "name": "#1234",
    "financial_status": "paid",
    "customer": {
      "phone": "+598991234567"
    }
  }
}
```

El bot recibe el webhook y notifica al cliente por WhatsApp.

### Setup por tienda

| Dato | Para qué |
|------|----------|
| Shopify Store URL | Conectarse a la tienda |
| Admin API Access Token | Autenticación |
| Webhook URL configurado | Recibir notificaciones de pago |

Tiempo de setup: ~15 minutos por tienda.

---

## Gemini Router (IA)

### Modelos (de mejor a peor)

| # | Modelo | Tier | RPD/key | Pool RPM |
|---|--------|------|---------|----------|
| 1 | gemini-3-flash-preview | Top | 20 | flash (5 RPM) |
| 2 | gemini-2.5-flash | Alto | 20 | flash (5 RPM) |
| 3 | gemini-2.5-flash-lite | Medio | 20 | lite (10 RPM) |
| 4 | **Gemini Flash pago** | **Fallback** | **Ilimitado** | **Ilimitado** |

> **Nota:** gemma-3-27b-it fue removido — calidad muy baja para seguir instrucciones de sistema (chatbot de tienda). No es viable en producción.

### Lógica de fallback

1. Intenta modelos gratuitos rotando keys
2. Flash y preview comparten pool RPM → si uno pega 429, el otro también duerme esa key
3. Flash-lite tiene pool independiente como backup
4. Si todo lo gratis se agota → fallback a key paga ($0.0006/conversación)

### Capacidad

~10 keys gratuitas + 1 key paga como fallback:

| Recurso | Free tier (~10 keys) | Con fallback pago |
|---------|-------------------|-------------------|
| RPD/día | ~600 (3 modelos × 20 RPD × 10 keys) | Ilimitado |
| Conversaciones/día | ~300 (2 calls/conv) | Ilimitadas |
| Tiendas chicas (15 conv/día) | ~20 | Sin límite |
| Tiendas medianas (40 conv/día) | ~7 | Sin límite |

En uso normal las ~10 keys gratis absorben toda la carga. El fallback pago solo se activa si hay un pico inusual, a un costo de $0.0006 por conversación.

---

## API Keys

### Setup inicial: ~10 keys gratuitas + 1 paga

- ~10 API keys de Gemini en cuentas free tier
- 1 API key paga (pay-as-you-go) como último fallback
- Las keys gratuitas se configuran en `~/.gemini-keys`
- La key paga va al final, solo se usa cuando las gratis se agotan

### Costo del fallback pago (Gemini Flash pay-as-you-go)

| Modelo | Input/MTok | Output/MTok | Costo/conversación |
|--------|-----------|-------------|-------------------|
| Gemini 2.5 Flash | $0.15 | $0.60 | ~$0.0006 |
| Gemini 2.5 Flash Lite | $0.10 | $0.40 | ~$0.0004 |

Incluso si 1000 conversaciones caen al fallback en un día, el costo es $0.60 USD.

### Escala futura

Si la demanda crece, se agregan más keys gratuitas. Cada key nueva suma +90 RPD/día, reduciendo la dependencia del fallback pago.

---

## Costos de WhatsApp (Meta API)

| Tipo de mensaje | Precio | Cuándo aplica |
|----------------|--------|---------------|
| Service (cliente escribe primero) | GRATIS | 90% de los casos |
| Utility (confirmaciones, tracking) | $0.0113 USD | Notificaciones |
| Marketing (promos) | $0.074 USD | Mensajes masivos |

Para el bot de atención: casi todo es service (gratis).

### Opciones de conexión a WhatsApp

| Opción | Costo | Riesgo |
|--------|-------|--------|
| Meta Cloud API (oficial) | $0 + per msg | Ninguno |
| Baileys (no oficial) | $0 total | Ban de Meta |
| Twilio/Gupshup (BSP) | ~$10-15/mes | Ninguno |

---

## Pricing

### Modelo: $1000 UYU/mes fijo (~$23 USD)

Simple, sin comisiones, sin límites de conversaciones.

Si algún cliente genera uso excesivo, es señal de agregar más API keys.

### Alternativa: créditos prepagos

| Paquete | Conversaciones | Precio |
|---------|---------------|--------|
| Starter | 200 | $500 UYU |
| Normal | 500 | $1000 UYU |
| Pro | 1500 | $2500 UYU |

Cobro via links de pago de Mercado Pago.

---

## Balance financiero

### 10 tiendas a $1000 UYU/mes

| Concepto | Costo/mes |
|----------|-----------|
| Gemini API (free tier) | $0 |
| Gemini fallback pago (peor caso) | ~$1-3 USD |
| Meta WhatsApp | $0-7 USD |
| Electricidad Pi | ~$1 USD |
| **Total costo** | **~$8-11 USD** |
| **Ingreso** | **$230 USD** |
| **Ganancia** | **~$220 USD/mes** |
| **Ganancia anual** | **~$2640 USD** |

### Inversión inicial

| Concepto | Costo |
|----------|-------|
| Raspberry Pi | Ya disponible |
| ~10 API keys gratuitas | $0 |
| 1 API key paga (billing habilitado) | $0 (pay-as-you-go) |
| **Total inversión** | **$0 USD** |

---

## Onboarding de un cliente nuevo

| Paso | Quién | Tiempo |
|------|-------|--------|
| Reunión, recibir info del negocio | Ambos | 30 min |
| Armar config.json y system prompt | Yo | 1 hora |
| Conectar WhatsApp Business | Yo | 15 min |
| Conectar Shopify (si tiene) | Yo | 30 min |
| Probar con mensajes reales | Yo | 30 min |
| **Total** | | **~2.5 horas** |

El comerciante no toca nada técnico.

---

## Target de clientes

### Mercado: tiendas Fenicio/Shopify en Uruguay

- +206 tiendas solo en Fenicio
- Foco en tiendas chicas/medianas con dueño accesible
- Rubros ideales: ropa, electro, ferretería, farmacia, veterinaria, óptica, regalos

### No apuntar a:
- Marcas multinacionales (Skechers, Puma, GAP, LG, etc.)
- Ya tienen equipos de IT

### El pitch

> "Tenés un local y te escriben por WhatsApp a las 11 de la noche preguntando precios.
> Vos estás durmiendo. Con mi bot, les contesta al toque, sabe tu catálogo,
> tus horarios, tus precios. No perdés más ventas.
> $1000 por mes, yo te lo dejo andando."

### Cómo cerrar

1. Armar demo con datos reales del negocio antes de la reunión
2. Mostrarle el bot funcionando en vivo
3. "Preguntale vos lo que quieras"
4. Si en 30 días no sirve, no paga
5. Comparar: un empleado part-time sale $15.000/mes, esto sale $1000

---

## Roadmap

### Fase 1: Bot de preguntas (MVP)
- Context stuffing: info del negocio en el system prompt
- Responde preguntas sobre productos, precios, horarios, envíos
- Sin integración de API, datos cargados manualmente

### Fase 2: Integración Shopify
- Consulta de productos y stock en tiempo real
- Creación de Draft Orders
- Links de checkout automáticos
- Webhooks de confirmación de pago

### Fase 3: Escala
- Panel web para que el cliente edite su info solo
- Integración Fenicio (si abren API)
- Más API keys según demanda
- Métricas: conversaciones, ventas cerradas, productos más consultados
