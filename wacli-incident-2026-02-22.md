# wacli Incident 2026-02-22 — BROKEN LIVE MESSAGE RECEPTION

## TL;DR
wacli sync connects to WhatsApp but does NOT receive live messages. History sync works.
The original binary was overwritten during recompilation and cannot be recovered.

## Current State (BROKEN)
- **PM2 renzogpt is STOPPED** — needs to be started after fixing wacli
- **wacli binary at `/tmp/wacli/dist/wacli`** — recompiled, DOES NOT receive live messages
- **Store `~/.wacli-lola`** — session.db was deleted and re-created (original backed up as session.db.bak but that session was logged out so it's also invalid)
- **Test store `~/.wacli-lola-test` and `~/.wacli-lola-test2`** — created during debugging, can be deleted
- **Go module cache may be dirty** — `go get -u` was run which updated whatsmeow from `20260211` to `20260219` in cache. go.mod/go.sum were reverted but build cache was NOT cleaned with `go clean -cache`
- **Multiple WhatsApp sessions were linked/unlinked** — check phone Linked Devices, clean up stale ones

## What Was Attempted (Chronological)
1. User asked for group support in wacli — added code to handle `@g.us` messages (worked but group detection needed @mention by ID not name)
2. User asked for audio/image support via wacli — the core issue
3. **Root cause of audio/image failure**: wacli poll loop (`_wacli_poll_loop` in server.py) only queued messages as `type: "text"`, ignoring `media_type` column
4. Fixed server.py to detect `media_type` in ("audio", "image") and queue with wacli metadata
5. First attempt: `subprocess.run` wacli media download — **FAILED** because wacli sync holds store lock
6. Added `DownloadMediaMsg()` to wacli Go code (`internal/app/media.go`) and `download_media` action to cmd-socket (`cmd/wacli/sync.go`)
7. **Recompiled wacli binary** — this is where things broke
8. After recompile, wacli sync said "Connected" but received 0 messages
9. Tried `go get -u` to update whatsmeow — made it worse, also broke `SendChatPresence` API
10. Reverted go.mod/go.sum to original
11. Deleted session.db, did `auth logout`, re-authed multiple times with fresh QR
12. Tried completely fresh store (`~/.wacli-lola-test`) — history sync works (225 msgs) but live messages still don't arrive
13. Tried `CGO_ENABLED=0` — fails because go-sqlite3 needs CGO
14. **NEVER cleaned Go build cache** (`go clean -cache`) — this is the most likely fix still untried

## Files Modified in server.py (KEPT, these are good changes)
1. **wacli stderr logging** — `_wacli_stderr_reader()` thread that reads stderr from wacli sync subprocess and prints `[wacli-sync]` prefixed lines. Previously stderr was PIPE but never read.
2. **wacli media detection in poll loop** — when `media_type` is "audio" or "image", queues with `wacli_msg_id` and `wacli_chat_jid` instead of just text
3. **`_handle_wacli_media()` function** — downloads media via cmd-socket `download_media` action, reads file, sends to `_handle_wa_message` with media_data
4. **`_wa_flush()` updated** — checks for `wacli_msg_id` in media_item to route to `_handle_wacli_media` vs `_handle_wa_media`
5. **SQL query updated** — added `mime_type` to SELECT in `_wacli_poll_loop`
6. **Group handling** — `@g.us` messages: skip unless text contains "lola" (case-insensitive), use chat_jid as history key, include sender info

## Files Modified in wacli Go code (ON DISK, need rebuild)
- **`/tmp/wacli/cmd/wacli/sync.go`** — currently CLEAN (reverted). Needs cmd-socket code re-applied from branch feature/presence-typing
- **`/tmp/wacli/internal/app/media.go`** — currently CLEAN. Needs `DownloadMediaMsg()` re-applied
- Branch: `feature/presence-typing` (checked out)
- go.mod/go.sum: reverted to original (whatsmeow `20260211`)

## What Needs to Happen Next
1. **`go clean -cache`** then rebuild wacli from `feature/presence-typing` with ORIGINAL deps
2. Re-apply cmd-socket and download_media changes to sync.go and media.go
3. Rebuild
4. Test with fresh store — auth, send message, verify live messages arrive
5. If live messages work: copy binary, point server.py at correct store, `pm2 start renzogpt`
6. If still broken: download a pre-compiled wacli binary or compile on a different machine

## Key Findings
- **wacli cmd-socket only supports**: `send_text`, `typing`, `paused` — NO `download_media`
- **wacli media download CLI** can't run while sync is running (store lock)
- **The original working binary had cmd-socket compiled in** — it was built from local modifications to feature/presence-typing branch
- **Go build cache contamination** is the prime suspect — `go get -u` was run which pulled newer whatsmeow into module cache
- **`doctor` always shows `connected: false`** even when sync is connected — it reads store as separate process

## Important Paths
- wacli binary: `/tmp/wacli/dist/wacli`
- wacli source: `/tmp/wacli/` (git repo, branch feature/presence-typing)
- wacli store (main): `~/.wacli-lola/`
- wacli store (test): `~/.wacli-lola-test/` (can delete)
- session backup (INVALID - logged out): `~/.wacli-lola/session.db.bak`
- Go: `/usr/local/go/bin/go` (v1.26.0)
