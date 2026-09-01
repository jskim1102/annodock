"""Delete accumulated test accounts from the auth directory.

Targets accounts that look machine-generated (QA/smoke signups):
  - *@example.com / *@annodock.internal
  - <word>-<digits>@* (rv-27791@..., hint-752531353@gmail.com, ...)

Safety gates — an account is deleted only when ALL hold:
  - not in KEEP_EMAILS
  - signed up more than GRACE_HOURS ago
  - owns nothing in annodock (no projects, no datasets, zero bytes_used)

Usage:
    .venv/bin/python scripts/cleanup_test_accounts.py [--dry-run]

Cron installs this daily; see crontab -l.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = BACKEND_ROOT.parent / ".env"

KEEP_EMAILS = {
    "weave.contact.us@gmail.com",
    "epfam126@naver.com",
    "deepi.contact.us@gmail.com",
}
TEST_EMAIL_PATTERN = re.compile(
    r"(@example\.com$|@annodock\.internal$|^[a-z]+-\d+@)",
    re.IGNORECASE,
)
GRACE_HOURS = 24


def _read_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} not configured")


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    auth = await asyncpg.connect(_dsn(_read_env("AUTH_DATABASE_URL")))
    app = await asyncpg.connect(_dsn(_read_env("DATABASE_URL")))
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=GRACE_HOURS)
        rows = await auth.fetch(
            "SELECT id, email, created_at FROM users"
            " WHERE email IS NOT NULL AND created_at < $1 ORDER BY id",
            cutoff,
        )
        candidates = [
            row
            for row in rows
            if row["email"] not in KEEP_EMAILS
            and TEST_EMAIL_PATTERN.search(row["email"])
        ]
        deleted = 0
        for row in candidates:
            owner_id = row["id"]
            owns = await app.fetchval(
                "SELECT (SELECT count(*) FROM projects WHERE owner_id = $1)"
                " + (SELECT count(*) FROM datasets WHERE owner_id = $1)"
                " + (SELECT coalesce(sum(bytes_used), 0)"
                "    FROM user_storage WHERE owner_id = $1)",
                owner_id,
            )
            if owns:
                print(f"skip {row['email']} (id={owner_id}): owns data")
                continue
            if args.dry_run:
                print(f"would delete {row['email']} (id={owner_id})")
                deleted += 1
                continue
            async with auth.transaction():
                for table in (
                    "auth_identities",
                    "oauth_codes",
                    "password_resets",
                    "refresh_tokens",
                ):
                    await auth.execute(
                        f"DELETE FROM {table} WHERE user_id = $1", owner_id
                    )
                await auth.execute("DELETE FROM users WHERE id = $1", owner_id)
            print(f"deleted {row['email']} (id={owner_id})")
            deleted += 1
        print(f"{'candidates' if args.dry_run else 'deleted'}: {deleted}")
    finally:
        await auth.close()
        await app.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
