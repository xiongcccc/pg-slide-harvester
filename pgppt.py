#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen


def default_root() -> Path:
    configured = os.environ.get("PGSH_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    module_root = Path(__file__).resolve().parent
    if (module_root / "config").exists():
        return module_root
    cwd = Path.cwd()
    if (cwd / "config").exists():
        return cwd
    return module_root


ROOT = default_root()
DB_PATH = ROOT / "data" / "pgppt.sqlite"
CATEGORIES_PATH = ROOT / "config" / "categories.json"
SOURCES_PATH = ROOT / "config" / "sources.json"
ASSET_EXTENSIONS = {".pdf", ".ppt", ".pptx", ".odp"}
NON_SLIDE_ASSET_KEYWORDS = {
    "brochure",
    "sponsor",
    "sponsorship",
    "prospectus",
    "ticket",
    "registration",
    "cfp",
    "call-for",
    "book-of-abstracts",
    "abstracts",
    "code-of-conduct",
    "terms",
    "invoice",
    "badge",
}
DISCOVERY_PAGE_KEYWORDS = {
    "schedule",
    "agenda",
    "program",
    "programme",
    "session",
    "sessions",
    "talk",
    "talks",
    "presentation",
    "presentations",
    "slides",
}
WINDOWS_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}
GENERIC_ASSET_LABELS = {
    "directory tree",
    "download",
    "download presentation",
    "download slides",
    "download the slides",
    "presentation",
    "presentations",
    "скачать презентацию",
    "slides",
    "slides pdf",
    "view slides",
}
TITLE_NOISE_PATTERNS = (
    "discord icon",
    "linkedin icon",
    "mastodon icon",
    "microsoft logo",
    "play icon",
    "talk bubbles",
    "x icon",
    "bsky icon",
    "elephant icon",
)
UNCATEGORIZED_TOPIC = "uncategorized"
NON_ENGLISH_ARCHIVE_ROOT = "non-english"
BLOCK_TEXT_TAGS = {"p", "li", "h1", "h2", "h3"}
ACTIVE_RUN_ID: int | None = None


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def slugify(value: str, fallback: str = "untitled") -> str:
    value = unquote(value or "").strip()
    value = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", value)
    value = re.sub(r"[_]+", "-", value)
    value = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff.-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-.")
    return value[:120] or fallback


