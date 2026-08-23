"""
OAuth callback server (aiohttp, port 8080).

Handles Strava, Whoop and Polar OAuth2 callbacks automatically —
the user no longer has to paste redirect URLs into Telegram.

Endpoints:
  GET /strava/callback?code=XXX&state=<telegram_id>
  GET /whoop/callback?code=XXX&state=<telegram_id>
  GET /polar/callback?code=XXX&state=<telegram_id>
  GET /health
"""
import asyncio
import json
import logging
import os
import time as _time

from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Telegram Application reference — set by bot.py before asyncio.run()
_telegram_app = None


def set_telegram_app(app) -> None:
    global _telegram_app
    _telegram_app = app


# ── HTML templates ────────────────────────────────────────────

_HTML_OK = """\
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Подключено!</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         display:flex;align-items:center;justify-content:center;
         min-height:100vh;background:#f0f4f8}}
    .card{{background:#fff;border-radius:20px;padding:48px 40px;
           text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.12);
           max-width:420px;width:90%}}
    .em{{font-size:72px;margin-bottom:20px;display:block}}
    h1{{color:#1a1a1a;font-size:26px;margin-bottom:12px}}
    p{{color:#555;font-size:16px;line-height:1.6}}
    .hint{{margin-top:24px;padding:16px;background:#f8f9fa;
           border-radius:12px;font-size:14px;color:#888}}
  </style>
</head>
<body>
  <div class="card">
    <span class="em">{emoji}</span>
    <h1>{title}</h1>
    <p>{body}</p>
    <div class="hint">Это окно можно закрыть</div>
  </div>
</body>
</html>"""

_HTML_ERR = """\
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ошибка</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         display:flex;align-items:center;justify-content:center;
         min-height:100vh;background:#f0f4f8}}
    .card{{background:#fff;border-radius:20px;padding:48px 40px;
           text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.12);
           max-width:420px;width:90%}}
    .em{{font-size:72px;margin-bottom:20px;display:block}}
    h1{{color:#cc3333;font-size:26px;margin-bottom:12px}}
    p{{color:#555;font-size:16px;line-height:1.6}}
  </style>
</head>
<body>
  <div class="card">
    <span class="em">❌</span>
    <h1>Ошибка</h1>
    <p>{message}</p>
  </div>
</body>
</html>"""


def _ok(emoji: str, title: str, body: str) -> web.Response:
    return web.Response(
        text=_HTML_OK.format(emoji=emoji, title=title, body=body),
        content_type="text/html",
    )


def _err(message: str, status: int = 400) -> web.Response:
    return web.Response(
        text=_HTML_ERR.format(message=message),
        content_type="text/html",
        status=status,
    )


# ── Strava callback ───────────────────────────────────────────

async def _strava_callback(request: web.Request) -> web.Response:
    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")  # telegram_id

    if not code or not state:
        return _err("Не переданы параметры code или state.")
    try:
        telegram_id = int(state)
    except ValueError:
        return _err("Некорректный параметр state.")

    from strava import exchange_code
    try:
        token_data = await exchange_code(code)
    except Exception as e:
        logger.error(f"Strava token exchange error: {e}")
        return _err("Ошибка при обмене кода на токен. Попробуй ещё раз.", 502)

    if "access_token" not in token_data:
        logger.error(f"Strava exchange bad response: {token_data}")
        return _err("Strava не вернул токен. Попробуй авторизоваться заново.")

    from database import get_or_create_user, save_token, set_strava_athlete_id
    db_user_id = get_or_create_user(telegram_id, "")
    save_token(
        db_user_id, "strava",
        token_data["access_token"],
        token_data["refresh_token"],
        str(token_data.get("expires_at", "")),
    )
    # Для вебхуков: события приходят с owner_id (athlete id) — запоминаем связку сразу
    _ath_id = (token_data.get("athlete") or {}).get("id")
    if _ath_id:
        set_strava_athlete_id(db_user_id, _ath_id)
    logger.info(f"Strava OAuth OK: telegram_id={telegram_id} db_user_id={db_user_id}")

    if _telegram_app:
        asyncio.create_task(_notify_strava(telegram_id, db_user_id, token_data["access_token"]))

    return _ok(
        "🏃",
        "Strava подключена!",
        "Вернись в Telegram и напиши /workout",
    )


