"""
Instagram Preview Bot per Telegram
====================================
Manda @username → ricevi gli ultimi 5 post + tutte le stories attive.

Requisiti:
    pip install python-telegram-bot instaloader pillow

Configurazione:
    1. Crea il bot su Telegram parlando con @BotFather → /newbot
    2. Copia il token e incollalo in BOT_TOKEN qui sotto
    3. Incolla il tuo Instagram sessionid in IG_SESSIONID
    4. Avvia con:  python3 ig_bot.py
"""

import asyncio
import io
import logging
import os
import time
import urllib.request
from datetime import timezone

# ─── Configurazione ───────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN",    "")
IG_USERNAME  = os.environ.get("IG_USERNAME",  "")
IG_SESSIONID = os.environ.get("IG_SESSIONID", "")

# Cartella dove salvare i file scaricati
DOWNLOAD_DIR = os.path.expanduser("~/Downloads/IgBot")

# Quanti post recenti mostrare
MAX_POSTS    = 5

# Ritardo tra richieste Instagram (secondi) — evita rate-limit
REQUEST_DELAY = 2.0

# ID Telegram degli utenti autorizzati (lascia vuoto [] per permettere a tutti)
# Es: ALLOWED_USERS = [123456789, 987654321]
ALLOWED_USERS = []
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

try:
    from telegram import (
        Update, InlineKeyboardButton, InlineKeyboardMarkup,
        InputMediaPhoto, InputMediaVideo,
    )
    from telegram.ext import (
        Application, CommandHandler, MessageHandler,
        CallbackQueryHandler, ContextTypes, filters,
    )
    from telegram.constants import ParseMode, ChatAction
    PTB_OK = True
except ImportError:
    PTB_OK = False
    print("❌  Installa python-telegram-bot:  pip install python-telegram-bot")

try:
    import instaloader
    IL_OK = True
except ImportError:
    IL_OK = False
    print("❌  Installa instaloader:  pip install instaloader")

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False


# ─── Errori che indicano davvero un blocco/autenticazione ─────────────────────
_BLOCK_KEYWORDS = ("login_required", "checkpoint_required", "challenge_required",
                   "Forbidden", "Not authorized", "LoginRequired",
                   "ProfileAccessDeniedException", "PrivateProfileNotFollowedException")

def _is_block_error(msg: str) -> bool:
    return any(k.lower() in msg.lower() for k in _BLOCK_KEYWORDS)


# ─── Instaloader singleton ────────────────────────────────────────────────────

_loader: "instaloader.Instaloader | None" = None

def _reset_loader():
    """Force a fresh loader on next call (e.g. after session expiry)."""
    global _loader
    _loader = None

def _get_loader() -> "instaloader.Instaloader":
    global _loader
    if _loader is not None:
        return _loader

    L = instaloader.Instaloader(
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
        sleep=True,
        request_timeout=30,
        max_connection_attempts=3,
    )

    # Try session file first
    script_dir   = os.path.dirname(os.path.abspath(__file__))
    session_file = os.path.join(script_dir, f"ig_session_{IG_USERNAME}")
    if os.path.exists(session_file):
        try:
            L.load_session_from_file(IG_USERNAME, session_file)
            logged = L.test_login()
            if logged:
                log.info(f"✅ Sessione riutilizzata per @{logged}")
                _loader = L
                return L
        except Exception as e:
            log.warning(f"Sessione scaduta: {e} — uso sessionid")
            try:
                os.remove(session_file)
            except Exception:
                pass

    # Inject sessionid
    sid = IG_SESSIONID.strip()
    if sid:
        L.context._session.cookies.set("sessionid", sid, domain=".instagram.com")
        L.context._session.cookies.set("ig_did",    "unknown", domain=".instagram.com")
        L.context._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"),
            "X-IG-App-ID":      "936619743392459",
            "X-IG-WWW-Claim":   "0",
            "X-Requested-With": "XMLHttpRequest",
            "Accept-Language":  "it-IT,it;q=0.9",
            "Referer":          "https://www.instagram.com/",
        })
        logged = L.test_login()
        if logged:
            log.info(f"✅ Autenticato come @{logged} via sessionid")
            try:
                L.save_session_to_file(session_file)
                log.info("💾 Sessione salvata")
            except Exception:
                pass
        else:
            log.warning("⚠️ sessionid non valido — accesso anonimo")
            # Reset so next call retries auth instead of reusing broken loader
            _loader = L
            return L

    _loader = L
    return L


# ─── Helpers ──────────────────────────────────────────────────────────────────

