import argparse
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


ROOT = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("BOUNCE_DATABASE", ROOT / "bounce.sqlite3"))
DATABASE_URL = os.environ.get("DATABASE_URL")
SESSION_COOKIE = "bounce_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
PASSWORD_ITERATIONS = 310_000
MATCHMAKING_TIMEOUT_SECONDS = 5 * 60
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")
MAX_BODY_SIZE = 16_384
RATE_LIMITS = {}
INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
if psycopg is not None:
    INTEGRITY_ERRORS += (psycopg.errors.UniqueViolation,)


class DatabaseConnection:
    def __init__(self, connection, is_postgres):
        self.connection = connection
        self.is_postgres = is_postgres

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exception_type, exception, traceback):
        try:
            return self.connection.__exit__(exception_type, exception, traceback)
        finally:
            if not self.is_postgres:
                self.connection.close()

    def execute(self, statement, parameters=()):
        if self.is_postgres:
            statement = statement.replace("?", "%s")
        return self.connection.execute(statement, parameters)

    def executescript(self, script):
        if not self.is_postgres:
            return self.connection.executescript(script)
        cursor = None
        for statement in script.split(";"):
            statement = statement.strip()
            if statement:
                cursor = self.execute(statement)
        return cursor

    def rollback(self):
        self.connection.rollback()


def connect_database():
    if DATABASE_URL:
        if psycopg is None:
            raise RuntimeError(
                "DATABASE_URL이 설정되어 있지만 psycopg가 설치되지 않았습니다."
            )
        connection = psycopg.connect(
            DATABASE_URL, connect_timeout=10, row_factory=dict_row
        )
        return DatabaseConnection(connection, True)
    if os.environ.get("BOUNCE_REQUIRE_POSTGRES") == "1":
        raise RuntimeError(
            "이 배포 환경은 PostgreSQL 연결이 필요합니다. DATABASE_URL을 설정해 주세요."
        )
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return DatabaseConnection(connection, False)


def begin_write_transaction(connection):
    if connection.is_postgres:
        connection.execute("SELECT pg_advisory_xact_lock(984592001)")
    else:
        connection.execute("BEGIN IMMEDIATE")


