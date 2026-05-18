"""
One-shot provisioning: create users, ACLs, and collections on all databases.

Used by Docker Compose as the db-init service. Runs the same init functions as
init.py but skips the backup step (no data to back up on first deploy).
"""

import asyncio

import pendulum

from backend.infra import init_document_store, init_redis


async def main():
    print(f"=== Dorian DB Provisioning: {pendulum.now().to_datetime_string()} ===")
    await asyncio.gather(
        init_document_store(),
        init_redis(),
    )
    print("=== Provisioning Complete ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
