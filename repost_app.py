#!/usr/bin/env python3
"""
Репостер — оконное приложение.

Слева: где искать (YouTube, TikTok, ВК, Reddit, X) и запрос.
В центре: найденные видео, нужные отмечаются галочками.
Справа: куда залить (страница и группы ВК, каналы YouTube).

Запуск: двойной щелчок по start.bat (или: python repost_app.py)
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from PySide6 import QtCore, QtGui, QtWidgets

# deno (нужен yt-dlp для YouTube) ставится рядом с python.exe — добавляем в PATH
os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")

APP_DIR = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "RepostApp"
DATA.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA / "settings.json"
COOKIES_FILE = DATA / "cookies.txt"

VK_VIDEO = "https://vkvideo.ru"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube.readonly"]
SOURCES = [("youtube", "YouTube"), ("tiktok", "TikTok"), ("vk", "ВКонтакте"),
           ("reddit", "Reddit"), ("x", "X (Twitter)")]
SOURCE_NAMES = dict(SOURCES)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RepostApp/1.0"

DEFAULTS = {
    "proxy": "", "show_browser": True, "add_credit": True, "pause_sec": 20,
    "vk_logged": False, "vk_groups": "", "vk_manual_publish": False,
    "out_dir": str(Path.home() / "Videos" / "Repost"),
    "yt_client_secret": "", "yt_channels": [],
}


# ================================================================ настройки

def load_settings() -> dict:
    st = dict(DEFAULTS)
    if SETTINGS_FILE.is_file():
        try:
            st.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    # настройки от прошлых версий: группы ВК были списком, вход — через ключ
    g = st.get("vk_groups")
    if isinstance(g, list):
        st["vk_groups"] = "\n".join(
            f"club{x['id']}" if isinstance(x, dict) and "id" in x else str(x) for x in g)
    if st.pop("vk", None):
        st["vk_logged"] = False
    for k in ("vk_client_id", "vk_redirect", "vk_user"):
        st.pop(k, None)
    # любое поле не того типа — сбрасываем на значение по умолчанию
    for k, v in DEFAULTS.items():
        if v is not None and not isinstance(st.get(k), type(v)):
            st[k] = v
    return st


def save_settings(st: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def proxies(st: dict) -> dict | None:
    return {"http": st["proxy"], "https": st["proxy"]} if st.get("proxy") else None


# ================================================================ база «что уже залито»

class Posted:
    def __init__(self):
        self.path = str(DATA / "posted.db")
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS posted (item TEXT, dest TEXT, "
                       "remote TEXT, at INTEGER, PRIMARY KEY (item, dest))")

    def has(self, item: str, dest: str) -> bool:
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT 1 FROM posted WHERE item=? AND dest=?",
                              (item, dest)).fetchone() is not None

    def any(self, item: str) -> bool:
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT 1 FROM posted WHERE item=?", (item,)).fetchone() is not None

    def add(self, item: str, dest: str, remote: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO posted VALUES (?,?,?,?)",
                       (item, dest, remote, int(time.time())))


# ================================================================ браузер (TikTok, X, вход во ВК)

def save_cookies(ctx) -> None:
    """Сохраняет сессии TikTok / X / Reddit в cookies.txt — их использует загрузчик."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in ctx.cookies():
        dom = c.get("domain", "")
        if not any(k in dom for k in ("tiktok", "x.com", "twitter", "reddit", "vk.com", "vk.ru", "vkvideo")):
            continue
        exp = int(c.get("expires") or 0)
        lines.append("\t".join([dom, "TRUE" if dom.startswith(".") else "FALSE",
                                c.get("path", "/"), "TRUE" if c.get("secure") else "FALSE",
                                str(exp if exp > 0 else 0), c["name"], c["value"]]))
    COOKIES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


@contextmanager
def browser(st: dict, headless: bool):
    """Отдельный профиль браузера приложения: входы в аккаунты сохраняются между запусками."""
    from playwright.sync_api import sync_playwright
    kw: dict = {"user_data_dir": str(DATA / "browser"), "headless": headless,
                "locale": "ru-RU", "viewport": {"width": 1200, "height": 850}}
    if st.get("proxy"):
        p = urlsplit(st["proxy"])
        kw["proxy"] = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
        if p.username:
            kw["proxy"] |= {"username": p.username, "password": p.password or ""}
    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(channel="msedge", **kw)
        except Exception:
            try:
                ctx = pw.chromium.launch_persistent_context(channel="chrome", **kw)
            except Exception:
                ctx = pw.chromium.launch_persistent_context(**kw)
        try:
            yield ctx
        finally:
            try:
                save_cookies(ctx)
                ctx.close()
            except Exception:
                pass


