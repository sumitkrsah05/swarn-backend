"""Cross-check Postgres object_key columns against the MinIO bucket.

Usage (from the repo root, with .env present):
    uv run python scripts/storage_check.py

Reports rows whose object is missing (data loss) and objects no row
references (orphans, safe to delete once you know why they exist).
Session summary.md objects are expected orphans: only trace.json has a row.
"""

from __future__ import annotations

import asyncio
import sys

import asyncpg
import boto3
from botocore.config import Config

from swarn_backend.settings import get_settings

REFERENCED = """
select object_key from upload_files
union select object_key from datasets
union select object_key from files
union select trace_key from agent_sessions where trace_key is not null
"""


async def referenced_keys(dsn: str) -> set[str]:
    conn = await asyncpg.connect(dsn.replace("postgresql+asyncpg", "postgresql"))
    try:
        return {r[0] for r in await conn.fetch(REFERENCED)}
    finally:
        await conn.close()


def bucket_keys(s) -> dict[str, int]:
    client = boto3.client("s3", endpoint_url=s.s3_endpoint, aws_access_key_id=s.s3_access_key,
                          aws_secret_access_key=s.s3_secret_key, region_name=s.s3_region,
                          config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    out: dict[str, int] = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=s.s3_bucket):
        for o in page.get("Contents", []) or []:
            out[o["Key"]] = int(o.get("Size", 0))
    return out


def main() -> int:
    s = get_settings()
    refs = asyncio.run(referenced_keys(s.database_url))
    objs = bucket_keys(s)
    missing = sorted(refs - objs.keys())
    orphans = sorted(set(objs) - refs)
    expected = [k for k in orphans if k.startswith("sessions/") and k.endswith("/summary.md")]
    unexpected = [k for k in orphans if k not in expected]

    print(f"bucket {s.s3_bucket}: {len(objs)} objects, {sum(objs.values())} bytes")
    print(f"rows pointing at objects: {len(refs)}")
    print(f"rows whose object is MISSING: {len(missing)}")
    for k in missing:
        print(f"  MISSING  {k}")
    print(f"orphan objects (no row): {len(orphans)}  "
          f"({len(expected)} session summaries, expected)")
    for k in unexpected:
        print(f"  ORPHAN   {objs[k]:>10}  {k}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
