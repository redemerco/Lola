# Config files

The server expects these files in your home directory (`~`).
This folder has templates showing the expected format for each one.

## How to use

Copy each file to `~` with the right name and fill in your values:

```bash
cp config/whatsapp-config.json ~/.whatsapp-config.json
cp config/instagram-config.json ~/.instagram-config.json
cp config/mercadopago-config.json ~/.mercadopago-config.json
cp config/gemini-keys ~/.gemini-keys
cp config/gemini-router.py ~/gemini-router.py   # you need the real file, not this placeholder
```

## Files

| Template file              | Destination              | Required to start? | What it does                                       |
|----------------------------|--------------------------|--------------------|----------------------------------------------------|
| `whatsapp-config.json`     | `~/.whatsapp-config.json`| No                 | WhatsApp Business API creds (phone ID, token, verify token) |
| `instagram-config.json`    | `~/.instagram-config.json`| No                | Instagram Messaging API creds (user ID, token, verify token) |
| `mercadopago-config.json`  | `~/.mercadopago-config.json` | No             | MercadoPago payment integration creds              |
| `gemini-keys`              | `~/.gemini-keys`         | Yes                | Gemini API keys for the AI router (one per line)   |
| `gemini-router.py`         | `~/gemini-router.py`     | Yes                | The Gemini router module (not in this repo)        |

## Auto-generated at runtime (don't create manually)

| File                  | What it is                                                        |
|-----------------------|-------------------------------------------------------------------|
| `~/.lola-master.key`  | Fernet encryption key for the DB. **Back this up.** Without it, encrypted data is lost. |
| `~/.lola-db.sqlite`   | SQLite database with tenant and subscriber data.                  |