async def _notify_strava(telegram_id: int, db_user_id: int, access_token: str) -> None:
    """Background: send Telegram message + load athlete cache."""
    try:
        msg = await _telegram_app.bot.send_message(
            telegram_id,
            "✅ Strava подключена! Теперь загружаю твои данные...",
        )
    except Exception as e:
        logger.error(f"Strava notify send error for {telegram_id}: {e}")
        return

    try:
        from strava import get_full_athlete_data
        from database import save_athlete_cache, get_user_display, count_users_with_service

        athlete_data = await get_full_athlete_data(access_token)
        save_athlete_cache(
            db_user_id,
            athlete_data["training_load"],
            athlete_data["predictions"],
            athlete_data["last_race"],
        )
        load = athlete_data["training_load"]
        await msg.edit_text(
            "✅ Strava подключена и данные загружены!\n\n"
            f"CTL: {load.get('ctl', '—')} | "
            f"ATL: {load.get('atl', '—')} | "
            f"TSB: {load.get('tsb', '—')}\n"
            f"{load.get('form_text', '—')}\n\n"
            "Попробуй /workout"
        )
    except Exception as e:
        logger.error(f"Strava post-connect cache error for {telegram_id}: {e}")
        try:
            await msg.edit_text("✅ Strava подключена! Попробуй /workout")
        except Exception:
            pass

    # Уведомление админа
    try:
        from database import get_user_display, count_users_with_service
        n = count_users_with_service("strava")
        display = get_user_display(telegram_id)
        await _telegram_app.bot.send_message(
            273726778,
            f"🟠 {display} подключил Strava\nВсего со Strava: {n}"
        )
    except Exception as e:
        logger.warning(f"Admin notify strava error: {e}")


# ── Whoop callback ────────────────────────────────────────────

async def _whoop_callback(request: web.Request) -> web.Response:
    # Whoop иногда редиректит сюда с error= вместо code= (insecure protocol warning)
    error = request.rel_url.query.get("error")
    if error:
        error_hint = request.rel_url.query.get("error_hint", "")
        state = request.rel_url.query.get("state", "")
        # Показываем инструкцию — пользователь должен вернуться в Telegram
        msg = "Whoop не разрешает незащищённые redirect URL. Свяжись с администратором бота."
        if "insecure" in error_hint.lower():
            msg = (
                "Whoop требует HTTPS для redirect URL. "
                "Вернись в Telegram и следуй инструкции по подключению."
            )
        return _err(msg, 400)

    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")  # telegram_id

    if not code or not state:
        return _err("Не переданы параметры code или state.")
    try:
        telegram_id = int(state)
    except ValueError:
        return _err("Некорректный параметр state.")

    from whoop import exchange_code as whoop_exchange
    try:
        token_data = await whoop_exchange(code)
    except Exception as e:
        logger.error(f"Whoop token exchange error: {e}")
        return _err("Ошибка при обмене кода на токен.", 502)

    if "access_token" not in token_data:
        logger.error(f"Whoop exchange bad response: {token_data}")
        return _err("Whoop не вернул токен. Попробуй авторизоваться заново.")

    from database import get_or_create_user, save_token
    db_user_id = get_or_create_user(telegram_id, "")
    save_token(
        db_user_id, "whoop",
        token_data["access_token"],
        token_data.get("refresh_token"),
        str(int(_time.time()) + token_data.get("expires_in", 3600)),
    )
    logger.info(f"Whoop OAuth OK: telegram_id={telegram_id} db_user_id={db_user_id}")

    if _telegram_app:
        asyncio.create_task(_notify_whoop(telegram_id))

    return _ok(
        "💪",
        "Whoop подключён!",
        "Вернись в Telegram и напиши /morning",
    )


