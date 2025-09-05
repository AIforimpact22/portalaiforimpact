import os
import json
import uuid
from contextlib import contextmanager
from datetime import datetime

from flask import (
    Flask, render_template, jsonify, abort,
    request, redirect, url_for
)
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# ---- Config from env (set in app.yaml) ----
INSTANCE_CONNECTION_NAME = os.getenv("INSTANCE_CONNECTION_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_NAME = os.getenv("DB_NAME")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@aiforimpact.local")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")  # optional simple protection; leave unset to disable

DB_HOST = f"/cloudsql/{INSTANCE_CONNECTION_NAME}" if INSTANCE_CONNECTION_NAME else None

# ---- Connection Pool (lazy) ----
_pg_pool: pool.SimpleConnectionPool | None = None

def init_pool():
    global _pg_pool
    if _pg_pool is None:
        if not all([DB_HOST, DB_NAME, DB_USER, DB_PASS]):
            raise RuntimeError("Database env vars are missing. Check app.yaml env_variables.")
        _pg_pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=5,
            host=DB_HOST,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            cursor_factory=RealDictCursor,
            connect_timeout=10,
            options="-c search_path=public"
        )

@contextmanager
def get_conn():
    """Borrow/return a connection from the pool (lazy-init on first use)."""
    if _pg_pool is None:
        init_pool()
    conn = _pg_pool.getconn()
    try:
        yield conn
    finally:
        _pg_pool.putconn(conn)

# ---- DB helpers ----
def fetch_all(q, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params or ())
            return cur.fetchall()

def fetch_one(q, params=None):
    rows = fetch_all(q, params)
    return rows[0] if rows else None

def execute(q, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params or ())
        conn.commit()

def execute_returning(q, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params or ())
            rows = cur.fetchall()
        conn.commit()
        return rows

# ---- Utility ----
def get_or_create_admin_user_id() -> int:
    """
    Ensure there's an admin user to satisfy courses.created_by FK.
    """
    row = fetch_one("SELECT id FROM users WHERE email = %s;", (ADMIN_EMAIL,))
    if row:
        return row["id"]
    # Create admin user
    rows = execute_returning("""
        INSERT INTO users (email, full_name, role)
        VALUES (%s, %s, 'admin')
        ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name
        RETURNING id;
    """, (ADMIN_EMAIL, "Portal Admin"))
    return rows[0]["id"]

def require_admin():
    """
    Optional very simple guard using a token. If ADMIN_TOKEN is set,
    require ?token=<ADMIN_TOKEN> query param on admin routes.
    """
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        abort(403)

def pretty_json(obj) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        return str(obj)

# ---- Public routes ----
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
    rows = fetch_all("""
        SELECT id, title, is_published, published_at, created_at
        FROM courses
        ORDER BY COALESCE(published_at, created_at) DESC
        LIMIT 100;
    """)
    for r in rows:
        for k in ("created_at", "published_at"):
            if r.get(k) and isinstance(r[k], datetime):
                r[k] = r[k].strftime("%Y-%m-%d %H:%M")
    return render_template("index.html", courses=rows)

@app.get("/api/courses")
def api_courses():
    rows = fetch_all("""
        SELECT id, title, is_published, published_at, created_at
        FROM courses
        ORDER BY COALESCE(published_at, created_at) DESC
        LIMIT 100;
    """)
    return jsonify(rows)

@app.get("/course/<int:course_id>")
def course_detail(course_id: int):
    row = fetch_one("""
        SELECT id, title, is_published, published_at, created_at, structure
        FROM courses
        WHERE id = %s;
    """, (course_id,))
    if not row:
        abort(404)

    structure = row.get("structure") or {}
    if isinstance(structure, str):
        try:
            structure = json.loads(structure)
        except Exception:
            structure = {"sections": []}

    sections = structure.get("sections", [])
    return render_template("course_detail.html", course=row, sections=sections)

# ---- Admin routes ----
@app.get("/admin")
def admin_home():
    require_admin()
    # list latest courses
    courses = fetch_all("""
        SELECT id, title, is_published, published_at, created_at
        FROM courses
        ORDER BY id DESC
        LIMIT 200;
    """)
    msg = request.args.get("msg")
    err = request.args.get("err")
    return render_template("admin.html", courses=courses, msg=msg, err=err)

