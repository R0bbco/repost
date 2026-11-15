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

VERSION = "1.6"
APP_FILES = ["repost_app.py", "README.md", "requirements.txt", "start.bat"]
APP_DIR = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "RepostApp"
DATA.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA / "settings.json"
COOKIES_FILE = DATA / "cookies.txt"

VK_VIDEO = "https://vkvideo.ru"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube.readonly"]
SOURCES = [("youtube", "YouTube"), ("tiktok", "TikTok"), ("vk", "ВКонтакте"),
           ("reddit", "Reddit"), ("instagram", "Instagram")]
# источники, которые добавляются только вручную (ссылкой или файлом), без поиска
SOURCE_NAMES = dict(SOURCES) | {"link": "Ссылка", "file": "Файл"}
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RepostApp/1.0"

DEFAULTS = {
    "proxy": "", "show_browser": True, "add_credit": True, "pause_sec": 20,
    "vk_logged": False, "vk_groups": "", "vk_manual_publish": False,
    "out_dir": str(Path.home() / "Videos" / "Repost"),
    "work_dir": "", "update_repo": "", "auto_update": True,
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


def work_dir(st: dict) -> Path:
    """Папка для временных файлов: ролик лежит там только пока заливается или смотрится."""
    d = Path(st.get("work_dir") or DATA / "downloads")
    d.mkdir(parents=True, exist_ok=True)
    return d


def clean_work_dir(st: dict) -> int:
    n = 0
    for f in work_dir(st).glob("*"):
        if f.is_file() and f.suffix.lower() in (".mp4", ".webm", ".mkv", ".part", ".m4a",
                                                 ".ytdl", ".mov", ".temp"):
            try:
                f.unlink()
                n += 1
            except Exception:
                pass
    return n


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


# ================================================================ браузер (TikTok, вход во ВК)

def save_cookies(ctx) -> None:
    """Сохраняет сессии TikTok / Reddit / ВК / Instagram в cookies.txt — их использует загрузчик."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in ctx.cookies():
        dom = c.get("domain", "")
        if not any(k in dom for k in ("tiktok", "reddit", "vk.com", "vk.ru", "vkvideo", "instagram")):
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


def query_stems(q: str) -> list[str]:
    """Слова запроса без окончаний: «рыбалка» найдёт и «рыбалку», и «рыбалке»."""
    out = []
    for w in re.findall(r"\w+", q.casefold()):
        out.append(w[:-2] if len(w) >= 6 else w[:-1] if len(w) == 5 else w)
    return out


def text_match(item: dict, stems: list[str]) -> bool:
    """Все слова запроса есть в названии или описании ролика (имя канала не считается)."""
    text = f"{item.get('title') or ''} {item.get('desc') or ''}".casefold()
    return all(s in text for s in stems)


def _cut(items: list[dict], n: int, ok) -> list[dict]:
    return [i for i in items if ok is None or ok(i)][:n]


def search_youtube(st: dict, q: str, n: int, ok=None) -> list[dict]:
    import yt_dlp
    m = n if ok is None else min(max(n * 5, 30), 300)     # с запасом: часть отсеется
    if q.startswith("@"):
        urls = [f"https://www.youtube.com/{q}/shorts", f"https://www.youtube.com/{q}/videos"]
    elif q.startswith("#"):
        urls = [f"https://www.youtube.com/hashtag/{quote(q[1:])}"]
    else:
        urls = [f"ytsearch{m}:{q}"]
    out, err = [], None
    for url in urls:
        try:
            with yt_dlp.YoutubeDL(ydl_opts(st) | {"extract_flat": True, "playlistend": m}) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            err = e
            continue
        for e in (info.get("entries") or [])[:m]:
            if not e or not e.get("id"):
                continue
            out.append({"source": "youtube", "id": e["id"],
                        "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
                        "author": e.get("channel") or e.get("uploader") or q.lstrip("@"),
                        "title": e.get("title") or "", "desc": e.get("description") or "",
                        "duration": _int(e.get("duration")),
                        "views": _int(e.get("view_count"))})
    if not out and err:
        raise err
    return _cut(out, n, ok)


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
            "desc": str(d.get("description") or ""),
            "duration": _int(d.get("duration")),
            "views": _int(views.get("count") if isinstance(views, dict) else views)}


def search_vk(st: dict, q: str, n: int, ok=None) -> list[dict]:
    if q.startswith("@"):
        raise RuntimeError("поиск по автору (@имя) для ВК не сделан — вставь ссылки на ролики")
    return _browser_search(st, f"{VK_VIDEO}/?q={quote(q.lstrip('#'))}", ("search", "Search"),
                           parse_vk, n, ok)


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
                    "desc": p.get("selftext") or "",
                    "duration": _int(rv.get("duration")), "views": _int(p.get("score"))})
    return out


def search_reddit(st: dict, q: str, n: int, ok=None) -> list[dict]:
    if q.startswith("@"):
        raise RuntimeError("поиск по автору (@имя) для Reddit не сделан")
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
        if len(_cut(out, n, ok)) >= n or not after:
            break
        time.sleep(1.5)
    return _cut(out, n, ok)


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


def parse_instagram(data, found: dict) -> None:
    """Достаёт видео из того, что страница Instagram сама загрузила (разбор терпимый)."""
    for d in _walk(data):
        code = d.get("code") or d.get("shortcode")
        if not isinstance(code, str) or not code:
            continue
        is_video = (d.get("media_type") == 2 or d.get("is_video") is True
                    or bool(d.get("video_versions")) or d.get("product_type") == "clips")
        if not is_video:
            continue
        cap = d.get("caption")
        text = cap.get("text") if isinstance(cap, dict) else cap if isinstance(cap, str) else None
        if not text:
            node = _find(d.get("edge_media_to_caption") or {}, "text")
            text = node if isinstance(node, str) else ""
        user = d.get("user") or d.get("owner") or {}
        views = next((d[k] for k in ("play_count", "ig_play_count", "video_view_count",
                                     "view_count") if d.get(k)), None)
        old = found.get(code) or {}
        found[code] = {"source": "instagram", "id": code,
                       "url": f"https://www.instagram.com/reel/{code}/",
                       "author": (user.get("username") if isinstance(user, dict) else "")
                                 or old.get("author") or "",
                       "title": (text or "").strip() or old.get("title") or "",
                       "duration": _int(d.get("video_duration")) or old.get("duration"),
                       "views": _int(views) or old.get("views")}


def _browser_search(st: dict, url: str, marks: tuple, parser, n: int, ok=None,
                    login_mark: str = "", login_hint: str = "") -> list[dict]:
    """Открывает страницу и собирает ролики из данных, которые она сама грузит."""
    found: dict[str, dict] = {}

    def on_response(resp):
        if any(m in resp.url for m in marks):
            try:
                parser(resp.json(), found)
            except Exception:
                pass

    def good() -> int:
        return len(_cut(list(found.values()), n, ok))

    with browser(st, headless=not st.get("show_browser", True)) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(2500)
        if login_mark and login_mark in page.url:
            raise RuntimeError(login_hint)
        try:                                 # первая порция часто лежит прямо в странице
            for text in page.evaluate(
                    "() => [...document.querySelectorAll('script[type=\"application/json\"]')]"
                    ".map(s => s.textContent)"):
                try:
                    parser(json.loads(text), found)
                except Exception:
                    pass
        except Exception:
            pass
        deadline, last, idle = time.time() + 120, 0, 0
        while good() < n and time.time() < deadline and idle < 12:
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)
            idle = idle + 1 if len(found) == last else 0
            last = len(found)
    return _cut(list(found.values()), n, ok)


def search_tiktok(st: dict, q: str, n: int, ok=None) -> list[dict]:
    if q.startswith("@"):
        url = f"https://www.tiktok.com/{q}"
    elif q.startswith("#"):
        url = f"https://www.tiktok.com/tag/{quote(q[1:])}"
    else:
        url = f"https://www.tiktok.com/search/video?q={quote(q)}"
    return _browser_search(st, url, ("/api/challenge/item_list", "/api/search/item/full",
                                     "/api/search/general/full", "/api/post/item_list"),
                           parse_tiktok, n, ok)


def search_instagram(st: dict, q: str, n: int, ok=None) -> list[dict]:
    """Instagram показывает поиск только вошедшим — работает после входа в окне программы."""
    if q.startswith("@"):
        url = f"https://www.instagram.com/{quote(q[1:])}/reels/"
    elif q.startswith("#"):
        url = f"https://www.instagram.com/explore/tags/{quote(q[1:])}/"
    else:
        url = f"https://www.instagram.com/explore/search/keyword/?q={quote(q)}"
    return _browser_search(
        st, url, ("/graphql", "/api/v1/"), parse_instagram, n, ok,
        login_mark="/accounts/login",
        login_hint="сначала войди в Instagram: «Аккаунты и настройки» → «Войти в Instagram»")


SEARCHERS = {"youtube": search_youtube, "tiktok": search_tiktok, "vk": search_vk,
             "reddit": search_reddit, "instagram": search_instagram}


# ================================================================ добавление вручную

def item_from_link(st: dict, url: str) -> dict:
    """Читает название, автора и длину ролика по ссылке (YouTube, TikTok, ВК, Reddit, Instagram…)."""
    import yt_dlp
    with yt_dlp.YoutubeDL(ydl_opts(st) | {"noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):
        info = next((e for e in info["entries"] if e), info)
    ext = (info.get("extractor_key") or info.get("extractor") or "").lower()
    source = next((k for k in ("youtube", "tiktok", "instagram", "reddit") if k in ext),
                  "vk" if ext.startswith("vk") else "link")
    return {"source": source, "id": str(info.get("id") or abs(hash(url))),
            "url": info.get("webpage_url") or url,
            "author": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
            "title": (info.get("description") if source in ("tiktok", "instagram") else None)
                     or info.get("title") or "",
            "duration": _int(info.get("duration")), "views": _int(info.get("view_count"))}


def item_from_file(path: str) -> dict:
    p = Path(path)
    return {"source": "file", "id": f"{p.name}_{p.stat().st_size}", "url": str(p),
            "local": str(p), "author": "", "title": p.stem, "duration": None, "views": None}


# ================================================================ скачивание

def download(st: dict, item: dict) -> Path:
    if item.get("local"):
        if not Path(item["local"]).is_file():
            raise FileNotFoundError(f"файл не найден: {item['local']}")
        return Path(item["local"])
    import yt_dlp
    out_dir = work_dir(st)
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
    if st.get("add_credit", True) and not item.get("local"):
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


# ================================================================ просмотр

def preview_stream(st: dict, item: dict) -> str:
    """Прямая ссылка на поток для проигрывания без скачивания."""
    import yt_dlp
    with yt_dlp.YoutubeDL(ydl_opts(st) | {
            "format": "b[ext=mp4][height<=720]/b[height<=720]/b"}) as ydl:
        info = ydl.extract_info(item["url"], download=False)
    if info.get("entries"):
        info = info["entries"][0]
    url = info.get("url")
    if not url:
        raise RuntimeError("нет прямой ссылки")
    return url


def preview_file(st: dict, item: dict) -> Path:
    """Запасной вариант: маленькая копия во временной папке, удаляется после просмотра."""
    import yt_dlp
    opts = ydl_opts(st) | {"format": "b[height<=480]/bv*[height<=480]+ba/b",
                           "merge_output_format": "mp4",
                           "outtmpl": str(work_dir(st) / "preview_%(id)s.%(ext)s")}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(item["url"], download=True)
        if info.get("entries"):
            info = info["entries"][0]
        rd = info.get("requested_downloads") or []
        return Path(rd[0]["filepath"]) if rd else Path(ydl.prepare_filename(info))


# ================================================================ обновления

def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def check_update(st: dict) -> str | None:
    """Новая версия из GitHub-репозитория (владелец/репозиторий) или None."""
    repo = (st.get("update_repo") or "").strip().strip("/")
    repo = re.sub(r"^https?://github\.com/", "", repo)
    if not repo:
        return None
    r = requests.get(f"https://raw.githubusercontent.com/{repo}/main/version.txt",
                     proxies=proxies(st), timeout=20, headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    remote = r.text.strip()
    return remote if _ver(remote) > _ver(VERSION) else None


def _install(files: dict[str, bytes]) -> None:
    """Проверяет и подменяет файлы программы; старые сохраняются в папку backup."""
    if "repost_app.py" not in files:
        raise RuntimeError("в обновлении нет repost_app.py")
    compile(files["repost_app.py"], "repost_app.py", "exec")      # битый файл не ставим
    backup = DATA / "backup"
    backup.mkdir(exist_ok=True)
    for name, data in files.items():
        cur = APP_DIR / name
        if cur.is_file():
            shutil.copy2(cur, backup / name)
        tmp = cur.with_suffix(cur.suffix + ".new")
        tmp.write_bytes(data)
        os.replace(tmp, cur)


def download_update(st: dict) -> None:
    repo = re.sub(r"^https?://github\.com/", "", st["update_repo"].strip().strip("/"))
    files = {}
    for name in APP_FILES:
        r = requests.get(f"https://raw.githubusercontent.com/{repo}/main/{name}",
                         proxies=proxies(st), timeout=60, headers={"Cache-Control": "no-cache"})
        if r.status_code == 404 and name != "repost_app.py":
            continue
        r.raise_for_status()
        files[name] = r.content
    _install(files)


def install_from_zip(path: str) -> None:
    import zipfile
    files = {}
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/").split("/")[-1]
            if name in APP_FILES:
                files[name] = z.read(info)
    _install(files)


def restart_app() -> None:
    bat = APP_DIR / "start.bat"
    if os.name == "nt" and bat.is_file():
        subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(APP_DIR),
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    else:
        subprocess.Popen([sys.executable, str(APP_DIR / "repost_app.py")], cwd=str(APP_DIR))
    QtWidgets.QApplication.quit()


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
    preview = QtCore.Signal(str, str)        # (источник: "url" | "file" | "error", значение)
    update_found = QtCore.Signal(str)
    added = QtCore.Signal(list)


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
        self.bus.preview.connect(self.on_preview)
        self.bus.update_found.connect(self.on_update_found)
        self.bus.added.connect(self.add_items)
        self.preview_item: dict | None = None
        self.preview_tmp: Path | None = None

        self.setWindowTitle("Репостер")
        self.resize(1280, 780)
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self.build_main_tab(), "Поиск и заливка")
        tabs.addTab(self.build_accounts_tab(), "Аккаунты и настройки")
        tabs.addTab(self.build_help_tab(), "Помощь")
        self.setCentralWidget(tabs)
        self.refresh_accounts()
        self.setWindowTitle(f"Репостер {VERSION}")
        self.log(f"Версия {VERSION}. Данные приложения: {DATA}")
        n = clean_work_dir(self.st)
        if n:
            self.log(f"Удалено временных файлов от прошлого запуска: {n}")
        threading.Thread(target=update_ytdlp, args=(self.bus.log.emit,), daemon=True).start()
        if self.st.get("auto_update") and self.st.get("update_repo"):
            threading.Thread(target=self._check_update_bg, args=(False,), daemon=True).start()

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
        lv.addWidget(QtWidgets.QLabel("Слова, #хэштег или @автор:"))
        self.query = QtWidgets.QLineEdit()
        self.query.setPlaceholderText("#рецепты, смешные коты или @имя_канала")
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
        self.strict = QtWidgets.QCheckBox("Слова должны быть в названии\nили описании ролика")
        self.strict.setChecked(True)
        self.strict.setToolTip("Отсеивает ролики, которые нашлись только по имени канала.\n"
                               "Если находит слишком мало — сними галочку.")
        lv.addWidget(self.strict)
        self.btn_search = QtWidgets.QPushButton("Найти")
        self.btn_search.clicked.connect(self.start_search)
        lv.addWidget(self.btn_search)
        lv.addSpacing(14)
        lab = QtWidgets.QLabel("Или добавить без поиска:")
        lv.addWidget(lab)
        b = QtWidgets.QPushButton("Вставить ссылки…")
        b.setToolTip("Ссылки на ролики из Instagram, TikTok, YouTube, ВК, Reddit и других сайтов")
        b.clicked.connect(self.add_links)
        lv.addWidget(b)
        b = QtWidgets.QPushButton("Добавить файлы с компьютера…")
        b.clicked.connect(self.add_files)
        lv.addWidget(b)
        self.keep_list = QtWidgets.QCheckBox("Новый поиск не очищает список")
        lv.addWidget(self.keep_list)
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
        self.table.cellDoubleClicked.connect(lambda r, c: self.start_preview(r))
        mv.addWidget(self.table, 3)
        mv.addWidget(self.build_player(), 2)
        row = QtWidgets.QHBoxLayout()
        for text, fn in (("Отметить все", lambda: self.check_all(True)),
                         ("Снять отметки", lambda: self.check_all(False))):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addWidget(QtWidgets.QLabel("Двойной щелчок по строке — посмотреть"))
        row.addStretch(1)
        mv.addLayout(row)

        right = QtWidgets.QGroupBox("3. Куда залить")
        rv = QtWidgets.QVBoxLayout(right)
        self.dest_list = QtWidgets.QListWidget()
        rv.addWidget(self.dest_list)
        self.no_dest = QtWidgets.QLabel("ВК и YouTube подключаются\nна вкладке «Аккаунты и настройки».")
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

    def build_player(self) -> QtWidgets.QWidget:
        from PySide6 import QtMultimedia, QtMultimediaWidgets
        box = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        self.video = QtMultimediaWidgets.QVideoWidget()
        self.video.setMinimumSize(200, 180)
        self.video.setStyleSheet("background:#111")
        self.player = QtMultimedia.QMediaPlayer(self)
        self.audio = QtMultimedia.QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.setLoops(QtMultimedia.QMediaPlayer.Loops.Infinite)
        self.player.errorOccurred.connect(self.on_player_error)
        lay.addWidget(self.video, 1)
        side = QtWidgets.QVBoxLayout()
        self.preview_title = QtWidgets.QLabel("Двойной щелчок по видео в списке —\nи оно покажется здесь")
        self.preview_title.setWordWrap(True)
        self.preview_title.setMaximumWidth(260)
        side.addWidget(self.preview_title)
        for text, fn in (("▶ / ❚❚", self.toggle_play), ("Звук вкл/выкл", self.toggle_mute),
                         ("Открыть в браузере", self.open_in_browser),
                         ("Отметить для заливки", self.check_previewed)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            side.addWidget(b)
        side.addStretch(1)
        lay.addLayout(side)
        return box

    # ---------- просмотр
    def start_preview(self, row: int) -> None:
        it = self.items[row]
        self.preview_item = it
        self.stop_preview()
        self.preview_title.setText(f"{SOURCE_NAMES[it['source']]} · {it['author']}\n"
                                   f"{it['title'][:120]}\n\nзагружаю…")
        if it.get("local"):
            self.preview_kind = "local"
            self.preview_title.setText(f"Файл\n{it['title'][:120]}")
            self.player.setSource(QtCore.QUrl.fromLocalFile(it["local"]))
            self.player.play()
            return

        def work():
            try:
                self.bus.preview.emit("url", preview_stream(self.st, it))
            except Exception as e:
                self.bus.log.emit(f"Просмотр потоком не вышел ({e}), пробую через временный файл…")
                self._preview_fallback(it)
        threading.Thread(target=work, daemon=True).start()

    def _preview_fallback(self, it: dict) -> None:
        try:
            self.bus.preview.emit("file", str(preview_file(self.st, it)))
        except Exception as e:
            self.bus.preview.emit("error", str(e))

    def on_preview(self, kind: str, value: str) -> None:
        it = self.preview_item
        if not it:
            return
        head = f"{SOURCE_NAMES[it['source']]} · {it['author']}\n{it['title'][:120]}"
        if kind == "error":
            self.preview_title.setText(head + "\n\nне удалось показать — открой в браузере")
            self.log(f"Просмотр: {value}")
            return
        if kind == "file":
            self.preview_tmp = Path(value)
            self.player.setSource(QtCore.QUrl.fromLocalFile(value))
        else:
            self.preview_tmp = None
            self.player.setSource(QtCore.QUrl(value))
        self.preview_kind = kind
        self.preview_title.setText(head)
        self.player.play()

    def on_player_error(self, err, text: str = "") -> None:
        it = self.preview_item
        if not it or err == self.player.Error.NoError:
            return
        if getattr(self, "preview_kind", "") == "url":
            self.preview_kind = "fallback"
            self.log("Плеер не смог открыть поток, скачиваю маленькую копию для просмотра…")
            threading.Thread(target=self._preview_fallback, args=(it,), daemon=True).start()
        else:
            self.bus.preview.emit("error", text or str(err))

    def stop_preview(self) -> None:
        self.player.stop()
        self.player.setSource(QtCore.QUrl())
        if self.preview_tmp:
            try:
                self.preview_tmp.unlink(missing_ok=True)
            except Exception:
                pass
            self.preview_tmp = None

    def toggle_play(self) -> None:
        if self.player.playbackState() == self.player.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def toggle_mute(self) -> None:
        self.audio.setMuted(not self.audio.isMuted())

    def open_in_browser(self) -> None:
        if self.preview_item:
            webbrowser.open(self.preview_item["url"])

    def check_previewed(self) -> None:
        for r, it in enumerate(self.items):
            if it is self.preview_item:
                self.table.item(r, 0).setCheckState(QtCore.Qt.Checked)

    def closeEvent(self, ev) -> None:
        self.stop_preview()
        super().closeEvent(ev)

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

        lay = box("TikTok, Instagram, Reddit — только поиск",
                  "Откроется окно браузера: войди в аккаунт как обычно и закрой окно — приложение "
                  "запомнит вход. TikTok по хэштегам часто работает и без входа. "
                  "Instagram без входа поиск не показывает совсем. Reddit входа не требует.")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(button("Войти в TikTok", lambda: self.do_site_login(
            "TikTok", "https://www.tiktok.com/login")))
        row.addWidget(button("Войти в Instagram", lambda: self.do_site_login(
            "Instagram", "https://www.instagram.com/accounts/login/")))
        row.addStretch(1)
        lay.addLayout(row)

        lay = box("Интернет",
                  "TikTok, YouTube и Instagram из России работают только через VPN. Проще всего включить VPN "
                  "на компьютере. Поле «Прокси» заполняй, только если у тебя именно прокси, а не VPN.")
        form = QtWidgets.QFormLayout()
        self.proxy = QtWidgets.QLineEdit(self.st["proxy"])
        self.proxy.setPlaceholderText("пусто  или  socks5://логин:пароль@адрес:порт")
        form.addRow("Прокси:", self.proxy)
        self.show_browser = QtWidgets.QCheckBox(
            "Показывать окно браузера при поиске в TikTok и ВК (надёжнее, можно пройти капчу)")
        self.show_browser.setChecked(self.st["show_browser"])
        form.addRow(self.show_browser)
        self.pause = QtWidgets.QSpinBox()
        self.pause.setRange(0, 600)
        self.pause.setValue(self.st["pause_sec"])
        form.addRow("Пауза между заливками, сек:", self.pause)
        lay.addLayout(form)
        lay.addWidget(button("Сохранить настройки", self.save_form))

        lay = box("Папки",
                  "Временная папка — куда видео ненадолго скачивается перед заливкой; после заливки "
                  "файл сразу удаляется. Можно указать другой диск или флешку, чтобы не занимать "
                  "системный. Папка «Скачать» — для площадки «Папка на компьютере», там видео "
                  "остаются насовсем.")
        grid = QtWidgets.QGridLayout()
        self.work_label = QtWidgets.QLabel()
        self.out_label = QtWidgets.QLabel(self.st["out_dir"])
        for r, (name, lab, fn) in enumerate((
                ("Временная:", self.work_label, self.pick_work_dir),
                ("Скачать:", self.out_label, self.pick_folder))):
            grid.addWidget(QtWidgets.QLabel(name), r, 0)
            grid.addWidget(lab, r, 1)
            grid.addWidget(button("Выбрать…", fn), r, 2)
            grid.addWidget(button("Открыть", lambda _=False, l=lab: self.open_folder(
                l.property("path") or l.text())), r, 3)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self.update_work_label()

        lay = box("Обновления",
                  "Программа сама проверяет обновления при запуске и предлагает установить. "
                  "Источник — GitHub-репозиторий (как его завести — «Помощь», раздел «Обновления»). "
                  "Если обновление пришло архивом в чате — нажми «Установить из архива».")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(f"Версия {VERSION}.  Репозиторий:"))
        self.update_repo = QtWidgets.QLineEdit(self.st["update_repo"])
        self.update_repo.setPlaceholderText("логин/repost  или  https://github.com/логин/repost")
        row.addWidget(self.update_repo, 1)
        row.addWidget(button("Проверить сейчас", lambda: self.check_update_now()))
        row.addWidget(button("Установить из архива…", self.update_from_zip))
        lay.addLayout(row)
        v.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        return scroll

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
                       add_credit=self.credit.isChecked(),
                       update_repo=self.update_repo.text().strip())
        save_settings(self.st)
        self.refresh_accounts()

    def pick_folder(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Куда сохранять видео",
                                                          self.st["out_dir"])
        if path:
            self.st["out_dir"] = path
            self.out_label.setText(path)
            save_settings(self.st)

    def open_folder(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)
        else:
            webbrowser.open(Path(path).as_uri())

    def update_work_label(self) -> None:
        d = work_dir(self.st)
        self.work_label.setProperty("path", str(d))
        try:
            free = shutil.disk_usage(d).free / 1024 ** 3
            self.work_label.setText(f"{d}   (свободно {free:.0f} ГБ)")
        except Exception:
            self.work_label.setText(str(d))

    def pick_work_dir(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Временная папка",
                                                          str(work_dir(self.st)))
        if path:
            clean_work_dir(self.st)
            self.st["work_dir"] = path
            save_settings(self.st)
            self.update_work_label()

    # ---------- обновления
    def _check_update_bg(self, loud: bool) -> None:
        try:
            new = check_update(self.st)
            if new:
                self.bus.update_found.emit(new)
            elif loud:
                self.bus.log.emit(f"Обновлений нет, у тебя последняя версия ({VERSION}).")
        except Exception as e:
            self.bus.log.emit(f"Не удалось проверить обновления: {e}")

    def check_update_now(self) -> None:
        self.save_form()
        if not self.st["update_repo"]:
            self.log("Впиши адрес репозитория с обновлениями.")
            return
        threading.Thread(target=self._check_update_bg, args=(True,), daemon=True).start()

    def on_update_found(self, new: str) -> None:
        if self.busy:
            self.log(f"Есть обновление {new} — установлю, когда закончится текущее действие. "
                     "Нажми «Проверить сейчас» позже.")
            return
        ask = QtWidgets.QMessageBox.question(
            self, "Обновление", f"Вышла версия {new} (у тебя {VERSION}).\nУстановить сейчас? "
            "Программа перезапустится.")
        if ask != QtWidgets.QMessageBox.Yes:
            return
        try:
            download_update(self.st)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Обновление", f"Не получилось: {e}")
            return
        self.stop_preview()
        restart_app()

    def update_from_zip(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Архив с обновлением (Repost.zip)", str(Path.home() / "Downloads"),
            "Архив (*.zip)")
        if not path:
            return
        try:
            install_from_zip(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Обновление", f"Не получилось: {e}")
            return
        QtWidgets.QMessageBox.information(self, "Обновление",
                                          "Установлено. Программа перезапустится.")
        self.stop_preview()
        restart_app()

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
        strict, stems = self.strict.isChecked(), query_stems(q)
        by_author = q.startswith("@")

        def make_ok(key):
            """Проверка ролика: длина и слова запроса в названии/описании."""
            words = (strict and not by_author and stems
                     and not (q.startswith("#") and key in ("youtube", "tiktok", "instagram")))
            if not short and not words:
                return None
            return lambda i: ((not short or not i["duration"] or i["duration"] <= 180)
                              and (not words or text_match(i, stems)))

        def fn():
            found, seen = [], set()
            for key in chosen:
                self.bus.log.emit(f"Ищу в {SOURCE_NAMES[key]}: {q}")
                try:
                    items = SEARCHERS[key](self.st, q, n, make_ok(key))
                except Exception as e:
                    self.bus.log.emit(f"{SOURCE_NAMES[key]}: не получилось — {e}")
                    continue
                items = [i for i in items if (i["source"], i["id"]) not in seen]
                seen |= {(i["source"], i["id"]) for i in items}
                self.bus.log.emit(f"{SOURCE_NAMES[key]}: найдено {len(items)}")
                found += items
            self.bus.results.emit(found)
        self.run_bg("Поиск", fn)

    def add_links(self) -> None:
        text, ok = QtWidgets.QInputDialog.getMultiLineText(
            self, "Вставить ссылки", "Ссылки на видео, по одной в строке:")
        urls = list(dict.fromkeys(re.findall(r"https?://\S+", text or "")))
        if not ok or not urls:
            return

        def fn():
            new = []
            for n, url in enumerate(urls, 1):
                try:
                    new.append(item_from_link(self.st, url))
                    self.bus.log.emit(f"[{n}/{len(urls)}] добавлено: {new[-1]['title'][:60] or url}")
                except Exception as e:
                    msg = str(e)
                    if "login" in msg.lower() or "cookies" in msg.lower():
                        msg = "сайт отдаёт это видео только после входа в аккаунт"
                    self.bus.log.emit(f"[{n}/{len(urls)}] не получилось ({url}): {msg[:200]}")
            self.bus.added.emit(new)
        self.run_bg("Ссылки", fn)

    def add_files(self) -> None:
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Видео с компьютера", self.st["out_dir"],
            "Видео (" + " ".join("*" + e for e in VIDEO_EXT) + ");;Все файлы (*)")
        if paths:
            self.add_items([item_from_file(p) for p in paths])

    def add_items(self, new: list) -> None:
        """Дописывает ролики в конец списка, сохраняя отметки; новые сразу отмечены."""
        have = {f"{i['source']}:{i['id']}" for i in self.items}
        new = [i for i in new if f"{i['source']}:{i['id']}" not in have]
        marks = {f"{it['source']}:{it['id']}" for r, it in enumerate(self.items)
                 if self.table.item(r, 0).checkState() == QtCore.Qt.Checked}
        marks |= {f"{i['source']}:{i['id']}" for i in new}
        self.show_results(self.items + new, marks)

    def show_results(self, items: list, marks: set | None = None) -> None:
        if marks is None and self.keep_list.isChecked() and self.items:
            return self.add_items_unmarked(items)
        marks = marks or set()
        self.items = items
        self.table.setRowCount(len(items))
        for r, it in enumerate(items):
            chk = QtWidgets.QTableWidgetItem()
            chk.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            chk.setCheckState(QtCore.Qt.Checked if f"{it['source']}:{it['id']}" in marks
                              else QtCore.Qt.Unchecked)
            self.table.setItem(r, 0, chk)
            done = self.posted.any(f"{it['source']}:{it['id']}")
            title = it["title"]
            if it.get("desc") and it["desc"].strip() not in title:
                title = f"{title} · {it['desc'].strip()[:200]}"
            cells = [SOURCE_NAMES[it["source"]], it["author"], title.replace("\n", " "),
                     fmt_dur(it["duration"]), fmt_num(it["views"]),
                     "уже заливалось" if done else ""]
            for c, text in enumerate(cells, start=1):
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(text))
        self.log(f"Всего в списке: {len(items)}")

    def add_items_unmarked(self, new: list) -> None:
        have = {f"{i['source']}:{i['id']}" for i in self.items}
        marks = {f"{it['source']}:{it['id']}" for r, it in enumerate(self.items)
                 if self.table.item(r, 0).checkState() == QtCore.Qt.Checked}
        self.show_results(self.items + [i for i in new
                                        if f"{i['source']}:{i['id']}" not in have], marks)

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
                    if file and file.exists() and not it.get("local"):
                        file.unlink()
                if n < len(picked) and self.st["pause_sec"]:
                    time.sleep(self.st["pause_sec"] * random.uniform(0.7, 1.3))
            self.bus.log.emit(f"Готово. Успешных заливок: {ok}")
        self.run_bg("Заливка", fn)


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    try:
        win = Main()
    except Exception:
        err = traceback.format_exc()
        (DATA / "crash.log").write_text(err, encoding="utf-8")
        QtWidgets.QMessageBox.critical(
            None, "Репостер не запустился",
            "Ошибка при запуске. Пришли разработчику текст ниже "
            f"(он также сохранён в {DATA / 'crash.log'}):\n\n" + err[-1500:])
        sys.exit(1)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
