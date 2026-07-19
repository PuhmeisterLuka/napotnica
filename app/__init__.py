import os

from dotenv import load_dotenv
from flask import Flask, current_app, g

from . import db as db_mod


def create_app(config=None):
    load_dotenv()
    app = Flask(__name__)
    app.config.update(
        DB_PATH=str(db_mod.DEFAULT_DB_PATH),
        PROFILE_PATH=str(db_mod._PROJECT_ROOT / "profile.yaml"),
        MAX_SCORE_BATCH=int(os.getenv("MAX_SCORE_BATCH", "40")),
    )
    if config:
        app.config.update(config)

    # Create the schema once at boot, and retire any scrape left 'running' by a hard kill.
    conn = db_mod.get_db(app.config["DB_PATH"])
    from . import repo
    repo.fail_stale_runs(conn, db_mod.now_iso())
    conn.commit()
    conn.close()

    app.teardown_appcontext(_close_conn)

    from . import routes
    routes.register(app)
    return app


def get_conn():
    if "conn" not in g:
        g.conn = db_mod.connect(current_app.config["DB_PATH"])
    return g.conn


def _close_conn(exc):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()