# FIX: usa gli stessi cookie/header della sessione Instagram per scaricare
# i media dalla CDN (scontent-*.cdninstagram.com), che blocca User-Agent generici.
def _fetch_bytes(url: str, loader: "instaloader.Instaloader | None" = None) -> "bytes | None":
    """Download raw bytes from a URL, optionally reusing the Instagram session."""
    try:
        if loader is not None and loader.context.is_logged_in:
            # Usa la sessione requests di instaloader (già autenticata + cookie)
            resp = loader.context._session.get(str(url), timeout=15, stream=False)
            resp.raise_for_status()
            return resp.content
        else:
            # Fallback senza sessione (per URL pubblici)
            req = urllib.request.Request(
                str(url),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://www.instagram.com/",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read()
    except Exception as e:
        log.warning(f"Fetch failed {url}: {e}")
        return None


def _thumb_bytes(raw: bytes, size: int = 800) -> bytes:
    """Resize image bytes to max `size` px on longest side."""
    if not PIL_OK:
        return raw
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((size, size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return raw


def _is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def _fmt_caption(post) -> str:
    """Build a caption string for a post."""
    date = post.date_utc.strftime("%d/%m/%Y")
    kind = "🎬 Reel" if (post.typename == "GraphReel" or
                         getattr(post, "product_type", "") == "clips") else \
           "📺 IGTV" if getattr(post, "product_type", "") == "igtv" else \
           "🎬 Video" if post.is_video else "🖼 Foto"
    likes = getattr(post, "likes", 0) or 0
    cap   = (post.caption or "")[:200]
    if len(post.caption or "") > 200:
        cap += "…"
    text = f"{kind}  ·  {date}  ·  ❤️ {likes:,}\n"
    if cap:
        text += f"\n{cap}"
    return text


# ─── Bot handlers ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Ciao! Sono il tuo bot Instagram.\n\n"
        "Mandami un username (con o senza @) e ti mostro:\n"
        "• 📷 Ultimi 5 post\n"
        "• 📖 Tutte le stories attive\n\n"
        "Es:  <code>aniram</code>  oppure  <code>@aniram</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Comandi disponibili:</b>\n\n"
        "/start — messaggio di benvenuto\n"
        "/help  — questo messaggio\n\n"
        "<b>Uso:</b>\n"
        "Manda direttamente il nome utente Instagram (senza slash).\n"
        "Es: <code>cristina_rossi</code>",
        parse_mode=ParseMode.HTML,
    )


async def handle_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Main handler: receives a username and sends posts + stories."""
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Non sei autorizzato.")
        return

    raw = update.message.text.strip().lstrip("@")
    if not raw or " " in raw:
        await update.message.reply_text(
            "Mandami solo uno username Instagram, es: <code>aniram</code>",
            parse_mode=ParseMode.HTML)
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    msg = await update.message.reply_text(f"🔍 Cerco <b>@{raw}</b>…", parse_mode=ParseMode.HTML)

    # Run the blocking Instagram fetch in executor
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _fetch_ig_data, raw)
    except Exception as e:
        await msg.edit_text(f"❌ Errore: {e}")
        return

    if "error" in result:
        await msg.edit_text(f"❌ {result['error']}")
        return

    profile  = result["profile"]
    posts    = result["posts"]
    stories  = result["stories"]

    # ── Profile header ────────────────────────────────────────────────────────
    priv = "🔒 " if profile["is_private"] else ""
    header = (
        f"{priv}<b>@{profile['username']}</b>"
        + (f"  —  {profile['full_name']}" if profile["full_name"] else "") + "\n"
        f"👥 {profile['followers']:,} follower  ·  🖼 {profile['posts']:,} post\n"
    )
    if profile["bio"]:
        header += f"\n{profile['bio'][:180]}"

    await msg.delete()   # remove the "🔍 Cerco..." message

    pic_bytes = _fetch_bytes(profile["pic_url"], result.get("loader")) if profile.get("pic_url") else None
    if pic_bytes:
        try:
            await update.message.reply_photo(
                photo=io.BytesIO(_thumb_bytes(pic_bytes, size=600)),
                caption=header,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await update.message.reply_text(header, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(header, parse_mode=ParseMode.HTML)

    # ── Posts ─────────────────────────────────────────────────────────────────
    if posts:
        await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
        await update.message.reply_text(
            f"📷 <b>Ultimi {len(posts)} post:</b>", parse_mode=ParseMode.HTML)

        for p in posts:
            caption = _fmt_caption(p["post"])
            raw_bytes = p.get("bytes")
            if not raw_bytes:
                await update.message.reply_text(f"⚠️ Post non disponibile\n{caption}")
                continue
            try:
                if p["is_video"]:
                    await update.message.reply_video(
                        video=io.BytesIO(raw_bytes),
                        caption=caption,
                        supports_streaming=True,
                    )
                else:
                    await update.message.reply_photo(
                        photo=io.BytesIO(_thumb_bytes(raw_bytes)),
                        caption=caption,
                    )
            except Exception as e:
                log.warning(f"Send post failed: {e}")
                await update.message.reply_text(f"⚠️ Impossibile inviare questo media\n{caption}")
            await asyncio.sleep(0.5)
    else:
        err = result.get("posts_error")
        if err == "private":
            await update.message.reply_text("📷 Profilo privato — post non accessibili.")
        elif err == "blocked":
            await update.message.reply_text("📷 Post non accessibili (profilo bloccato o privato).")
        else:
            await update.message.reply_text("📷 Nessun post recente trovato.")

    # ── Stories ───────────────────────────────────────────────────────────────
    if stories:
        await update.message.reply_text(
            f"📖 <b>{len(stories)} stories attive:</b>", parse_mode=ParseMode.HTML)
        await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)

        for s in stories:
            date_str = s["date"].strftime("%d/%m/%Y %H:%M")
            caption  = f"📖 Story  ·  {date_str}"
            raw_bytes = s.get("bytes")
            if not raw_bytes:
                await update.message.reply_text(f"⚠️ Story non disponibile · {date_str}")
                continue
            try:
                if s["is_video"]:
                    await update.message.reply_video(
                        video=io.BytesIO(raw_bytes),
                        caption=caption,
                        supports_streaming=True,
                    )
                else:
                    await update.message.reply_photo(
                        photo=io.BytesIO(_thumb_bytes(raw_bytes)),
                        caption=caption,
                    )
            except Exception as e:
                log.warning(f"Send story failed: {e}")
                await update.message.reply_text(f"⚠️ Impossibile inviare questa story · {date_str}")
            await asyncio.sleep(0.5)
    else:
        err = result.get("stories_error")
        if err == "no_login":
            await update.message.reply_text("📖 Stories non disponibili (accesso anonimo).")
        elif err == "fetch_error":
            await update.message.reply_text("📖 Errore nel recupero delle stories.")
        else:
            await update.message.reply_text("📖 Nessuna storia pubblicata di recente.")

    # Note if we fell back to anonymous
    used_anon = result.get("profile", {}).get("used_anon", False)
    anon_note = "\n<i>ℹ️ Profilo caricato in modalità pubblica (sei bloccato o account privato)</i>" if used_anon else ""

    await update.message.reply_text(
        f"✅ <b>@{profile['username']}</b> — fatto!{anon_note}",
        parse_mode=ParseMode.HTML)


# ─── Blocking Instagram fetch (runs in executor) ──────────────────────────────

def _fetch_ig_data(username: str) -> dict:
    L = _get_loader()

    # ── Load profile ──────────────────────────────────────────────────────────
    profile      = None
    used_anon    = False   # True if we had to fall back to anonymous access
    active_loader = L      # loader actually used (may switch to anon)

    log.info(f"Loading profile @{username}, logged_in={L.context.is_logged_in}")
    try:
        profile = instaloader.Profile.from_username(L.context, username)
        log.info(f"Profile loaded: private={profile.is_private}")
    except instaloader.exceptions.ProfileNotExistsException:
        return {"error": f"Profilo @{username} non trovato su Instagram."}
    except Exception as e:
        err_msg = str(e)
        log.warning(f"Profile load error: {err_msg[:100]}")

        # FIX: fallback anonimo SOLO per veri errori di blocco/autenticazione,
        # non per qualsiasi errore che contenga "400" (es. nei log/URL)
        if _is_block_error(err_msg):
            log.info(f"Accesso negato ({err_msg[:60]}) — riprovo anonimamente")
            try:
                anon = instaloader.Instaloader(quiet=True, sleep=True, request_timeout=20)
                profile      = instaloader.Profile.from_username(anon.context, username)
                active_loader = anon
                used_anon    = True
            except instaloader.exceptions.ProfileNotExistsException:
                return {"error": f"Profilo @{username} non trovato su Instagram."}
            except Exception as e2:
                return {"error": f"Impossibile caricare @{username}: {e2}"}
        else:
            # Errore generico (timeout, network) → resetta loader per prossima call
            _reset_loader()
            return {"error": f"Errore di rete: {err_msg}"}

    # Get profile pic — FIX: passa il loader per usare la sessione autenticata
    pic_url = ""
    try:
        pic_url = str(profile.profile_pic_url)
        log.info(f"Profile pic URL obtained: {pic_url[:60]}…")
    except Exception as e:
        log.warning(f"profile_pic_url failed: {e}")

    # Fallback via web API pubblica
    if not pic_url:
        try:
            import json as _json
            api_url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={profile.username}"
            req = urllib.request.Request(api_url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "X-IG-App-ID": "936619743392459",
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                data = _json.loads(r.read())
            pic_url = data["data"]["user"].get("profile_pic_url_hd") or data["data"]["user"].get("profile_pic_url", "")
        except Exception as ep:
            log.warning(f"Profile pic API fallback failed: {ep}")

    prof_info = {
        "username":   profile.username,
        "full_name":  profile.full_name or "",
        "followers":  profile.followers,
        "posts":      profile.mediacount,
        "bio":        (profile.biography or "").replace("\n", " ").strip(),
        "is_private": profile.is_private,
        "used_anon":  used_anon,
        "pic_url":    pic_url,
    }

    # ── Posts ─────────────────────────────────────────────────────────────────
    posts_data  = []
    posts_error = None

    if profile.is_private and not active_loader.context.is_logged_in:
        posts_error = "private"
    else:
        try:
            for post in profile.get_posts():
                if len(posts_data) >= MAX_POSTS:
                    break
                url = post.video_url if post.is_video else post.url
                raw = _fetch_bytes(url, active_loader)
                posts_data.append({
                    "post":     post,
                    "is_video": post.is_video,
                    "bytes":    raw,
                })
                time.sleep(REQUEST_DELAY)
        except Exception as e:
            err = str(e)
            log.warning(f"Posts fetch error: {err}")
            if not posts_data:
                # FIX: fallback anonimo solo per veri blocchi, non errori generici
                if not used_anon and _is_block_error(err):
                    log.info("Post fetch bloccato — riprovo anonimamente")
                    try:
                        anon = instaloader.Instaloader(quiet=True, sleep=True, request_timeout=20)
                        prof2 = instaloader.Profile.from_username(anon.context, username)
                        for post in prof2.get_posts():
                            if len(posts_data) >= MAX_POSTS:
                                break
                            url = post.video_url if post.is_video else post.url
                            raw = _fetch_bytes(url, anon)
                            posts_data.append({
                                "post":     post,
                                "is_video": post.is_video,
                                "bytes":    raw,
                            })
                            time.sleep(REQUEST_DELAY)
                        prof_info["used_anon"] = True
                    except Exception as e3:
                        log.warning(f"Anon post fetch also failed: {e3}")
                        posts_error = "blocked"
                else:
                    posts_error = "blocked"

    # ── Stories ───────────────────────────────────────────────────────────────
    stories_data  = []
    stories_error = None

    if not active_loader.context.is_logged_in:
        stories_error = "no_login"
    else:
        try:
            for story_batch in active_loader.get_stories(userids=[profile.userid]):
                for item in story_batch.get_items():
                    url = item.video_url if item.is_video else item.url
                    raw = _fetch_bytes(url, active_loader)
                    stories_data.append({
                        "date":     item.date_utc.replace(tzinfo=timezone.utc),
                        "is_video": item.is_video,
                        "bytes":    raw,
                    })
                    time.sleep(REQUEST_DELAY)
        except Exception as e:
            log.warning(f"Stories fetch error: {e}")
            stories_error = "fetch_error"

    return {
        "profile":       prof_info,
        "loader":        active_loader,   # passato al handler per fetch foto profilo
        "posts":         posts_data,
        "posts_error":   posts_error,
        "stories":       stories_data,
        "stories_error": stories_error,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not PTB_OK or not IL_OK:
        print("\n❌ Dipendenze mancanti. Installa con:")
        print("   pip install python-telegram-bot instaloader pillow")
        return

    if not BOT_TOKEN:
        print("\n❌ BOT_TOKEN non configurato!")
        print("   1. Parla con @BotFather su Telegram → /newbot")
        print("   2. Copia il token e incollalo in BOT_TOKEN nello script")
        return

    # Pre-authenticate Instagram at startup
    log.info("🔐 Autenticazione Instagram...")
    try:
        _get_loader()
    except Exception as e:
        log.warning(f"Login Instagram fallito: {e}")

    log.info("🤖 Bot avviato. Premi Ctrl+C per fermare.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_username))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
