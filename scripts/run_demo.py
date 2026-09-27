"""Run the local demo with durable embedded PostgreSQL and a configured development-org CLI login.

uv run python scripts/run_demo.py
"""

import asyncio
import hashlib
import os
import subprocess
from pathlib import Path

import asyncpg
import pgserver

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(pgserver.__file__).parent / "pginstall" / "bin"


def command(*args):
    subprocess.run([str(BIN / args[0]), *args[1:]], check=True, stdout=subprocess.DEVNULL)


async def migrate(url):
    connection = await asyncpg.connect(url)
    try:
        await connection.execute((ROOT / "migrations/001_initial.sql").read_text())
    finally:
        await connection.close()


def main():
    directory = ROOT / ".demo-data"
    directory.mkdir(exist_ok=True, mode=0o700)
    data = directory / "postgres"
    socket = Path("/tmp") / ("lq-demo-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12])
    socket.mkdir(exist_ok=True, mode=0o700)
    if not (data / "PG_VERSION").exists():
        command(
            "initdb",
            "-D",
            str(data),
            "-U",
            "postgres",
            "--auth-local=trust",
            "--auth-host=reject",
            "--no-locale",
        )
    status = subprocess.run([str(BIN / "pg_ctl"), "-D", str(data), "status"], capture_output=True)
    owns_server = status.returncode != 0
    if owns_server:
        command(
            "pg_ctl",
            "-D",
            str(data),
            "-l",
            str(directory / "postgres.log"),
            "-o",
            f'-h "" -k {socket}',
            "-w",
            "start",
        )
    try:
        url = f"postgresql://postgres@/postgres?host={socket}"
        asyncio.run(migrate(url))
        print("Demo: http://127.0.0.1:8765/demo", flush=True)
        print(
            "SerpAPI Places; simulated partners; real writes to the pinned development org.",
            flush=True,
        )
        import sys

        subprocess.run(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "lead_api.demo:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
                "--no-access-log",
            ],
            cwd=ROOT,
            env=dict(os.environ, LQ_DEMO_DATABASE_URL=url),
            check=True,
        )
    except KeyboardInterrupt:
        pass
    finally:
        if owns_server:
            command("pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop")


if __name__ == "__main__":
    main()
