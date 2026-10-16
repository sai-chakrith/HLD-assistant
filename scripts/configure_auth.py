"""Provision local access tokens for existing accounts without printing credentials.

Run with python -m scripts.configure_auth, then restart the server. Existing
configuration is preserved; remove it deliberately before rotating credentials.
"""
import hashlib
import json
import secrets

from app import controls
from app.config import settings


def main():
    env_path = settings.project_root / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if any(line.strip().startswith("HLD_AUTH_TOKEN_HASHES=") for line in text.splitlines()):
        raise SystemExit("Authentication is already configured; no credentials were changed.")
    with controls._use(None) as conn:
        users = [name for name in ("admin", "reviewer", "viewer")
                 if controls.get_role(name, conn) is not None]
    if not users:
        raise SystemExit("Provision users before configuring authentication.")
    directory = settings.data_dir / "auth"
    directory.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for user in users:
        token = secrets.token_urlsafe(32)
        path = directory / f"{user}-access.txt"
        with path.open("x", encoding="utf-8") as output:
            output.write(token + "\n")
        hashes[user] = hashlib.sha256(token.encode()).hexdigest()
    with env_path.open("a", encoding="utf-8") as output:
        output.write("\nHLD_AUTH_TOKEN_HASHES=" + json.dumps(hashes) + "\n")
    print("Authentication enabled for: " + ", ".join(users))
    print("Private token files: " + str(directory))
    print("Restart the application. Never commit or share these files publicly.")


if __name__ == "__main__":
    main()
