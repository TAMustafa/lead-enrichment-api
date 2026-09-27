"""Create a private local .env once; never overwrite existing credentials or print secret values."""

import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    template = (ROOT / ".env.example").read_text()
    values = {
        "LQ_API_KEYS": json.dumps({"local-dev": secrets.token_urlsafe(36)}, separators=(",", ":")),
        "LQ_FINGERPRINT_KEY": secrets.token_urlsafe(48),
    }
    lines = [
        f"{line.split('=', 1)[0]}={values[line.split('=', 1)[0]]}"
        if line.split("=", 1)[0] in values
        else line
        for line in template.splitlines()
    ]
    try:
        fd = os.open(ROOT / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Existing .env preserved; no values were changed.")
        return
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(lines) + "\n")
    print("Created private .env with generated local API/fingerprint keys. Provider credentials need setup.")


if __name__ == "__main__":
    main()
