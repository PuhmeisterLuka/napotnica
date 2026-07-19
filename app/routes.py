"""Flask views: index with filters, background scrape/score, job detail, exports.

Scraping and scoring run in daemon threads with their own database connection, never
inside a request handler (Playwright must not run in the request path). Scrape progress
is read back from the scrape_runs table; score progress is held in a small in-memory
state that the index page polls.
"""
from __future__ import annotations

import json
import threading
from datetime import date

from flask import (
    Response, current_app, flash, redirect, render_template,
    request, send_file, url_for,
)

from . import db as db_mod
from . import export, matching, repo, scraper
from . import get_conn

# --------------------------------------------------------------------------- #
# Background tasks
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_scrape_thread: threading.Thread | None = None
_score_thread: threading.Thread | None = None
_score_state: dict = {"running": False, "result": None, "error": None}


def _start_scrape(app) -> bool:
    global _scrape_thread
    with _lock:
        if _scrape_thread and _scrape_thread.is_alive():
            return False

        def work():
            try:
                scraper.run_scrape(db_path=app.config["DB_PATH"])
            except BaseException:
                pass  # run_scrape already records the failure in scrape_runs

        _scrape_thread = threading.Thread(target=work, daemon=True)
        _scrape_thread.start()
        return True


def _start_score(app, force: bool = False) -> bool:
    global _score_thread
    with _lock:
        if _score_thread and _score_thread.is_alive():
            return False
        _score_state.update(running=True, result=None, error=None)

        def work():
            conn = db_mod.connect(app.config["DB_PATH"])
            try:
                result = matching.run_scoring(
                    conn, profile_path=app.config["PROFILE_PATH"],
                    max_batch=app.config["MAX_SCORE_BATCH"], force=force,
                )
                _score_state.update(running=False, result=result)
            except Exception as exc:
                _score_state.update(running=False, error=str(exc))
            finally:
                conn.close()

        _score_thread = threading.Thread(target=work, daemon=True)
        _score_thread.start()
        return True


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
def _filters_from_args(args) -> dict:
    def _num(name, cast):
        raw = (args.get(name) or "").strip()
        try:
            return cast(raw) if raw else None
        except ValueError:
            return None

    return {
        "search": (args.get("search") or "").strip() or None,
        "location": (args.get("location") or "").strip() or None,
        "category": (args.get("category") or "").strip() or None,
        "min_score": _num("min_score", int),
        "min_pay": _num("min_pay", float),
        "hide_applied": args.get("hide_applied") == "1",
        "show_inactive": args.get("show_inactive") == "1",
        "sort": "date" if args.get("sort") == "date" else "score",
    }


def _reasons(row) -> dict:
    if not row["match_reasons_json"]:
        return {"fits": [], "gaps": []}
    try:
        return json.loads(row["match_reasons_json"])
    except (ValueError, TypeError):
        return {"fits": [], "gaps": []}


def _skills(row) -> list:
    if not row["skills_json"]:
        return []
    try:
        return json.loads(row["skills_json"])
    except (ValueError, TypeError):
        return []


def _score_chip(score) -> dict:
    """Chip class + label following the brand score bands."""
    if score is None:
        return {"cls": "chip-none", "text": "—"}
    if score >= 75:
        return {"cls": "chip-strong", "text": score}
    if score >= 50:
        return {"cls": "chip-mid", "text": score}
    return {"cls": "chip-weak", "text": score}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def register(app):
    app.secret_key = app.config.setdefault("SECRET_KEY", "napotnica-local")
    app.jinja_env.globals["score_chip"] = _score_chip

    @app.context_processor
    def _inject_masthead():
        return {"today": date.today().strftime("%d. %m. %Y")}

    @app.get("/")
    def index():
        conn = get_conn()
        filters = _filters_from_args(request.args)
        jobs = repo.query_jobs(conn, **filters)
        return render_template(
            "index.html",
            jobs=jobs,
            filters=filters,
            categories=repo.distinct_categories(conn),
            scrape_run=repo.latest_scrape_run(conn),
            score_state=_score_state,
        )

    @app.post("/scrape")
    def scrape_now():
        started = _start_scrape(current_app._get_current_object())
        flash("Scrape started." if started else "A scrape is already running.")
        return redirect(url_for("index", **request.args))

    @app.get("/api/scrape/status")
    def scrape_status():
        run = repo.latest_scrape_run(get_conn())
        return dict(run) if run else {"status": "none"}

    @app.post("/score")
    def score_now():
        force = request.form.get("force") == "1"
        started = _start_score(current_app._get_current_object(), force=force)
        flash("Scoring started." if started else "Scoring is already running.")
        return redirect(url_for("index", **request.args))

    @app.get("/api/score/status")
    def score_status():
        return _score_state

    @app.get("/job/<int:job_id>")
    def job_detail(job_id):
        job = repo.get_job(get_conn(), job_id)
        if job is None:
            return render_template("not_found.html"), 404
        return render_template(
            "job_detail.html", job=job, reasons=_reasons(job), skills=_skills(job)
        )

    @app.post("/job/<int:job_id>/applied")
    def job_applied(job_id):
        conn = get_conn()
        repo.mark_applied(conn, job_id, db_mod.now_iso())
        conn.commit()
        flash("Marked as applied.")
        return redirect(url_for("job_detail", job_id=job_id))

    @app.get("/profile")
    def profile_page():
        try:
            profile = matching.load_profile(current_app.config["PROFILE_PATH"])
        except FileNotFoundError:
            return render_template("profile.html", profile=None,
                                   error="profile.yaml not found. Copy profile.example.yaml to profile.yaml.")
        except Exception as exc:
            return render_template("profile.html", profile=None, error=str(exc))
        return render_template("profile.html", profile=profile, error=None)

    @app.get("/export.csv")
    def export_csv():
        jobs = repo.query_jobs(get_conn(), **_filters_from_args(request.args))
        path = export.write_csv(jobs)
        return send_file(path, as_attachment=True, download_name=path.name,
                         mimetype="text/csv")

    @app.get("/export.json")
    def export_json():
        jobs = repo.query_jobs(get_conn(), **_filters_from_args(request.args))
        path = export.write_json(jobs)
        return send_file(path, as_attachment=True, download_name=path.name,
                         mimetype="application/json")
