import sqlite3
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, session, flash

import login as login_helpers

app = Flask(__name__)
# 为 session 设置密钥，实际项目中请使用环境变量或更复杂的随机串
app.secret_key = "your_secret_key_here"

BASE_DIR = Path(__file__).resolve().parent  # 当前目录：frontend
DATABASE_PATH = BASE_DIR / "users.db"


def get_db_connection():
    """Create a SQLite connection with rows accessible by column name."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Ensure the users table exists before handling requests."""
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                phone TEXT,
                email TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


init_db()


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("map_page"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        conn = get_db_connection()
        try:
            cur = conn.execute(
                "SELECT id, username, password FROM users WHERE username = ?",
                (username,),
            )
            user = cur.fetchone()
        finally:
            conn.close()

        if user and user["password"] == password:
            session["user_id"] = user["id"]
            session["username"] = user["username"]

            flash("登录成功！", "success")
            return redirect(url_for("map_page"))

        error = "用户名或密码错误"
        return render_template("login.html", error=error)

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("已退出登录", "info")
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()

        if not username or not password:
            error = "用户名和密码不能为空"
        else:
            conn = get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO users (username, password, phone, email) VALUES (?, ?, ?, ?)",
                    (username, password, phone, email),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                error = "用户名已存在，请换一个"
            finally:
                conn.close()

            if not error:
                flash("注册成功，请使用新账户登录。", "success")
                return redirect(url_for("login"))

    return render_template("register.html", error=error)


@app.route("/map")
def map_page():
    if "user_id" not in session:
        return redirect(url_for("login"))
    # users 数据改为前端异步加载，不再通过模板注入
    return render_template("map.html", username=session.get("username"))


if __name__ == "__main__":
    app.run(debug=True)
