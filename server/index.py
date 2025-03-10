import asyncio
from datetime import datetime
import functools
import logging
from urllib.parse import urlparse
import warnings
import json
import sys

from aiohttp import web
import aiohttp_session

try:
    import htmlmin

    html_minify = functools.partial(htmlmin.minify, remove_optional_attribute_quotes=False)
except ImportError:
    warnings.warn("Not using HTML minification, htmlmin not imported.")
    sys.exit(0)

from const import (
    LANGUAGES,
    TROPHIES,
    VARIANTS,
    VARIANT_ICONS,
    VARIANT_GROUPS,
    RATED,
    IMPORTED,
    T_CREATED,
    TRANSLATED_VARIANT_NAMES,
    TRANSLATED_PAIRING_SYSTEM_NAMES,
)
from fairy import FairyBoard
from glicko2.glicko2 import DEFAULT_PERF, PROVISIONAL_PHI
from robots import ROBOTS_TXT
from settings import (
    ADMINS,
    TOURNAMENT_DIRECTORS,
    MAX_AGE,
    URI,
    STATIC_ROOT,
    BR_EXTENSION,
    SOURCE_VERSION,
    DEV,
)
from generate_highscore import generate_highscore
from misc import time_control_str
from news import NEWS
from videos import VIDEO_TAGS, VIDEO_TARGETS
from user import User
from utils import load_game, join_seek, tv_game, tv_game_user
from tournaments import (
    get_winners,
    get_latest_tournaments,
    load_tournament,
    create_or_update_tournament,
    get_tournament_name,
)
from custom_trophy_owners import CUSTOM_TROPHY_OWNERS

log = logging.getLogger(__name__)


@web.middleware
async def handle_404(request, handler):
    try:
        return await handler(request)
    except web.HTTPException as ex:
        if ex.status == 404:
            template = request.app["jinja"]["en"].get_template("404.html")
            text = await template.render_async(
                {
                    "dev": DEV,
                    "home": URI,
                    "view_css": "404.css",
                    "asseturl": STATIC_ROOT,
                    "js": "/static/pychess-variants.js%s%s" % (BR_EXTENSION, SOURCE_VERSION),
                }
            )
            return web.Response(text=html_minify(text), content_type="text/html")
        else:
            raise