async def _notify_whoop(telegram_id: int) -> None:
    try:
        await _telegram_app.bot.send_message(
            telegram_id,
            "✅ Whoop подключён! Утренние рекомендации теперь точнее.\n\n"
            "Завтра утром напиши /morning — проверю твоё восстановление.",
        )
    except Exception as e:
        logger.error(f"Whoop notify error for {telegram_id}: {e}")

    # Уведомление админа
    try:
        from database import get_user_display, count_users_with_service
        n = count_users_with_service("whoop")
        display = get_user_display(telegram_id)
        await _telegram_app.bot.send_message(
            273726778,
            f"⚪ {display} подключил Whoop\nВсего с Whoop: {n}"
        )
    except Exception as e:
        logger.warning(f"Admin notify whoop error: {e}")


# ── Polar callback ────────────────────────────────────────────

async def _polar_callback(request: web.Request) -> web.Response:
    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")  # telegram_id

    if not code or not state:
        return _err("Не переданы параметры code или state.")
    try:
        telegram_id = int(state)
    except ValueError:
        return _err("Некорректный параметр state.")

    from polar import exchange_code as polar_exchange, register_user as polar_register
    try:
        token_data = await polar_exchange(code)
    except Exception as e:
        logger.error(f"Polar token exchange error: {e}")
        return _err("Ошибка при обмене кода на токен. Попробуй ещё раз.", 502)

    if "access_token" not in token_data:
        logger.error(f"Polar exchange bad response: {token_data}")
        return _err("Polar не вернул токен. Попробуй авторизоваться заново.")

    from database import get_or_create_user, save_token
    db_user_id = get_or_create_user(telegram_id, "")
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 21600)
    x_user_id = token_data.get("x_user_id")

    save_token(
        db_user_id, "polar",
        access_token,
        refresh_token,
        str(int(_time.time()) + expires_in),
    )
    logger.info(f"Polar OAuth OK: telegram_id={telegram_id} db_user_id={db_user_id}")

    if _telegram_app:
        asyncio.create_task(_notify_polar(telegram_id, db_user_id, access_token, x_user_id))

    return _ok(
        "❄️",
        "Polar подключён!",
        "Вернись в Telegram и напиши /morning",
    )


async def _notify_polar(telegram_id: int, db_user_id: int,
                        access_token: str, x_user_id=None) -> None:
    """Background: register user in Polar, fetch initial data, send Telegram message."""
    try:
        msg = await _telegram_app.bot.send_message(
            telegram_id,
            "✅ Polar подключён! Регистрирую аккаунт...",
        )
    except Exception as e:
        logger.error(f"Polar notify send error for {telegram_id}: {e}")
        return

    try:
        from polar import register_user as polar_register, get_vo2max, get_nightly_recharge
        from database import save_user_profile, count_users_with_service, get_user_display

        # Регистрируем пользователя в Polar AccessLink
        polar_user_id = await polar_register(access_token, db_user_id, x_user_id)
        if polar_user_id:
            save_user_profile(db_user_id, polar_user_id=polar_user_id)
            logger.info(f"Polar user registered: {polar_user_id} for db_user_id={db_user_id}")

        # Получаем начальные данные
        vo2max, recharge = await asyncio.gather(
            get_vo2max(db_user_id),
            get_nightly_recharge(db_user_id),
            return_exceptions=True,
        )

        polar_vo2max_found = not isinstance(vo2max, Exception) and vo2max is not None
        if polar_vo2max_found:
            from database import save_vo2max_device
            save_vo2max_device(db_user_id, float(vo2max), "polar")
            save_user_profile(db_user_id, vo2max=vo2max, vo2max_source="polar")
            try:
                import zones as _zones
                _zones.recalculate_and_save(db_user_id)
            except Exception as _e:
                print(f"Zones recalc error (polar connect): {_e}")

        lines = ["✅ Polar подключён!\n", "Загружено из Polar:"]

        if polar_vo2max_found:
            lines.append(f"📊 VO2max: {vo2max} мл/кг/мин")
        else:
            lines.append("📊 VO2max: не найден — укажи вручную в /profile")

        if not isinstance(recharge, Exception) and recharge:
            ans = recharge.get("recovery_score")
            hrv = recharge.get("hrv")
            if ans is not None:
                lines.append(f"🔋 ANS Recharge: {ans}%")
            if hrv is not None:
                lines.append(f"💗 HRV (ночной): {hrv} мс")

        if polar_vo2max_found:
            lines.append("\nХочешь уточнить лактатный порог? → /profile")
            lines.append("Или сразу попробуй /workout")
        else:
            lines.append("\nУтром напиши /morning — проверю восстановление.")

        await msg.edit_text("\n".join(lines))

    except Exception as e:
        logger.error(f"Polar post-connect error for {telegram_id}: {e}")
        try:
            await msg.edit_text("✅ Polar подключён! Попробуй /morning")
        except Exception:
            pass

    # Уведомление админа
    try:
        from database import get_user_display, count_users_with_service
        n = count_users_with_service("polar")
        display = get_user_display(telegram_id)
        await _telegram_app.bot.send_message(
            273726778,
            f"❄️ {display} подключил Polar\nВсего с Polar: {n}"
        )
    except Exception as e:
        logger.warning(f"Admin notify polar error: {e}")


