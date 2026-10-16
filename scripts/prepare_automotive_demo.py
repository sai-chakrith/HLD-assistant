"""Add source-backed synthetic ECU releases to the automotive demo workspace.

Keeps sample facts separate from public AUTOSAR references. Re-running skips
ingestion and preserves human reviews through the normal extraction pipeline.
"""
from app import controls
from app.checks import run_checks
from app.compare import compare
from app.config import settings
from app.extract import extract_document
from app.ingest import ingest_document


def main():
    project = "automotive-platform"
    with controls._use(None) as conn:
        for version in ("1.0", "2.0"):
            suffix = version[0]
            doc_id = f"automotive-platform_demo_v{suffix}"
            exists = conn.execute(
                "SELECT 1 FROM documents WHERE project_id=? AND doc_id=?", (project, doc_id)
            ).fetchone()
            if not exists:
                ingest_document(
                    settings.synthetic_dir / f"hld_v{suffix}.pdf", project, "hld", version,
                    "synthetic", "admin", doc_id=doc_id,
                    title=f"Demo — Window Lift ECU v{version}", conn=conn,
                )
            counts = extract_document(project, doc_id, conn=conn)
            findings = run_checks(project, doc_id, version, conn=conn)
            print(doc_id, counts, "findings:", len(findings))
        diff = compare(project, "automotive-platform_demo_v1", "1.0",
                       "automotive-platform_demo_v2", "2.0", conn=conn)
        print("Version comparison:", diff.summary)


if __name__ == "__main__":
    main()