async def index(request):
    """Create home html."""

    users = request.app["users"]
    games = request.app["games"]
    db = request.app["db"]

    # Who made the request?
    session = await aiohttp_session.get_session(request)
    session_user = session.get("user_name")

    session["last_visit"] = datetime.now().isoformat()
    session["guest"] = True
    if session_user is not None:
        log.info("+++ Existing user %s connected.", session_user)
        doc = None
        try:
            doc = await db.user.find_one({"_id": session_user})
        except Exception:
            log.error("Failed to get user %s from mongodb!", session_user)
        if doc is not None:
            session["guest"] = False

            if not doc.get("enabled", True):
                log.info("Closed account %s tried to connect.", session_user)
                session.invalidate()
                return web.HTTPFound("/")

        if session_user in users:
            user = users[session_user]
        else:
            if session_user.startswith("Anon-"):
                session.invalidate()
                return web.HTTPFound(request.rel_url)

            log.debug("New lichess user %s joined.", session_user)
            title = session["title"] if "title" in session else ""
            perfs = {variant: DEFAULT_PERF for variant in VARIANTS}
            user = User(
                request.app,
                username=session_user,
                anon=session["guest"],
                title=title,
                perfs=perfs,
            )
            users[user.username] = user
    else:
        user = User(request.app, anon=True)
        log.info("+++ New guest user %s connected.", user.username)
        users[user.username] = user
        session["user_name"] = user.username

    lang = session.get("lang")
    if lang is None:
        lang = detect_locale(request)

    get_template = request.app["jinja"][lang].get_template

    lang_translation = request.app["gettext"][lang]
    lang_translation.install()

    def variant_display_name(variant):
        return lang_translation.gettext(TRANSLATED_VARIANT_NAMES[variant])

    def pairing_system_name(system):
        return lang_translation.gettext(TRANSLATED_PAIRING_SYSTEM_NAMES[system])

    def video_tag(tag):
        return lang_translation.gettext(VIDEO_TAGS[tag])

    def video_target(target):
        return lang_translation.gettext(VIDEO_TARGETS[target])

    view = "lobby"
    gameId = request.match_info.get("gameId")
    ply = request.rel_url.query.get("ply")

    tournamentId = request.match_info.get("tournamentId")

    if request.path == "/about":
        view = "about"
    elif request.path == "/faq":
        view = "faq"
    elif request.path == "/stats":
        view = "stats"
    elif request.path.startswith("/news"):
        view = "news"
    elif request.path.startswith("/variants"):
        view = "variants"
    elif request.path.startswith("/video"):
        videoId = request.match_info.get("videoId")
        view = "videos" if videoId is None else "video"
    elif request.path.startswith("/players"):
        view = "players"
    elif request.path == "/allplayers":
        view = "allplayers"
    elif request.path == "/games":
        view = "games"
    elif request.path == "/patron":
        view = "patron"
    elif request.path == "/patron/thanks":
        view = "thanks"
    elif request.path == "/level8win":
        view = "level8win"
    elif request.path == "/tv":
        view = "tv"
        gameId = await tv_game(db, request.app)
    elif request.path.startswith("/editor"):
        view = "editor"
    elif request.path.startswith("/analysis"):
        view = "analysis"
    elif request.path.startswith("/embed"):
        view = "embed"
    elif request.path == "/paste":
        view = "paste"
    elif request.path.startswith("/tournaments"):
        if request.path.startswith("/tournaments/shields"):
            view = "shields"
        elif request.path.startswith("/tournaments/winners"):
            view = "winners"
        else:
            view = "tournaments"
            if user.username in TOURNAMENT_DIRECTORS:
                if request.path.endswith("/new"):
                    view = "arena-new"
                elif request.path.endswith("/edit"):
                    view = "arena-new"
                    tournament = await load_tournament(request.app, tournamentId)
                    if tournament is None or tournament.status != T_CREATED:
                        view = "tournaments"
                elif request.path.endswith("/arena"):
                    data = await request.post()
                    await create_or_update_tournament(request.app, user.username, data)
    elif request.path.startswith("/tournament"):
        view = "tournament"
        tournament = await load_tournament(request.app, tournamentId)

        if tournament is None:
            return web.HTTPFound("/")

        if user.username in TOURNAMENT_DIRECTORS and tournament.status == T_CREATED:
            if request.path.endswith("/edit"):
                data = await request.post()
                await create_or_update_tournament(
                    request.app, user.username, data, tournament=tournament
                )

            elif request.path.endswith("/cancel"):
                await tournament.abort()
                return web.HTTPFound("/tournaments")

        if request.path.endswith("/pause") and user in tournament.players:
            await tournament.pause(user)
    elif request.path.startswith("/calendar"):
        view = "calendar"

    profileId = request.match_info.get("profileId")
    if profileId is not None and profileId not in users:
        await asyncio.sleep(3)
        raise web.HTTPNotFound()

    variant = request.match_info.get("variant")
    if (variant is not None) and ((variant not in VARIANTS) and variant != "terminology"):
        log.debug("Invalid variant %s in request", variant)
        raise web.HTTPNotFound()

    fen = request.rel_url.query.get("fen")
    rated = None

    if (fen is not None) and "//" in fen:
        log.debug("Invelid FEN %s in request", fen)
        raise web.HTTPNotFound()

    if profileId is not None:
        view = "profile"
        if request.path[-3:] == "/tv":
            view = "tv"
            # TODO: tv for variants
            gameId = await tv_game_user(db, users, profileId)
        elif request.path[-7:] == "/import":
            rated = IMPORTED
        elif request.path[-6:] == "/rated":
            rated = RATED
        elif request.path[-3:] == "/me":
            rated = -1
        elif "/challenge" in request.path:
            view = "lobby"
            if user.anon and profileId != "Fairy-Stockfish":
                return web.HTTPFound("/")

    # Play menu (Create a game)
    if request.rel_url.query.get("any") is not None:
        profileId = "any#"

    # Do we have gameId in request url?
    if (gameId is not None) and gameId != "variants":
        if view not in ("tv", "analysis", "embed"):
            view = "round"
        invites = request.app["invites"]
        if (gameId not in games) and (gameId in invites):
            seek_id = invites[gameId].id
            seek = request.app["seeks"][seek_id]
            if request.path.startswith("/invite/accept/"):
                player = request.match_info.get("player")
                seek_status = await join_seek(request.app, user, seek_id, gameId, join_as=player)

                if seek_status["type"] == "seek_joined":
                    view = "invite"
                    inviter = "wait"
                elif seek_status["type"] == "seek_occupied":
                    view = "invite"
                    inviter = "occupied"
                elif seek_status["type"] == "seek_yourself":
                    view = "invite"
                    inviter = "yourself"
                elif seek_status["type"] == "new_game":
                    try:
                        # Put response data to sse subscribers queue
                        channels = request.app["invite_channels"]
                        for queue in channels:
                            await queue.put(json.dumps({"gameId": gameId}))
                        # return games[game_id]
                    except ConnectionResetError:
                        pass

            else:
                view = "invite"
                inviter = seek.creator.username if user.username != seek.creator.username else ""

        if view != "invite":
            game = await load_game(request.app, gameId)
            if game is None:
                raise web.HTTPNotFound()

            if (ply is not None) and (view != "embed"):
                view = "analysis"

            if user.username not in (game.wplayer.username, game.bplayer.username):
                game.spectators.add(user)

    if view in ("profile", "level8win"):
        if (profileId in users) and not users[profileId].enabled:
            template = get_template("closed.html")
        else:
            template = get_template("profile.html")
    elif view == "players":
        if variant is None:
            template = get_template("players.html")
        else:
            template = get_template("players50.html")
    elif view == "shields":
        template = get_template("shields.html")
    elif view == "winners":
        template = get_template("winners.html")
    elif view == "allplayers":
        template = get_template("allplayers.html")
    elif view == "tournaments":
        template = get_template("tournaments.html")
    elif view == "arena-new":
        template = get_template("arena-new.html")
    elif view == "news":
        template = get_template("news.html")
    elif view == "variants":
        template = get_template("variants.html")
    elif view == "videos":
        template = get_template("videos.html")
    elif view == "video":
        template = get_template("video.html")
    elif view == "patron":
        template = get_template("patron.html")
    elif view == "faq":
        template = get_template("FAQ.html")
    elif view == "analysis":
        template = get_template("analysis.html")
    elif view == "embed":
        template = get_template("embed.html")
    else:
        template = get_template("index.html")

    if view == "lobby":
        page_title = "PyChess • Free Online Chess Variants"
    else:
        page_title = "%s • PyChess" % view.capitalize()

    render = {
        "js": "/static/pychess-variants.js%s%s" % (BR_EXTENSION, SOURCE_VERSION),
        "dev": DEV,
        "app_name": "PyChess",
        "languages": LANGUAGES,
        "lang": lang,
        "title": page_title,
        "view": view,
        "asseturl": STATIC_ROOT,
        "view_css": ("round" if view == "tv" else view) + ".css",
        "home": URI,
        "user": user.username if session["guest"] else "",
        "anon": user.anon,
        "username": user.username,
        "guest": session["guest"],
        "profile": profileId if profileId is not None else "",
        "variant": variant if variant is not None else "",
        "fen": fen.replace(".", "+").replace("_", " ") if fen is not None else "",
        "variants": VARIANTS,
        "variant_display_name": variant_display_name,
        "tournamentdirector": user.username in TOURNAMENT_DIRECTORS,
    }

    if view in ("profile", "level8win"):
        if view == "level8win":
            profileId = "Fairy-Stockfish"
            render["trophies"] = []
        else:
            hs = request.app["highscore"]
            render["trophies"] = [(v, "top10") for v in hs if profileId in hs[v].keys()[:10]]
            for i, (v, kind) in enumerate(render["trophies"]):
                if hs[v].peekitem(0)[0] == profileId:
                    render["trophies"][i] = (v, "top1")
            render["trophies"] = sorted(render["trophies"], key=lambda x: x[1])

            if not users[profileId].bot:
                shield_owners = request.app["shield_owners"]
                render["trophies"] += [
                    (v, "shield") for v in shield_owners if shield_owners[v] == profileId
                ]

            if profileId in CUSTOM_TROPHY_OWNERS:
                trophies = CUSTOM_TROPHY_OWNERS[profileId]
                for v, kind in trophies:
                    if v in VARIANTS:
                        render["trophies"].append((v, kind))

        render["title"] = "Profile • " + profileId
        render["icons"] = VARIANT_ICONS
        render["cup"] = TROPHIES
        if profileId not in users or users[profileId].perfs is None:
            render["ratings"] = {}
        else:
            render["ratings"] = {
                k: (
                    "%s%s"
                    % (
                        int(round(v["gl"]["r"], 0)),
                        "?" if v["gl"]["d"] > PROVISIONAL_PHI else "",
                    ),
                    v["nb"],
                )
                for (k, v) in sorted(
                    users[profileId].perfs.items(),
                    key=lambda x: x[1]["nb"],
                    reverse=True,
                )
            }
        if variant is not None:
            render["variant"] = variant
        render["profile_title"] = users[profileId].title if profileId in users else ""
        render["rated"] = rated

    elif view == "players":
        online_users = [
            u for u in users.values() if u.username == user.username or (u.online and not u.anon)
        ]
        anon_online = sum((1 for u in users.values() if u.anon and u.online))

        render["icons"] = VARIANT_ICONS
        render["users"] = users
        render["online_users"] = online_users
        render["anon_online"] = anon_online
        render["admin"] = user.username in ADMINS
        if variant is None:
            hs = request.app["highscore"]
            render["highscore"] = {variant: dict(hs[variant].items()[:10]) for variant in hs}
        else:
            hs = await generate_highscore(request.app["db"], variant)
            print(hs)
            render["highscore"] = hs
            view = "players50"

    elif view in ("shields", "winners"):
        wi = await get_winners(request.app, shield=(view == "shields"), variant=variant)
        render["view_css"] = "players.css"
        render["users"] = users
        render["icons"] = VARIANT_ICONS
        render["winners"] = wi

    elif view == "allplayers":
        allusers = [u for u in users.values() if not u.anon]
        render["allusers"] = allusers

    elif view == "tournaments":
        render["icons"] = VARIANT_ICONS
        render["pairing_system_name"] = pairing_system_name
        render["time_control_str"] = time_control_str
        render["tables"] = await get_latest_tournaments(request.app, lang)
        render["td"] = user.username in TOURNAMENT_DIRECTORS

    if (gameId is not None) and gameId != "variants":
        if view == "invite":
            render["gameid"] = gameId
            render["variant"] = seek.variant
            render["chess960"] = seek.chess960
            render["rated"] = seek.rated
            render["base"] = seek.base
            render["inc"] = seek.inc
            render["byo"] = seek.byoyomi_period
            render["inviter"] = inviter
            render["seekempty"] = seek.player1 is None and seek.player2 is None
        else:
            render["gameid"] = gameId
            render["variant"] = game.variant
            render["wplayer"] = game.wplayer.username
            render["wtitle"] = game.wplayer.title
            render["wrating"] = game.wrating
            render["wrdiff"] = game.wrdiff
            render["chess960"] = game.chess960
            render["rated"] = game.rated
            render["level"] = game.level
            render["bplayer"] = game.bplayer.username
            render["btitle"] = game.bplayer.title
            render["brating"] = game.brating
            render["brdiff"] = game.brdiff
            render["fen"] = game.board.fen
            render["base"] = game.base
            render["inc"] = game.inc
            render["byo"] = game.byoyomi_period
            render["result"] = game.result
            render["status"] = game.status
            render["date"] = game.date.isoformat()
            render["title"] = game.browser_title
            render["ply"] = ply if ply is not None else game.board.ply - 1
            render["ct"] = json.dumps(game.crosstable)
            render["board"] = json.dumps(game.get_board(full=True))
            if game.tournamentId is not None:
                tournament_name = await get_tournament_name(request, game.tournamentId)
                render["tournamentid"] = game.tournamentId
                render["tournamentname"] = tournament_name
                render["wberserk"] = game.wberserk
                render["bberserk"] = game.bberserk

    if tournamentId is not None:
        tournament_name = await get_tournament_name(request, tournamentId)
        render["tournamentid"] = tournamentId
        render["tournamentname"] = tournament_name
        render["description"] = tournament.description
        render["variant"] = tournament.variant
        render["chess960"] = tournament.chess960
        render["rated"] = tournament.rated
        render["base"] = tournament.base
        render["inc"] = tournament.inc
        render["byo"] = tournament.byoyomi_period
        render["fen"] = tournament.fen
        render["before_start"] = tournament.before_start
        render["minutes"] = tournament.minutes
        render["date"] = tournament.starts_at
        render["rounds"] = tournament.rounds
        render["frequency"] = tournament.frequency
        render["status"] = tournament.status
        render["title"] = tournament.browser_title

    # variant None indicates terminology.md
    if lang in ("es", "hu", "it", "pt", "fr", "zh", "zh_CN", "zh_TW"):
        locale = ".%s" % lang
    else:
        locale = ""

    if view == "level8win":
        render["level"] = 8
        render["profile"] = "Fairy-Stockfish"

    elif view == "variants":
        render["icons"] = VARIANT_ICONS
        render["groups"] = VARIANT_GROUPS

        if variant == "terminology":
            render["variant"] = "docs/terminology%s.html" % locale
        else:
            render["variant"] = (
                "docs/" + ("terminology" if variant is None else variant) + "%s.html" % locale
            )

    elif view == "videos":
        tag = request.rel_url.query.get("tags")
        videos = []
        if tag is None:
            cursor = db.video.find()
        else:
            cursor = db.video.find({"tags": tag})

        async for doc in cursor:
            videos.append(doc)
        render["videos"] = videos
        render["tags"] = VIDEO_TAGS
        render["video_tag"] = video_tag
        render["video_target"] = video_target

    elif view == "video":
        render["view_css"] = "videos.css"
        render["videoId"] = videoId
        render["tags"] = VIDEO_TAGS

    elif view == "news":
        news_item = request.match_info.get("news_item")
        if (news_item is None) or (news_item not in NEWS):
            news_item = list(NEWS.keys())[0]
        news_item = news_item.replace("_", " ")

        render["news"] = NEWS
        render["news_item"] = "news/%s%s.html" % (news_item, locale)

    elif view == "faq":
        render["faq"] = "docs/faq%s.html" % locale

    elif view == "editor" or (view == "analysis" and gameId is None):
        if fen is None:
            fen = FairyBoard(variant).start_fen(variant)
        else:
            fen = fen.replace(".", "+").replace("_", " ")
        render["variant"] = variant
        render["fen"] = fen

    elif view == "arena-new":
        render["edit"] = tournamentId is not None
        render["admin"] = user.username in ADMINS
        if tournamentId is None:
            render["rated"] = True

    try:
        text = await template.render_async(render)
    except Exception:
        log.exception("ERROR: template.render_async() failed.")
        return web.HTTPFound("/")

    response = web.Response(text=html_minify(text), content_type="text/html")
    parts = urlparse(URI)
    response.set_cookie(
        "user",
        session["user_name"],
        domain=parts.hostname,
        secure=parts.scheme == "https",
        samesite="Lax",
        max_age=None if user.anon else MAX_AGE,
    )
    return response


