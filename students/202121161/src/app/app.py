"""스마트홈 가이드 - 집 구조를 입력받아 규칙 기반으로 스마트홈 제품을 추천하는 웹 서비스.

- DB_HOST 환경변수가 있으면 PostgreSQL(OpenShift), 없으면 SQLite(로컬 개발)를 사용한다.
- 추천 규칙은 rules.json(ConfigMap)에서 읽는다.
"""
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

APP_DIR = os.environ.get("APP_DIR", os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(APP_DIR, "..", "static"))
RULES_PATH = os.environ.get("RULES_PATH", os.path.join(APP_DIR, "rules.json"))

DB_HOST = os.environ.get("DB_HOST")
USE_PG = bool(DB_HOST)
SQLITE_PATH = os.environ.get("SQLITE_PATH", os.path.join(APP_DIR, "..", "local.db"))

app = Flask(__name__, template_folder=APP_DIR, static_folder=STATIC_DIR)
app.secret_key = os.environ.get("SECRET_KEY", "local-dev-secret")
app.config["APP_TITLE"] = os.environ.get("APP_TITLE", "스마트홈 가이드")

with open(RULES_PATH, encoding="utf-8") as f:
    RULES = json.load(f)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")
KST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------- DB

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(20) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS plans (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(50) NOT NULL,
    house_input TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(20) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(50) NOT NULL,
    house_input TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def connect():
    if USE_PG:
        import psycopg2
        return psycopg2.connect(
            host=DB_HOST,
            port=int(os.environ.get("DB_PORT", "5432")),
            dbname=os.environ.get("DB_NAME", "smarthome"),
            user=os.environ.get("DB_USER", "smarthome"),
            password=os.environ.get("DB_PASSWORD", ""),
            connect_timeout=3,
        )
    conn = sqlite3.connect(SQLITE_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, params=(), one=False, commit=False):
    """SQL은 SQLite 형식(?)으로 작성하고, PostgreSQL이면 %s로 바꿔 실행한다."""
    if USE_PG:
        sql = sql.replace("?", "%s")
    db = get_db()
    cur = db.cursor()
    cur.execute(sql, params)
    rows = []
    if cur.description:
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if commit:
        db.commit()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def init_db(retries=30, delay=2):
    """DB Pod가 먼저 뜨지 않았을 수 있으므로 연결될 때까지 재시도한 뒤 테이블을 만든다."""
    for attempt in range(1, retries + 1):
        try:
            conn = connect()
            break
        except Exception as e:  # noqa: BLE001
            print(f"[init_db] DB 연결 대기 중 ({attempt}/{retries}): {e}", flush=True)
            time.sleep(delay)
    else:
        raise RuntimeError("DB에 연결할 수 없습니다.")
    cur = conn.cursor()
    if USE_PG:
        cur.execute(SCHEMA_PG)
    else:
        cur.executescript(SCHEMA_SQLITE)
    conn.commit()
    conn.close()
    print(f"[init_db] 준비 완료 ({'PostgreSQL' if USE_PG else 'SQLite'})", flush=True)


# ---------------------------------------------------------------- 추천 엔진

def matches(cond, house):
    if cond.get("always"):
        return True
    if "space" in cond and cond["space"] not in house["spaces"]:
        return False
    if "household" in cond and cond["household"] not in house["household"]:
        return False
    if "priority" in cond and cond["priority"] not in house["priorities"]:
        return False
    if "housing" in cond and house["housing"] not in cond["housing"]:
        return False
    if "min_area" in cond and house["area"] < cond["min_area"]:
        return False
    if "min_rooms" in cond and house["rooms"] < cond["min_rooms"]:
        return False
    return True


def resolve_qty(qty, house):
    if qty == "rooms":
        return max(1, house["rooms"])
    return int(qty)


def recommend(house):
    """규칙을 위에서부터 적용해 공간(group)별 추천 목록을 만들고 예산에 맞춰 표시한다."""
    products = RULES["products"]
    picked = {}  # product_id -> item
    for rule in RULES["rules"]:
        if not matches(rule["when"], house):
            continue
        for entry in rule["products"]:
            pid, qty = entry["id"], resolve_qty(entry.get("qty", 1), house)
            if pid in picked:
                item = picked[pid]
                item["qty"] = max(item["qty"], qty)
                if rule["reason"] not in item["reasons"]:
                    item["reasons"].append(rule["reason"])
                continue
            p = products[pid]
            picked[pid] = {
                "id": pid,
                "name": p["name"],
                "desc": p["desc"],
                "image": p["image"],
                "price": p["price"],
                "priority": p["priority"],
                "group": rule["group"],
                "qty": qty,
                "reasons": [rule["reason"]],
            }

    budget = RULES["budgets"][house["budget"]]["limit"]
    total = 0
    in_budget_total = 0
    for item in sorted(picked.values(), key=lambda x: (x["priority"], x["price"])):
        item["subtotal"] = item["price"] * item["qty"]
        total += item["subtotal"]
        if budget is None or in_budget_total + item["subtotal"] <= budget:
            item["in_budget"] = True
            in_budget_total += item["subtotal"]
        else:
            item["in_budget"] = False

    groups = []
    for gid in RULES["group_order"]:
        items = [i for i in picked.values() if i["group"] == gid]
        if items:
            items.sort(key=lambda x: (not x["in_budget"], x["priority"]))
            groups.append({"id": gid, "label": RULES["groups"][gid], "items": items})

    return {
        "groups": groups,
        "count": len(picked),
        "total": total,
        "in_budget_total": in_budget_total,
        "budget": budget,
    }


DEFAULT_HOUSE = {
    "housing": "apartment", "area": 24, "rooms": 2,
    "spaces": ["living", "kitchen", "bedroom", "bathroom", "entrance"],
    "household": [], "priorities": [], "budget": "mid",
}


def parse_house(form):
    def as_int(name, lo, hi, default):
        try:
            return min(hi, max(lo, int(form.get(name, default))))
        except ValueError:
            return default

    options = RULES["options"]
    housing = form.get("housing", "apartment")
    budget = form.get("budget", "mid")
    return {
        "housing": housing if housing in options["housing"] else "apartment",
        "area": as_int("area", 5, 200, 24),
        "rooms": as_int("rooms", 1, 8, 2),
        "spaces": [s for s in form.getlist("spaces") if s in options["spaces"]],
        "household": [h for h in form.getlist("household") if h in options["household"]],
        "priorities": [p for p in form.getlist("priorities") if p in options["priorities"]],
        "budget": budget if budget in RULES["budgets"] else "mid",
    }


@app.template_filter("won")
def won(value):
    return f"{value:,}원"


@app.context_processor
def inject_globals():
    return {"options": RULES["options"], "budgets": RULES["budgets"],
            "current_user": session.get("username")}


# ---------------------------------------------------------------- 인증

def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("로그인이 필요합니다.")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not USERNAME_RE.match(username):
            flash("아이디는 영문, 숫자, _ 로 3~20자여야 합니다.")
        elif len(password) < 6:
            flash("비밀번호는 6자 이상이어야 합니다.")
        elif query("SELECT id FROM users WHERE username = ?", (username,), one=True):
            flash("이미 사용 중인 아이디입니다.")
        else:
            query("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                  (username, generate_password_hash(password)), commit=True)
            flash("가입이 완료되었습니다. 로그인해 주세요.")
            return redirect(url_for("login"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = query("SELECT id, username, password_hash FROM users WHERE username = ?",
                     (username,), one=True)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            next_url = request.args.get("next", "")
            return redirect(next_url if next_url.startswith("/") else url_for("plan"))
        flash("아이디 또는 비밀번호가 올바르지 않습니다.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------------------------------------------------------- 페이지

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/plan", methods=["GET", "POST"])
@login_required
def plan():
    if request.method == "POST":
        house = parse_house(request.form)
        title = request.form.get("title", "").strip()[:50] or "우리집 스마트홈 플랜"
        if not house["spaces"]:
            flash("공간을 하나 이상 선택해 주세요.")
            return render_template("plan.html", house=house, title=title)
        result = recommend(house)
        row = query(
            "INSERT INTO plans (user_id, title, house_input, result, created_at) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            (session["user_id"], title, json.dumps(house, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False),
             datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")),
            one=True, commit=True)
        return redirect(url_for("plan_detail", plan_id=row["id"]))
    return render_template("plan.html", house=DEFAULT_HOUSE, title="")


@app.route("/history")
@login_required
def history():
    plans = query(
        "SELECT id, title, house_input, result, created_at FROM plans "
        "WHERE user_id = ? ORDER BY id DESC", (session["user_id"],))
    for p in plans:
        p["house"] = json.loads(p["house_input"])
        p["summary"] = json.loads(p["result"])
    return render_template("history.html", plans=plans)


def load_plan(plan_id):
    row = query("SELECT * FROM plans WHERE id = ? AND user_id = ?",
                (plan_id, session["user_id"]), one=True)
    if row is None:
        abort(404)
    return row


@app.route("/history/<int:plan_id>")
@login_required
def plan_detail(plan_id):
    row = load_plan(plan_id)
    return render_template("result.html", plan=row,
                           house=json.loads(row["house_input"]),
                           result=json.loads(row["result"]))


@app.route("/history/<int:plan_id>/delete", methods=["POST"])
@login_required
def plan_delete(plan_id):
    load_plan(plan_id)
    query("DELETE FROM plans WHERE id = ? AND user_id = ?",
          (plan_id, session["user_id"]), commit=True)
    flash("기록을 삭제했습니다.")
    return redirect(url_for("history"))


# ---------------------------------------------------------------- Probe

@app.route("/healthz")
def healthz():
    """Liveness: 웹 프로세스가 살아 있는지만 확인."""
    return {"status": "ok"}


@app.route("/readyz")
def readyz():
    """Readiness: DB까지 연결되어야 트래픽을 받는다."""
    try:
        query("SELECT 1 AS ok", one=True)
        return {"status": "ready", "db": "postgresql" if USE_PG else "sqlite"}
    except Exception as e:  # noqa: BLE001
        return {"status": "not ready", "error": str(e)}, 503


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