def initialize_postgres_database():
    schema = """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            password_salt BYTEA NOT NULL,
            password_hash BYTEA NOT NULL,
            created_at BIGINT NOT NULL,
            points BIGINT NOT NULL DEFAULT 0,
            points_initialized INTEGER NOT NULL DEFAULT 0,
            best_catches BIGINT NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_unique
        ON users (LOWER(username));
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash BYTEA PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS matchmaking_queue (
            user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            queued_at BIGINT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS friends (
            user1_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            user2_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at BIGINT NOT NULL,
            PRIMARY KEY (user1_id, user2_id),
            CHECK (user1_id < user2_id)
        );
        CREATE TABLE IF NOT EXISTS friend_requests (
            sender_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            recipient_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at BIGINT NOT NULL,
            PRIMARY KEY (sender_id, recipient_id),
            CHECK (sender_id != recipient_id)
        );
        CREATE TABLE IF NOT EXISTS matches (
            id TEXT PRIMARY KEY,
            player1_id BIGINT NOT NULL REFERENCES users(id),
            player2_id BIGINT NOT NULL REFERENCES users(id),
            player1_score BIGINT NOT NULL DEFAULT 0,
            player2_score BIGINT NOT NULL DEFAULT 0,
            player1_finished INTEGER NOT NULL DEFAULT 0,
            player2_finished INTEGER NOT NULL DEFAULT 0,
            player1_seen BIGINT NOT NULL,
            player2_seen BIGINT NOT NULL,
            player1_balls TEXT NOT NULL DEFAULT '[]',
            player2_balls TEXT NOT NULL DEFAULT '[]',
            started_at_ms BIGINT NOT NULL DEFAULT 0,
            player1_finished_at_ms BIGINT,
            player2_finished_at_ms BIGINT,
            status TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            completed_at BIGINT,
            reward_awarded INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS matches_active_players
        ON matches(status, player1_id, player2_id);
    """
    with connect_database() as connection:
        connection.executescript(schema)
        match_migrations = {
            "player1_balls": "TEXT NOT NULL DEFAULT '[]'",
            "player2_balls": "TEXT NOT NULL DEFAULT '[]'",
            "started_at_ms": "BIGINT NOT NULL DEFAULT 0",
            "player1_finished_at_ms": "BIGINT",
            "player2_finished_at_ms": "BIGINT",
            "reward_awarded": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in match_migrations.items():
            connection.execute(
                "ALTER TABLE matches ADD COLUMN IF NOT EXISTS %s %s"
                % (column, definition)
            )
        user_migrations = {
            "points": "BIGINT NOT NULL DEFAULT 0",
            "points_initialized": "INTEGER NOT NULL DEFAULT 0",
            "best_catches": "BIGINT NOT NULL DEFAULT 0",
        }
        for column, definition in user_migrations.items():
            connection.execute(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS %s %s"
                % (column, definition)
            )
        connection.execute(
            "UPDATE matches SET started_at_ms = created_at * 1000 WHERE started_at_ms = 0"
        )


def initialize_database():
    if DATABASE_URL:
        initialize_postgres_database()
        return
    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with connect_database() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                points_initialized INTEGER NOT NULL DEFAULT 0,
                best_catches INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash BLOB PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS matchmaking_queue (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                queued_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS friends (
                user1_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                user2_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (user1_id, user2_id),
                CHECK (user1_id < user2_id)
            );
            CREATE TABLE IF NOT EXISTS friend_requests (
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (sender_id, recipient_id),
                CHECK (sender_id != recipient_id)
            );
            CREATE TABLE IF NOT EXISTS matches (
                id TEXT PRIMARY KEY,
                player1_id INTEGER NOT NULL REFERENCES users(id),
                player2_id INTEGER NOT NULL REFERENCES users(id),
                player1_score INTEGER NOT NULL DEFAULT 0,
                player2_score INTEGER NOT NULL DEFAULT 0,
                player1_finished INTEGER NOT NULL DEFAULT 0,
                player2_finished INTEGER NOT NULL DEFAULT 0,
                player1_seen INTEGER NOT NULL,
                player2_seen INTEGER NOT NULL,
                player1_balls TEXT NOT NULL DEFAULT '[]',
                player2_balls TEXT NOT NULL DEFAULT '[]',
                started_at_ms INTEGER NOT NULL DEFAULT 0,
                player1_finished_at_ms INTEGER,
                player2_finished_at_ms INTEGER,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                reward_awarded INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS matches_active_players
            ON matches(status, player1_id, player2_id);
            """
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(matches)")
        }
        migrations = {
            "player1_balls": "TEXT NOT NULL DEFAULT '[]'",
            "player2_balls": "TEXT NOT NULL DEFAULT '[]'",
            "started_at_ms": "INTEGER NOT NULL DEFAULT 0",
            "player1_finished_at_ms": "INTEGER",
            "player2_finished_at_ms": "INTEGER",
        }
        for column, definition in migrations.items():
            if column not in columns:
                connection.execute(
                    "ALTER TABLE matches ADD COLUMN %s %s" % (column, definition)
                )
        user_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(users)")
        }
        user_migrations = {
            "points": "INTEGER NOT NULL DEFAULT 0",
            "points_initialized": "INTEGER NOT NULL DEFAULT 0",
            "best_catches": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in user_migrations.items():
            if column not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN %s %s" % (column, definition)
                )
        if "reward_awarded" not in columns:
            connection.execute(
                "ALTER TABLE matches ADD COLUMN reward_awarded INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            "UPDATE matches SET started_at_ms = created_at * 1000 WHERE started_at_ms = 0"
        )


def derive_password(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )


def hash_token(token):
    return hashlib.sha256(token.encode("ascii")).digest()


class BounceHandler(BaseHTTPRequestHandler):
    server_version = "BounceServer/1.0"

    def log_message(self, format_string, *args):
        print("%s - %s" % (self.address_string(), format_string % args))

    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if extra_headers:
            for name, value in extra_headers:
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json(status, {"error": message})

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("요청 형식이 올바르지 않습니다.")
        if length < 0 or length > MAX_BODY_SIZE:
            raise ValueError("요청이 너무 큽니다.")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("요청 형식이 올바르지 않습니다.")
        if not isinstance(payload, dict):
            raise ValueError("요청 형식이 올바르지 않습니다.")
        return payload

    def origin_is_valid(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        expected_scheme = "https" if os.environ.get("BOUNCE_HTTPS") == "1" else "http"
        expected_origin = "%s://%s" % (expected_scheme, self.headers.get("Host", ""))
        return hmac.compare_digest(origin.rstrip("/"), expected_origin.rstrip("/"))

    def client_key(self):
        return self.client_address[0]

    def allow_auth_attempt(self):
        now = int(time.time())
        key = self.client_key()
        attempts = [stamp for stamp in RATE_LIMITS.get(key, []) if now - stamp < 900]
        if len(attempts) >= 12:
            RATE_LIMITS[key] = attempts
            return False
        attempts.append(now)
        RATE_LIMITS[key] = attempts
        if len(RATE_LIMITS) > 2048:
            for old_key in list(RATE_LIMITS)[:512]:
                RATE_LIMITS.pop(old_key, None)
        return True

    def current_user(self, connection):
        cookie_header = self.headers.get("Cookie", "")
        try:
            cookie = cookies.SimpleCookie()
            cookie.load(cookie_header)
            token = cookie[SESSION_COOKIE].value
        except (cookies.CookieError, KeyError):
            return None
        row = connection.execute(
            """
            SELECT users.*
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            (hash_token(token), int(time.time())),
        ).fetchone()
        return row

    def require_user(self, connection):
        user = self.current_user(connection)
        if not user:
            self.send_error_json(401, "로그인이 필요합니다.")
            return None
        return user

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.serve_game()
        elif path == "/api/me":
            with connect_database() as connection:
                user = self.current_user(connection)
                self.send_json(
                    200,
                    {
                        "username": user["username"],
                        "points": user["points"],
                        "pointsInitialized": bool(user["points_initialized"]),
                        "bestCatches": user["best_catches"],
                    } if user else {"username": None},
                )
        elif path == "/api/leaderboards":
            self.get_leaderboards()
        elif path == "/api/matchmaking":
            self.get_matchmaking_status()
        elif path == "/api/friends":
            self.get_friends()
        elif path.startswith("/api/matches/"):
            self.get_match_status(path.removeprefix("/api/matches/"))
        else:
            self.send_error_json(404, "요청한 주소를 찾을 수 없습니다.")

    def do_POST(self):
        if not self.origin_is_valid():
            self.send_error_json(403, "요청 출처를 확인할 수 없습니다.")
            return
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
        except ValueError as error:
            self.send_error_json(400, str(error))
            return
        if path in ("/api/register", "/api/login") and not self.allow_auth_attempt():
            self.send_error_json(429, "잠시 후 다시 시도해 주세요.")
        elif path == "/api/register":
            self.register(payload)
        elif path == "/api/login":
            self.login(payload)
        elif path == "/api/logout":
            self.logout()
        elif path == "/api/points":
            self.update_points(payload)
        elif path == "/api/points/initialize":
            self.initialize_points(payload)
        elif path == "/api/progress":
            self.update_progress(payload)
        elif path == "/api/matchmaking":
            self.join_matchmaking()
        elif path == "/api/friend-requests":
            self.send_friend_request(payload)
        elif path.startswith("/api/friend-requests/"):
            request_path = path.strip("/").split("/")
            if len(request_path) == 4 and request_path[3] == "accept":
                self.respond_friend_request(request_path[2], True)
            else:
                self.send_error_json(404, "요청한 주소를 찾을 수 없습니다.")
        elif path.startswith("/api/matches/") and path.endswith("/state"):
            match_id = path[len("/api/matches/") : -len("/state")].strip("/")
            self.update_match_state(match_id, payload)
        else:
            self.send_error_json(404, "요청한 주소를 찾을 수 없습니다.")

    def do_DELETE(self):
        if not self.origin_is_valid():
            self.send_error_json(403, "요청 출처를 확인할 수 없습니다.")
            return
        path = urlparse(self.path).path
        if path == "/api/matchmaking":
            with connect_database() as connection:
                user = self.require_user(connection)
                if not user:
                    return
                connection.execute(
                    "DELETE FROM matchmaking_queue WHERE user_id = ?", (user["id"],)
                )
            self.send_json(200, {"ok": True})
        elif path.startswith("/api/friend-requests/"):
            request_path = path.strip("/").split("/")
            if len(request_path) == 3:
                self.delete_friend_request(request_path[2])
            else:
                self.send_error_json(404, "요청한 주소를 찾을 수 없습니다.")
        elif path.startswith("/api/friends/"):
            username = path[len("/api/friends/") :]
            if username:
                self.remove_friend(username)
            else:
                self.send_error_json(404, "요청한 주소를 찾을 수 없습니다.")
        else:
            self.send_error_json(404, "요청한 주소를 찾을 수 없습니다.")

    def serve_game(self):
        try:
            content = (ROOT / "bounce.html").read_bytes()
        except OSError:
            self.send_error(500, "게임 파일을 읽지 못했습니다.")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(content)

    def read_credentials(self, payload):
        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not USERNAME_PATTERN.fullmatch(username):
            raise ValueError("닉네임은 영문, 숫자, 밑줄로 3~16자여야 합니다.")
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise ValueError("비밀번호는 8~128자로 입력해 주세요.")
        return username, password

    def read_points(self, value, label="포인트"):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > 1_000_000_000
        ):
            raise ValueError("%s 값이 올바르지 않습니다." % label)
        return value

    def account_payload(self, user):
        return {
            "username": user["username"],
            "points": user["points"],
            "pointsInitialized": bool(user["points_initialized"]),
            "bestCatches": user["best_catches"],
        }

    def register(self, payload):
        try:
            username, password = self.read_credentials(payload)
            initial_points = self.read_points(payload.get("points", 0))
        except ValueError as error:
            self.send_error_json(400, str(error))
            return
        salt = secrets.token_bytes(16)
        password_hash = derive_password(password, salt)
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        try:
            with connect_database() as connection:
                insert_user = """
                    INSERT INTO users(
                        username, password_salt, password_hash, created_at,
                        points, points_initialized
                    ) VALUES (?, ?, ?, ?, ?, 1)
                """
                if connection.is_postgres:
                    user_id = connection.execute(
                        insert_user + " RETURNING id",
                        (username, salt, password_hash, now, initial_points),
                    ).fetchone()["id"]
                else:
                    cursor = connection.execute(
                        insert_user,
                        (username, salt, password_hash, now, initial_points),
                    )
                    user_id = cursor.lastrowid
                connection.execute(
                    "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                    (hash_token(token), user_id, now + SESSION_SECONDS),
                )
                user = connection.execute(
                    "SELECT * FROM users WHERE id = ?", (user_id,)
                ).fetchone()
        except INTEGRITY_ERRORS:
            self.send_error_json(409, "이미 사용 중인 닉네임입니다.")
            return
        self.send_json(
            201,
            self.account_payload(user),
            [("Set-Cookie", self.session_cookie_value(token, SESSION_SECONDS))],
        )

    def session_cookie_value(self, token, max_age):
        secure = "; Secure" if os.environ.get("BOUNCE_HTTPS") == "1" else ""
        return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" % (
            SESSION_COOKIE,
            token,
            max_age,
            secure,
        )

    def login(self, payload):
        try:
            username, password = self.read_credentials(payload)
            initial_points = self.read_points(payload.get("points", 0))
        except ValueError as error:
            self.send_error_json(400, str(error))
            return
        now = int(time.time())
        with connect_database() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE LOWER(username) = LOWER(?)",
                (username,),
            ).fetchone()
            if not row or not hmac.compare_digest(
                derive_password(password, row["password_salt"]), row["password_hash"]
            ):
                self.send_error_json(401, "닉네임 또는 비밀번호가 올바르지 않습니다.")
                return
            if not row["points_initialized"]:
                connection.execute(
                    """
                    UPDATE users SET points = ?, points_initialized = 1 WHERE id = ?
                    """,
                    (initial_points, row["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM users WHERE id = ?", (row["id"],)
                ).fetchone()
            token = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (hash_token(token), row["id"], now + SESSION_SECONDS),
            )
        self.send_json(
            200,
            self.account_payload(row),
            [("Set-Cookie", self.session_cookie_value(token, SESSION_SECONDS))],
        )

    def initialize_points(self, payload):
        try:
            initial_points = self.read_points(payload.get("points", 0))
        except ValueError as error:
            self.send_error_json(400, str(error))
            return
        with connect_database() as connection:
            user = self.require_user(connection)
            if not user:
                return
            connection.execute(
                """
                UPDATE users
                SET points = ?, points_initialized = 1
                WHERE id = ? AND points_initialized = 0
                """,
                (initial_points, user["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user["id"],)
            ).fetchone()
        self.send_json(200, self.account_payload(updated))

    def update_points(self, payload):
        delta = payload.get("delta")
        if (
            not isinstance(delta, int)
            or isinstance(delta, bool)
            or abs(delta) > 1_000_000
        ):
            self.send_error_json(400, "포인트 변경 값이 올바르지 않습니다.")
            return
        with connect_database() as connection:
            begin_write_transaction(connection)
            user = self.require_user(connection)
            if not user:
                connection.rollback()
                return
            connection.execute(
                """
                UPDATE users
                SET points = CASE WHEN points + ? < 0 THEN 0 ELSE points + ? END,
                    points_initialized = 1
                WHERE id = ?
                """,
                (delta, delta, user["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user["id"],)
            ).fetchone()
        self.send_json(200, self.account_payload(updated))

    def update_progress(self, payload):
        try:
            best_catches = self.read_points(
                payload.get("bestCatches"), "받은 횟수"
            )
        except ValueError as error:
            self.send_error_json(400, str(error))
            return
        with connect_database() as connection:
            user = self.require_user(connection)
            if not user:
                return
            connection.execute(
                """
                UPDATE users
                SET best_catches = CASE
                    WHEN best_catches < ? THEN ? ELSE best_catches
                END
                WHERE id = ?
                """,
                (best_catches, best_catches, user["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user["id"],)
            ).fetchone()
        self.send_json(200, self.account_payload(updated))

    def get_leaderboards(self):
        with connect_database() as connection:
            most_catches = connection.execute(
                """
                SELECT username, best_catches AS value
                FROM users
                WHERE best_catches > 0
                ORDER BY best_catches DESC, LOWER(username)
                LIMIT 10
                """
            ).fetchall()
            most_points = connection.execute(
                """
                SELECT username, points AS value
                FROM users
                WHERE points > 0
                ORDER BY points DESC, LOWER(username)
                LIMIT 10
                """
            ).fetchall()
        self.send_json(
            200,
            {
                "mostCaught": [
                    {"username": row["username"], "value": row["value"]}
                    for row in most_catches
                ],
                "mostPoints": [
                    {"username": row["username"], "value": row["value"]}
                    for row in most_points
                ],
            },
        )

    def logout(self):
        with connect_database() as connection:
            cookie_header = self.headers.get("Cookie", "")
            try:
                cookie = cookies.SimpleCookie()
                cookie.load(cookie_header)
                token = cookie[SESSION_COOKIE].value
                user = connection.execute(
                    "SELECT user_id FROM sessions WHERE token_hash = ?", (hash_token(token),)
                ).fetchone()
                connection.execute(
                    "DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),)
                )
                if user:
                    connection.execute(
                        "DELETE FROM matchmaking_queue WHERE user_id = ?", (user["user_id"],)
                    )
            except (cookies.CookieError, KeyError):
                pass
        self.send_json(
            200,
            {"ok": True},
            [("Set-Cookie", self.session_cookie_value("", 0))],
        )

    def get_friends(self):
        with connect_database() as connection:
            user = self.require_user(connection)
            if not user:
                return
            user_id = user["id"]
            friends = connection.execute(
                """
                SELECT users.username
                FROM friends
                JOIN users ON users.id = CASE
                    WHEN friends.user1_id = ? THEN friends.user2_id
                    ELSE friends.user1_id
                END
                WHERE friends.user1_id = ? OR friends.user2_id = ?
                ORDER BY LOWER(users.username)
                """,
                (user_id, user_id, user_id),
            ).fetchall()
            incoming = connection.execute(
                """
                SELECT users.username
                FROM friend_requests
                JOIN users ON users.id = friend_requests.sender_id
                WHERE friend_requests.recipient_id = ?
                ORDER BY friend_requests.created_at, LOWER(users.username)
                """,
                (user_id,),
            ).fetchall()
            outgoing = connection.execute(
                """
                SELECT users.username
                FROM friend_requests
                JOIN users ON users.id = friend_requests.recipient_id
                WHERE friend_requests.sender_id = ?
                ORDER BY friend_requests.created_at, LOWER(users.username)
                """,
                (user_id,),
            ).fetchall()
        self.send_json(
            200,
            {
                "friends": [row["username"] for row in friends],
                "incoming": [row["username"] for row in incoming],
                "outgoing": [row["username"] for row in outgoing],
            },
        )

    def friend_target_name(self, username):
        if not USERNAME_PATTERN.fullmatch(username):
            self.send_error_json(400, "닉네임은 영문, 숫자, 밑줄로 3~16자여야 합니다.")
            return None
        return username

    def send_friend_request(self, payload):
        username = payload.get("username")
        if not isinstance(username, str):
            self.send_error_json(400, "친구로 추가할 닉네임을 입력해 주세요.")
            return
        username = self.friend_target_name(username)
        if not username:
            return
        now = int(time.time())
        with connect_database() as connection:
            begin_write_transaction(connection)
            user = self.require_user(connection)
            if not user:
                connection.rollback()
                return
            target = connection.execute(
                "SELECT id, username FROM users WHERE LOWER(username) = LOWER(?)",
                (username,),
            ).fetchone()
            if not target:
                self.send_error_json(404, "해당 닉네임의 계정을 찾을 수 없습니다.")
                return
            if target["id"] == user["id"]:
                self.send_error_json(400, "자기 자신에게 친구 신청을 보낼 수 없습니다.")
                return
            user1_id, user2_id = sorted((user["id"], target["id"]))
            already_friends = connection.execute(
                "SELECT 1 FROM friends WHERE user1_id = ? AND user2_id = ?",
                (user1_id, user2_id),
            ).fetchone()
            if already_friends:
                self.send_error_json(409, "이미 친구입니다.")
                return
            reciprocal = connection.execute(
                """
                SELECT 1 FROM friend_requests
                WHERE sender_id = ? AND recipient_id = ?
                """,
                (target["id"], user["id"]),
            ).fetchone()
            if reciprocal:
                connection.execute(
                    "INSERT INTO friends(user1_id, user2_id, created_at) VALUES (?, ?, ?)",
                    (user1_id, user2_id, now),
                )
                connection.execute(
                    """
                    DELETE FROM friend_requests
                    WHERE (sender_id = ? AND recipient_id = ?)
                       OR (sender_id = ? AND recipient_id = ?)
                    """,
                    (user["id"], target["id"], target["id"], user["id"]),
                )
                self.send_json(
                    200,
                    {"state": "friends", "username": target["username"]},
                )
                return
            existing = connection.execute(
                """
                SELECT 1 FROM friend_requests
                WHERE sender_id = ? AND recipient_id = ?
                """,
                (user["id"], target["id"]),
            ).fetchone()
            if existing:
                self.send_error_json(409, "이미 친구 신청을 보냈습니다.")
                return
            connection.execute(
                """
                INSERT INTO friend_requests(sender_id, recipient_id, created_at)
                VALUES (?, ?, ?)
                """,
                (user["id"], target["id"], now),
            )
        self.send_json(201, {"state": "pending", "username": target["username"]})

    def respond_friend_request(self, username, accept):
        username = self.friend_target_name(username)
        if not username:
            return
        now = int(time.time())
        with connect_database() as connection:
            begin_write_transaction(connection)
            user = self.require_user(connection)
            if not user:
                connection.rollback()
                return
            sender = connection.execute(
                "SELECT id, username FROM users WHERE LOWER(username) = LOWER(?)",
                (username,),
            ).fetchone()
            if not sender:
                self.send_error_json(404, "친구 신청을 찾을 수 없습니다.")
                return
            pending = connection.execute(
                """
                SELECT 1 FROM friend_requests
                WHERE sender_id = ? AND recipient_id = ?
                """,
                (sender["id"], user["id"]),
            ).fetchone()
            if not pending:
                self.send_error_json(404, "받은 친구 신청을 찾을 수 없습니다.")
                return
            if accept:
                user1_id, user2_id = sorted((user["id"], sender["id"]))
                connection.execute(
                    "INSERT INTO friends(user1_id, user2_id, created_at) VALUES (?, ?, ?)",
                    (user1_id, user2_id, now),
                )
            connection.execute(
                "DELETE FROM friend_requests WHERE sender_id = ? AND recipient_id = ?",
                (sender["id"], user["id"]),
            )
        self.send_json(
            200,
            {"state": "friends" if accept else "declined", "username": sender["username"]},
        )

    def delete_friend_request(self, username):
        username = self.friend_target_name(username)
        if not username:
            return
        with connect_database() as connection:
            user = self.require_user(connection)
            if not user:
                return
            target = connection.execute(
                "SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (username,)
            ).fetchone()
            if not target:
                self.send_error_json(404, "친구 신청을 찾을 수 없습니다.")
                return
            cursor = connection.execute(
                """
                DELETE FROM friend_requests
                WHERE (sender_id = ? AND recipient_id = ?)
                   OR (sender_id = ? AND recipient_id = ?)
                """,
                (user["id"], target["id"], target["id"], user["id"]),
            )
            if cursor.rowcount == 0:
                self.send_error_json(404, "취소할 친구 신청을 찾을 수 없습니다.")
                return
        self.send_json(200, {"ok": True})

    def remove_friend(self, username):
        username = self.friend_target_name(username)
        if not username:
            return
        with connect_database() as connection:
            user = self.require_user(connection)
            if not user:
                return
            target = connection.execute(
                "SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (username,)
            ).fetchone()
            if not target:
                self.send_error_json(404, "친구를 찾을 수 없습니다.")
                return
            user1_id, user2_id = sorted((user["id"], target["id"]))
            cursor = connection.execute(
                "DELETE FROM friends WHERE user1_id = ? AND user2_id = ?",
                (user1_id, user2_id),
            )
            if cursor.rowcount == 0:
                self.send_error_json(404, "해당 플레이어는 친구 목록에 없습니다.")
                return
        self.send_json(200, {"ok": True})

    def get_live_match(self, connection, user_id, now_ms):
        active = connection.execute(
            """
            SELECT id FROM matches
            WHERE status = 'playing' AND (player1_id = ? OR player2_id = ?)
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, user_id),
        ).fetchone()
        if not active:
            return None
        self.match_payload(connection, active["id"], user_id, now_ms)
        return connection.execute(
            """
            SELECT id FROM matches
            WHERE status = 'playing' AND (player1_id = ? OR player2_id = ?)
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, user_id),
        ).fetchone()

    def join_matchmaking(self):
        now = int(time.time())
        now_ms = int(time.time() * 1000)
        with connect_database() as connection:
            begin_write_transaction(connection)
            user = self.require_user(connection)
            if not user:
                connection.rollback()
                return
            active = self.get_live_match(connection, user["id"], now_ms)
            if active:
                current = connection.execute(
                    "SELECT points FROM users WHERE id = ?", (user["id"],)
                ).fetchone()
                self.send_json(
                    200,
                    {
                        "state": "matched",
                        "matchId": active["id"],
                        "points": current["points"],
                    },
                )
                return
            if user["points"] < 300:
                self.send_error_json(400, "온라인 대전에는 300 포인트가 필요합니다.")
                return
            connection.execute(
                "DELETE FROM matchmaking_queue WHERE queued_at <= ?",
                (now - MATCHMAKING_TIMEOUT_SECONDS,),
            )
            opponent = connection.execute(
                """
                SELECT queue.user_id, users.username
                FROM matchmaking_queue AS queue
                JOIN users ON users.id = queue.user_id
                WHERE queue.user_id != ? AND users.points >= 300
                ORDER BY queue.queued_at
                LIMIT 1
                """,
                (user["id"],),
            ).fetchone()
            if opponent:
                connection.execute(
                    "DELETE FROM matchmaking_queue WHERE user_id IN (?, ?)",
                    (user["id"], opponent["user_id"]),
                )
                connection.execute(
                    "UPDATE users SET points = points - 300 WHERE id IN (?, ?)",
                    (user["id"], opponent["user_id"]),
                )
                match_id = secrets.token_urlsafe(12)
                connection.execute(
                    """
                    INSERT INTO matches(
                        id, player1_id, player2_id, player1_seen, player2_seen,
                        status, created_at, started_at_ms
                    ) VALUES (?, ?, ?, ?, ?, 'playing', ?, ?)
                    """,
                    (match_id, opponent["user_id"], user["id"], now, now, now, now_ms),
                )
                self.send_json(
                    200,
                    {
                        "state": "matched",
                        "matchId": match_id,
                        "opponent": opponent["username"],
                        "points": user["points"] - 300,
                    },
                )
                return
            connection.execute(
                """
                INSERT INTO matchmaking_queue(user_id, queued_at) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET queued_at = excluded.queued_at
                """,
                (user["id"], now),
            )
        self.send_json(200, {"state": "waiting"})

    def get_matchmaking_status(self):
        now = int(time.time())
        now_ms = int(time.time() * 1000)
        with connect_database() as connection:
            user = self.require_user(connection)
            if not user:
                return
            active = self.get_live_match(connection, user["id"], now_ms)
            if active:
                current = connection.execute(
                    "SELECT points FROM users WHERE id = ?", (user["id"],)
                ).fetchone()
                self.send_json(
                    200,
                    {
                        "state": "matched",
                        "matchId": active["id"],
                        "points": current["points"],
                    },
                )
                return
            queued = connection.execute(
                "SELECT queued_at FROM matchmaking_queue WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
            if queued and queued["queued_at"] <= now - MATCHMAKING_TIMEOUT_SECONDS:
                cursor = connection.execute(
                    "DELETE FROM matchmaking_queue WHERE user_id = ? AND queued_at = ?",
                    (user["id"], queued["queued_at"]),
                )
                if cursor.rowcount:
                    self.send_json(200, {"state": "expired"})
                    return
                active = connection.execute(
                    """
                    SELECT id FROM matches
                    WHERE status = 'playing' AND (player1_id = ? OR player2_id = ?)
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (user["id"], user["id"]),
                ).fetchone()
                if active:
                    current = connection.execute(
                        "SELECT points FROM users WHERE id = ?", (user["id"],)
                    ).fetchone()
                    self.send_json(
                        200,
                        {
                            "state": "matched",
                            "matchId": active["id"],
                            "points": current["points"],
                        },
                    )
                    return
                queued = connection.execute(
                    "SELECT queued_at FROM matchmaking_queue WHERE user_id = ?",
                    (user["id"],),
                ).fetchone()
        self.send_json(200, {"state": "waiting" if queued else "idle"})

    def get_match_status(self, match_id):
        now_ms = int(time.time() * 1000)
        with connect_database() as connection:
            user = self.require_user(connection)
            if not user:
                return
            result = self.match_payload(connection, match_id, user["id"], now_ms)
            if result is None:
                self.send_error_json(404, "대전을 찾을 수 없습니다.")
                return
        self.send_json(200, result)

    def match_payload(self, connection, match_id, user_id, now_ms):
        match = connection.execute(
            """
            SELECT matches.*, first.username AS player1_name, second.username AS player2_name
            FROM matches
            JOIN users AS first ON first.id = matches.player1_id
            JOIN users AS second ON second.id = matches.player2_id
            WHERE matches.id = ? AND (matches.player1_id = ? OR matches.player2_id = ?)
            """,
            (match_id, user_id, user_id),
        ).fetchone()
        if not match:
            return None

        is_player1 = user_id == match["player1_id"]
        rival_seen = match["player2_seen"] if is_player1 else match["player1_seen"]
        if match["status"] == "playing" and now_ms - rival_seen * 1000 > 45_000:
            if is_player1:
                p1_finished_at = now_ms
                p2_finished_at = now_ms - 1
            else:
                p1_finished_at = now_ms - 1
                p2_finished_at = now_ms
            connection.execute(
                """
                UPDATE matches
                SET status = 'completed', completed_at = ?,
                    player1_finished = 1, player2_finished = 1,
                    player1_finished_at_ms = ?, player2_finished_at_ms = ?
                WHERE id = ?
                """,
                (now_ms // 1000, p1_finished_at, p2_finished_at, match_id),
            )
            match = connection.execute(
                """
                SELECT matches.*, first.username AS player1_name, second.username AS player2_name
                FROM matches
                JOIN users AS first ON first.id = matches.player1_id
                JOIN users AS second ON second.id = matches.player2_id
                WHERE matches.id = ?
                """,
                (match_id,),
            ).fetchone()

        if match["status"] == "completed":
            first_finished = match["player1_finished_at_ms"]
            second_finished = match["player2_finished_at_ms"]
            if (
                first_finished is not None
                and second_finished is not None
                and first_finished != second_finished
            ):
                winner_id = (
                    match["player1_id"]
                    if first_finished > second_finished
                    else match["player2_id"]
                )
                cursor = connection.execute(
                    "UPDATE matches SET reward_awarded = 1 WHERE id = ? AND reward_awarded = 0",
                    (match_id,),
                )
                if cursor.rowcount:
                    connection.execute(
                        "UPDATE users SET points = points + 700 WHERE id = ?",
                        (winner_id,),
                    )

        p1_finished_at = match["player1_finished_at_ms"]
        p2_finished_at = match["player2_finished_at_ms"]
        p1_survival = max(
            0.0,
            ((p1_finished_at or now_ms) - match["started_at_ms"]) / 1000,
        )
        p2_survival = max(
            0.0,
            ((p2_finished_at or now_ms) - match["started_at_ms"]) / 1000,
        )
        own_survival = p1_survival if is_player1 else p2_survival
        rival_survival = p2_survival if is_player1 else p1_survival
        if match["status"] == "completed":
            if p1_finished_at == p2_finished_at:
                winner = "draw"
            else:
                winner_is_player1 = p1_finished_at > p2_finished_at
                winner = "you" if winner_is_player1 == is_player1 else "opponent"
        else:
            winner = None

        own_score = match["player1_score"] if is_player1 else match["player2_score"]
        rival_score = match["player2_score"] if is_player1 else match["player1_score"]
        own_finished = match["player1_finished"] if is_player1 else match["player2_finished"]
        rival_finished = match["player2_finished"] if is_player1 else match["player1_finished"]
        rival_name = match["player2_name"] if is_player1 else match["player1_name"]
        rival_balls = match["player2_balls"] if is_player1 else match["player1_balls"]
        own_points = connection.execute(
            "SELECT points FROM users WHERE id = ?", (user_id,)
        ).fetchone()["points"]
        try:
            balls = json.loads(rival_balls)
        except (TypeError, json.JSONDecodeError):
            balls = []
        return {
            "state": match["status"],
            "opponent": rival_name,
            "score": own_score,
            "opponentScore": rival_score,
            "finished": bool(own_finished),
            "opponentFinished": bool(rival_finished),
            "opponentOnline": now_ms - rival_seen * 1000 <= 45_000,
            "winner": winner,
            "survivalTime": round(own_survival, 2),
            "opponentSurvivalTime": round(rival_survival, 2),
            "balls": balls,
            "points": own_points,
        }

    def update_match_state(self, match_id, payload):
        score = payload.get("score")
        finished = payload.get("finished", False)
        balls = payload.get("balls", [])
        if not isinstance(score, int) or isinstance(score, bool) or score < 0:
            self.send_error_json(400, "점수 값이 올바르지 않습니다.")
            return
        if not isinstance(finished, bool):
            self.send_error_json(400, "대전 상태 값이 올바르지 않습니다.")
            return
        if not isinstance(balls, list) or len(balls) > 24:
            self.send_error_json(400, "공 상태 데이터가 올바르지 않습니다.")
            return
        clean_balls = []
        for ball in balls:
            if not isinstance(ball, dict):
                self.send_error_json(400, "공 상태 데이터가 올바르지 않습니다.")
                return
            values = [ball.get(key) for key in ("x", "y", "vx", "vy", "radius")]
            if any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                for value in values
            ):
                self.send_error_json(400, "공 위치 데이터가 올바르지 않습니다.")
                return
            x, y, vx, vy, radius = values
            ball_id = ball.get("id")
            style_id = ball.get("styleId", "classic")
            if (
                not isinstance(ball_id, str)
                or not 1 <= len(ball_id) <= 80
                or not isinstance(style_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", style_id)
                or not (0 <= x <= 700 and 0 <= y <= 500)
                or not (-20 <= vx <= 20 and -20 <= vy <= 20)
                or not (2 <= radius <= 40)
            ):
                self.send_error_json(400, "공 상태 데이터가 범위를 벗어났습니다.")
                return
            clean_balls.append({
                "id": ball_id,
                "x": x,
                "y": y,
                "vx": vx,
                "vy": vy,
                "radius": radius,
                "styleId": style_id,
                "boosted": bool(ball.get("boosted")),
            })

        now = int(time.time())
        now_ms = int(time.time() * 1000)
        with connect_database() as connection:
            begin_write_transaction(connection)
            user = self.require_user(connection)
            if not user:
                connection.rollback()
                return
            match = connection.execute(
                "SELECT * FROM matches WHERE id = ? AND (player1_id = ? OR player2_id = ?)",
                (match_id, user["id"], user["id"]),
            ).fetchone()
            if not match:
                self.send_error_json(404, "대전을 찾을 수 없습니다.")
                return
            if match["status"] == "completed":
                result = self.match_payload(connection, match_id, user["id"], now_ms)
                self.send_json(200, result)
                return
            is_player1 = user["id"] == match["player1_id"]
            score_column = "player1_score" if is_player1 else "player2_score"
            finished_column = "player1_finished" if is_player1 else "player2_finished"
            balls_column = "player1_balls" if is_player1 else "player2_balls"
            seen_column = "player1_seen" if is_player1 else "player2_seen"
            finished_at_column = (
                "player1_finished_at_ms" if is_player1 else "player2_finished_at_ms"
            )
            connection.execute(
                """
                UPDATE matches
                SET {score} = CASE WHEN {score} < ? THEN ? ELSE {score} END,
                    {finished} = CASE WHEN {finished} < ? THEN ? ELSE {finished} END,
                    {finished_at} = CASE WHEN ? THEN COALESCE({finished_at}, ?) ELSE {finished_at} END,
                    {seen} = ?,
                    {balls} = ?
                WHERE id = ?
                """.format(
                    score=score_column,
                    finished=finished_column,
                    finished_at=finished_at_column,
                    seen=seen_column,
                    balls=balls_column,
                ),
                (
                    score,
                    score,
                    int(finished),
                    int(finished),
                    int(finished),
                    now_ms,
                    now,
                    json.dumps(clean_balls),
                    match_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM matches WHERE id = ?",
                (match_id,),
            ).fetchone()
            rival_seen = updated["player2_seen"] if is_player1 else updated["player1_seen"]
            if (
                updated["status"] == "playing"
                and now - rival_seen > 45
            ):
                if is_player1:
                    p1_finished_at = now_ms
                    p2_finished_at = now_ms - 1
                else:
                    p1_finished_at = now_ms - 1
                    p2_finished_at = now_ms
                connection.execute(
                    """
                    UPDATE matches
                    SET status = 'completed', completed_at = ?,
                        player1_finished = 1, player2_finished = 1,
                        player1_finished_at_ms = ?, player2_finished_at_ms = ?
                    WHERE id = ?
                    """,
                    (now, p1_finished_at, p2_finished_at, match_id),
                )
            updated = connection.execute(
                "SELECT player1_finished, player2_finished, status FROM matches WHERE id = ?",
                (match_id,),
            ).fetchone()
            status = "completed" if updated["player1_finished"] and updated["player2_finished"] else "playing"
            if status == "completed":
                connection.execute(
                    "UPDATE matches SET status = ?, completed_at = ? WHERE id = ?",
                    (status, now, match_id),
                )
            result = self.match_payload(connection, match_id, user["id"], now_ms)
        self.send_json(200, result)


def main():
    parser = argparse.ArgumentParser(description="Run the Bounce online game server.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    arguments = parser.parse_args()
    initialize_database()
    server = ThreadingHTTPServer((arguments.host, arguments.port), BounceHandler)
    print("Bounce is available at http://%s:%d" % (arguments.host, arguments.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Bounce server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