def readable_text(value: str | None) -> str:
    value = html.unescape(unquote(value or "")).strip()
    suffix = Path(value).suffix.lower()
    if suffix in ASSET_EXTENSIONS:
        value = value[: -len(suffix)]
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def safe_filename_stem(value: str | None, fallback: str = "untitled", max_length: int = 160) -> str:
    value = readable_text(value)
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    value = re.sub(r'[<>:"/\\|?*]+', " ", value)
    value = re.sub(r"(?<=[A-Za-z0-9])[-_]+(?=[A-Za-z0-9])", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-_")
    if not value:
        value = fallback
    if value.upper() in WINDOWS_RESERVED_FILENAMES:
        value = f"{value} slides"
    if len(value) > max_length:
        value = value[:max_length].rstrip(" .-_")
    return value or fallback


def title_has_noise(value: str) -> bool:
    lowered = value.lower()
    return any(pattern in lowered for pattern in TITLE_NOISE_PATTERNS)


def normalized_asset_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() == "github.com":
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) >= 5 and parts[2] == "blob":
            owner, repo, _blob, branch = parts[:4]
            path = "/".join(parts[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    return url


def asset_title_from_context(base_title: str, label: str, asset_url: str, asset_count: int) -> str:
    base = readable_text(base_title) or infer_session_title(asset_url)
    base_lower = base.lower()
    if asset_count <= 1:
        inferred = readable_text(infer_session_title(asset_url))
        if base_lower in GENERIC_ASSET_LABELS or title_has_noise(base):
            return inferred or base
        return base

    label_text = readable_text(label)
    inferred = readable_text(infer_session_title(asset_url))
    if base_lower in GENERIC_ASSET_LABELS or title_has_noise(base):
        return inferred or label_text or base
    extra = label_text
    if not extra or extra.lower() in GENERIC_ASSET_LABELS:
        extra = inferred
    if not extra or extra.lower() == base.lower() or extra.lower() in base.lower():
        return base
    return f"{base} - {extra}"


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def connect() -> sqlite3.Connection:
    ROOT.joinpath("data").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists events (
            id integer primary key,
            name text not null,
            slug text not null unique,
            year integer,
            source_url text,
            website_url text,
            status text not null default 'discovered',
            last_checked_at text,
            next_check_at text,
            check_count integer not null default 0,
            created_at text not null,
            updated_at text not null
        );

        create table if not exists sessions (
            id integer primary key,
            event_id integer not null references events(id) on delete cascade,
            title text not null,
            slug text not null,
            speakers text,
            abstract text,
            session_url text,
            asset_status text not null default 'missing',
            last_checked_at text,
            next_check_at text,
            check_count integer not null default 0,
            created_at text not null,
            updated_at text not null,
            unique(event_id, slug)
        );

        create table if not exists assets (
            id integer primary key,
            session_id integer not null references sessions(id) on delete cascade,
            file_url text not null,
            local_path text not null,
            file_type text not null,
            sha256 text not null,
            size_bytes integer not null,
            downloaded_at text not null,
            created_at text not null,
            unique(file_url),
            unique(sha256)
        );

        create table if not exists asset_sources (
            file_url text primary key,
            asset_id integer not null references assets(id) on delete cascade,
            first_seen_at text not null,
            last_seen_at text not null,
            last_status text not null
        );

        create table if not exists tags (
            id integer primary key,
            slug text not null unique,
            label text not null
        );

        create table if not exists session_tags (
            session_id integer not null references sessions(id) on delete cascade,
            tag_id integer not null references tags(id) on delete cascade,
            confidence real not null,
            reason text,
            primary key(session_id, tag_id)
        );

        create table if not exists crawl_runs (
            id integer primary key,
            command text not null,
            started_at text not null,
            finished_at text,
            status text not null,
            notes text
        );

        create table if not exists run_assets (
            run_id integer not null references crawl_runs(id) on delete cascade,
            asset_id integer not null references assets(id) on delete cascade,
            action text not null,
            source_url text,
            message text,
            created_at text not null,
            primary key(run_id, asset_id, action)
        );

        create table if not exists run_sessions (
            run_id integer not null references crawl_runs(id) on delete cascade,
            session_id integer not null references sessions(id) on delete cascade,
            status text not null,
            message text,
            source_url text,
            created_at text not null,
            primary key(run_id, session_id, status)
        );
        """
    )
    ensure_column(conn, "run_assets", "source_url", "text")
    ensure_column(conn, "run_sessions", "source_url", "text")
    conn.execute(
        """
        insert or ignore into asset_sources(file_url, asset_id, first_seen_at, last_seen_at, last_status)
        select file_url, id, created_at, downloaded_at, 'backfilled'
        from assets
        """
    )
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, spec: str) -> None:
    existing = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
    if column not in existing:
        conn.execute(f"alter table {table} add column {column} {spec}")


def ensure_tags(conn: sqlite3.Connection) -> None:
    categories = load_json(CATEGORIES_PATH, {})
    for slug, item in categories.items():
        conn.execute(
            """
            insert into tags(slug, label)
            values(?, ?)
            on conflict(slug) do update set label = excluded.label
            """,
            (slug, item.get("label", slug)),
        )
    conn.commit()


def begin_run(conn: sqlite3.Connection, command: str) -> int:
    cur = conn.execute(
        "insert into crawl_runs(command, started_at, status) values(?, ?, 'running')",
        (command, utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, notes: str = "") -> None:
    conn.execute(
        "update crawl_runs set finished_at = ?, status = ?, notes = ? where id = ?",
        (utcnow(), status, notes, run_id),
    )
    conn.commit()


def record_run_asset(
    conn: sqlite3.Connection,
    asset_id: int | None,
    action: str,
    message: str = "",
    source_url: str | None = None,
    run_id: int | None = None,
) -> None:
    run_id = run_id if run_id is not None else ACTIVE_RUN_ID
    if run_id is None or asset_id is None:
        return
    conn.execute(
        """
        insert into run_assets(run_id, asset_id, action, source_url, message, created_at)
        values(?, ?, ?, ?, ?, ?)
        on conflict(run_id, asset_id, action) do update set
            source_url = coalesce(excluded.source_url, run_assets.source_url),
            message = excluded.message,
            created_at = excluded.created_at
        """,
        (run_id, asset_id, action, source_url, message, utcnow()),
    )
    conn.commit()


def record_asset_source(conn: sqlite3.Connection, asset_id: int | None, file_url: str, status: str) -> None:
    if asset_id is None:
        return
    now = utcnow()
    conn.execute(
        """
        insert into asset_sources(file_url, asset_id, first_seen_at, last_seen_at, last_status)
        values(?, ?, ?, ?, ?)
        on conflict(file_url) do update set
            asset_id = excluded.asset_id,
            last_seen_at = excluded.last_seen_at,
            last_status = excluded.last_status
        """,
        (file_url, asset_id, now, now, status),
    )
    conn.commit()


def record_run_session(
    conn: sqlite3.Connection,
    session_id: int | None,
    status: str,
    message: str = "",
    source_url: str | None = None,
    run_id: int | None = None,
) -> None:
    run_id = run_id if run_id is not None else ACTIVE_RUN_ID
    if run_id is None or session_id is None:
        return
    conn.execute(
        """
        insert into run_sessions(run_id, session_id, status, message, source_url, created_at)
        values(?, ?, ?, ?, ?, ?)
        on conflict(run_id, session_id, status) do update set
            message = excluded.message,
            source_url = coalesce(excluded.source_url, run_sessions.source_url),
            created_at = excluded.created_at
        """,
        (run_id, session_id, status, message, source_url, utcnow()),
    )
    conn.commit()


def upsert_event(
    conn: sqlite3.Connection,
    name: str,
    source_url: str | None = None,
    website_url: str | None = None,
) -> int:
    now = utcnow()
    slug = slugify(name)
    year_match = re.search(r"(20\d{2})", name)
    year = int(year_match.group(1)) if year_match else None
    conn.execute(
        """
        insert into events(name, slug, year, source_url, website_url, created_at, updated_at)
        values(?, ?, ?, ?, ?, ?, ?)
        on conflict(slug) do update set
            source_url = coalesce(excluded.source_url, events.source_url),
            website_url = coalesce(excluded.website_url, events.website_url),
            updated_at = excluded.updated_at
        """,
        (name, slug, year, source_url, website_url, now, now),
    )
    row = conn.execute("select id from events where slug = ?", (slug,)).fetchone()
    conn.commit()
    return int(row["id"])


def upsert_session(
    conn: sqlite3.Connection,
    event_id: int,
    title: str,
    session_url: str | None = None,
    asset_status: str = "missing",
    speakers: str | None = None,
    abstract: str | None = None,
) -> int:
    now = utcnow()
    slug = slugify(title)
    next_check = compute_next_check(asset_status, 0)
    conn.execute(
        """
        insert into sessions(
            event_id, title, slug, session_url, asset_status,
            next_check_at, created_at, updated_at
        )
        values(?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(event_id, slug) do update set
            session_url = coalesce(excluded.session_url, sessions.session_url),
            speakers = coalesce(excluded.speakers, sessions.speakers),
            abstract = coalesce(excluded.abstract, sessions.abstract),
            asset_status = case
                when sessions.asset_status = 'downloaded' then sessions.asset_status
                else excluded.asset_status
            end,
            updated_at = excluded.updated_at
        """,
        (event_id, title, slug, session_url, asset_status, next_check, now, now),
    )
    if speakers or abstract:
        conn.execute(
            """
            update sessions
            set speakers = coalesce(?, speakers),
                abstract = coalesce(?, abstract),
                updated_at = ?
            where event_id = ? and slug = ?
            """,
            (speakers, abstract, now, event_id, slug),
        )
    row = conn.execute(
        "select id from sessions where event_id = ? and slug = ?", (event_id, slug)
    ).fetchone()
    conn.commit()
    return int(row["id"])


def is_asset_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in ASSET_EXTENSIONS)


def is_probably_slide_asset(url: str, label: str = "") -> bool:
    if not is_asset_url(url) and not is_asset_url(label):
        return False
    text = f"{unquote(url)} {label}".lower()
    return not any(keyword in text for keyword in NON_SLIDE_ASSET_KEYWORDS)


def infer_event_name(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if "events" in parts:
        idx = parts.index("events")
        if idx + 1 < len(parts):
            return humanize_event_slug(parts[idx + 1])
    host = parsed.netloc.replace("www.", "")
    return host


def humanize_event_slug(value: str) -> str:
    value = unquote(value).strip("/")
    match = re.match(r"pgconfdev(20\d{2})", value, re.IGNORECASE)
    if match:
        return f"PGConf.dev {match.group(1)}"
    match = re.match(r"pgconfeu(20\d{2})", value, re.IGNORECASE)
    if match:
        return f"PGConf.EU {match.group(1)}"
    spaced = re.sub(r"(?i)(20\d{2})", r" \1", value)
    return slugify(spaced).replace("-", " ").title()


def infer_session_title(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if is_asset_url(url):
        filename = Path(parts[-1]).name if parts else "slides"
        return slugify(filename).replace("-", " ")
    if "session" in parts:
        idx = parts.index("session")
        if idx + 2 < len(parts):
            return slugify(parts[idx + 2]).replace("-", " ")
    return slugify(parts[-1] if parts else parsed.netloc).replace("-", " ")


def configured_positive_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(1, parsed)


def request_url(url: str, timeout: int | None = None, retries: int | None = None):
    timeout = timeout if timeout is not None else configured_positive_int("PGSH_REQUEST_TIMEOUT", 15)
    retries = retries if retries is not None else configured_positive_int("PGSH_REQUEST_RETRIES", 2)
    sources = load_json(SOURCES_PATH, {})
    headers = {"User-Agent": sources.get("default_user_agent", "pgppt-harvester/0.1")}
    parsed = urlparse(url)
    safe_path = quote(parsed.path, safe="/%:@&=+$,;~")
    safe_url = parsed._replace(path=safe_path).geturl()
    last_error = None
    for attempt in range(retries):
        req = Request(safe_url, headers=headers)
        try:
            return urlopen(req, timeout=timeout)
        except URLError as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1)
    if last_error:
        raise last_error
    return urlopen(Request(safe_url, headers=headers), timeout=timeout)


def guess_extension(url: str, content_type: str | None = None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return guessed
    return ".bin"


def archive_asset_dir(topic_slug: str, language_slug: str | None = None) -> Path:
    topic = slugify(topic_slug, UNCATEGORIZED_TOPIC)
    if language_slug:
        language = slugify(language_slug, language_slug)
        return ROOT / "archive" / NON_ENGLISH_ARCHIVE_ROOT / language / topic
    return ROOT / "archive" / topic


def contains_range(value: str, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(start <= ord(char) <= end for char in value for start, end in ranges)


def non_english_language_slug(parts: Iterable[str]) -> str | None:
    text = " ".join(readable_text(part) for part in parts if part)
    lowered = text.lower()
    if not lowered:
        return None
    if contains_range(text, ((0x4E00, 0x9FFF),)):
        return "chinese"
    if contains_range(text, ((0x3040, 0x30FF),)):
        return "japanese"
    if contains_range(text, ((0xAC00, 0xD7AF),)):
        return "korean"
    if contains_range(text, ((0x0400, 0x04FF), (0x0500, 0x052F))):
        return "russian"
    if contains_range(text, ((0x0600, 0x06FF),)):
        return "arabic"
    if contains_range(text, ((0x0370, 0x03FF),)):
        return "greek"
    if contains_range(text, ((0x0590, 0x05FF),)):
        return "hebrew"

    keyword_groups = (
        ("russian", ("russia", "russian", "moscow", "piter", "saint petersburg", "pgbootcamp/russia")),
        ("japanese", ("japan", "japanese", "tokyo", "postgresql conference japan")),
        ("chinese", ("china", "chinese", "beijing", "shanghai", "taiwan", "hong kong")),
        ("korean", ("korea", "korean", "seoul")),
        ("spanish", ("spanish", "espanol", "español", "spain", "latam")),
        ("french", ("french", "france", "francais", "français")),
        ("german", ("german", "deutsch", "germany")),
        ("portuguese", ("portuguese", "portugues", "português", "brasil", "brazil")),
    )
    for slug, keywords in keyword_groups:
        if any(keyword in lowered for keyword in keywords):
            return slug
    return None


def asset_language_slug(
    event_name: str | None,
    session_title: str | None,
    asset_title: str | None,
    file_url: str | None,
) -> str | None:
    return non_english_language_slug(
        [
            event_name or "",
            session_title or "",
            asset_title or "",
            unquote(urlparse(file_url or "").path),
            file_url or "",
        ]
    )


def primary_topic_slug(conn: sqlite3.Connection, session_id: int) -> str:
    row = conn.execute(
        """
        select t.slug
        from session_tags st
        join tags t on t.id = st.tag_id
        where st.session_id = ?
        order by st.confidence desc, t.slug
        limit 1
        """,
        (session_id,),
    ).fetchone()
    return row["slug"] if row else UNCATEGORIZED_TOPIC


def best_topic_slug_from_parts(parts: Iterable[str]) -> str:
    text = " ".join(readable_text(part) for part in parts if part).lower()
    if not text:
        return UNCATEGORIZED_TOPIC
    categories = load_json(CATEGORIES_PATH, {})
    best_slug = UNCATEGORIZED_TOPIC
    best_score = 0
    for slug, item in categories.items():
        score = sum(1 for keyword in item.get("keywords", []) if keyword_matches(text, keyword))
        if score > best_score:
            best_slug = slug
            best_score = score
    return best_slug if best_score else UNCATEGORIZED_TOPIC


def asset_topic_slug(
    conn: sqlite3.Connection,
    session_id: int,
    asset_title: str,
    file_url: str,
) -> str:
    primary = best_topic_slug_from_parts([asset_title, infer_session_title(file_url)])
    if primary != UNCATEGORIZED_TOPIC:
        return primary
    row = conn.execute(
        "select title, abstract from sessions where id = ?",
        (session_id,),
    ).fetchone()
    parts: list[str] = []
    if row:
        session_title = readable_text(row["title"] or "")
        abstract = readable_text(row["abstract"] or "")
        if session_title.lower() not in GENERIC_ASSET_LABELS and not title_has_noise(session_title):
            parts.append(session_title)
        if abstract.lower() not in GENERIC_ASSET_LABELS and not title_has_noise(abstract):
            parts.append(abstract)
    return best_topic_slug_from_parts(parts)


def unique_asset_path(dest_dir: Path, filename_stem: str, ext: str) -> Path:
    dest = dest_dir / f"{filename_stem}{ext}"
    counter = 2
    while dest.exists():
        dest = dest_dir / f"{filename_stem}-{counter}{ext}"
        counter += 1
    return dest


def generic_title(value: str) -> bool:
    normalized = readable_text(value).lower()
    return normalized in GENERIC_ASSET_LABELS or title_has_noise(normalized)


def preferred_asset_stem(local_path: str, file_url: str, session_title: str) -> str:
    local_stem = safe_filename_stem(Path(local_path or "").stem, fallback="")
    inferred = safe_filename_stem(infer_session_title(file_url or ""), fallback="")
    session_stem = safe_filename_stem(session_title, fallback="")

    if local_stem.lower().startswith("directory tree - "):
        return safe_filename_stem(local_stem.split(" - ", 1)[1], fallback=local_stem)
    if generic_title(local_stem) and inferred:
        return inferred
    if title_has_noise(local_stem) and inferred:
        return inferred
    if session_stem.lower() in GENERIC_ASSET_LABELS and inferred:
        return inferred
    return local_stem or inferred or session_stem or "untitled"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def asset_content_error(path: Path, ext: str, content_type: str) -> str | None:
    lowered_type = (content_type or "").lower()
    if any(marker in lowered_type for marker in ("text/html", "application/xhtml", "text/plain")):
        return f"unexpected asset content-type={content_type or 'unknown'}"
    with path.open("rb") as f:
        header = f.read(8)
    lowered_ext = ext.lower()
    if lowered_ext == ".pdf" and not header.startswith(b"%PDF"):
        return "invalid pdf header"
    if lowered_ext in {".pptx", ".odp"} and not header.startswith(b"PK"):
        return f"invalid {lowered_ext.lstrip('.')} header"
    if lowered_ext == ".ppt" and not (
        header.startswith(b"\xd0\xcf\x11\xe0") or header.startswith(b"PK")
    ):
        return "invalid ppt header"
    return None


def download_asset(
    conn: sqlite3.Connection,
    session_id: int,
    url: str,
    event_name: str,
    session_title: str,
) -> tuple[bool, str]:
    url = normalized_asset_url(url)
    classify_session(conn, session_id)
    existing_asset_id: int | None = None
    existing = conn.execute("select id, local_path, session_id from assets where file_url = ?", (url,)).fetchone()
    if existing:
        existing_asset_id = int(existing["id"])
        local_path = ROOT / existing["local_path"]
        if local_path.exists():
            same_session = int(existing["session_id"]) == session_id
            status = "downloaded" if same_session else "duplicate_content"
            mark_session_checked(
                conn,
                session_id,
                status,
                message=f"{status}: {existing['local_path']}",
                source_url=url,
            )
            classify_session(conn, session_id)
            action = "already_exists" if same_session else "duplicate_content"
            record_asset_source(conn, existing_asset_id, url, action)
            if same_session:
                record_run_asset(conn, existing_asset_id, action, existing["local_path"], source_url=url)
            return False, f"{action}: {existing['local_path']}"
        if int(existing["session_id"]) != session_id:
            conn.execute("update assets set session_id = ? where id = ?", (session_id, existing_asset_id))
            conn.commit()

    try:
        with request_url(url) as resp:
            content_type = resp.headers.get("Content-Type", "")
            ext = guess_extension(url, content_type)
            if ext.lower() not in ASSET_EXTENSIONS and "pdf" not in content_type.lower():
                return False, f"skipped non-slide asset content-type={content_type}"
            topic_slug = asset_topic_slug(conn, session_id, session_title, url)
            language_slug = asset_language_slug(event_name, session_title, session_title, url)
            topic_dir = archive_asset_dir(topic_slug, language_slug)
            topic_dir.mkdir(parents=True, exist_ok=True)
            filename_stem = safe_filename_stem(session_title)
            dest = unique_asset_path(topic_dir, filename_stem, ext)
            with tempfile.NamedTemporaryFile(delete=False, dir=str(topic_dir)) as tmp:
                tmp_path = Path(tmp.name)
                shutil.copyfileobj(resp, tmp)
            if error := asset_content_error(tmp_path, ext, content_type):
                tmp_path.unlink(missing_ok=True)
                mark_session_checked(conn, session_id, "failed", message=error, source_url=url)
                return False, error
        tmp_path.replace(dest)
    except HTTPError as exc:
        mark_session_checked(conn, session_id, "login_required" if exc.code in (401, 403) else "failed")
        return False, f"http error {exc.code}: {exc.reason}"
    except URLError as exc:
        mark_session_checked(conn, session_id, "failed")
        return False, f"url error: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        mark_session_checked(conn, session_id, "failed")
        return False, f"download failed: {exc}"

    digest = sha256_file(dest)
    size = dest.stat().st_size
    file_type = dest.suffix.lower().lstrip(".")
    now = utcnow()
    try:
        if existing_asset_id is not None:
            conn.execute(
                """
                update assets
                set session_id = ?,
                    local_path = ?,
                    file_type = ?,
                    sha256 = ?,
                    size_bytes = ?,
                    downloaded_at = ?
                where id = ?
                """,
                (session_id, str(dest.relative_to(ROOT)), file_type, digest, size, now, existing_asset_id),
            )
        else:
            conn.execute(
                """
                insert into assets(file_url, local_path, file_type, sha256, size_bytes, downloaded_at, created_at, session_id)
                values(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (url, str(dest.relative_to(ROOT)), file_type, digest, size, now, now, session_id),
            )
            existing_asset_id = int(conn.execute("select last_insert_rowid() as id").fetchone()["id"])
    except sqlite3.IntegrityError:
        dest.unlink(missing_ok=True)
        row = conn.execute("select id, local_path, session_id from assets where sha256 = ?", (digest,)).fetchone()
        same_session = bool(row and int(row["session_id"]) == session_id)
        status = "downloaded" if same_session else "duplicate_content"
        duplicate_path = row["local_path"] if row else digest
        mark_session_checked(
            conn,
            session_id,
            status,
            message=f"duplicate content: {duplicate_path}",
            source_url=url,
        )
        classify_session(conn, session_id)
        if row:
            record_asset_source(conn, int(row["id"]), url, "duplicate_content")
            if same_session:
                record_run_asset(conn, int(row["id"]), "duplicate_content", row["local_path"], source_url=url)
        return False, f"duplicate content: {row['local_path'] if row else digest}"

    mark_session_checked(conn, session_id, "downloaded")
    classify_session(conn, session_id)
    record_asset_source(conn, existing_asset_id, url, "downloaded")
    record_run_asset(conn, existing_asset_id, "downloaded", str(dest.relative_to(ROOT)), source_url=url)
    conn.commit()
    return True, f"downloaded: {dest.relative_to(ROOT)}"


def compute_next_check(status: str, check_count: int) -> str:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    if status in {"downloaded", "found"} and check_count < 7:
        delta = dt.timedelta(days=1)
    elif status in {"missing", "partial_assets"} and check_count < 7:
        delta = dt.timedelta(days=1)
    elif check_count < 15:
        delta = dt.timedelta(days=3)
    elif check_count < 30:
        delta = dt.timedelta(days=7)
    else:
        delta = dt.timedelta(days=30)
    return (now + delta).isoformat()


def mark_session_checked(
    conn: sqlite3.Connection,
    session_id: int,
    status: str,
    message: str = "",
    source_url: str | None = None,
) -> None:
    row = conn.execute("select check_count from sessions where id = ?", (session_id,)).fetchone()
    check_count = int(row["check_count"] if row else 0) + 1
    conn.execute(
        """
        update sessions
        set asset_status = ?, last_checked_at = ?, next_check_at = ?, check_count = ?, updated_at = ?
        where id = ?
        """,
        (status, utcnow(), compute_next_check(status, check_count), check_count, utcnow(), session_id),
    )
    conn.commit()
    record_run_session(conn, session_id, status, message=message, source_url=source_url)


def keyword_matches(text: str, keyword: str) -> bool:
    keyword = keyword.lower().strip()
    if not keyword:
        return False
    if re.search(r"[a-z0-9]", keyword):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(keyword) + r"(?![A-Za-z0-9])"
        return bool(re.search(pattern, text))
    return keyword in text


def asset_signal_parts(conn: sqlite3.Connection, session_id: int) -> list[str]:
    rows = conn.execute(
        """
        select local_path, file_url
        from assets
        where session_id = ?
        """,
        (session_id,),
    ).fetchall()
    parts: list[str] = []
    for row in rows:
        local_name = Path(row["local_path"] or "").stem
        if local_name:
            parts.append(local_name)
        inferred = infer_session_title(row["file_url"] or "")
        if inferred:
            parts.append(inferred)
    return parts


def classify_session(conn: sqlite3.Connection, session_id: int) -> None:
    row = conn.execute(
        """
        select s.title, s.abstract
        from sessions s
        where s.id = ?
        """,
        (session_id,),
    ).fetchone()
    if not row:
        return
    conn.execute("delete from session_tags where session_id = ?", (session_id,))
    title_text = (row["title"] or "").lower()
    abstract_text = (row["abstract"] or "").lower()
    asset_text = " ".join(asset_signal_parts(conn, session_id)).lower()
    categories = load_json(CATEGORIES_PATH, {})
    for slug, item in categories.items():
        abstract_hits = []
        title_hits = []
        asset_hits = []
        for keyword in item.get("keywords", []):
            if keyword_matches(abstract_text, keyword):
                abstract_hits.append(keyword)
            elif keyword_matches(title_text, keyword):
                title_hits.append(keyword)
            elif keyword_matches(asset_text, keyword):
                asset_hits.append(keyword)
        hits = abstract_hits + title_hits + asset_hits
        if not hits:
            continue
        tag = conn.execute("select id from tags where slug = ?", (slug,)).fetchone()
        if not tag:
            continue
        confidence = min(
            1.0,
            0.35 + len(abstract_hits) * 0.22 + len(title_hits) * 0.1 + len(asset_hits) * 0.08,
        )
        reason_parts = []
        if abstract_hits:
            reason_parts.append("abstract: " + ", ".join(abstract_hits[:6]))
        if title_hits:
            reason_parts.append("title: " + ", ".join(title_hits[:6]))
        if asset_hits:
            reason_parts.append("asset: " + ", ".join(asset_hits[:6]))
        conn.execute(
            """
            insert into session_tags(session_id, tag_id, confidence, reason)
            values(?, ?, ?, ?)
            on conflict(session_id, tag_id) do update set
                confidence = excluded.confidence,
                reason = excluded.reason
            """,
            (session_id, int(tag["id"]), confidence, "; ".join(reason_parts)),
        )
    conn.commit()


def classify_all(conn: sqlite3.Connection) -> int:
    conn.execute("delete from session_tags")
    rows = conn.execute("select id from sessions").fetchall()
    for row in rows:
        classify_session(conn, int(row["id"]))
    conn.commit()
    return len(rows)


class LinkParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self.meta_description = ""
        self.text_blocks: list[str] = []
        self._current_href: str | None = None
        self._text_parts: list[str] = []
        self._in_title = False
        self._block_depth = 0
        self._block_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        attr_map = {key.lower(): value for key, value in attrs}
        if tag == "meta":
            name = (attr_map.get("name") or attr_map.get("property") or "").lower()
            if name in {"description", "og:description", "twitter:description"}:
                self.meta_description = readable_text(attr_map.get("content") or self.meta_description)
        if tag == "title":
            self._in_title = True
        if tag in BLOCK_TEXT_TAGS:
            if self._block_depth == 0:
                self._block_parts = []
            self._block_depth += 1
        if tag != "a":
            return
        href = attr_map.get("href")
        if href:
            self._current_href = urljoin(self.base_url, href)
            self._text_parts = []

    def handle_data(self, data: str):
        if self._current_href:
            self._text_parts.append(data)
        if self._in_title:
            self.title = readable_text(f"{self.title} {data}")
        if self._block_depth:
            self._block_parts.append(data)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in BLOCK_TEXT_TAGS and self._block_depth:
            self._block_depth -= 1
            if self._block_depth == 0:
                block = readable_text(" ".join(self._block_parts))
                if block:
                    self.text_blocks.append(block)
                self._block_parts = []
        if tag == "a" and self._current_href:
            self.links.append((self._current_href, html.unescape(" ".join(self._text_parts)).strip()))
            self._current_href = None
            self._text_parts = []


def summarize_text_blocks(blocks: list[str], max_length: int = 1800) -> str:
    summary_parts: list[str] = []
    for block in blocks:
        block = readable_text(block)
        if not block or block.lower() in GENERIC_ASSET_LABELS:
            continue
        summary_parts.append(block)
        if len(" ".join(summary_parts)) >= max_length:
            break
    return " ".join(summary_parts)[:max_length].strip()


def parse_html_page(page_url: str, text: str) -> dict[str, object]:
    parser = LinkParser(page_url)
    parser.feed(text)
    abstract = parser.meta_description or summarize_text_blocks(parser.text_blocks)
    return {"links": parser.links, "title": parser.title, "abstract": abstract}


def extract_page_info(page_url: str) -> dict[str, object]:
    with request_url(page_url) as resp:
        content = resp.read()
    text = content.decode("utf-8", errors="replace")
    return parse_html_page(page_url, text)


def extract_asset_links(page_url: str) -> list[tuple[str, str]]:
    info = extract_page_info(page_url)
    return [(url, label) for url, label in info["links"] if is_asset_url(url)]


def extract_links(page_url: str) -> list[tuple[str, str]]:
    info = extract_page_info(page_url)
    return list(info["links"])


def read_url_text(url: str) -> str:
    with request_url(url) as resp:
        content = resp.read()
    return content.decode("utf-8", errors="replace")


def discover_pgevents_sessions(sessions_url: str) -> list[tuple[str, str]]:
    """Return (session_url, title) pairs from a pgevents.ca sessions listing."""
    parsed_source = urlparse(sessions_url)
    path_parts = [part for part in parsed_source.path.split("/") if part]
    event_slug = path_parts[path_parts.index("events") + 1] if "events" in path_parts and path_parts.index("events") + 1 < len(path_parts) else ""
    links = extract_links(sessions_url)
    seen: set[str] = set()
    sessions: list[tuple[str, str]] = []
    for url, label in links:
        parsed = urlparse(url)
        if parsed.netloc != parsed_source.netloc:
            continue
        pattern = rf"^/events/{re.escape(event_slug)}/sessions/session/\d+-[^/]+/?$"
        if not re.match(pattern, parsed.path):
            continue
        normalized = parsed._replace(query="", fragment="").geturl()
        if normalized in seen:
            continue
        seen.add(normalized)
        title = re.sub(r"\s+", " ", label).strip() or infer_session_title(normalized)
        sessions.append((normalized, title))
    return sessions


def json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def discover_indico_contributions(timetable_url: str) -> list[dict[str, object]]:
    """Extract public slide attachments from an Indico timetable page."""
    text = read_url_text(timetable_url)
    chunks = re.split(r'(?="_type":"ContribSchEntry")', text)
    contributions: list[dict[str, object]] = []
    for chunk in chunks:
        if '"_type":"ContribSchEntry"' not in chunk:
            continue
        title_match = re.search(r'"title":"((?:\\.|[^"\\])*)","uniqueId"', chunk)
        if not title_match:
            title_matches = re.findall(r'"title":"((?:\\.|[^"\\])*)"', chunk)
            title_match = title_matches[-1] if title_matches else None
        if not title_match:
            continue
        raw_title = title_match.group(1) if hasattr(title_match, "group") else title_match
        title = html.unescape(json_string(raw_title))
        desc_match = re.search(r'"description":"((?:\\.|[^"\\])*)"', chunk)
        abstract = ""
        if desc_match:
            abstract = readable_text(re.sub(r"<[^>]+>", " ", html.unescape(json_string(desc_match.group(1)))))
        url_match = re.search(r'"url":"((?:\\.|[^"\\])*)"', chunk)
        session_url = urljoin(timetable_url, json_string(url_match.group(1))) if url_match else timetable_url
        assets = []
        for asset_match in re.finditer(
            r'"download_url":"((?:\\.|[^"\\])*)".{0,160}?"title":"((?:\\.|[^"\\])*)"',
            chunk,
            flags=re.DOTALL,
        ):
            asset_url = urljoin(timetable_url, json_string(asset_match.group(1)))
            asset_title = html.unescape(json_string(asset_match.group(2)))
            if is_asset_url(asset_url) or is_asset_url(asset_title):
                assets.append((asset_url, asset_title))
        contributions.append({"title": title, "session_url": session_url, "abstract": abstract, "assets": assets})
    return contributions


def crawl_indico(
    conn: sqlite3.Connection,
    timetable_url: str,
    event_name: str | None = None,
    delay_seconds: float = 0.5,
    limit: int | None = None,
) -> list[str]:
    event = event_name or infer_event_name(timetable_url)
    event_id = upsert_event(conn, event, source_url=timetable_url, website_url=timetable_url)
    contributions = discover_indico_contributions(timetable_url)
    if limit is not None:
        contributions = contributions[:limit]

    messages = [f"discovered contributions: {len(contributions)}"]
    downloaded = 0
    skipped = 0
    missing = 0
    failed = 0
    for contribution in contributions:
        title = str(contribution["title"])
        session_id = upsert_session(
            conn,
            event_id,
            title,
            session_url=str(contribution["session_url"]),
            asset_status="missing",
            abstract=str(contribution.get("abstract") or ""),
        )
        assets = list(contribution["assets"])
        if not assets:
            mark_session_checked(conn, session_id, "missing")
            missing += 1
            messages.append(f"WAIT {title}: no slide attachments yet")
            continue
        for asset_url, label in assets:
            asset_title = asset_title_from_context(title, label, asset_url, len(assets))
            ok, msg = download_asset(conn, session_id, asset_url, event, asset_title)
            if ok:
                downloaded += 1
                messages.append(f"OK {title}: {msg}")
            else:
                skipped += 1
                messages.append(f"SKIP {title}: {msg}")
        if delay_seconds:
            time.sleep(delay_seconds)

    messages.append(
        f"summary: downloaded={downloaded}, skipped={skipped}, missing={missing}, failed={failed}"
    )
    return messages


def wordpress_pages_api_url(site_url: str) -> str:
    parsed = urlparse(site_url)
    return f"{parsed.scheme}://{parsed.netloc}/wp-json/wp/v2/pages?per_page=100"


def discover_wordpress_pages(site_url: str) -> list[dict[str, str]]:
    api_url = wordpress_pages_api_url(site_url)
    with request_url(api_url) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    pages: list[dict[str, str]] = []
    if not isinstance(payload, list):
        return pages
    for page in payload:
        title = html.unescape(page.get("title", {}).get("rendered", "") or page.get("slug", ""))
        content = page.get("content", {}).get("rendered", "") or ""
        excerpt = page.get("excerpt", {}).get("rendered", "") or ""
        page_info = parse_html_page(page.get("link", site_url), html.unescape(content))
        pages.append(
            {
                "title": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title)).strip() or page.get("slug", "page"),
                "slug": page.get("slug", ""),
                "link": page.get("link", site_url),
                "content": html.unescape(content),
                "abstract": readable_text(re.sub(r"<[^>]+>", " ", html.unescape(excerpt))) or str(page_info["abstract"]),
            }
        )
    return pages


def page_is_discovery_candidate(page: dict[str, str]) -> bool:
    text = f"{page.get('slug', '')} {page.get('title', '')}".lower()
    return any(keyword in text for keyword in DISCOVERY_PAGE_KEYWORDS)


def elementor_widget_texts(content: str) -> list[str]:
    texts: list[str] = []
    for match in re.finditer(
        r'<div\b[^>]*data-widget_type="(?:html|text-editor|heading)\.default"[^>]*>.*?(?=<div\b[^>]*data-widget_type=|<div class="elementor-element|</body>|</html>|$)',
        content,
        flags=re.DOTALL,
    ):
        chunk = match.group(0)
        chunk = re.sub(r"<script.*?</script>", " ", chunk, flags=re.DOTALL | re.IGNORECASE)
        chunk = re.sub(r"<style.*?</style>", " ", chunk, flags=re.DOTALL | re.IGNORECASE)
        chunk = re.sub(r"<svg.*?</svg>", " ", chunk, flags=re.DOTALL | re.IGNORECASE)
        chunk = re.sub(r"<[^>]+>", " ", chunk)
        chunk = readable_text(re.sub(r'\bdata-widget_type="[^"]+">', " ", chunk))
        if chunk:
            texts.append(chunk)
    return texts


def schedule_text_is_session_title(text: str) -> bool:
    normalized = readable_text(text)
    if not normalized or len(normalized) < 8:
        return False
    lowered = normalized.lower()
    if re.search(r"\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b", lowered):
        return False
    blocked = {
        "time",
        "tentative schedule",
        "hyderabad pg days 2026",
        "hall - 1",
        "hall - 2",
        "hall - 3",
        "hall 1",
        "hall 2",
        "hall 3",
        "tea break",
        "break/networking",
        "group photograph | lunch break",
        "closing remarks",
        "evening reception",
    }
    if lowered in blocked:
        return False
    if any(keyword in lowered for keyword in ("lunch", "welcome & opening", "training - lunch")):
        return False
    return any(
        keyword in lowered
        for keyword in (
            "postgres",
            "postgresql",
            "oracle",
            "vacuum",
            "replication",
            "dba",
            "sql",
            "vector",
            "database",
            "lightning talks",
            "keynote",
        )
    )


def discover_wordpress_schedule_sessions(page: dict[str, str]) -> list[dict[str, str]]:
    text = f"{page.get('slug', '')} {page.get('title', '')}".lower()
    if not any(keyword in text for keyword in ("schedule", "agenda", "program", "programme")):
        return []
    seen: set[str] = set()
    sessions: list[dict[str, str]] = []
    for item in elementor_widget_texts(page.get("content", "")):
        title = readable_text(item)
        if not schedule_text_is_session_title(title):
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        sessions.append(
            {
                "title": title,
                "session_url": f"{page['link']}#{slugify(title)}",
                "abstract": title,
            }
        )
    return sessions


def crawl_wordpress(
    conn: sqlite3.Connection,
    site_url: str,
    event_name: str | None = None,
    delay_seconds: float = 0.5,
    limit: int | None = None,
) -> list[str]:
    event = event_name or infer_event_name(site_url)
    event_id = upsert_event(conn, event, source_url=site_url, website_url=site_url)
    pages = discover_wordpress_pages(site_url)

    messages = [f"discovered wordpress pages: {len(pages)}"]
    downloaded = 0
    skipped = 0
    missing = 0
    failed = 0
    candidate_pages = 0

    def reached_limit() -> bool:
        return limit is not None and (downloaded + skipped + missing + failed) >= limit

    for page in pages:
        if reached_limit():
            break
        links = []
        try:
            parser = LinkParser(page["link"])
            parser.feed(page["content"])
            links = parser.links
        except Exception as exc:  # noqa: BLE001
            failed += 1
            messages.append(f"ERROR {page['title']}: page parse failed: {exc}")
            continue

        assets = [(url, label) for url, label in links if is_probably_slide_asset(url, label)]
        candidate = page_is_discovery_candidate(page) or bool(assets)
        if not candidate:
            continue
        candidate_pages += 1
        if not assets:
            schedule_sessions = discover_wordpress_schedule_sessions(page)
            if schedule_sessions:
                for session in schedule_sessions:
                    if reached_limit():
                        break
                    session_id = upsert_session(
                        conn,
                        event_id,
                        session["title"],
                        session_url=session["session_url"],
                        asset_status="missing",
                        abstract=session.get("abstract", ""),
                    )
                    mark_session_checked(conn, session_id, "missing")
                    missing += 1
                    messages.append(f"WAIT {session['title']}: no slides yet")
                continue

        session_id = upsert_session(
            conn,
            event_id,
            page["title"],
            session_url=page["link"],
            asset_status="missing",
            abstract=page.get("abstract", ""),
        )

        if not assets:
            mark_session_checked(conn, session_id, "missing")
            missing += 1
            messages.append(f"WAIT {page['title']}: no slide assets yet")
            continue

        for asset_url, label in assets:
            if reached_limit():
                break
            asset_title = asset_title_from_context(page["title"], label, asset_url, len(assets))
            ok, msg = download_asset(conn, session_id, asset_url, event, asset_title)
            if ok:
                downloaded += 1
                messages.append(f"OK {page['title']}: {msg}")
            else:
                skipped += 1
                messages.append(f"SKIP {page['title']}: {msg}")
        if delay_seconds:
            time.sleep(delay_seconds)

    messages.append(f"candidate pages: {candidate_pages}")
    messages.append(
        f"summary: downloaded={downloaded}, skipped={skipped}, missing={missing}, failed={failed}"
    )
    return messages


def link_looks_relevant(url: str, label: str = "") -> bool:
    text = f"{unquote(url)} {label}".lower()
    return any(keyword in text for keyword in DISCOVERY_PAGE_KEYWORDS) or is_probably_slide_asset(url, label)


def crawl_generic_site(
    conn: sqlite3.Connection,
    site_url: str,
    event_name: str | None = None,
    delay_seconds: float = 0.5,
    limit: int | None = None,
    max_pages: int = 25,
) -> list[str]:
    event = event_name or infer_event_name(site_url)
    event_id = upsert_event(conn, event, source_url=site_url, website_url=site_url)
    parsed_site = urlparse(site_url)
    root = f"{parsed_site.scheme}://{parsed_site.netloc}"
    queue = [site_url]
    seen: set[str] = set()
    pages: list[tuple[str, str]] = []

    while queue and len(pages) < max_pages:
        page_url = queue.pop(0)
        if page_url in seen:
            continue
        seen.add(page_url)
        try:
            links = extract_links(page_url)
        except Exception:
            continue
        label = infer_session_title(page_url)
        pages.append((page_url, label))
        for url, link_label in links:
            parsed = urlparse(url)
            normalized = parsed._replace(fragment="").geturl()
            if parsed.netloc != parsed_site.netloc:
                continue
            if normalized in seen or normalized in queue:
                continue
            if link_looks_relevant(normalized, link_label):
                queue.append(normalized)

    if limit is not None:
        pages = pages[:limit]

    messages = [f"generic pages scanned: {len(pages)}"]
    downloaded = 0
    skipped = 0
    missing = 0
    failed = 0
    candidate_pages = 0

    for page_url, title in pages:
        try:
            page_info = extract_page_info(page_url)
            links = list(page_info["links"])
        except Exception as exc:  # noqa: BLE001
            failed += 1
            messages.append(f"ERROR {title}: page scan failed: {exc}")
            continue
        title = str(page_info["title"] or title)
        assets = [(url, label) for url, label in links if is_probably_slide_asset(url, label)]
        candidate = link_looks_relevant(page_url, title) or bool(assets)
        if not candidate:
            continue
        candidate_pages += 1
        session_id = upsert_session(
            conn,
            event_id,
            title,
            session_url=page_url,
            asset_status="missing",
            abstract=str(page_info["abstract"] or ""),
        )
        if not assets:
            mark_session_checked(conn, session_id, "missing")
            missing += 1
            messages.append(f"WAIT {title}: no slide assets yet")
            continue
        for asset_url, label in assets:
            asset_title = asset_title_from_context(title, label, asset_url, len(assets))
            ok, msg = download_asset(conn, session_id, asset_url, event, asset_title)
            if ok:
                downloaded += 1
                messages.append(f"OK {title}: {msg}")
            else:
                skipped += 1
                messages.append(f"SKIP {title}: {msg}")
        if delay_seconds:
            time.sleep(delay_seconds)

    messages.append(f"candidate pages: {candidate_pages}")
    messages.append(
        f"summary: downloaded={downloaded}, skipped={skipped}, missing={missing}, failed={failed}"
    )
    return messages


def crawl_pgevents(
    conn: sqlite3.Connection,
    sessions_url: str,
    event_name: str | None = None,
    delay_seconds: float = 0.5,
    limit: int | None = None,
) -> list[str]:
    event = event_name or infer_event_name(sessions_url)
    event_id = upsert_event(conn, event, source_url=sessions_url, website_url=sessions_url)
    discovered = discover_pgevents_sessions(sessions_url)
    if limit is not None:
        discovered = discovered[:limit]

    messages = [f"discovered sessions: {len(discovered)}"]
    downloaded = 0
    skipped = 0
    missing = 0
    failed = 0

    for session_url, title in discovered:
        session_id = upsert_session(conn, event_id, title, session_url=session_url, asset_status="missing")
        try:
            page_info = extract_page_info(session_url)
            assets = [(url, label) for url, label in page_info["links"] if is_asset_url(url)]
            if page_info["abstract"]:
                upsert_session(
                    conn,
                    event_id,
                    title,
                    session_url=session_url,
                    asset_status="missing",
                    abstract=str(page_info["abstract"]),
                )
        except HTTPError as exc:
            status = "login_required" if exc.code in (401, 403) else "failed"
            mark_session_checked(conn, session_id, status)
            failed += 1
            messages.append(f"ERROR {title}: http error {exc.code}")
            continue
        except Exception as exc:  # noqa: BLE001
            mark_session_checked(conn, session_id, "failed")
            failed += 1
            messages.append(f"ERROR {title}: {exc}")
            continue

        if not assets:
            mark_session_checked(conn, session_id, "missing")
            missing += 1
            messages.append(f"WAIT {title}: no slides yet")
            if delay_seconds:
                time.sleep(delay_seconds)
            continue

        for asset_url, label in assets:
            asset_title = asset_title_from_context(title, label, asset_url, len(assets))
            ok, msg = download_asset(conn, session_id, asset_url, event, asset_title)
            if ok:
                downloaded += 1
                messages.append(f"OK {title}: {msg}")
            else:
                skipped += 1
                messages.append(f"SKIP {title}: {msg}")
        if delay_seconds:
            time.sleep(delay_seconds)

    messages.append(
        f"summary: downloaded={downloaded}, skipped={skipped}, missing={missing}, failed={failed}"
    )
    return messages


def discover_postgresql_eu_sessions(schedule_url: str) -> list[tuple[str, str]]:
    """Return (session_url, title) pairs from a PostgreSQL Europe schedule page."""
    parsed_schedule = urlparse(schedule_url)
    links = extract_links(schedule_url)
    seen: set[str] = set()
    sessions: list[tuple[str, str]] = []
    for url, label in links:
        parsed = urlparse(url)
        if parsed.netloc != parsed_schedule.netloc:
            continue
        if not re.search(r"/events/[^/]+/schedule/session/\d+", parsed.path):
            continue
        normalized = parsed._replace(query="", fragment="").geturl()
        if normalized in seen:
            continue
        seen.add(normalized)
        title = re.sub(r"\s+", " ", label).strip() or infer_session_title(normalized)
        sessions.append((normalized, title))
    return sessions


def crawl_postgresql_eu(
    conn: sqlite3.Connection,
    schedule_url: str,
    event_name: str | None = None,
    delay_seconds: float = 0.5,
    limit: int | None = None,
) -> list[str]:
    event = event_name or infer_event_name(schedule_url)
    event_id = upsert_event(conn, event, source_url=schedule_url, website_url=schedule_url)
    discovered = discover_postgresql_eu_sessions(schedule_url)
    if limit is not None:
        discovered = discovered[:limit]

    messages = [f"discovered postgresql.eu sessions: {len(discovered)}"]
    downloaded = 0
    skipped = 0
    missing = 0
    failed = 0

    for session_url, title in discovered:
        session_id = upsert_session(conn, event_id, title, session_url=session_url, asset_status="missing")
        try:
            page_info = extract_page_info(session_url)
            if page_info["abstract"]:
                session_id = upsert_session(
                    conn,
                    event_id,
                    title,
                    session_url=session_url,
                    asset_status="missing",
                    abstract=str(page_info["abstract"]),
                )
            assets = [(url, label) for url, label in page_info["links"] if is_probably_slide_asset(url, label)]
        except HTTPError as exc:
            status = "login_required" if exc.code in (401, 403) else "failed"
            mark_session_checked(conn, session_id, status)
            failed += 1
            messages.append(f"ERROR {title}: http error {exc.code}")
            continue
        except Exception as exc:  # noqa: BLE001
            mark_session_checked(conn, session_id, "failed")
            failed += 1
            messages.append(f"ERROR {title}: {exc}")
            continue

        if not assets:
            mark_session_checked(conn, session_id, "missing")
            missing += 1
            messages.append(f"WAIT {title}: no slides yet")
            if delay_seconds:
                time.sleep(delay_seconds)
            continue

        for asset_url, label in assets:
            asset_title = asset_title_from_context(title, label, asset_url, len(assets))
            ok, msg = download_asset(conn, session_id, asset_url, event, asset_title)
            if ok:
                downloaded += 1
                messages.append(f"OK {title}: {msg}")
            else:
                skipped += 1
                messages.append(f"SKIP {title}: {msg}")
        if delay_seconds:
            time.sleep(delay_seconds)

    messages.append(
        f"summary: downloaded={downloaded}, skipped={skipped}, missing={missing}, failed={failed}"
    )
    return messages


def eventyay_event_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host == "eventyay.com":
        match = re.match(r"^/(?:e|ev)/([^/]+)/?", parsed.path)
        if match:
            return f"{parsed.scheme}://{parsed.netloc}/ev/{match.group(1)}/"

    try:
        probe_url = url
        if host.endswith("summit.fossasia.org") and "pgday-sponsorship" in parsed.path:
            probe_url = f"{parsed.scheme}://{parsed.netloc}/"
        text = read_url_text(probe_url)
    except Exception:  # noqa: BLE001
        return None

    refresh_match = re.search(r'http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\'>]+)', text, flags=re.IGNORECASE)
    if refresh_match:
        refreshed = urljoin(probe_url, html.unescape(refresh_match.group(1)).strip())
        parsed_refreshed = urlparse(refreshed)
        if parsed_refreshed.netloc.lower() == "eventyay.com":
            return eventyay_event_url(refreshed)

    for link_url, _label in parse_html_page(probe_url, text)["links"]:
        parsed_link = urlparse(link_url)
        if parsed_link.netloc.lower() == "eventyay.com":
            resolved = eventyay_event_url(link_url)
            if resolved:
                return resolved
    return None


def extract_eventyay_schedule_data(event_url: str) -> dict[str, object]:
    text = read_url_text(event_url)
    match = re.search(
        r'<script[^>]+id=["\']pretalx-schedule-data["\'][^>]*>(.*?)</script>',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return {}
    return json.loads(html.unescape(match.group(1)).strip())


def eventyay_session_url(event_url: str, talk: dict[str, object]) -> str:
    code = str(talk.get("code") or "").strip()
    if code:
        return urljoin(event_url, f"talk/{quote(code)}/")
    talk_id = str(talk.get("id") or "").strip()
    return urljoin(event_url, f"talk/{quote(talk_id)}/") if talk_id else event_url


def eventyay_track_name(track: dict[str, object]) -> str:
    name = track.get("name", "")
    if isinstance(name, dict):
        return readable_text(str(name.get("en") or next(iter(name.values()), "")))
    return readable_text(str(name))


def value_asset_links(value, base_url: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for child in value.values():
            links.extend(value_asset_links(child, base_url))
    elif isinstance(value, list):
        for child in value:
            links.extend(value_asset_links(child, base_url))
    elif isinstance(value, str):
        for match in re.finditer(r"https?://[^\s\"'<>]+|/[^\s\"'<>]+", value):
            candidate = urljoin(base_url, html.unescape(match.group(0)).rstrip(").,;"))
            if is_probably_slide_asset(candidate):
                links.append((candidate, Path(urlparse(candidate).path).name))
    return links


def discover_eventyay_sessions(event_url: str) -> list[dict[str, object]]:
    data = extract_eventyay_schedule_data(event_url)
    talks = data.get("talks", [])
    tracks = data.get("tracks", [])
    if not isinstance(talks, list):
        return []
    track_names = {track.get("id"): eventyay_track_name(track) for track in tracks if isinstance(track, dict)}
    preferred_track_ids = {
        track_id
        for track_id, name in track_names.items()
        if any(keyword in name.lower() for keyword in ("pgday", "postgres", "database"))
    }
    sessions: list[dict[str, object]] = []
    for talk in talks:
        if not isinstance(talk, dict):
            continue
        track_id = talk.get("track")
        title = readable_text(str(talk.get("title") or ""))
        abstract = readable_text(re.sub(r"<[^>]+>", " ", str(talk.get("abstract") or "")))
        text = f"{title} {abstract} {track_names.get(track_id, '')}".lower()
        if preferred_track_ids:
            if track_id not in preferred_track_ids:
                continue
        elif not any(keyword in text for keyword in ("postgres", "postgresql", "pgday")):
            continue
        sessions.append(
            {
                "title": title or infer_session_title(event_url),
                "abstract": abstract,
                "session_url": eventyay_session_url(event_url, talk),
                "assets": value_asset_links(talk, event_url),
            }
        )
    return sessions


def crawl_eventyay(
    conn: sqlite3.Connection,
    event_url: str,
    event_name: str | None = None,
    delay_seconds: float = 0.5,
    limit: int | None = None,
) -> list[str]:
    event = event_name or infer_event_name(event_url)
    event_id = upsert_event(conn, event, source_url=event_url, website_url=event_url)
    sessions = discover_eventyay_sessions(event_url)
    if limit is not None:
        sessions = sessions[:limit]

    messages = [f"discovered eventyay sessions: {len(sessions)}"]
    downloaded = 0
    skipped = 0
    missing = 0
    failed = 0

    for session in sessions:
        title = str(session["title"])
        session_id = upsert_session(
            conn,
            event_id,
            title,
            session_url=str(session["session_url"]),
            asset_status="missing",
            abstract=str(session.get("abstract") or ""),
        )
        assets = list(session.get("assets") or [])
        if not assets:
            mark_session_checked(conn, session_id, "missing")
            missing += 1
            messages.append(f"WAIT {title}: no slides yet")
            continue
        for asset_url, label in assets:
            asset_title = asset_title_from_context(title, label, asset_url, len(assets))
            ok, msg = download_asset(conn, session_id, asset_url, event, asset_title)
            if ok:
                downloaded += 1
                messages.append(f"OK {title}: {msg}")
            else:
                skipped += 1
                messages.append(f"SKIP {title}: {msg}")
        if delay_seconds:
            time.sleep(delay_seconds)

    messages.append(
        f"summary: downloaded={downloaded}, skipped={skipped}, missing={missing}, failed={failed}"
    )
    return messages


def find_event(conn: sqlite3.Connection, event_query: str) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
    exact = conn.execute(
        "select * from events where lower(name) = lower(?)",
        (event_query,),
    ).fetchall()
    if len(exact) == 1:
        return exact[0], exact
    contains = conn.execute(
        """
        select * from events
        where lower(name) like '%' || lower(?) || '%'
        order by coalesce(year, 0) desc, name
        """,
        (event_query,),
    ).fetchall()
    if len(contains) == 1:
        return contains[0], contains
    return None, exact or contains


def event_external_links(event_url: str) -> list[tuple[str, str]]:
    parsed_event = urlparse(event_url)
    links = extract_links(event_url)
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url, label in links:
        parsed = urlparse(url)
        if not parsed.scheme.startswith("http"):
            continue
        if parsed.netloc in {parsed_event.netloc, "www.postgresql.org", "postgresql.org"}:
            continue
        normalized = parsed._replace(fragment="").geturl()
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append((normalized, re.sub(r"\s+", " ", label).strip()))
    return candidates


def indico_timetable_url(url: str) -> str | None:
    parsed = urlparse(url)
    if "indico" not in parsed.netloc:
        return None
    match = re.search(r"/event/(\d+)", parsed.path)
    if not match:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/event/{match.group(1)}/timetable/"


def pgevents_sessions_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc != "www.pgevents.ca":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if "events" not in parts:
        return None
    idx = parts.index("events")
    if idx + 1 >= len(parts):
        return None
    event_slug = parts[idx + 1]
    return f"{parsed.scheme}://{parsed.netloc}/events/{event_slug}/sessions/"


def postgresql_eu_schedule_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.endswith("postgresql.eu") or host.endswith("pgconf.eu"):
        old_match = re.match(r"^/events/schedule/([^/]+)/?$", parsed.path)
        if old_match:
            return f"{parsed.scheme}://{parsed.netloc}/events/{old_match.group(1)}/schedule/"
        if re.match(r"^/events/[^/]+/schedule/?$", parsed.path):
            return parsed._replace(query="", fragment="").geturl()

    try:
        for link_url, label in extract_links(url):
            link = urlparse(link_url)
            link_host = link.netloc.lower()
            text = f"{link_url} {label}".lower()
            if not (link_host.endswith("postgresql.eu") or link_host.endswith("pgconf.eu")):
                continue
            if "/schedule/" not in link.path and "schedule" not in text:
                continue
            return postgresql_eu_schedule_url(link_url) or link._replace(query="", fragment="").geturl()
    except Exception:  # noqa: BLE001
        return None
    return None


def classify_adapter_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in {"www.postgresql.org", "postgresql.org"} and parsed.path.startswith("/about/event/"):
        return "postgresql-official"
    if "indico" in host:
        return "indico"
    if host == "www.pgevents.ca":
        return "pgevents"
    if host.endswith("postgresql.eu") or host.endswith("pgconf.eu"):
        return "postgresql-eu"
    if host == "eventyay.com":
        return "eventyay"
    if host.endswith("pghyd.in"):
        return "wordpress"
    return "unknown"


def is_wordpress_site(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    try:
        sources = load_json(SOURCES_PATH, {})
        headers = {"User-Agent": sources.get("default_user_agent", "pgppt-harvester/0.1")}
        req = Request(f"{parsed.scheme}://{parsed.netloc}/wp-json/wp/v2/pages?per_page=1", headers=headers)
        with urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def classify_event_row(row: sqlite3.Row, resolve: bool = False, probe_wordpress: bool = False) -> tuple[str, str]:
    primary_url = row["website_url"] or row["source_url"] or ""
    adapter = classify_adapter_url(primary_url)
    evidence = primary_url
    if not resolve:
        return adapter, evidence

    urls = [(primary_url, "event source")]
    parsed = urlparse(primary_url)
    if parsed.netloc in {"www.postgresql.org", "postgresql.org"} and parsed.path.startswith("/about/event/"):
        try:
            urls.extend(event_external_links(primary_url))
        except Exception:  # noqa: BLE001
            pass

    for url, _label in urls:
        candidate = classify_adapter_url(url)
        if candidate != "unknown" and candidate != "postgresql-official":
            return candidate, url
        schedule_url = postgresql_eu_schedule_url(url)
        if schedule_url:
            return "postgresql-eu", schedule_url
        eventyay_url = eventyay_event_url(url)
        if eventyay_url:
            return "eventyay", eventyay_url

    if probe_wordpress:
        for url, _label in urls:
            if is_wordpress_site(url):
                return "wordpress", url

    for url, _label in urls:
        if classify_adapter_url(url) == "unknown":
            return "generic-fallback", url
    return adapter, evidence


def adapter_summary(
    conn: sqlite3.Connection,
    resolve: bool = False,
    limit: int | None = None,
    probe_wordpress: bool = False,
) -> list[str]:
    rows = conn.execute(
        """
        select name, website_url, source_url
        from events
        order by coalesce(year, 0) desc, name
        """
    ).fetchall()
    if limit is not None:
        rows = rows[:limit]
    counts: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    for row in rows:
        adapter, evidence = classify_event_row(row, resolve=resolve, probe_wordpress=probe_wordpress)
        counts[adapter] = counts.get(adapter, 0) + 1
        examples.setdefault(adapter, [])
        if len(examples[adapter]) < 8:
            examples[adapter].append(f"{row['name']} | {evidence}")

    mode = "resolved" if resolve else "local"
    if probe_wordpress:
        mode += "+wp-probe"
    messages = [f"adapter classification ({mode}, events={len(rows)}):"]
    for adapter, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        messages.append(f"{adapter}: {count}")
        for example in examples.get(adapter, []):
            messages.append(f"  - {example}")
    return messages


def inferred_event_adapter_links(event_name: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    normalized = event_name.lower()
    year_match = re.search(r"\b(20\d{2})\b", event_name)
    if year_match and "pgconf.dev" in normalized:
        year = year_match.group(1)
        links.append((f"https://www.pgevents.ca/events/pgconfdev{year}/sessions/", "inferred pgconf.dev sessions"))
    return links


def download_event_by_name(
    conn: sqlite3.Connection,
    event_query: str,
    delay_seconds: float = 0.5,
    limit: int | None = None,
) -> list[str]:
    event, matches = find_event(conn, event_query)
    if event is None:
        if not matches:
            return [f"ERROR no event matched: {event_query}", "hint: run ./pgppt.py scan-official first"]
        choices = [f"- {row['name']} | {row['website_url'] or ''}" for row in matches[:20]]
        return [f"ERROR event name is ambiguous: {event_query}", *choices]

    event_name = event["name"]
    seed_urls = [event["website_url"], event["source_url"]]
    candidate_links: list[tuple[str, str]] = []
    messages = [f"event: {event_name}"]
    candidate_links.extend(inferred_event_adapter_links(event_name))

    for seed in [url for url in seed_urls if url]:
        candidate_links.append((seed, "event source"))
        parsed = urlparse(seed)
        if parsed.netloc in {"www.postgresql.org", "postgresql.org"} and parsed.path.startswith("/about/event/"):
            try:
                external = event_external_links(seed)
                candidate_links.extend(external)
                messages.append(f"official page links: {len(external)}")
            except Exception as exc:  # noqa: BLE001
                messages.append(f"ERROR official page scan failed: {exc}")

    seen_targets: set[str] = set()
    ran_adapter = False
    for url, label in candidate_links:
        indico_url = indico_timetable_url(url)
        if indico_url and indico_url not in seen_targets:
            seen_targets.add(indico_url)
            ran_adapter = True
            messages.append(f"adapter: indico ({label or url}) -> {indico_url}")
            messages.extend(crawl_indico(conn, indico_url, event_name, delay_seconds, limit))
            continue

        sessions_url = pgevents_sessions_url(url)
        if sessions_url and sessions_url not in seen_targets:
            seen_targets.add(sessions_url)
            ran_adapter = True
            messages.append(f"adapter: pgevents ({label or url}) -> {sessions_url}")
            messages.extend(crawl_pgevents(conn, sessions_url, event_name, delay_seconds, limit))
            continue

        pgeu_schedule_url = postgresql_eu_schedule_url(url)
        if pgeu_schedule_url and pgeu_schedule_url not in seen_targets:
            seen_targets.add(pgeu_schedule_url)
            ran_adapter = True
            messages.append(f"adapter: postgresql-eu ({label or url}) -> {pgeu_schedule_url}")
            messages.extend(crawl_postgresql_eu(conn, pgeu_schedule_url, event_name, delay_seconds, limit))
            continue
        if pgeu_schedule_url and pgeu_schedule_url in seen_targets:
            continue

        eventyay_url = eventyay_event_url(url)
        if eventyay_url and eventyay_url not in seen_targets:
            seen_targets.add(eventyay_url)
            ran_adapter = True
            messages.append(f"adapter: eventyay ({label or url}) -> {eventyay_url}")
            messages.extend(crawl_eventyay(conn, eventyay_url, event_name, delay_seconds, limit))
            continue
        if eventyay_url and eventyay_url in seen_targets:
            continue

        if classify_adapter_url(url) == "wordpress" and url not in seen_targets:
            seen_targets.add(url)
            ran_adapter = True
            messages.append(f"adapter: wordpress ({label or url}) -> {url}")
            messages.extend(crawl_wordpress(conn, url, event_name, delay_seconds, limit))
            continue

        parsed = urlparse(url)
        if parsed.scheme.startswith("http") and parsed.netloc not in {"www.postgresql.org", "postgresql.org"} and url not in seen_targets:
            seen_targets.add(url)
            ran_adapter = True
            messages.append(f"adapter: generic ({label or url}) -> {url}")
            messages.extend(crawl_generic_site(conn, url, event_name, delay_seconds, limit))
            continue

    if ran_adapter:
        return messages

    messages.append("ERROR no supported adapter found")
    messages.append("candidate links:")
    for url, label in candidate_links:
        messages.append(f"- {label or 'link'}: {url}")
    return messages


def discover_official_events(conn: sqlite3.Connection) -> list[str]:
    sources = load_json(SOURCES_PATH, {})
    messages: list[str] = []
    seen: set[str] = set()
    for source_url in sources.get("official_events", []):
        try:
            links = extract_links(source_url)
        except Exception as exc:  # noqa: BLE001
            messages.append(f"ERROR {source_url}: {exc}")
            continue
        count = 0
        for url, label in links:
            parsed = urlparse(url)
            if parsed.netloc not in {"www.postgresql.org", "postgresql.org"}:
                continue
            if not parsed.path.startswith("/about/event/"):
                continue
            if url in seen:
                continue
            seen.add(url)
            name = re.sub(r"\s+", " ", label).strip()
            if not name or name.lower() in {"read more", "details"}:
                name = infer_session_title(url)
            upsert_event(conn, name, source_url=source_url, website_url=url)
            count += 1
        messages.append(f"OK {source_url}: discovered {count} event links")
    return messages


def ingest_url(conn: sqlite3.Connection, url: str, event_name: str | None, session_title: str | None) -> list[str]:
    messages: list[str] = []
    if is_asset_url(url):
        event = event_name or infer_event_name(url)
        title = session_title or infer_session_title(url)
        event_id = upsert_event(conn, event, source_url=url, website_url=f"{urlparse(url).scheme}://{urlparse(url).netloc}")
        session_id = upsert_session(conn, event_id, title, session_url=url, asset_status="found")
        ok, msg = download_asset(conn, session_id, url, event, title)
        messages.append(("OK " if ok else "SKIP ") + msg)
        return messages

    event = event_name or infer_event_name(url)
    title = session_title or infer_session_title(url)
    event_id = upsert_event(conn, event, source_url=url, website_url=url)
    try:
        page_info = extract_page_info(url)
        links = [(asset_url, label) for asset_url, label in page_info["links"] if is_asset_url(asset_url)]
    except HTTPError as exc:
        session_id = upsert_session(conn, event_id, title, session_url=url, asset_status="missing")
        mark_session_checked(conn, session_id, "login_required" if exc.code in (401, 403) else "failed")
        return [f"ERROR http error {exc.code}: {exc.reason}"]
    except Exception as exc:  # noqa: BLE001
        session_id = upsert_session(conn, event_id, title, session_url=url, asset_status="missing")
        mark_session_checked(conn, session_id, "failed")
        return [f"ERROR page scan failed: {exc}"]

    session_id = upsert_session(
        conn,
        event_id,
        title,
        session_url=url,
        asset_status="missing",
        abstract=str(page_info["abstract"] or ""),
    )
    if not links:
        mark_session_checked(conn, session_id, "missing")
        return [f"WAIT no slide assets found; next check scheduled"]

    for asset_url, label in links:
        asset_title = asset_title_from_context(title, label, asset_url, len(links))
        child_session_id = upsert_session(conn, event_id, asset_title, session_url=url, asset_status="found")
        ok, msg = download_asset(conn, child_session_id, asset_url, event, asset_title)
        messages.append(("OK " if ok else "SKIP ") + msg)
    mark_session_checked(conn, session_id, "found")
    return messages


def tick(conn: sqlite3.Connection, limit: int) -> list[str]:
    now = utcnow()
    rows = conn.execute(
        """
        select s.id, s.session_url, s.title, e.name as event_name
        from sessions s join events e on e.id = s.event_id
        where s.next_check_at <= ?
          and s.session_url is not null
          and s.asset_status in ('missing', 'found', 'failed', 'partial_assets')
        order by s.next_check_at asc
        limit ?
        """,
        (now, limit),
    ).fetchall()
    messages: list[str] = []
    for row in rows:
        messages.append(f"checking: {row['title']}")
        messages.extend(ingest_url(conn, row["session_url"], row["event_name"], row["title"]))
        time.sleep(0.5)
    if not rows:
        messages.append("nothing due")
    return messages


def organize_archive_by_topic(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        select
            a.id as asset_id,
            a.local_path,
            a.file_url,
            s.id as session_id,
            s.title as session_title,
            e.name as event_name
        from assets a
        join sessions s on s.id = a.session_id
        join events e on e.id = s.event_id
        order by a.downloaded_at
        """
    ).fetchall()
    messages: list[str] = []
    moved = 0
    missing = 0
    for row in rows:
        classify_session(conn, int(row["session_id"]))
        src = ROOT / row["local_path"]
        if not src.exists():
            missing += 1
            messages.append(f"MISS {row['local_path']}")
            continue
        stem = preferred_asset_stem(row["local_path"], row["file_url"], row["session_title"])
        topic_slug = asset_topic_slug(conn, int(row["session_id"]), stem, row["file_url"])
        language_slug = asset_language_slug(row["event_name"], row["session_title"], stem, row["file_url"])
        topic_dir = archive_asset_dir(topic_slug, language_slug)
        topic_dir.mkdir(parents=True, exist_ok=True)
        preferred_dest = topic_dir / f"{stem}{src.suffix}"
        if src.resolve() == preferred_dest.resolve():
            continue
        dest = unique_asset_path(topic_dir, stem, src.suffix)
        shutil.move(str(src), str(dest))
        conn.execute(
            "update assets set local_path = ? where id = ?",
            (str(dest.relative_to(ROOT)), int(row["asset_id"])),
        )
        moved += 1
        messages.append(f"MOVE {row['local_path']} -> {dest.relative_to(ROOT)}")
    conn.commit()
    messages.append(f"summary: moved={moved}, missing={missing}, total={len(rows)}")
    return messages


def rebuild_topic_index(conn: sqlite3.Connection) -> None:
    topic_root = ROOT / "archive"
    topic_root.mkdir(parents=True, exist_ok=True)


def report(conn: sqlite3.Connection) -> tuple[Path, Path]:
    rebuild_topic_index(conn)
    report_dir = ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = report_dir / "index.csv"
    html_path = report_dir / "index.html"
    rows = conn.execute(
        """
        select
            e.name as event_name,
            s.title as session_title,
            case
                when s.asset_status = 'downloaded' and a.id is null then 'downloaded_without_asset'
                else s.asset_status
            end as asset_status,
            s.last_checked_at,
            s.next_check_at,
            a.local_path,
            a.file_url,
            a.file_type,
            a.size_bytes,
            group_concat(t.label, ', ') as tags
        from sessions s
        join events e on e.id = s.event_id
        left join assets a on a.session_id = s.id
        left join session_tags st on st.session_id = s.id
        left join tags t on t.id = st.tag_id
        group by s.id, a.id
        order by e.year desc, e.name, s.title
        """
    ).fetchall()
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "event",
                "session",
                "status",
                "tags",
                "local_path",
                "file_url",
                "file_type",
                "size_bytes",
                "last_checked_at",
                "next_check_at",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["event_name"],
                    row["session_title"],
                    row["asset_status"],
                    row["tags"] or "",
                    row["local_path"] or "",
                    row["file_url"] or "",
                    row["file_type"] or "",
                    row["size_bytes"] or "",
                    row["last_checked_at"] or "",
                    row["next_check_at"] or "",
                ]
            )
    items = []
    for row in rows:
        local = row["local_path"]
        link = f'<a href="../{html.escape(local)}">{html.escape(local)}</a>' if local else ""
        items.append(
            "<tr>"
            f"<td>{html.escape(row['event_name'])}</td>"
            f"<td>{html.escape(row['session_title'])}</td>"
            f"<td>{html.escape(row['asset_status'])}</td>"
            f"<td>{html.escape(row['tags'] or '')}</td>"
            f"<td>{link}</td>"
            f"<td>{html.escape(row['next_check_at'] or '')}</td>"
            "</tr>"
        )
    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PG PPT Archive</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2933; }}
    h1 {{ font-size: 24px; margin-bottom: 8px; }}
    .meta {{ color: #5f6b7a; margin-bottom: 24px; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 10px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f7f9fb; position: sticky; top: 0; }}
    a {{ color: #0f5ea8; }}
  </style>
</head>
<body>
  <h1>PG PPT Archive</h1>
  <div class="meta">Generated at {html.escape(utcnow())}. Total rows: {len(rows)}.</div>
  <table>
    <thead>
      <tr><th>Event</th><th>Session</th><th>Status</th><th>Tags</th><th>File</th><th>Next Check</th></tr>
    </thead>
    <tbody>
      {''.join(items)}
    </tbody>
  </table>
</body>
</html>
"""
    html_path.write_text(html_doc, encoding="utf-8")
    return html_path, csv_path


def report_slug(value: str, fallback: str = "run") -> str:
    return slugify(value, fallback).lower()


def run_report(conn: sqlite3.Connection, run_id: int) -> tuple[Path, Path, int]:
    run = conn.execute("select * from crawl_runs where id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError(f"run not found: {run_id}")
    started = parse_time(run["started_at"]) or dt.datetime.now(dt.timezone.utc)
    run_date = started.astimezone().date().isoformat()
    asset_rows = conn.execute(
        """
        select
            ra.action,
            ra.message,
            ra.created_at as recorded_at,
            e.name as event_name,
            s.title as session_title,
            a.local_path,
            coalesce(ra.source_url, a.file_url) as file_url,
            a.file_type,
            a.size_bytes,
            a.downloaded_at,
            group_concat(t.label, ', ') as tags
        from run_assets ra
        join assets a on a.id = ra.asset_id
        join sessions s on s.id = a.session_id
        join events e on e.id = s.event_id
        left join session_tags st on st.session_id = s.id
        left join tags t on t.id = st.tag_id
        where ra.run_id = ?
        group by ra.run_id, ra.asset_id, ra.action
        order by ra.created_at, e.name, s.title
        """,
        (run_id,),
    ).fetchall()
    session_rows = conn.execute(
        """
        select
            rs.status as action,
            rs.message,
            rs.created_at as recorded_at,
            e.name as event_name,
            s.title as session_title,
            '' as local_path,
            coalesce(rs.source_url, s.session_url) as file_url,
            '' as file_type,
            '' as size_bytes,
            '' as downloaded_at,
            group_concat(t.label, ', ') as tags
        from run_sessions rs
        join sessions s on s.id = rs.session_id
        join events e on e.id = s.event_id
        left join session_tags st on st.session_id = s.id
        left join tags t on t.id = st.tag_id
        where rs.run_id = ?
          and not exists (
              select 1
              from run_assets ra
              join assets a on a.id = ra.asset_id
              where ra.run_id = rs.run_id
                and a.session_id = rs.session_id
          )
        group by rs.run_id, rs.session_id, rs.status
        order by rs.created_at, e.name, s.title
        """,
        (run_id,),
    ).fetchall()
    rows = list(asset_rows) + list(session_rows)
    event_names = sorted({row["event_name"] for row in rows if row["event_name"]})
    if len(event_names) == 1:
        event_part = report_slug(event_names[0])
    elif len(event_names) > 1:
        event_part = "multiple-events"
    else:
        event_part = report_slug(run["command"] or "run")
    report_dir = ROOT / "reports" / "runs" / run_date
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{event_part}-run-{run_id}"
    csv_path = report_dir / f"{stem}.csv"
    html_path = report_dir / f"{stem}.html"

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "run_id",
                "run_date",
                "command",
                "action",
                "message",
                "event",
                "session",
                "tags",
                "local_path",
                "file_url",
                "file_type",
                "size_bytes",
                "downloaded_at",
                "recorded_at",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    run_id,
                    run_date,
                    run["command"],
                    row["action"],
                    row["message"] or "",
                    row["event_name"],
                    row["session_title"],
                    row["tags"] or "",
                    row["local_path"],
                    row["file_url"],
                    row["file_type"],
                    row["size_bytes"],
                    row["downloaded_at"],
                    row["recorded_at"],
                ]
            )

    items = []
    for row in rows:
        local = row["local_path"]
        if local:
            link_target = os.path.relpath(ROOT / local, report_dir)
            link = f'<a href="{html.escape(link_target)}">{html.escape(local)}</a>'
        else:
            link = html.escape(row["file_url"] or "")
        items.append(
            "<tr>"
            f"<td>{html.escape(row['action'])}</td>"
            f"<td>{html.escape(row['message'] or '')}</td>"
            f"<td>{html.escape(row['event_name'])}</td>"
            f"<td>{html.escape(row['session_title'])}</td>"
            f"<td>{html.escape(row['tags'] or '')}</td>"
            f"<td>{link}</td>"
            f"<td>{html.escape(str(row['size_bytes']))}</td>"
            f"<td>{html.escape(row['recorded_at'])}</td>"
            "</tr>"
        )
    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PG Slide Run Report #{run_id}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #1f2933; }}
    h1 {{ font-size: 24px; margin-bottom: 8px; }}
    .meta {{ color: #5f6b7a; margin-bottom: 24px; line-height: 1.6; }}
    code {{ background: #f3f4f6; padding: 2px 5px; border-radius: 4px; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 10px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f7f9fb; position: sticky; top: 0; }}
    a {{ color: #0f5ea8; }}
  </style>
</head>
<body>
  <h1>PG Slide Run Report #{run_id}</h1>
  <div class="meta">
    Date: {html.escape(run_date)}<br>
    Command: <code>{html.escape(run['command'])}</code><br>
    Status: {html.escape(run['status'])}<br>
    Items: {len(rows)}
  </div>
  <table>
    <thead>
      <tr><th>Action</th><th>Message</th><th>Event</th><th>Session</th><th>Tags</th><th>File</th><th>Size</th><th>Recorded At</th></tr>
    </thead>
    <tbody>
      {''.join(items)}
    </tbody>
  </table>
</body>
</html>
"""
    html_path.write_text(html_doc, encoding="utf-8")
    return html_path, csv_path, len(rows)


def finish_with_reports(
    conn: sqlite3.Connection,
    run_id: int,
    messages: list[str],
    include_run_report: bool = False,
) -> None:
    html_path, _ = report(conn)
    for msg in messages:
        print(msg)
    print(f"report: {html_path.relative_to(ROOT)}")
    finish_run(conn, run_id, "ok", "\n".join(messages))
    if include_run_report:
        run_html_path, _, item_count = run_report(conn, run_id)
        print(f"run report: {run_html_path.relative_to(ROOT)} ({item_count} items)")


def list_rows(conn: sqlite3.Connection, target: str) -> list[str]:
    if target == "events":
        rows = conn.execute(
            """
            select name, status, website_url, updated_at
            from events
            order by coalesce(year, 0) desc, name
            """
        ).fetchall()
        return [f"{row['name']} | {row['status']} | {row['website_url'] or ''} | updated={row['updated_at']}" for row in rows]
    if target == "assets":
        rows = conn.execute(
            """
            select e.name as event_name, s.title, a.local_path, a.size_bytes
            from assets a
            join sessions s on s.id = a.session_id
            join events e on e.id = s.event_id
            order by a.downloaded_at desc
            """
        ).fetchall()
        return [f"{row['event_name']} | {row['title']} | {row['local_path']} | {row['size_bytes']} bytes" for row in rows]
    rows = conn.execute(
        """
        select e.name as event_name, s.title, s.asset_status, s.next_check_at
        from sessions s join events e on e.id = s.event_id
        order by e.name, s.title
        """
    ).fetchall()
    return [f"{row['event_name']} | {row['title']} | {row['asset_status']} | next={row['next_check_at']}" for row in rows]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PostgreSQL conference slide harvester")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="initialize SQLite database and tags")

    sub.add_parser("scan-official", help="discover events from postgresql.org events pages")

    analyze = sub.add_parser("analyze-events", help="classify local events by currently supported adapter type")
    analyze.add_argument("--resolve", action="store_true", help="visit official event pages and classify external websites")
    analyze.add_argument("--probe-wordpress", action="store_true", help="probe unknown external websites for WordPress REST API")
    analyze.add_argument("--limit", type=int, help="limit number of events to analyze")

    ingest = sub.add_parser("ingest", help="ingest one asset URL or scan one page URL")
    ingest.add_argument("url")
    ingest.add_argument("--event")
    ingest.add_argument("--title")

    pgevents = sub.add_parser("crawl-pgevents", help="crawl a pgevents.ca sessions listing")
    pgevents.add_argument("sessions_url")
    pgevents.add_argument("--event")
    pgevents.add_argument("--delay", type=float, default=0.5)
    pgevents.add_argument("--limit", type=int)

    generic = sub.add_parser("crawl-generic", help="crawl a generic conference website for slide assets")
    generic.add_argument("site_url")
    generic.add_argument("--event")
    generic.add_argument("--delay", type=float, default=0.5)
    generic.add_argument("--limit", type=int)
    generic.add_argument("--max-pages", type=int, default=25)

    download_event = sub.add_parser("download-event", help="download slides by event name from the local event list")
    download_event.add_argument("event_name")
    download_event.add_argument("--delay", type=float, default=0.5)
    download_event.add_argument("--limit", type=int)

    tick_parser = sub.add_parser("tick", help="check sessions whose next_check_at is due")
    tick_parser.add_argument("--limit", type=int, default=20)

    sub.add_parser("report", help="generate HTML and CSV report")

    sub.add_parser("classify", help="rebuild tags for all sessions")

    sub.add_parser("organize-archive", help="move downloaded assets into archive/<category>/")

    list_parser = sub.add_parser("list", help="list sessions or assets")
    list_parser.add_argument("target", choices=["events", "sessions", "assets"])
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    global ACTIVE_RUN_ID
    args = build_parser().parse_args(argv)
    conn = connect()
    init_db(conn)
    ensure_tags(conn)

    run_id = begin_run(conn, " ".join(sys.argv[1:]))
    ACTIVE_RUN_ID = run_id
    try:
        if args.command == "init":
            finish_run(conn, run_id, "ok", "initialized")
            print(f"initialized: {DB_PATH.relative_to(ROOT)}")
            return 0

        if args.command == "ingest":
            messages = ingest_url(conn, args.url, args.event, args.title)
            finish_with_reports(conn, run_id, messages, include_run_report=True)
            return 0

        if args.command == "scan-official":
            messages = discover_official_events(conn)
            finish_with_reports(conn, run_id, messages)
            return 0

        if args.command == "analyze-events":
            messages = adapter_summary(
                conn,
                resolve=args.resolve,
                limit=args.limit,
                probe_wordpress=args.probe_wordpress,
            )
            for msg in messages:
                print(msg)
            finish_run(conn, run_id, "ok", "\n".join(messages))
            return 0

        if args.command == "crawl-pgevents":
            messages = crawl_pgevents(conn, args.sessions_url, args.event, args.delay, args.limit)
            finish_with_reports(conn, run_id, messages, include_run_report=True)
            return 0

        if args.command == "crawl-generic":
            messages = crawl_generic_site(conn, args.site_url, args.event, args.delay, args.limit, args.max_pages)
            finish_with_reports(conn, run_id, messages, include_run_report=True)
            return 0

        if args.command == "download-event":
            messages = download_event_by_name(conn, args.event_name, args.delay, args.limit)
            finish_with_reports(conn, run_id, messages, include_run_report=True)
            return 0

        if args.command == "tick":
            messages = tick(conn, args.limit)
            finish_with_reports(conn, run_id, messages, include_run_report=True)
            return 0

        if args.command == "report":
            html_path, csv_path = report(conn)
            print(f"html: {html_path.relative_to(ROOT)}")
            print(f"csv: {csv_path.relative_to(ROOT)}")
            finish_run(conn, run_id, "ok", "report generated")
            return 0

        if args.command == "classify":
            count = classify_all(conn)
            html_path, _ = report(conn)
            print(f"classified sessions: {count}")
            print(f"report: {html_path.relative_to(ROOT)}")
            finish_run(conn, run_id, "ok", f"classified {count} sessions")
            return 0

        if args.command == "organize-archive":
            messages = organize_archive_by_topic(conn)
            finish_with_reports(conn, run_id, messages)
            return 0

        if args.command == "list":
            for line in list_rows(conn, args.target):
                print(line)
            finish_run(conn, run_id, "ok", f"listed {args.target}")
            return 0
    except Exception as exc:  # noqa: BLE001
        finish_run(conn, run_id, "failed", str(exc))
        raise
    finally:
        ACTIVE_RUN_ID = None
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