# ── Strava webhook ────────────────────────────────────────
# GET — рукопожатие при создании подписки (эхо hub.challenge),
# POST — события: activity create/update/delete и athlete deauthorize.
# Требование Strava: отвечать 200 быстро (<2 сек), обработка — неблокирующая.

STRAVA_WEBHOOK_VERIFY = os.getenv("STRAVA_WEBHOOK_VERIFY_TOKEN", "dodick-strava-hook")


async def _strava_webhook_verify(request: web.Request) -> web.Response:
    """Валидация подписки: Strava шлёт hub.mode/hub.verify_token/hub.challenge."""
    if request.query.get("hub.verify_token") != STRAVA_WEBHOOK_VERIFY:
        return web.Response(status=403, text="bad verify_token")
    return web.Response(
        text=json.dumps({"hub.challenge": request.query.get("hub.challenge", "")}),
        content_type="application/json",
    )


async def _strava_activity_ingest(uid: int, activity_id) -> None:
    """Фоновая обработка события activity.create: полный цикл загрузки одного юзера.
    fetch_raw (сырьё) → run_normalization (unified_cache; для Strava-only юзеров это
    ЕДИНСТВЕННАЯ точка нормализации — wakeup_poll их не трогает) →
    refresh_athlete_cache (CTL/ATL/TSB + прогнозы).
    Заменяет ночной опрос Strava: данные те же, триггер — событие, а не расписание."""
    try:
        import strava as _sv
        from data_normalizer import run_normalization
        from fitness import refresh_athlete_cache
        token = await _sv.ensure_valid_token(uid)
        if not token:
            logger.warning(f"strava ingest: uid={uid} без валидного токена — пропуск")
            return
        await _sv.fetch_raw(uid)
        run_normalization(uid)
        await refresh_athlete_cache(uid, token)
        logger.info(f"strava ingest: uid={uid} activity={activity_id} — "
                    f"сырьё+нормализация+кэш обновлены по вебхуку")
    except Exception as e:
        logger.error(f"strava ingest error uid={uid}: {e}")


async def _strava_webhook_event(request: web.Request) -> web.Response:
    """События Strava. Отвечаем 200 сразу, обработка — create_task (требование <2 сек).
    deauth (athlete + authorized=false) → чистка токена + уведомление админа.
    activity.create → фоновый ингест (_strava_activity_ingest) — замена ночного поллинга.
    activity.update игнорируем намеренно: атрибуты сохраняются асинхронно и одно
    сохранение атлета даёт несколько событий — фетч на каждое устроил бы шторм."""
    try:
        event = await request.json()
    except Exception:
        return web.Response(text="ok")
    logger.info(f"strava webhook: {event}")
    try:
        if (event.get("object_type") == "athlete"
                and str((event.get("updates") or {}).get("authorized")).lower() == "false"):
            from database import get_user_by_strava_athlete_id, purge_strava_data
            uid = get_user_by_strava_athlete_id(event.get("owner_id"))
            if uid:
                purge_strava_data(uid)
                logger.info(f"strava webhook: deauth uid={uid} "
                            f"(athlete {event.get('owner_id')}) — токен удалён")
                if _telegram_app:
                    try:
                        await _telegram_app.bot.send_message(
                            273726778,
                            f"🔌 Strava: uid={uid} отозвал доступ — токен удалён, "
                            f"слот освобождён",
                        )
                    except Exception:
                        pass
            else:
                logger.warning(
                    f"strava webhook: deauth неизвестного athlete {event.get('owner_id')}")
        elif (event.get("object_type") == "activity"
                and event.get("aspect_type") == "create"):
            from database import get_user_by_strava_athlete_id
            uid = get_user_by_strava_athlete_id(event.get("owner_id"))
            if uid:
                asyncio.create_task(
                    _strava_activity_ingest(uid, event.get("object_id")))
            else:
                logger.warning(
                    f"strava webhook: activity от неизвестного athlete {event.get('owner_id')}")
    except Exception as e:
        logger.error(f"strava webhook processing error: {e}")
    return web.Response(text="ok")


