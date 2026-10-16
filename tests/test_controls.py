import pytest

from app import controls
from app.controls import PermissionDenied


def test_require_role_membership_and_level(conn):
    assert controls.require_role("admin", "p1", "admin", conn) == "admin"
    controls.require_role("view", "p1", "viewer", conn)
    with pytest.raises(PermissionDenied):
        controls.require_role("view", "p1", "reviewer", conn)
    with pytest.raises(PermissionDenied):
        controls.require_role("ghost", "p1", "viewer", conn)
    controls.add_user("outsider", "admin", conn)
    with pytest.raises(PermissionDenied):
        controls.require_role("outsider", "p1", "viewer", conn)  # not a member


def test_viewer_cannot_review_and_is_logged(conn):
    with pytest.raises(PermissionDenied):
        controls.review("finding", 1, "approved", "ok", "view", conn=conn)
    row = conn.execute("SELECT action FROM audit_log WHERE user='view'").fetchone()
    assert row["action"] == "review_denied"
    assert conn.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 0


def test_reviewer_review_updates_status_and_logs(conn):
    conn.execute("CREATE TABLE components(id INTEGER PRIMARY KEY, review_status TEXT DEFAULT 'unreviewed')")
    conn.execute("INSERT INTO components(id) VALUES(1)")
    controls.review("component", 1, "approved", "looks right", "rev", project_id="p1", conn=conn)
    assert conn.execute("SELECT review_status s FROM components").fetchone()["s"] == "approved"
    assert conn.execute("SELECT decision FROM reviews").fetchone()["decision"] == "approved"
    assert conn.execute("SELECT 1 FROM audit_log WHERE action='review'").fetchone()


def test_review_rejects_bad_decision(conn):
    with pytest.raises(ValueError):
        controls.review("finding", 1, "maybe", "", "rev", conn=conn)
