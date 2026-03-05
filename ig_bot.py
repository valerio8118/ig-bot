"""
Instagram Preview Bot per Telegram — modalità anonima
======================================================
Nessun sessionid richiesto. Funziona con profili pubblici.
Stories non disponibili (richiedono login).

Requisiti:
    pip install python-telegram-bot instaloader pillow

Variabili d'ambiente su Railway:
    BOT_TOKEN   — token del bot Telegram
"""

import asyncio
import io
import logging
import os
import time
import urllib.request
from datetime import timezone

# ─── Configurazione ───────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
REQUEST_DELAY = 3.0   # secondi tra richieste Instagram
ALLOWED_USERS = []    # lascia vuoto per permettere a tutti
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        Application, CommandHandler, MessageHandler,
        CallbackQueryHandler, ContextTypes, filters,
    )
    from telegram.constants import ParseMode, ChatAction
    PTB_OK = True
except ImportError:
    PTB_OK = False
    print("❌  pip install python-telegram-bot")

try:
    import instaloader
    IL_OK = True
except ImportError:
    IL_OK = False
    print("❌  pip install instaloader")

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False


# ─── Instaloader anonimo ──────────────────────────────────────────────────────

def _make_anon_loader() -> "instaloader.Instaloader":
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
    # Imposta un User-Agent realistico anche in modalità anonima
    L.context._session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "it-IT,it;q=0.9",
        "Referer":         "https://www.instagram.com/",
    })
    return L


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fetch_bytes(url: str, loader: "instaloader.Instaloader | None" = None) -> "bytes | None":
    try:
        if loader is not None:
            resp = loader.context._session.get(str(url), timeout=15, stream=False)
            resp.raise_for_status()
            return resp.content
        else:
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
    date  = post.date_utc.strftime("%d/%m/%Y")
    kind  = "🎬 Reel" if (post.typename == "GraphReel" or
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


# ─── Keyboard builders ────────────────────────────────────────────────────────

def _kb_posts(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("0 post",  callback_data=f"posts|0|{username}"),
            InlineKeyboardButton("3 post",  callback_data=f"posts|3|{username}"),
            InlineKeyboardButton("5 post",  callback_data=f"posts|5|{username}"),
        ],
        [
            InlineKeyboardButton("10 post", callback_data=f"posts|10|{username}"),
            InlineKeyboardButton("20 post", callback_data=f"posts|20|{username}"),
        ],
    ])


# ─── Bot handlers ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Ciao! Sono il tuo bot Instagram.\n\n"
        "Mandami uno username Instagram (con o senza @) e ti mostro profilo e post.\n\n"
        "⚠️ Funziona solo con <b>profili pubblici</b>.\n\n"
        "Es:  <code>aniram</code>  oppure  <code>@aniram</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Comandi disponibili:</b>\n\n"
        "/start — messaggio di benvenuto\n"
        "/help  — questo messaggio\n\n"
        "<b>Uso:</b>\n"
        "Manda uno username Instagram → scegli quanti post estrarre.\n"
        "Es: <code>cristina_rossi</code>\n\n"
        "⚠️ Solo profili pubblici. Stories non disponibili in modalità anonima.",
        parse_mode=ParseMode.HTML,
    )