@app.post("/admin/course/create")
def admin_create_course_quick():
    require_admin()
    try:
        title = (request.form.get("title") or "").strip()
        is_published = bool(request.form.get("is_published"))
        description_md = (request.form.get("description_md") or "").strip()
        section_title = (request.form.get("section_title") or "Section 1").strip()
        lesson_title = (request.form.get("lesson_title") or "Lesson 1").strip()
        lesson_kind = request.form.get("lesson_kind") or "video"

        # Build minimal structure
        lesson_uid = str(uuid.uuid4())
        content = {}
        if lesson_kind == "video":
            video_url = (request.form.get("video_url") or "").strip()
            duration_sec = request.form.get("duration_sec")
            content = {
                "provider": "url" if video_url else "upload",
                "url": video_url,
                "duration_sec": int(duration_sec) if (duration_sec or "").isdigit() else None,
                "notes_md": (request.form.get("notes_md") or "").strip()
            }
        elif lesson_kind == "article":
            content = {
                "body_md": (request.form.get("article_body_md") or "## Article\nWrite here...").strip()
            }
        else:
            # For assignment/quiz, recommend Raw JSON method
            return redirect(url_for("admin_home", err="Quick create supports Video or Article only. Use Raw JSON for others."))

        structure = {
            "description_md": description_md,
            "sections": [
                {
                    "title": section_title,
                    "order": 1,
                    "lessons": [
                        {
                            "lesson_uid": lesson_uid,
                            "title": lesson_title,
                            "kind": lesson_kind,
                            "order": 1,
                            "content": content
                        }
                    ]
                }
            ]
        }

        created_by = get_or_create_admin_user_id()
        rows = execute_returning("""
            INSERT INTO courses (title, created_by, is_published, published_at, structure)
            VALUES (%s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END, %s)
            RETURNING id;
        """, (title, created_by, is_published, is_published, json.dumps(structure)))
        new_id = rows[0]["id"]
        return redirect(url_for("admin_edit_course", course_id=new_id, msg="Course created"))
    except Exception as e:
        return redirect(url_for("admin_home", err=f"Create failed: {e}"))

@app.post("/admin/course/create-raw")
def admin_create_course_raw():
    require_admin()
    try:
        title = (request.form.get("title_raw") or "").strip()
        is_published = bool(request.form.get("is_published_raw"))
        structure_text = request.form.get("structure_json") or "{}"
        structure = json.loads(structure_text)  # will raise if invalid

        created_by = get_or_create_admin_user_id()
        rows = execute_returning("""
            INSERT INTO courses (title, created_by, is_published, published_at, structure)
            VALUES (%s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END, %s)
            RETURNING id;
        """, (title, created_by, is_published, is_published, json.dumps(structure)))
        new_id = rows[0]["id"]
        return redirect(url_for("admin_edit_course", course_id=new_id, msg="Course created"))
    except Exception as e:
        return redirect(url_for("admin_home", err=f"Create failed: {e}"))

@app.get("/admin/course/<int:course_id>/edit")
def admin_edit_course(course_id: int):
    require_admin()
    row = fetch_one("""
        SELECT id, title, is_published, published_at, created_at, structure
        FROM courses
        WHERE id = %s;
    """, (course_id,))
    if not row:
        abort(404)

    structure = row.get("structure") or {}
    if isinstance(structure, str):
        try:
            structure = json.loads(structure)
        except Exception:
            structure = {"sections": []}

    msg = request.args.get("msg")
    err = request.args.get("err")
    return render_template(
        "admin_edit_course.html",
        course=row,
        structure_text=pretty_json(structure),
        msg=msg,
        err=err
    )

@app.post("/admin/course/<int:course_id>/edit")
def admin_edit_course_post(course_id: int):
    require_admin()
    try:
        title = (request.form.get("title") or "").strip()
        is_published = bool(request.form.get("is_published"))
        structure_text = request.form.get("structure_json") or "{}"
        structure = json.loads(structure_text)  # validate JSON

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

@app.post("/admin/course/<int:course_id>/delete")
def admin_delete_course(course_id: int):
    require_admin()
    try:
        execute("DELETE FROM courses WHERE id = %s;", (course_id,))
        return redirect(url_for("admin_home", msg="Deleted"))
    except Exception as e:
        return redirect(url_for("admin_home", err=f"Delete failed: {e}"))

# Local dev
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
