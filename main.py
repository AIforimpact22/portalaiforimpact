import os
import re
import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, render_template, jsonify, abort, request, redirect, url_for
from markupsafe import Markup, escape

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# -----------------------------------------------------------------------------
# Env config
# -----------------------------------------------------------------------------
INSTANCE_CONNECTION_NAME = os.getenv("INSTANCE_CONNECTION_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_NAME = os.getenv("DB_NAME")

# Admin controls (simple: default open during your build phase)
ADMIN_MODE = os.getenv("ADMIN_MODE", "open").lower()   # open | token | email
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
SUPERADMIN_EMAIL = os.getenv("SUPERADMIN_EMAIL", "aiforimpact22@gmail.com")
ADMIN_EMAIL = SUPERADMIN_EMAIL

# DB URL fallbacks
DATABASE_URL = os.getenv("DATABASE_URL")
DATABASE_URL_LOCAL = os.getenv("DATABASE_URL_LOCAL")
DB_HOST_OVERRIDE = os.getenv("DB_HOST")
DB_PORT_OVERRIDE = os.getenv("DB_PORT")
FORCE_TCP = os.getenv("FORCE_TCP", "").lower() in ("1", "true", "yes")

# Rich content controls
ALLOW_RAW_HTML = os.getenv("ALLOW_RAW_HTML", "1").lower() in ("1", "true", "yes")
SANITIZE_HTML = os.getenv("SANITIZE_HTML", "0").lower() in ("1", "true", "yes")

# bleach allowlist if you switch on SANITIZE_HTML=1
BLEACH_ALLOWED_TAGS = [
    "a","abbr","acronym","b","blockquote","code","em","i","li","ol","strong","ul",
    "p","h1","h2","h3","h4","h5","h6","pre","hr","br","span","div","img","table",
    "thead","tbody","tr","th","td","caption","figure","figcaption","video","source",
    "iframe"
]
BLEACH_ALLOWED_ATTRS = {
    "*": ["class","id","style","title"],
    "a": ["href","name","target","rel"],
    "img": ["src","alt","width","height","loading"],
    "video": ["src","controls","preload","poster","width","height"],
    "source": ["src","type"],
    "iframe": ["src","width","height","allow","allowfullscreen","frameborder"]
}
BLEACH_ALLOWED_PROTOCOLS = ["http","https","mailto","data"]

# -----------------------------------------------------------------------------
# Runtime/DB connection selection
# -----------------------------------------------------------------------------
def _on_managed_runtime() -> bool:
    return os.getenv("GAE_ENV", "").startswith("standard") or bool(os.getenv("K_SERVICE"))

def _log_choice(kwargs: dict, origin: str):
    if "host" in kwargs and isinstance(kwargs["host"], str) and kwargs["host"].startswith("/cloudsql/"):
        print(f"[DB] {origin}: Unix socket -> {kwargs['host']}")
    else:
        host = kwargs.get("host", "localhost")
        port = kwargs.get("port", 5432)
        print(f"[DB] {origin}: TCP -> {host}:{port}")

def _parse_database_url(url: str) -> dict:
    if not url:
        raise ValueError("Empty DATABASE_URL")
    if url.startswith("postgresql+psycopg2://"):
        url = "postgresql://" + url.split("postgresql+psycopg2://", 1)[1]
    if url.startswith("postgres+psycopg2://"):
        url = "postgres://" + url.split("postgres+psycopg2://", 1)[1]
    p = urlparse(url)
    if p.scheme not in ("postgresql", "postgres"):
        raise ValueError(f"Unsupported scheme '{p.scheme}'")
    user = unquote(p.username or "")
    password = unquote(p.password or "")
    dbname = (p.path or "").lstrip("/")
    qs = parse_qs(p.query or "", keep_blank_values=True)
    host = p.hostname
    port = p.port
    if "host" in qs and qs["host"]:
        host = qs["host"][0]
    if not dbname:
        if "dbname" in qs and qs["dbname"]:
            dbname = qs["dbname"][0]
        else:
            raise ValueError("DATABASE_URL missing dbname")
    kwargs = {
        "dbname": dbname,
        "user": user,
        "password": password,
        "connect_timeout": 10,
        "options": "-c search_path=public",
    }
    if host:
        kwargs["host"] = host
    if port and not (isinstance(host, str) and host.startswith("/")):
        kwargs["port"] = port
    if "sslmode" in qs and qs["sslmode"]:
        kwargs["sslmode"] = qs["sslmode"][0]
    return kwargs

def _tcp_kwargs() -> dict:
    host = DB_HOST_OVERRIDE or "127.0.0.1"
    port = int(DB_PORT_OVERRIDE or "5432")
    if not all([DB_NAME, DB_USER, DB_PASS]):
        raise RuntimeError("DB_NAME, DB_USER, DB_PASS must be set for TCP mode.")
    return {
        "host": host,
        "port": port,
        "dbname": DB_NAME,
        "user": DB_USER,
        "password": DB_PASS,
        "sslmode": "disable",
        "connect_timeout": 10,
        "options": "-c search_path=public",
    }

def _socket_kwargs() -> dict:
    if not all([INSTANCE_CONNECTION_NAME, DB_NAME, DB_USER, DB_PASS]):
        raise RuntimeError("INSTANCE_CONNECTION_NAME, DB_NAME, DB_USER, DB_PASS must be set for socket mode.")
    return {
        "host": f"/cloudsql/{INSTANCE_CONNECTION_NAME}",
        "dbname": DB_NAME,
        "user": DB_USER,
        "password": DB_PASS,
        "connect_timeout": 10,
        "options": "-c search_path=public",
    }

def _connection_kwargs() -> dict:
    managed = _on_managed_runtime()
    if FORCE_TCP and not managed:
        kwargs = _tcp_kwargs(); _log_choice(kwargs, "FORCE_TCP"); return kwargs
    if not managed and DATABASE_URL_LOCAL:
        try:
            kwargs = _parse_database_url(DATABASE_URL_LOCAL); _log_choice(kwargs, "Using DATABASE_URL_LOCAL (parsed)"); return kwargs
        except Exception as e:
            print(f"[DB] Ignoring DATABASE_URL_LOCAL: {e}")
    if DATABASE_URL:
        try:
            parsed = _parse_database_url(DATABASE_URL)
            if (not managed) and isinstance(parsed.get("host"), str) and parsed["host"].startswith("/cloudsql/"):
                print("[DB] DATABASE_URL targets /cloudsql/ but we are local; ignoring and using TCP.")
            else:
                _log_choice(parsed, "Using DATABASE_URL (parsed)"); return parsed
        except Exception as e:
            print(f"[DB] Ignoring DATABASE_URL: {e}")
    if managed:
        kwargs = _socket_kwargs(); _log_choice(kwargs, "Managed runtime"); return kwargs
    kwargs = _tcp_kwargs(); _log_choice(kwargs, "Local dev"); return kwargs

# -----------------------------------------------------------------------------
# Connection pool
# -----------------------------------------------------------------------------
_pg_pool: pool.SimpleConnectionPool | None = None

def init_pool():
    global _pg_pool
    if _pg_pool is not None: return
    kwargs = _connection_kwargs()
    _pg_pool = psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=6, **kwargs)