async def robots(request):
    return web.Response(text=ROBOTS_TXT, content_type="text/plain")


async def select_lang(request):
    data = await request.post()
    lang = data.get("lang")

    if lang is not None:
        referer = request.headers.get("REFERER")
        session = await aiohttp_session.get_session(request)
        session_user = session.get("user_name")
        users = request.app["users"]
        if session_user in users:
            user = users[session_user]
            user.lang = lang
            if user.db is not None:
                await user.db.user.find_one_and_update(
                    {"_id": user.username}, {"$set": {"lang": lang}}
                )
        session["lang"] = lang
        return web.HTTPFound(referer)
    else:
        raise web.HTTPNotFound()


def parse_accept_language(accept_language):
    languages = accept_language.split(",")
    locale_q_pairs = []

    for language in languages:
        parts = language.split(";")
        if parts[0] == language:
            # no q => q = 1
            locale_q_pairs.append((language.strip(), "1"))
        else:
            locale_q_pairs.append((parts[0].strip(), parts[1].split("=")[1]))

    return locale_q_pairs


def detect_locale(request):
    default_locale = "en"
    accept_language = request.headers.get("Accept-Language")

    if accept_language is not None:
        locale_q_pairs = parse_accept_language(accept_language)

        for pair in locale_q_pairs:
            for locale in LANGUAGES:
                # pair[0] is locale, pair[1] is q value
                if pair[0].replace("-", "_").lower().startswith(locale.lower()):
                    return locale

    return default_locale
