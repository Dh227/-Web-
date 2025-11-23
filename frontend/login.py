import sqlite3
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, session, flash

app = Flask(__name__)

# 用于 session 加密，随便写一串字符就行，但是不要给别人看
app.secret_key = "your_secret_key_here"

BASE_DIR = Path(__file__).resolve().parent  # 当前目录：frontend
DATABASE_PATH = BASE_DIR / "users.db"


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the users table if it has not been created yet."""
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

def fetch_all_users():
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "SELECT id, username, phone, email, created_at FROM users ORDER BY id DESC"
        )
        return cur.fetchall()
    finally:
        conn.close()

# ---------------- 首页 / 测试用 ----------------
@app.route("/")
def index():
    # 如果已经登录，跳转到地图页；否则去登录页
    if "user_id" in session:
        return redirect(url_for("map_page"))
    return redirect(url_for("login"))


# ---------------- 登录接口（配合 login.html）----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # 1. 拿到表单数据（和 login.html 里面的 name 对应）
        username = request.form.get("username")
        password = request.form.get("password")

        # 2. 去数据库查这个用户
        conn = get_db_connection()
        try:
            cur = conn.execute(
                "SELECT id, username, password FROM users WHERE username = ?",
                (username,)
            )
            user = cur.fetchone()
        finally:
            conn.close()

        # 3. 校验密码（这里只是明文对比，你以后可以改成 hash 加密）
        if user and user["password"] == password:
            # 登录成功，把用户信息写入 session
            session["user_id"] = user["id"]
            session["username"] = user["username"]

            flash("登录成功！", "success")
            # 登录成功后跳转地图页面（你有 map.html 的话）
            return redirect(url_for("map_page"))
        else:
            # 登录失败，返回登录页并显示错误
            error = "用户名或密码错误"
            return render_template("login.html", error=error)

    # GET 请求：直接渲染登录页面
    return render_template("login.html")


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
                conn.execute(
                    """
                    INSERT INTO users (username, password, phone, email)
                    VALUES (?, ?, ?, ?)
                    """,
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

        # POST 失败时回显用户输入
        return render_template("register.html", error=error)

    return render_template("register.html")


# ---------------- 退出登录 ----------------
@app.route("/logout")
def logout():
    session.clear()
    flash("已退出登录", "info")
    return redirect(url_for("login"))


# ---------------- 示例：地图页面（需登录才能访问）----------------
@app.route("/map")
def map_page():
    # 简单的登录校验：没有登录就回登录页
    if "user_id" not in session:
        return redirect(url_for("login"))
    users = fetch_all_users()
    # 这里渲染你的地图页面，例如 map.html
    return render_template("map.html", username=session.get("username"), users=users)


# ---------------- 程序入口 ----------------
if __name__ == "__main__":
    app.run(debug=True)