async def handle_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 1 — riceve lo username, chiede quanti post."""
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Non sei autorizzato.")
        return

    username = update.message.text.strip().lstrip("@")
    if not username or " " in username:
        await update.message.reply_text(
            "Mandami solo uno username Instagram, es: <code>aniram</code>",
            parse_mode=ParseMode.HTML)
        return

    await update.message.reply_text(
        f"📷 Quanti <b>post</b> vuoi estrarre da <b>@{username}</b>?",
        parse_mode=ParseMode.HTML,
        reply_markup=_kb_posts(username),
    )


async def handle_posts_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 2 — riceve la scelta e avvia il fetch."""
    query = update.callback_query
    await query.answer()

    _, n_posts_str, username = query.data.split("|", 2)
    n_posts = int(n_posts_str)

    await query.edit_message_text(
        f"🔍 Cerco <b>@{username}</b>… (post: <b>{n_posts}</b>)",
        parse_mode=ParseMode.HTML,
    )
    await query.message.chat.send_action(ChatAction.TYPING)

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _fetch_ig_data, username, n_posts)
    except Exception as e:
        await query.edit_message_text(f"❌ Errore: {e}")
        return

    if "error" in result:
        await query.edit_message_text(f"❌ {result['error']}")
        return

    profile = result["profile"]
    posts   = result["posts"]
    loader  = result["loader"]

    # ── Profile header ────────────────────────────────────────────────────────
    priv = "🔒 " if profile["is_private"] else ""
    header = (
        f"{priv}<b>@{profile['username']}</b>"
        + (f"  —  {profile['full_name']}" if profile["full_name"] else "") + "\n"
        f"👥 {profile['followers']:,} follower  ·  🖼 {profile['posts']:,} post\n"
    )
    if profile["bio"]:
        header += f"\n{profile['bio'][:180]}"

    await query.delete_message()

    pic_bytes = _fetch_bytes(profile["pic_url"], loader) if profile.get("pic_url") else None
    if pic_bytes:
        try:
            await query.message.reply_photo(
                photo=io.BytesIO(_thumb_bytes(pic_bytes, size=600)),
                caption=header,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            await query.message.reply_text(header, parse_mode=ParseMode.HTML)
    else:
        await query.message.reply_text(header, parse_mode=ParseMode.HTML)

    # ── Posts ─────────────────────────────────────────────────────────────────
    if posts:
        await query.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
        await query.message.reply_text(
            f"📷 <b>Ultimi {len(posts)} post:</b>", parse_mode=ParseMode.HTML)

        for p in posts:
            caption   = _fmt_caption(p["post"])
            raw_bytes = p.get("bytes")
            if not raw_bytes:
                await query.message.reply_text(f"⚠️ Post non disponibile\n{caption}")
                continue
            try:
                if p["is_video"]:
                    await query.message.reply_video(
                        video=io.BytesIO(raw_bytes),
                        caption=caption,
                        supports_streaming=True,
                    )
                else:
                    await query.message.reply_photo(
                        photo=io.BytesIO(_thumb_bytes(raw_bytes)),
                        caption=caption,
                    )
            except Exception as e:
                log.warning(f"Send post failed: {e}")
                await query.message.reply_text(f"⚠️ Impossibile inviare questo media\n{caption}")
            await asyncio.sleep(0.5)
    elif n_posts > 0:
        err = result.get("posts_error")
        if err == "private":
            await query.message.reply_text("🔒 Profilo privato — post non accessibili in modalità anonima.")
        else:
            await query.message.reply_text("📷 Nessun post trovato.")

    await query.message.reply_text(
        f"✅ <b>@{profile['username']}</b> — fatto!\n"
        f"<i>ℹ️ Stories non disponibili in modalità anonima.</i>",
        parse_mode=ParseMode.HTML)


# ─── Blocking Instagram fetch ─────────────────────────────────────────────────

def _fetch_ig_data(username: str, max_posts: int) -> dict:
    L = _make_anon_loader()

    log.info(f"Loading profile @{username} (anonimo)")
    try:
        profile = instaloader.Profile.from_username(L.context, username)
        log.info(f"Profile loaded: private={profile.is_private}, posts={profile.mediacount}")
    except instaloader.exceptions.ProfileNotExistsException:
        return {"error": f"Profilo @{username} non trovato su Instagram."}
    except Exception as e:
        return {"error": f"Impossibile caricare @{username}: {e}"}

    # Profile pic
    pic_url = ""
    try:
        pic_url = str(profile.profile_pic_url)
        log.info(f"Profile pic URL: {pic_url[:60]}…")
    except Exception as e:
        log.warning(f"profile_pic_url failed: {e}")

    prof_info = {
        "username":   profile.username,
        "full_name":  profile.full_name or "",
        "followers":  profile.followers,
        "posts":      profile.mediacount,
        "bio":        (profile.biography or "").replace("\n", " ").strip(),
        "is_private": profile.is_private,
        "pic_url":    pic_url,
    }

    # ── Posts ─────────────────────────────────────────────────────────────────
    posts_data  = []
    posts_error = None

    if max_posts == 0:
        pass
    elif profile.is_private:
        posts_error = "private"
    else:
        try:
            for post in profile.get_posts():
                if len(posts_data) >= max_posts:
                    break
                url = post.video_url if post.is_video else post.url
                raw = _fetch_bytes(url, L)
                posts_data.append({
                    "post":     post,
                    "is_video": post.is_video,
                    "bytes":    raw,
                })
                time.sleep(REQUEST_DELAY)
        except Exception as e:
            log.warning(f"Posts fetch error: {e}")
            if not posts_data:
                posts_error = "blocked"

    return {
        "profile":     prof_info,
        "loader":      L,
        "posts":       posts_data,
        "posts_error": posts_error,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not PTB_OK or not IL_OK:
        print("\n❌ Dipendenze mancanti: pip install python-telegram-bot instaloader pillow")
        return

    if not BOT_TOKEN:
        print("\n❌ BOT_TOKEN non configurato!")
        return

    log.info("🤖 Bot avviato in modalità anonima (nessun login Instagram).")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CallbackQueryHandler(handle_posts_choice, pattern=r"^posts\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