# ── COROS push service ─────────────────────────────

_COROS_PENDING_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DoDick — COROS connection</title>
<style>
  body{margin:0;background:#F7F6F2;color:#15181D;
       font:400 17px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;
       display:flex;min-height:100vh;align-items:center;justify-content:center}
  .box{max-width:32rem;padding:32px 28px;text-align:center}
  h1{font-size:26px;letter-spacing:-.02em;margin:0 0 14px}
  p{margin:0 0 12px;color:#2A2F38}
  a{color:#2E2BD6}
  .logo{font-weight:700;font-size:20px;margin-bottom:22px}
  .logo span{color:#2E2BD6}
</style></head><body><div class="box">
  <div class="logo">Do<span>Dick</span></div>
  <h1>COROS connection is not enabled yet</h1>
  <p>This address is the OAuth redirect endpoint for DoDick, a training
     assistant for the Dusty Dumbbells running club. The COROS API
     application is under review — the endpoint is reserved and will handle
     the authorization callback once access is granted.</p>
  <p>More about the service: <a href="https://dodick.run">dodick.run</a> ·
     <a href="https://dodick.run/support">support</a></p>
</div></body></html>"""


async def _coros_callback(request: web.Request) -> web.Response:
    """Адрес возврата после разрешения доступа в COROS.

    Пока доступ не одобрен — опрятная заглушка вместо ошибки: адрес
    указан в заявке, его могут открыть при проверке.
    """
    if request.query.get("code"):
        logger.info("coros callback: пришёл код авторизации, обмен ещё не реализован")
    return web.Response(text=_COROS_PENDING_HTML, content_type="text/html")


async def _coros_webhook(request: web.Request) -> web.Response:
    """Приём уведомлений COROS о новых тренировках (их push service).

    Пока только принимает и пишет в журнал, отвечая в их формате — адрес
    должен быть рабочим на момент подачи заявки. Разбор данных — после
    одобрения доступа.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = await request.text()
    logger.info(f"coros webhook: {str(payload)[:500]}")
    return web.Response(
        text=json.dumps({"message": "ok", "result": "0000"}),
        content_type="application/json",
    )


async def _coros_webhook_check(request: web.Request) -> web.Response:
    """GET на тот же адрес — проверка доступности (и для нас, и для них)."""
    return web.Response(
        text=json.dumps({"message": "ok", "result": "0000"}),
        content_type="application/json",
    )


# ── Health check ──────────────────────────────────────────────

async def _health(request: web.Request) -> web.Response:
    from version import VERSION
    return web.Response(
        text=json.dumps({"status": "ok", "version": VERSION}),
        content_type="application/json",
    )


# ── App factory ───────────────────────────────────────────────

def create_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/strava/callback", _strava_callback)
    app.router.add_get("/strava/webhook", _strava_webhook_verify)
    app.router.add_post("/strava/webhook", _strava_webhook_event)
    app.router.add_get("/whoop/callback",  _whoop_callback)
    app.router.add_get("/polar/callback",  _polar_callback)
    app.router.add_post("/coros/webhook",  _coros_webhook)
    app.router.add_get("/coros/webhook",   _coros_webhook_check)
    app.router.add_get("/coros/callback",  _coros_callback)
    app.router.add_get("/health",          _health)
    return app
