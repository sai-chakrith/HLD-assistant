import pytest

from app.controls import PermissionDenied, list_reviews, review


def _finding(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS findings(id INTEGER PRIMARY KEY, review_status TEXT DEFAULT 'unreviewed')")
    conn.execute("INSERT INTO findings(id) VALUES(1)")
    conn.commit()


def test_viewer_denied_reviewer_approves(conn):
    _finding(conn)
    with pytest.raises(PermissionDenied):
        review("finding", 1, "approved", "ok", "view", project_id="p1", conn=conn)
    assert conn.execute("SELECT review_status FROM findings").fetchone()[0] == "unreviewed"
    review("finding", 1, "approved", "ok", "rev", project_id="p1", conn=conn)
    assert conn.execute("SELECT review_status FROM findings").fetchone()[0] == "approved"
    actions = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions == ["review_denied", "review"]
    hist = list_reviews("finding", 1, conn=conn)
    assert len(hist) == 1 and hist[0]["reviewer"] == "rev" and hist[0]["decision"] == "approved"


def test_review_component(conn):
    from app.extract import ensure_schema
    ensure_schema(conn)
    conn.execute("INSERT INTO components(project_id, doc_id, version, name, norm_name, fields, review_status) "
                 "VALUES('p1','a','1','X','x','{}','unreviewed')")
    conn.commit()
    review("component", 1, "rejected", "no", "admin", project_id="p1", conn=conn)
    assert conn.execute("SELECT review_status FROM components").fetchone()[0] == "rejected"
    assert list_reviews(conn=conn)[0]["item_type"] == "component"
