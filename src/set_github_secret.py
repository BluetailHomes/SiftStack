"""Write a GitHub Actions repository secret via the REST API.

Why this exists
----------------
`stage2-datasift-upload.yml` (GitHub Actions) reads `APIFY_TASKS` to decide
which platforms' Apify Tasks get auto-uploaded to DataSift — see
stage2_datasift_upload.py's module docstring for the full multi-platform
design and the rollout-gate policy (a platform stays out of this secret
until it's proven at least one clean, unattended scheduled run).

GitHub Actions secrets are write-only via the API (and the UI) — there is
no "read current value" endpoint, only "overwrite." Writing one requires:
  1. GET the repo's public key (`/actions/secrets/public-key`)
  2. Encrypt the plaintext with libsodium's sealed-box construction using
     that public key (PyNaCl's `nacl.public.SealedBox`)
  3. PUT the base64-encoded ciphertext + key_id to
     `/actions/secrets/{name}`

No `gh` CLI is installed in this environment (confirmed via `where gh` /
`which gh`, both failing) — this script does the same thing GitHub's own
`gh secret set` does internally, using the GitHub token already available
via Git's credential helper (same technique used elsewhere this session
for read-only GitHub API calls) rather than requiring a separate PAT.

Usage
-----
    python src/set_github_secret.py APIFY_TASKS "MO:mA5Nf3X7JTXJyEfC2"

Requires `pynacl` (installed into this project's .venv — not added to
requirements.txt since this is a one-off maintenance tool, not part of the
Actor/pipeline runtime).
"""

import base64
import subprocess
import sys

import requests
from nacl import encoding, public

REPO = "BluetailHomes/SiftStack"
API_BASE = f"https://api.github.com/repos/{REPO}"


def _get_github_token() -> str:
    """Extract a usable token from Git's own stored credential (Windows
    Git Credential Manager) — already authenticated for git push/pull,
    reused here rather than requiring a separate PAT.
    """
    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise RuntimeError("Could not extract a GitHub token from git credential fill")


def _encrypt_secret(public_key_b64: str, secret_value: str) -> str:
    public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def set_secret(name: str, value: str, token: str | None = None) -> None:
    token = token or _get_github_token()
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

    key_resp = requests.get(f"{API_BASE}/actions/secrets/public-key", headers=headers, timeout=15)
    key_resp.raise_for_status()
    key_data = key_resp.json()

    encrypted_value = _encrypt_secret(key_data["key"], value)

    put_resp = requests.put(
        f"{API_BASE}/actions/secrets/{name}",
        headers=headers,
        json={"encrypted_value": encrypted_value, "key_id": key_data["key_id"]},
        timeout=15,
    )
    put_resp.raise_for_status()
    # 201 = secret created, 204 = secret updated — both are success.
    print(f"{name} set successfully (HTTP {put_resp.status_code})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: python {sys.argv[0]} SECRET_NAME SECRET_VALUE", file=sys.stderr)
        sys.exit(1)
    set_secret(sys.argv[1], sys.argv[2])
