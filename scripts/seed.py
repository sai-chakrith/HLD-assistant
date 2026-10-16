"""Seed the pilot DB: users admin/reviewer/viewer, four projects, per-project memberships.

Roles are per project (project_members.role). users.role is only the default / global-admin flag. To demonstrate
this, user "reviewer" is a REVIEWER on three projects but only a VIEWER on "chassis"."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import controls  # noqa: E402

PROJECTS = ["body-comfort", "chassis", "hmi", "synthetic-ecu"]
USERS = {"admin": "admin", "reviewer": "reviewer", "viewer": "viewer"}
ROLE_OVERRIDES = {("chassis", "reviewer"): "viewer"}


def seed(conn=None) -> None:
    with controls._use(conn) as c:
        for p in PROJECTS:
            controls.create_project(p, conn=c)
        for u, role in USERS.items():
            controls.add_user(u, role, conn=c)
            for p in PROJECTS:
                controls.add_member(p, u, ROLE_OVERRIDES.get((p, u)), conn=c)
        controls.log_action("system", "seed", "db", f"{len(USERS)} users, {len(PROJECTS)} projects", c)


if __name__ == "__main__":
    seed()
    print(f"Seeded users {list(USERS)} and projects {PROJECTS}")
