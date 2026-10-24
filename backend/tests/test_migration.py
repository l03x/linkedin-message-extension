import sqlite3

from sqlalchemy import inspect

from app.db import Database


def test_old_database_gains_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        # The generations table as the previous version created it (trimmed to what matters here).
        conn.executescript(
            """
            CREATE TABLE generations (
                id INTEGER PRIMARY KEY, profile_id INTEGER, mode VARCHAR(40), language VARCHAR(10),
                extra_instruction TEXT, prompt_version_id INTEGER, user_context_id INTEGER, model VARCHAR(100),
                draft_subject TEXT, draft_text TEXT, final_subject TEXT, final_text TEXT, over_limit BOOLEAN,
                attempts INTEGER, status VARCHAR(20), generated_at DATETIME, sent_at DATETIME, replied_at DATETIME
            );
            INSERT INTO generations (id, mode, draft_text, status) VALUES (1, 'message', 'old draft', 'sent');
            """
        )
    db = Database(f"sqlite:///{path}")
    db.create_all()
    cols = {c["name"] for c in inspect(db.engine).get_columns("generations")}
    assert {"strategy", "strategy_reason", "cv_match", "facts_used", "company_id", "research_used", "research_snapshot"} <= cols
    assert inspect(db.engine).has_table("companies")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT draft_text, strategy, research_used FROM generations").fetchone() == ("old draft", None, None)
    db.create_all()  # running it again is a no-op
