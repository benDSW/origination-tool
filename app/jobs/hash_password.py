"""Generate ADMIN_PASSWORD_HASH: ``python -m app.jobs.hash_password``.

Prompts twice without echoing, then prints only the Argon2 hash. The password
itself is never printed, logged or written anywhere.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence

from app.web.auth import recommended_hasher

MIN_LENGTH = 12


def main(argv: Sequence[str] | None = None, *, prompt: Callable[[str], str] = getpass.getpass) -> int:
    argparse.ArgumentParser(
        prog="python -m app.jobs.hash_password", description="Print an Argon2 hash for ADMIN_PASSWORD_HASH."
    ).parse_args(argv)
    password = prompt("Admin password: ")
    if len(password) < MIN_LENGTH:
        sys.stderr.write(f"Password must be at least {MIN_LENGTH} characters.\n")
        return 1
    if prompt("Repeat password: ") != password:
        sys.stderr.write("Passwords did not match.\n")
        return 1
    # Explicit OWASP parameters (19 MiB, t=2, p=1): the public login verifies this
    # hash, so its memory cost bounds what a burst of login attempts can consume.
    sys.stdout.write(recommended_hasher().hash(password) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