@contextmanager
def get_conn():
    if _pg_pool is None:
        init_pool()
    conn = _pg_pool.getconn()
    try:
        yield conn
    finally:
        _pg_pool.putconn(conn)

# -----------------------------------------------------------------------------
# DB helpers
# -----------------------------------------------------------------------------
def fetch_all(q, params=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(q, params or ())
            return cur.fetchall()

def fetch_one(q, params=None):
    rows = fetch_all(q, params)
    return rows[0] if rows else None

def execute(q, params=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(q, params or ())
        conn.commit()

def execute_returning(q, params=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(q, params or ())
            rows = cur.fetchall()
        conn.commit()
        return rows

# -----------------------------------------------------------------------------
# Rendering helpers (Markdown + HTML)
# -----------------------------------------------------------------------------
_HTML_PATTERN = re.compile(r"</?\w+[^>]*>")

def _sanitize_if_enabled(html: str) -> str:
    if not SANITIZE_HTML:
        return html
    try:
        import bleach
        return bleach.clean(
            html,
            tags=BLEACH_ALLOWED_TAGS,
            attributes=BLEACH_ALLOWED_ATTRS,
            protocols=BLEACH_ALLOWED_PROTOCOLS,
            strip=False
        )
    except Exception:
        return html

# unwrap a single outer <p>…</p>
_SINGLE_P_RE = re.compile(r"^\s*<p[^>]*>(?P<body>[\s\S]*?)</p>\s*$", re.IGNORECASE)

def _unwrap_single_p(html: str) -> str:
    m = _SINGLE_P_RE.match(html or "")
    return m.group("body") if m else html

def render_rich(text: Optional[str]) -> Markup:
    """
    Render Markdown or raw HTML:
      - If content looks like HTML and ALLOW_RAW_HTML=1 -> render as-is (sanitized if SANITIZE_HTML=1).
      - Else -> render as Markdown (HTML enabled), then sanitize if enabled.
    NOTE: This base renderer does NOT unwrap a single outer <p>…</p>.
    """
    if not text:
        return Markup("")

    # Raw HTML path
    if ALLOW_RAW_HTML and _HTML_PATTERN.search(text):
        html = _sanitize_if_enabled(text)
        return Markup(html)

    # Markdown path (allow HTML in Markdown)
    try:
        import markdown
        html = markdown.markdown(
            text,
            extensions=[
                "fenced_code", "tables", "sane_lists",
                "toc", "codehilite", "md_in_html", "attr_list"
            ],
            output_format="html5",
        )
        html = _sanitize_if_enabled(html)
        return Markup(html)
    except Exception:
        # fallback: escape and keep breaks
        safe = "<p>" + escape(text).replace("\n\n", "</p><p>").replace("\n", "<br/>") + "</p>"
        return Markup(safe)

def render_rich_intro(text: Optional[str]) -> Markup:
    """
    Like render_rich, but additionally unwraps a single outer <p>…</p>.
    Use this ONLY where you want to suppress the leading paragraph wrapper.
    """
    html = render_rich(text)              # Markup
    unwrapped = _unwrap_single_p(str(html))
    return Markup(unwrapped)

# Register Jinja filters
app.jinja_env.filters["rich"] = render_rich
app.jinja_env.filters["rich_intro"] = render_rich_intro

def slugify(s: str) -> str:
    out = []
    for ch in s.lower():
        if ch.isalnum(): out.append(ch)
        elif ch in (" ", "-", "_"): out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "course"

def ensure_structure(structure_raw: Any) -> Dict[str, Any]:
    if not structure_raw: return {"sections": []}
    if isinstance(structure_raw, dict): return structure_raw
    try:
        return json.loads(structure_raw)
    except Exception:
        return {"sections": []}

def flatten_lessons(structure: Dict[str, Any]):
    out = []
    secs = structure.get("sections") or []
    secs = sorted(secs, key=lambda s: (s.get("order") or 0, s.get("title") or ""))
    for s in secs:
        lessons = s.get("lessons") or []
        lessons = sorted(lessons, key=lambda l: (l.get("order") or 0, l.get("title") or ""))
        for l in lessons:
            out.append((s, l))
    return out

def first_lesson_uid(structure: Dict[str, Any]) -> Optional[str]:
    flat = flatten_lessons(structure)
    return str(flat[0][1].get("lesson_uid")) if flat else None

def find_lesson(structure: Dict[str, Any], lesson_uid: str):
    secs = structure.get("sections") or []
    for si, s in enumerate(secs):
        for li, l in enumerate(s.get("lessons") or []):
            if str(l.get("lesson_uid")) == str(lesson_uid):
                return si, li
    return None, None

def next_prev_uids(structure: Dict[str, Any], current_uid: str):
    flat = [str(l["lesson_uid"]) for _, l in flatten_lessons(structure) if "lesson_uid" in l]
    if not flat: return (None, None)
    try:
        idx = flat.index(str(current_uid))
    except ValueError:
        return (None, None)
    prev_uid = flat[idx - 1] if idx > 0 else None
    next_uid = flat[idx + 1] if idx < len(flat) - 1 else None
    return (prev_uid, next_uid)

def total_course_duration(structure: Dict[str, Any]) -> int:
    total = 0
    for _, l in flatten_lessons(structure):
        c = l.get("content") or {}
        dur = c.get("duration_sec") or 0
        if isinstance(dur, int): total += max(0, dur)
    return total

def format_duration(total_sec: Optional[int]) -> str:
    if not total_sec: return "—"
    m, _ = divmod(total_sec, 60)
    h, m = divmod(m, 60)
    if h: return f"{h}h {m}m"
    return f"{m}m"

app.jinja_env.filters["duration"] = format_duration

# -----------------------------------------------------------------------------
# Single course seed
# -----------------------------------------------------------------------------
COURSE_TITLE = "Advanced AI Utilization and Real-Time Deployment"
COURSE_COVER = "https://i.imgur.com/iIMdWOn.jpeg"
COURSE_DESC = (
    "This course is a master course that offers Participants will develop advanced skills in "
    "coding, database management, machine learning, and real-time application deployment. "
    "This course focuses on practical implementations, enabling learners to create AI-driven solutions, "
    "deploy them in real-world scenarios, and integrate apps with cloud and database systems."
)
WEEKS = [
    "Week 1: Ice Breaker for Coding",
    "Week 2: UI and UX",
    "Week 3: Modularity",
    "Week 4: Advanced SQL and Databases",
    "Week 5: Fundamental of Statistics for Machine Learning",
    "Week 6: Unsupervised Machine Learning",
    "Week 7: Supervised Machine Learning",
    "Week 8: Utilizing AI API",
    "Week 9: Capstone Project",
]

def seed_course_if_missing() -> int:
    row = fetch_one("SELECT id FROM courses WHERE title = %s;", (COURSE_TITLE,))
    if row:
        return row["id"]
    structure = {
        "thumbnail_url": COURSE_COVER,
        "category": "Artificial Intelligence",
        "level": "Intermediate–Advanced",
        "rating": 4.9,
        "description_md": COURSE_DESC,
        "what_you_will_learn": [
            "Design end‑to‑end AI applications.",
            "Integrate cloud + database with ML pipelines.",
            "Deploy real‑time inference and monitoring."
        ],
        "instructors": [
            {"name": "Course Lead", "title": "AI Engineer", "avatar_url": ""}
        ],
        "sections": []
    }
    for i, title in enumerate(WEEKS, start=1):
        structure["sections"].append({"title": title, "order": i, "lessons": []})
    created = execute_returning("""
        WITH admin_user AS (
            INSERT INTO users (email, full_name, role)
            VALUES (%s, %s, 'admin')
            ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name
            RETURNING id
        )
        INSERT INTO courses (title, created_by, is_published, published_at, structure)
        SELECT %s, admin_user.id, TRUE, now(), %s
        FROM admin_user
        RETURNING id;
    """, (ADMIN_EMAIL, "Portal Admin", COURSE_TITLE, json.dumps(structure)))
    return created[0]["id"]

# -----------------------------------------------------------------------------
# Admin access (open by default)
# -----------------------------------------------------------------------------
def require_admin():
    mode = ADMIN_MODE
    if mode == "open":
        return
    if mode == "token":
        provided = request.args.get("token") or request.form.get("token") or request.headers.get("X-Admin-Token")
        if ADMIN_TOKEN and provided == ADMIN_TOKEN:
            return
        abort(403)
    if mode == "email":
        h = (
            request.headers.get("X-Goog-Authenticated-User-Email")
            or request.headers.get("X-Appengine-User-Email")
            or request.headers.get("X-User-Email")
        )
        if h:
            email = h.split(":")[-1].strip().lower()
            if email == (SUPERADMIN_EMAIL or "").lower():
                return
        abort(403)
    return

@app.get("/admin/whoami")
def admin_whoami():
    return jsonify({
        "admin_mode": ADMIN_MODE,
        "allow_raw_html": ALLOW_RAW_HTML,
        "sanitize_html": SANITIZE_HTML,
        "superadmin_email": SUPERADMIN_EMAIL,
    })

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    try:
        row = fetch_one("SELECT 1 AS ok;")
        ok = bool(row and row.get("ok") == 1)
        return ("ok" if ok else "db-fail", 200 if ok else 500)
    except Exception as e:
        return (f"error: {e}", 500)

@app.get("/")
def index():
    """Single-course landing page with spotlight hero + stats + curriculum."""
    try:
        seed_course_if_missing()
    except Exception as e:
        print(f"Seed failed: {e}")

    c = fetch_one("""
        SELECT id, title, is_published, published_at, created_at, structure
        FROM courses WHERE title = %s LIMIT 1;
    """, (COURSE_TITLE,))
    if not c:
        return render_template("index.html", course=None, err="Course not found.")

    st = ensure_structure(c.get("structure"))
    weeks = st.get("sections") or []
    modules_count = len(weeks)
    lessons_flat = flatten_lessons(st)
    lessons_count = len(lessons_flat)
    duration_total = format_duration(total_course_duration(st))
    first_uid = first_lesson_uid(st)

    c["slug"] = slugify(c.get("title") or f"course-{c['id']}")
    c["thumbnail_url"] = st.get("thumbnail_url") or COURSE_COVER
    c["category"] = st.get("category") or "Artificial Intelligence"
    c["level"] = st.get("level") or "Intermediate–Advanced"
    c["lessons_count"] = lessons_count
    c["duration_total"] = duration_total

    weeks_meta = [{"title": s.get("title") or "", "lessons_count": len(s.get("lessons") or [])} for s in weeks]

    return render_template(
        "index.html",
        course=c,
        err=None,
        weeks=weeks_meta,
        modules_count=modules_count,
        lessons_count=lessons_count,
        duration_total=duration_total,
        first_uid=first_uid
    )

@app.get("/course/<int:course_id>")
@app.get("/course/<int:course_id>-<slug>")
def course_detail(course_id: int, slug: Optional[str] = None):
    row = fetch_one("""
        SELECT id, title, is_published, published_at, created_at, structure
        FROM courses WHERE id = %s;
    """, (course_id,))
    if not row: abort(404)
    st = ensure_structure(row.get("structure"))
    sections = st.get("sections", [])
    row["duration_total"] = format_duration(total_course_duration(st))
    row["lessons_count"] = len(flatten_lessons(st))
    row["level"] = st.get("level") or "All levels"
    row["category"] = st.get("category") or "Artificial Intelligence"
    row["thumbnail_url"] = st.get("thumbnail_url") or COURSE_COVER
    row["slug"] = slugify(row.get("title") or f"course-{course_id}")
    return render_template(
        "course_detail.html",
        course=row,
        sections=sections,
        instructors=st.get("instructors") or [],
        what_learn=st.get("what_you_will_learn") or [],
        description_md=st.get("description_md") or ""
    )

@app.get("/learn/<int:course_id>")
def learn_redirect_to_first(course_id: int):
    row = fetch_one("SELECT id, structure FROM courses WHERE id = %s;", (course_id,))
    if not row: abort(404)
    st = ensure_structure(row.get("structure"))
    uid = first_lesson_uid(st)
    if not uid:
        return redirect(url_for("course_detail", course_id=course_id))
    return redirect(url_for("learn_lesson", course_id=course_id, lesson_uid=uid))

@app.get("/learn/<int:course_id>/<lesson_uid>")
def learn_lesson(course_id: int, lesson_uid: str):
    row = fetch_one("SELECT id, title, structure FROM courses WHERE id = %s;", (course_id,))
    if not row: abort(404)
    st = ensure_structure(row.get("structure"))
    secs = st.get("sections") or []
    si, li = find_lesson(st, lesson_uid)
    if si is None:
        uid = first_lesson_uid(st)
        if uid: return redirect(url_for("learn_lesson", course_id=course_id, lesson_uid=uid))
        return redirect(url_for("course_detail", course_id=course_id))
    section = secs[si]
    lesson = (section.get("lessons") or [])[li]
    prev_uid, next_uid = next_prev_uids(st, lesson_uid)
    course_meta = {
        "id": row["id"],
        "title": row.get("title"),
        "slug": slugify(row.get("title") or f"course-{course_id}"),
        "duration_total": format_duration(total_course_duration(st)),
        "lessons_count": len(flatten_lessons(st)),
    }
    return render_template(
        "learn.html",
        course=course_meta,
        sections=secs,
        current_section_index=si,
        current_lesson_uid=str(lesson_uid),
        lesson=lesson,
        prev_uid=prev_uid,
        next_uid=next_uid
    )

# -----------------------------------------------------------------------------
# Admin (Builder + Raw JSON)
# -----------------------------------------------------------------------------
def get_or_create_admin_user_id() -> int:
    row = fetch_one("SELECT id FROM users WHERE email = %s;", (ADMIN_EMAIL,))
    if row:
        return row["id"]
    rows = execute_returning("""
        INSERT INTO users (email, full_name, role)
        VALUES (%s, %s, 'admin')
        ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name
        RETURNING id;
    """, (ADMIN_EMAIL, "Portal Admin"))
    return rows[0]["id"]

@app.get("/admin")
def admin_home():
    require_admin()
    course = fetch_one("""
        SELECT id, title, is_published, published_at, created_at
        FROM courses WHERE title = %s LIMIT 1;
    """, (COURSE_TITLE,))
    return render_template("admin.html", course=course, msg=request.args.get("msg"), err=request.args.get("err"))

@app.get("/admin/seed")
def admin_seed():
    require_admin()
    try:
        cid = seed_course_if_missing()
        return redirect(url_for("admin_builder", course_id=cid, msg="Course seeded"))
    except Exception as e:
        return redirect(url_for("admin_home", err=f"Seed failed: {e}"))

@app.get("/admin/course/<int:course_id>/edit")
def admin_edit_course(course_id: int):
    require_admin()
    row = fetch_one("""
        SELECT id, title, is_published, published_at, created_at, structure
        FROM courses WHERE id = %s;
    """, (course_id,))
    if not row: abort(404)
    structure = ensure_structure(row.get("structure"))
    return render_template("admin_edit_course.html", course=row,
                           structure_text=json.dumps(structure, indent=2),
                           msg=request.args.get("msg"), err=request.args.get("err"))

@app.post("/admin/course/<int:course_id>/edit")
def admin_edit_course_post(course_id: int):
    require_admin()
    try:
        title = (request.form.get("title") or "").strip()
        is_published = bool(request.form.get("is_published"))
        structure_text = request.form.get("structure_json") or "{}"
        structure = json.loads(structure_text)
        execute("""
            UPDATE courses
            SET title = %s,
                is_published = %s,
                published_at = CASE WHEN %s THEN COALESCE(published_at, now()) ELSE NULL END,
                structure = %s
            WHERE id = %s;
        """, (title, is_published, is_published, json.dumps(structure), course_id))
        return redirect(url_for("admin_edit_course", course_id=course_id, msg="Saved"))
    except Exception as e:
        return redirect(url_for("admin_edit_course", course_id=course_id, err=f"Save failed: {e}"))

@app.get("/admin/course/<int:course_id>/builder")
def admin_builder(course_id: int):
    require_admin()
    row = fetch_one("SELECT id, title, structure FROM courses WHERE id = %s;", (course_id,))
    if not row: abort(404)
    st = ensure_structure(row.get("structure"))
    return render_template("admin_builder.html", course=row, sections=st.get("sections") or [],
                           msg=request.args.get("msg"), err=request.args.get("err"))

def _save_structure(course_id: int, structure: Dict[str, Any]):
    execute("UPDATE courses SET structure = %s WHERE id = %s;", (json.dumps(structure), course_id))

@app.post("/admin/course/<int:course_id>/add-week")
def admin_add_week(course_id: int):
    require_admin()
    title = (request.form.get("title") or "").strip() or f"Week {uuid.uuid4().hex[:4]}"
    row = fetch_one("SELECT structure FROM courses WHERE id = %s;", (course_id,))
    st = ensure_structure(row.get("structure"))
    secs = st.get("sections") or []
    order = (max([s.get("order", 0) for s in secs]) + 1) if secs else 1
    secs.append({"title": title, "order": order, "lessons": []})
    st["sections"] = secs
    _save_structure(course_id, st)
    return redirect(url_for("admin_builder", course_id=course_id, msg="Week added"))

@app.post("/admin/course/<int:course_id>/add-lesson")
def admin_add_lesson(course_id: int):
    require_admin()
    week_index = int(request.form.get("week_index", "0"))
    kind = request.form.get("kind") or "article"
    title = (request.form.get("title") or kind.title()).strip()
    uid = str(uuid.uuid4())

    if kind == "article":
        content = {"body_md": (request.form.get("body_md") or "<h2>Article</h2>").strip()}
    elif kind == "video":
        url = (request.form.get("video_url") or "").strip()
        duration = request.form.get("duration_sec")
        content = {
            "provider": "url" if url else "upload",
            "url": url,
            "duration_sec": int(duration) if (duration or "").isdigit() else None,
            "notes_md": (request.form.get("notes_md") or "").strip()
        }
    elif kind == "quiz":
        try:
            content = json.loads(request.form.get("quiz_json") or '{"questions":[]}')
        except Exception:
            content = {"questions": []}
    elif kind == "assignment":
        content = {
            "instructions_md": (request.form.get("instructions_md") or "<h3>Assignment</h3>").strip(),
            "resource_url": (request.form.get("resource_url") or "").strip()
        }
    else:
        content = {}

    row = fetch_one("SELECT structure FROM courses WHERE id = %s;", (course_id,))
    st = ensure_structure(row.get("structure"))
    secs = st.get("sections") or []
    if week_index < 0 or week_index >= len(secs):
        return redirect(url_for("admin_builder", course_id=course_id, err="Invalid week index"))
    lessons = secs[week_index].get("lessons") or []
    order = (max([l.get("order", 0) for l in lessons]) + 1) if lessons else 1
    lessons.append({"lesson_uid": uid, "title": title, "kind": kind, "order": order, "content": content})
    secs[week_index]["lessons"] = lessons
    st["sections"] = secs
    _save_structure(course_id, st)
    return redirect(url_for("admin_builder", course_id=course_id, msg=f"Added {kind}"))

@app.post("/admin/course/<int:course_id>/remove-lesson")
def admin_remove_lesson(course_id: int):
    require_admin()
    week_index = int(request.form.get("week_index", "0"))
    lesson_uid = request.form.get("lesson_uid") or ""
    row = fetch_one("SELECT structure FROM courses WHERE id = %s;", (course_id,))
    st = ensure_structure(row.get("structure"))
    secs = st.get("sections") or []
    if week_index < 0 or week_index >= len(secs):
        return redirect(url_for("admin_builder", course_id=course_id, err="Invalid week index"))
    lessons = secs[week_index].get("lessons") or []
    lessons = [l for l in lessons if str(l.get("lesson_uid")) != str(lesson_uid)]
    secs[week_index]["lessons"] = lessons
    st["sections"] = secs
    _save_structure(course_id, st)
    return redirect(url_for("admin_builder", course_id=course_id, msg="Lesson removed"))

# -----------------------------------------------------------------------------
# Local dev entry
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