def login_site(st: dict, url: str) -> None:
    """Открывает окно браузера; пользователь входит в аккаунт и закрывает окно."""
    with browser(st, headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        while True:
            try:
                if page.is_closed() or not ctx.pages:
                    break
                save_cookies(ctx)
                page.wait_for_timeout(2000)
            except Exception:
                break


# ================================================================ поиск

def _walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _find(o, key):
    for d in _walk(o):
        if key in d and d[key]:
            return d[key]
    return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def ydl_opts(st: dict) -> dict:
    opts = {"quiet": True, "no_warnings": True, "retries": 5, "socket_timeout": 30,
            "noprogress": True}
    if st.get("proxy"):
        opts["proxy"] = st["proxy"]
    if COOKIES_FILE.is_file():
        opts["cookiefile"] = str(COOKIES_FILE)
    try:
        import imageio_ffmpeg
        opts["ffmpeg_location"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return opts


def search_youtube(st: dict, q: str, n: int) -> list[dict]:
    import yt_dlp
    url = (f"https://www.youtube.com/hashtag/{quote(q[1:])}" if q.startswith("#")
           else f"ytsearch{n}:{q}")
    with yt_dlp.YoutubeDL(ydl_opts(st) | {"extract_flat": True, "playlistend": n}) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for e in (info.get("entries") or [])[:n]:
        if not e or not e.get("id"):
            continue
        out.append({"source": "youtube", "id": e["id"],
                    "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
                    "author": e.get("channel") or e.get("uploader") or "",
                    "title": e.get("title") or "", "duration": _int(e.get("duration")),
                    "views": _int(e.get("view_count"))})
    return out


def parse_vk(data, found: dict) -> None:
    """Достаёт ролики из ответов сайта VK Видео (формат может меняться — разбор терпимый)."""
    names: dict[int, str] = {}
    for d in _walk(data):
        if isinstance(d.get("id"), int) and "first_name" in d:
            names[d["id"]] = f"{d.get('first_name', '')} {d.get('last_name', '')}".strip()
        elif isinstance(d.get("id"), int) and "name" in d and "screen_name" in d:
            names[-d["id"]] = str(d["name"])
    for d in _walk(data):
        owner, vid = d.get("owner_id"), d.get("id")
        if not (isinstance(owner, int) and isinstance(vid, int)
                and "title" in d and "duration" in d):
            continue
        views = d.get("views")
        found[f"{owner}_{vid}"] = {
            "source": "vk", "id": f"{owner}_{vid}",
            "url": f"https://vk.com/video{owner}_{vid}",
            "author": names.get(owner, str(owner)), "title": str(d.get("title") or ""),
            "duration": _int(d.get("duration")),
            "views": _int(views.get("count") if isinstance(views, dict) else views)}


def search_vk(st: dict, q: str, n: int) -> list[dict]:
    return _browser_search(st, f"{VK_VIDEO}/?q={quote(q)}", ("search", "Search"), parse_vk, n)


def parse_reddit(data: dict) -> list[dict]:
    out = []
    for ch in (data.get("data") or {}).get("children", []):
        p = ch.get("data") or {}
        if not p.get("is_video") or p.get("over_18"):
            continue
        rv = ((p.get("media") or {}).get("reddit_video") or {})
        out.append({"source": "reddit", "id": p.get("id", ""),
                    "url": "https://www.reddit.com" + p.get("permalink", ""),
                    "author": p.get("author") or "", "title": p.get("title") or "",
                    "duration": _int(rv.get("duration")), "views": _int(p.get("score"))})
    return out


def search_reddit(st: dict, q: str, n: int) -> list[dict]:
    out, after = [], None
    for _ in range(5):                       # до 5 страниц по 100 постов
        r = requests.get("https://www.reddit.com/search.json",
                         params={"q": q.lstrip("#"), "limit": 100, "type": "link",
                                 "sort": "relevance", "raw_json": 1, "after": after},
                         headers={"User-Agent": UA}, proxies=proxies(st), timeout=30)
        if r.status_code in (403, 429):
            raise RuntimeError(f"Reddit отказал (код {r.status_code}) — попробуй позже или с VPN")
        r.raise_for_status()
        data = r.json()
        out += parse_reddit(data)
        after = (data.get("data") or {}).get("after")
        if len(out) >= n or not after:
            break
        time.sleep(1.5)
    return out[:n]


def parse_tiktok(data: dict, found: dict) -> None:
    items = list(data.get("itemList") or data.get("item_list") or [])
    items += [d["item"] for d in data.get("data") or []
              if isinstance(d, dict) and isinstance(d.get("item"), dict)]
    for it in items:
        vid, author = str(it.get("id") or ""), (it.get("author") or {})
        user = author.get("uniqueId") or author.get("unique_id") or ""
        if not vid or not user:
            continue
        stats = it.get("stats") or {}
        found[vid] = {"source": "tiktok", "id": vid,
                      "url": f"https://www.tiktok.com/@{user}/video/{vid}",
                      "author": user, "title": (it.get("desc") or "").strip(),
                      "duration": _int((it.get("video") or {}).get("duration")),
                      "views": _int(stats.get("playCount"))}


def parse_x(data, found: dict) -> None:
    for d in _walk(data):
        leg = d.get("legacy")
        if not (d.get("rest_id") and isinstance(leg, dict) and "full_text" in leg):
            continue
        media = [m for m in (leg.get("extended_entities") or {}).get("media") or []
                 if m.get("type") in ("video", "animated_gif")]
        if not media:
            continue
        user = _find(d.get("core") or {}, "screen_name") or "i"
        ms = (media[0].get("video_info") or {}).get("duration_millis")
        found[d["rest_id"]] = {
            "source": "x", "id": d["rest_id"],
            "url": f"https://x.com/{user}/status/{d['rest_id']}", "author": user,
            "title": " ".join(leg["full_text"].split())[:200],
            "duration": _int(ms / 1000) if ms else None,
            "views": _int((d.get("views") or {}).get("count"))}


def _browser_search(st: dict, url: str, marks: tuple, parser, n: int) -> list[dict]:
    """Открывает страницу поиска и собирает ролики из ответов, которые она сама грузит."""
    found: dict[str, dict] = {}

    def on_response(resp):
        if any(m in resp.url for m in marks):
            try:
                parser(resp.json(), found)
            except Exception:
                pass

    with browser(st, headless=not st.get("show_browser", True)) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        deadline, last, idle = time.time() + 90, 0, 0
        while len(found) < n and time.time() < deadline and idle < 12:
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)
            idle = idle + 1 if len(found) == last else 0
            last = len(found)
    return list(found.values())[:n]


def search_tiktok(st: dict, q: str, n: int) -> list[dict]:
    url = (f"https://www.tiktok.com/tag/{quote(q[1:])}" if q.startswith("#")
           else f"https://www.tiktok.com/search/video?q={quote(q)}")
    return _browser_search(st, url, ("/api/challenge/item_list", "/api/search/item/full",
                                     "/api/search/general/full"), parse_tiktok, n)


def search_x(st: dict, q: str, n: int) -> list[dict]:
    url = f"https://x.com/search?q={quote(q + ' filter:videos')}&src=typed_query&f=top"
    return _browser_search(st, url, ("SearchTimeline",), parse_x, n)


SEARCHERS = {"youtube": search_youtube, "tiktok": search_tiktok, "vk": search_vk,
             "reddit": search_reddit, "x": search_x}


# ================================================================ скачивание

def download(st: dict, item: dict) -> Path:
    import yt_dlp
    out_dir = DATA / "downloads"
    out_dir.mkdir(exist_ok=True)
    fmt = ("best[format_note!*=watermarked][vcodec^=h264]/best[format_note!*=watermarked]/best"
           if item["source"] == "tiktok" else
           "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/b[height<=1080][ext=mp4]/"
           "bv*[height<=1080]+ba/b")
    opts = ydl_opts(st) | {"format": fmt, "merge_output_format": "mp4",
                           "outtmpl": str(out_dir / "%(extractor)s_%(id)s.%(ext)s")}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(item["url"], download=True)
        if info.get("entries"):                       # пост с несколькими видео — берём первое
            info = info["entries"][0]
        rd = info.get("requested_downloads") or []
        path = Path(rd[0]["filepath"]) if rd else Path(ydl.prepare_filename(info))
    if not path.exists():
        raise FileNotFoundError(str(path))
    return path


# ================================================================ ВКонтакте (через сайт)

VK_OPEN = ["Добавить видео", "Загрузить видео", "Опубликовать видео", "Создать", "Добавить"]
VK_PICK = ["Выбрать файл", "Выберите файл", "Выбрать видео", "Загрузить файл",
           "Загрузить с устройства", "С устройства", "Загрузить видео", "Добавить видео"]
VK_PUBLISH = ["Опубликовать", "Готово", "Сохранить"]


def _visible(page, texts: list[str]):
    """Первый видимый элемент (кнопка, ссылка, пункт меню или надпись) с одним из текстов."""
    for t in texts:
        rx = re.compile(t, re.I)
        exact = re.compile(rf"^\s*{re.escape(t)}\s*$", re.I)
        for loc in (page.get_by_role("button", name=rx), page.get_by_role("menuitem", name=rx),
                    page.get_by_role("link", name=rx), page.get_by_text(exact)):
            try:
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    if el.is_visible():
                        return el
            except Exception:
                continue
    return None


def _click(page, texts: list[str]) -> bool:
    el = _visible(page, texts)
    if not el:
        return False
    try:
        el.click(timeout=3000)
        return True
    except Exception:
        return False


def _fill(page, labels: list[str], value: str) -> bool:
    for lab in labels:
        rx = re.compile(lab, re.I)
        for loc in (page.get_by_label(rx), page.get_by_placeholder(rx)):
            try:
                for i in range(min(loc.count(), 3)):
                    el = loc.nth(i)
                    if el.is_visible():
                        el.fill(value, timeout=3000)
                        return True
            except Exception:
                continue
    return False


def _wait(page, seconds: float, cond) -> bool:
    """Ждёт, пока cond() не станет истинным. False — время вышло, None-исключение — окно закрыто."""
    t0 = time.time()
    while time.time() - t0 < seconds:
        if cond():
            return True
        if page.is_closed():
            raise RuntimeError("окно браузера закрыто")
        page.wait_for_timeout(700)
    return False


def vk_web_upload(st: dict, file: Path, title: str, desc: str, screen: str | None, log) -> str:
    """
    Заливает видео через сайт VK Видео в браузере приложения (без ключей и API).
    screen — короткий адрес группы (club123 или имя), None — своя страница.
    Чего не удалось сделать автоматически — просит доделать руками в открытом окне.
    """
    url = f"{VK_VIDEO}/@{screen}" if screen else VK_VIDEO
    with browser(st, headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        state = {"file": False}

        def on_chooser(fc):
            try:
                fc.set_files(str(file))
                state["file"] = True
            except Exception:
                pass

        page.on("filechooser", on_chooser)

        def try_input() -> bool:
            if state["file"]:
                return True
            try:
                for i in range(page.locator("input[type=file]").count()):
                    inp = page.locator("input[type=file]").nth(i)
                    acc = (inp.get_attribute("accept") or "").lower()
                    if acc and not any(k in acc for k in ("video", "mp4", "*")):
                        continue
                    inp.set_input_files(str(file))
                    state["file"] = True
                    return True
            except Exception:
                pass
            return False

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)

            # 1. открыть окно загрузки и подставить файл
            for rnd in range(4):
                if try_input():
                    break
                _click(page, VK_OPEN if rnd % 2 == 0 else VK_PICK)
                page.wait_for_timeout(1500)
                if try_input():
                    break
                _click(page, VK_PICK)
                page.wait_for_timeout(1500)
            if not try_input():
                log("   ВК: не нашёл кнопку загрузки. Нажми в открытом окне «+» → «Добавить видео» "
                    "и «Выбрать файл» — файл подставится сам. (Если ты не вошёл во ВК — войди.)")
                if not _wait(page, 300, try_input):
                    raise RuntimeError("файл не выбран за 5 минут")

            # 2. название и описание
            if not _wait(page, 60, lambda: _fill(page, ["Название"], title)):
                log("   ВК: не нашёл поле названия — впиши название сам.")
            if desc and not _fill(page, ["Описание"], desc):
                log("   ВК: не нашёл поле описания — пропускаю.")

            # 3. публикация
            if st.get("vk_manual_publish"):
                log("   ВК: проверь видео, нажми «Опубликовать» и закрой окно браузера.")
                _wait_closed(page, 3600)
                return "опубликовано вручную"

            def publish() -> bool:
                el = _visible(page, VK_PUBLISH)
                try:
                    if el and el.is_enabled():
                        el.click(timeout=3000)
                        return True
                except Exception:
                    pass
                return False

            if not _wait(page, 1800, publish):
                log("   ВК: не нашёл кнопку публикации — нажми её сам и закрой окно браузера.")
                _wait_closed(page, 3600)
                return "опубликовано вручную"
            page.wait_for_timeout(6000)
            return page.url
        except Exception:
            _debug_dump(page)
            raise


def _wait_closed(page, seconds: float) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            if page.is_closed():
                return
            page.wait_for_timeout(1000)
        except Exception:
            return


def _debug_dump(page) -> None:
    """Сохраняет снимок и код страницы, чтобы можно было разобраться, что пошло не так."""
    try:
        d = DATA / "debug"
        d.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        page.screenshot(path=str(d / f"vk_{stamp}.png"))
        (d / f"vk_{stamp}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def vk_group_list(st: dict) -> list[str]:
    """Короткие адреса групп из поля настроек: ссылки или имена, по одной в строке."""
    out = []
    for line in (st.get("vk_groups") or "").splitlines():
        name = line.strip().rstrip("/").split("/")[-1].lstrip("@").split("?")[0]
        if name and name not in out:
            out.append(name)
    return out


# ================================================================ YouTube

def yt_connect(st: dict) -> str:
    """Вход в Google в обычном браузере; возвращает название подключённого канала."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    flow = InstalledAppFlow.from_client_secrets_file(st["yt_client_secret"], YT_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300,
                                  success_message="Готово. Вкладку можно закрыть.")
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    if not items:
        raise RuntimeError("У этого аккаунта Google нет YouTube-канала")
    cid, title = items[0]["id"], items[0]["snippet"]["title"]
    token_file = DATA / f"yt_{cid}.json"
    token_file.write_text(creds.to_json(), encoding="utf-8")
    st["yt_channels"] = [c for c in st["yt_channels"] if c["id"] != cid] + [
        {"id": cid, "name": title, "token": str(token_file)}]
    save_settings(st)
    return title


def yt_upload(ch: dict, file: Path, title: str, desc: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload
    creds = Credentials.from_authorized_user_file(ch["token"], YT_SCOPES)
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception:
            raise RuntimeError("Вход в YouTube устарел — подключи канал заново")
        Path(ch["token"]).write_text(creds.to_json(), encoding="utf-8")
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    body = {"snippet": {"title": title[:100], "description": desc[:5000], "categoryId": "22"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(str(file), mimetype="video/mp4",
                            chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp, retries = None, 0
    while resp is None:
        try:
            _, resp = req.next_chunk()
        except HttpError as e:
            text = str(e)
            if e.resp.status == 403 and ("quotaExceeded" in text or "uploadLimitExceeded" in text):
                raise RuntimeError("дневной лимит YouTube исчерпан, попробуй завтра")
            if e.resp.status in (500, 502, 503, 504) and retries < 5:
                retries += 1
                time.sleep(2 ** retries)
                continue
            raise
    return f"https://youtu.be/{resp['id']}"


# ================================================================ площадки и заливка

def destinations(st: dict) -> list[dict]:
    out = []
    if st.get("vk_logged"):
        out.append({"key": "vk:self", "kind": "vk", "screen": None, "label": "ВК — моя страница"})
        out += [{"key": f"vk:{g}", "kind": "vk", "screen": g, "label": f"ВК — группа {g}"}
                for g in vk_group_list(st)]
    out += [{"key": f"yt:{c['id']}", "kind": "yt", "channel": c,
             "label": f"YouTube — {c['name']}"} for c in st.get("yt_channels", [])]
    out.append({"key": "folder", "kind": "folder", "label": "Папка на компьютере (просто скачать)"})
    return out


def save_to_folder(st: dict, file: Path, title: str, item: dict) -> str:
    out = Path(st["out_dir"])
    out.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\r\n]+', " ", title).strip()[:80] or "video"
    dest = out / f"{safe} [{item['source']}_{item['id']}]{file.suffix}"
    shutil.copy2(file, dest)
    return str(dest)


def make_texts(st: dict, item: dict) -> tuple[str, str]:
    title = (item["title"] or "").strip().split("\n")[0][:90] or "Видео"
    desc = (item["title"] or "").strip()
    if st.get("add_credit", True):
        desc += f"\n\nАвтор: {item['author']}\nИсточник: {item['url']}"
    return title, desc.strip()


def update_ytdlp(log) -> None:
    """Раз в сутки обновляет загрузчик; новая версия подхватится при следующем запуске."""
    stamp = DATA / ".ytdlp_update"
    if stamp.exists() and time.time() - stamp.stat().st_mtime < 86400:
        return
    try:
        res = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "-q",
                              "yt-dlp[default]"], capture_output=True, text=True, timeout=300)
        stamp.touch()
        if res.returncode:
            log("Не удалось обновить загрузчик: " + res.stderr.strip()[-200:])
    except Exception as e:
        log(f"Не удалось обновить загрузчик: {e}")


# ================================================================ окно

def fmt_dur(s) -> str:
    return f"{s // 60}:{s % 60:02d}" if s else ""


def fmt_num(n) -> str:
    if n is None:
        return ""
    return f"{n / 1e6:.1f} млн" if n >= 1e6 else f"{n / 1e3:.0f} тыс." if n >= 1e3 else str(n)


class Bus(QtCore.QObject):
    log = QtCore.Signal(str)
    results = QtCore.Signal(list)
    row_status = QtCore.Signal(str, str)
    finished = QtCore.Signal()
    accounts = QtCore.Signal()


class Main(QtWidgets.QMainWindow):
    COLS = ["", "Источник", "Автор", "Название", "Длина", "Просмотры", "Статус"]

    def __init__(self):
        super().__init__()
        self.st = load_settings()
        self.posted = Posted()
        self.items: list[dict] = []
        self.busy = False
        self.bus = Bus()
        self.bus.log.connect(self.log)
        self.bus.results.connect(self.show_results)
        self.bus.row_status.connect(self.set_row_status)
        self.bus.finished.connect(self.on_finished)
        self.bus.accounts.connect(self.refresh_accounts)

        self.setWindowTitle("Репостер")
        self.resize(1280, 780)
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self.build_main_tab(), "Поиск и заливка")
        tabs.addTab(self.build_accounts_tab(), "Аккаунты")
        tabs.addTab(self.build_help_tab(), "Помощь")
        self.setCentralWidget(tabs)
        self.refresh_accounts()
        self.log(f"Данные приложения: {DATA}")
        threading.Thread(target=update_ytdlp, args=(self.bus.log.emit,), daemon=True).start()

    # ---------- вкладка «Поиск и заливка»
    def build_main_tab(self) -> QtWidgets.QWidget:
        left = QtWidgets.QGroupBox("1. Где и что искать")
        lv = QtWidgets.QVBoxLayout(left)
        self.src_boxes = {}
        for key, name in SOURCES:
            cb = QtWidgets.QCheckBox(name)
            cb.setChecked(key == "youtube")
            self.src_boxes[key] = cb
            lv.addWidget(cb)
        lv.addSpacing(8)
        lv.addWidget(QtWidgets.QLabel("Слова или #хэштег:"))
        self.query = QtWidgets.QLineEdit()
        self.query.setPlaceholderText("например: #рецепты  или  смешные коты")
        self.query.returnPressed.connect(self.start_search)
        lv.addWidget(self.query)
        lv.addWidget(QtWidgets.QLabel("Сколько видео с каждого источника:"))
        self.count = QtWidgets.QSpinBox()
        self.count.setRange(5, 200)
        self.count.setValue(20)
        lv.addWidget(self.count)
        self.only_short = QtWidgets.QCheckBox("Только короткие (до 3 минут)")
        self.only_short.setChecked(True)
        lv.addWidget(self.only_short)
        self.btn_search = QtWidgets.QPushButton("Найти")
        self.btn_search.clicked.connect(self.start_search)
        lv.addWidget(self.btn_search)
        lv.addStretch(1)

        mid = QtWidgets.QGroupBox("2. Найденные видео — отметь нужные")
        mv = QtWidgets.QVBoxLayout(mid)
        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.Stretch)
        self.table.cellDoubleClicked.connect(
            lambda r, c: webbrowser.open(self.items[r]["url"]))
        mv.addWidget(self.table)
        row = QtWidgets.QHBoxLayout()
        for text, fn in (("Отметить все", lambda: self.check_all(True)),
                         ("Снять отметки", lambda: self.check_all(False))):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addWidget(QtWidgets.QLabel("Двойной щелчок по строке — открыть видео"))
        row.addStretch(1)
        mv.addLayout(row)

        right = QtWidgets.QGroupBox("3. Куда залить")
        rv = QtWidgets.QVBoxLayout(right)
        self.dest_list = QtWidgets.QListWidget()
        rv.addWidget(self.dest_list)
        self.no_dest = QtWidgets.QLabel("ВК и YouTube подключаются\nна вкладке «Аккаунты».")
        rv.addWidget(self.no_dest)
        self.credit = QtWidgets.QCheckBox("Указывать автора и ссылку\nна оригинал в описании")
        self.credit.setChecked(self.st["add_credit"])
        rv.addWidget(self.credit)
        self.btn_upload = QtWidgets.QPushButton("Залить отмеченные")
        self.btn_upload.clicked.connect(self.start_upload)
        rv.addWidget(self.btn_upload)

        split = QtWidgets.QSplitter()
        for w in (left, mid, right):
            split.addWidget(w)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 760, 270])

        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(1000)
        vsplit = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        vsplit.addWidget(split)
        vsplit.addWidget(self.log_box)
        vsplit.setSizes([560, 160])
        return vsplit

    # ---------- вкладка «Аккаунты»
    def build_accounts_tab(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)

        def box(title: str, hint: str) -> QtWidgets.QVBoxLayout:
            g = QtWidgets.QGroupBox(title)
            lay = QtWidgets.QVBoxLayout(g)
            lab = QtWidgets.QLabel(hint)
            lab.setWordWrap(True)
            lay.addWidget(lab)
            v.addWidget(g)
            return lay

        def button(text: str, fn) -> QtWidgets.QPushButton:
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            b.setSizePolicy(QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.Fixed)
            return b

        lay = box("ВКонтакте — поиск и заливка",
                  "Ключи не нужны. Нажми «Войти во ВК»: откроется окно браузера, войди как обычно "
                  "и закрой окно. Ниже впиши ссылки на свои группы, куда хочешь заливать, "
                  "по одной в строке (например https://vk.com/club123456).")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(button("Войти во ВК", self.do_vk_login))
        self.vk_status = QtWidgets.QLabel()
        row.addWidget(self.vk_status, 1)
        lay.addLayout(row)
        self.vk_groups = QtWidgets.QPlainTextEdit(self.st["vk_groups"])
        self.vk_groups.setPlaceholderText("Ссылки на группы ВК, по одной в строке")
        self.vk_groups.setMaximumHeight(70)
        lay.addWidget(self.vk_groups)
        self.vk_manual = QtWidgets.QCheckBox(
            "Я сам проверяю видео и нажимаю «Опубликовать» в окне браузера")
        self.vk_manual.setChecked(self.st["vk_manual_publish"])
        lay.addWidget(self.vk_manual)

        lay = box("YouTube — заливка",
                  "Нужен файл-ключ от Google (один раз, вкладка «Помощь», раздел 4). Нажми кнопку, "
                  "выбери этот файл, затем войди в Google в открывшемся браузере. "
                  "Для поиска по YouTube вход не нужен.")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(button("Подключить канал YouTube", self.do_yt_connect))
        row.addWidget(button("Отключить все каналы", self.do_yt_clear))
        self.yt_status = QtWidgets.QLabel()
        row.addWidget(self.yt_status, 1)
        lay.addLayout(row)

        lay = box("TikTok, X (Twitter), Reddit — только поиск",
                  "Откроется окно браузера: войди в аккаунт как обычно и закрой окно — приложение "
                  "запомнит вход. TikTok по хэштегам часто работает и без входа; X без входа не ищет. "
                  "Reddit входа не требует.")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(button("Войти в TikTok", lambda: self.do_site_login(
            "TikTok", "https://www.tiktok.com/login")))
        row.addWidget(button("Войти в X", lambda: self.do_site_login(
            "X", "https://x.com/i/flow/login")))
        row.addStretch(1)
        lay.addLayout(row)

        lay = box("Интернет",
                  "TikTok, X и YouTube из России работают только через VPN. Проще всего включить VPN "
                  "на компьютере. Поле «Прокси» заполняй, только если у тебя именно прокси, а не VPN.")
        form = QtWidgets.QFormLayout()
        self.proxy = QtWidgets.QLineEdit(self.st["proxy"])
        self.proxy.setPlaceholderText("пусто  или  socks5://логин:пароль@адрес:порт")
        form.addRow("Прокси:", self.proxy)
        self.show_browser = QtWidgets.QCheckBox(
            "Показывать окно браузера при поиске в TikTok и X (надёжнее, можно пройти капчу)")
        self.show_browser.setChecked(self.st["show_browser"])
        form.addRow(self.show_browser)
        self.pause = QtWidgets.QSpinBox()
        self.pause.setRange(0, 600)
        self.pause.setValue(self.st["pause_sec"])
        form.addRow("Пауза между заливками, сек:", self.pause)
        lay.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(button("Папка для скачанных видео…", self.pick_folder))
        self.out_label = QtWidgets.QLabel(self.st["out_dir"])
        row.addWidget(self.out_label, 1)
        lay.addLayout(row)
        lay.addWidget(button("Сохранить настройки", self.save_form))
        v.addStretch(1)
        return w

    def build_help_tab(self) -> QtWidgets.QWidget:
        tb = QtWidgets.QTextBrowser()
        tb.setOpenExternalLinks(True)
        readme = APP_DIR / "README.md"
        tb.setMarkdown(readme.read_text(encoding="utf-8") if readme.is_file()
                       else "Файл README.md не найден рядом с программой.")
        return tb

    # ---------- общее
    def log(self, text: str) -> None:
        line = time.strftime("%H:%M:%S ") + text
        self.log_box.appendPlainText(line)
        try:
            with (DATA / "app.log").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    def save_form(self) -> None:
        self.st.update(proxy=self.proxy.text().strip(), vk_groups=self.vk_groups.toPlainText(),
                       vk_manual_publish=self.vk_manual.isChecked(),
                       show_browser=self.show_browser.isChecked(), pause_sec=self.pause.value(),
                       add_credit=self.credit.isChecked())
        save_settings(self.st)
        self.refresh_accounts()

    def pick_folder(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Куда сохранять видео",
                                                          self.st["out_dir"])
        if path:
            self.st["out_dir"] = path
            self.out_label.setText(path)
            save_settings(self.st)

    def run_bg(self, what: str, fn) -> None:
        if self.busy:
            self.log("Подожди, пока закончится текущее действие.")
            return
        self.save_form()
        self.busy = True
        self.btn_search.setEnabled(False)
        self.btn_upload.setEnabled(False)

        def work():
            try:
                fn()
            except Exception as e:
                self.bus.log.emit(f"{what}: ошибка — {e}")
                try:
                    with (DATA / "app.log").open("a", encoding="utf-8") as fh:
                        fh.write(traceback.format_exc() + "\n")
                except Exception:
                    pass
            finally:
                self.bus.finished.emit()

        threading.Thread(target=work, daemon=True).start()

    def on_finished(self) -> None:
        self.busy = False
        self.btn_search.setEnabled(True)
        self.btn_upload.setEnabled(True)

    def refresh_accounts(self) -> None:
        st = self.st
        self.vk_status.setText(f"✔ вход выполнен, групп: {len(vk_group_list(st))}"
                               if st.get("vk_logged") else "вход не выполнен")
        self.yt_status.setText("✔ " + ", ".join(c["name"] for c in st["yt_channels"])
                               if st["yt_channels"] else "каналы не подключены")
        checked = {self.dest_list.item(i).data(QtCore.Qt.UserRole)["key"]
                   for i in range(self.dest_list.count())
                   if self.dest_list.item(i).checkState() == QtCore.Qt.Checked}
        self.dest_list.clear()
        for d in destinations(st):
            it = QtWidgets.QListWidgetItem(d["label"])
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if d["key"] in checked else QtCore.Qt.Unchecked)
            it.setData(QtCore.Qt.UserRole, d)
            self.dest_list.addItem(it)
        self.no_dest.setVisible(self.dest_list.count() <= 1)

    # ---------- аккаунты
    def do_vk_login(self) -> None:
        def fn():
            self.bus.log.emit("Открываю ВК. Войди в аккаунт и закрой окно браузера.")
            login_site(self.st, VK_VIDEO)
            self.st["vk_logged"] = True
            save_settings(self.st)
            self.bus.log.emit("ВК: вход сохранён.")
            self.bus.accounts.emit()
        self.run_bg("Вход во ВК", fn)

    def do_yt_connect(self) -> None:
        if not self.st["yt_client_secret"] or not Path(self.st["yt_client_secret"]).is_file():
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Выбери файл-ключ Google (client_secret….json)", str(Path.home()),
                "JSON (*.json)")
            if not path:
                return
            keep = DATA / "google_client.json"
            keep.write_bytes(Path(path).read_bytes())
            self.st["yt_client_secret"] = str(keep)

        def fn():
            self.bus.log.emit("Открываю браузер для входа в Google…")
            name = yt_connect(self.st)
            self.bus.log.emit(f"YouTube подключён: {name}")
            self.bus.accounts.emit()
        self.run_bg("Подключение YouTube", fn)

    def do_yt_clear(self) -> None:
        self.st["yt_channels"] = []
        self.st["yt_client_secret"] = ""
        save_settings(self.st)
        self.refresh_accounts()

    def do_site_login(self, name: str, url: str) -> None:
        def fn():
            self.bus.log.emit(f"Открываю {name}. Войди в аккаунт и закрой окно браузера.")
            login_site(self.st, url)
            self.bus.log.emit(f"{name}: вход сохранён.")
        self.run_bg(f"Вход в {name}", fn)

    # ---------- поиск
    def start_search(self) -> None:
        q = self.query.text().strip()
        chosen = [k for k, cb in self.src_boxes.items() if cb.isChecked()]
        if not q or not chosen:
            self.log("Впиши запрос и отметь хотя бы один источник.")
            return
        n, short = self.count.value(), self.only_short.isChecked()

        def fn():
            found, seen = [], set()
            for key in chosen:
                self.bus.log.emit(f"Ищу в {SOURCE_NAMES[key]}: {q}")
                try:
                    items = SEARCHERS[key](self.st, q, n)
                except Exception as e:
                    self.bus.log.emit(f"{SOURCE_NAMES[key]}: не получилось — {e}")
                    continue
                if short:
                    items = [i for i in items if not i["duration"] or i["duration"] <= 180]
                items = [i for i in items if (i["source"], i["id"]) not in seen]
                seen |= {(i["source"], i["id"]) for i in items}
                self.bus.log.emit(f"{SOURCE_NAMES[key]}: найдено {len(items)}")
                found += items
            self.bus.results.emit(found)
        self.run_bg("Поиск", fn)

    def show_results(self, items: list) -> None:
        self.items = items
        self.table.setRowCount(len(items))
        for r, it in enumerate(items):
            chk = QtWidgets.QTableWidgetItem()
            chk.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            chk.setCheckState(QtCore.Qt.Unchecked)
            self.table.setItem(r, 0, chk)
            done = self.posted.any(f"{it['source']}:{it['id']}")
            cells = [SOURCE_NAMES[it["source"]], it["author"], it["title"].replace("\n", " "),
                     fmt_dur(it["duration"]), fmt_num(it["views"]),
                     "уже заливалось" if done else ""]
            for c, text in enumerate(cells, start=1):
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(text))
        self.log(f"Всего в списке: {len(items)}")

    def check_all(self, on: bool) -> None:
        for r in range(self.table.rowCount()):
            self.table.item(r, 0).setCheckState(QtCore.Qt.Checked if on else QtCore.Qt.Unchecked)

    def set_row_status(self, key: str, text: str) -> None:
        for r, it in enumerate(self.items):
            if f"{it['source']}:{it['id']}" == key:
                self.table.item(r, 6).setText(text)

    # ---------- заливка
    def start_upload(self) -> None:
        picked = [self.items[r] for r in range(self.table.rowCount())
                  if self.table.item(r, 0).checkState() == QtCore.Qt.Checked]
        dests = [self.dest_list.item(i).data(QtCore.Qt.UserRole)
                 for i in range(self.dest_list.count())
                 if self.dest_list.item(i).checkState() == QtCore.Qt.Checked]
        if not picked or not dests:
            self.log("Отметь галочками видео и хотя бы одну площадку справа.")
            return
        ask = QtWidgets.QMessageBox.question(
            self, "Залить?", f"Видео: {len(picked)}\nПлощадок: {len(dests)}\n\n"
            "Заливай только свои ролики и те, на которые есть разрешение автора.\nПродолжить?")
        if ask != QtWidgets.QMessageBox.Yes:
            return

        def fn():
            ok = 0
            for n, it in enumerate(picked, 1):
                key = f"{it['source']}:{it['id']}"
                todo = [d for d in dests if not self.posted.has(key, d["key"])]
                if not todo:
                    self.bus.row_status.emit(key, "уже заливалось")
                    continue
                self.bus.row_status.emit(key, "скачиваю…")
                self.bus.log.emit(f"[{n}/{len(picked)}] Скачиваю: {it['url']}")
                file = None
                try:
                    file = download(self.st, it)
                    title, desc = make_texts(self.st, it)
                    marks = []
                    for d in todo:
                        self.bus.row_status.emit(key, f"заливаю: {d['label']}")
                        try:
                            if d["kind"] == "vk":
                                link = vk_web_upload(self.st, file, title, desc, d["screen"],
                                                     self.bus.log.emit)
                            elif d["kind"] == "yt":
                                link = yt_upload(d["channel"], file, title, desc)
                            else:
                                link = save_to_folder(self.st, file, title, it)
                            self.posted.add(key, d["key"], link)
                            self.bus.log.emit(f"   ✔ {d['label']}: {link}")
                            marks.append("✔")
                            ok += 1
                        except Exception as e:
                            self.bus.log.emit(f"   ✖ {d['label']}: {e}")
                            marks.append("✖")
                    self.bus.row_status.emit(
                        key, "залито" if "✖" not in marks else
                        "ошибка" if "✔" not in marks else "залито частично")
                except Exception as e:
                    self.bus.row_status.emit(key, "не скачалось")
                    self.bus.log.emit(f"   ✖ не скачалось: {e}")
                finally:
                    if file and file.exists():
                        file.unlink()
                if n < len(picked) and self.st["pause_sec"]:
                    time.sleep(self.st["pause_sec"] * random.uniform(0.7, 1.3))
            self.bus.log.emit(f"Готово. Успешных заливок: {ok}")
        self.run_bg("Заливка", fn)


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    win = Main()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
