"""Import only the current local Codex access token into a personal installation.

Run on the host after enabling DARKFACTORY_PERSONAL_MODE=1 in .env.
Refresh tokens and the host credential directory never enter task containers.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--base-url", help="Optional existing personal Codex HTTP gateway, reachable from Docker"
    )
    parser.add_argument("--model", help="Model configured in your personal Codex client")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        raise SystemExit("Use the loopback URL of your personal installation")
    auth_file = Path.home() / ".codex" / "auth.json"
    if not auth_file.is_file():
        raise SystemExit("Run codex login first; no local file-based login found")
    auth = json.loads(auth_file.read_text())
    tokens = auth.get("tokens") or {}
    if auth.get("auth_mode") != "chatgpt" or not tokens.get("access_token") or not tokens.get("account_id"):
        raise SystemExit("A local ChatGPT login with an account ID is required")
    bootstrap = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "app",
            "sh",
            "-c",
            'printf %s "${DARKFACTORY_ADMIN_TOKEN:-$(cat /data/bootstrap-token)}"',
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip()

    def request(method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        req = Request(
            args.url.rstrip("/") + "/api" + path,
            data=body,
            method=method,
            headers={"Authorization": "Bearer " + bootstrap, "Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            # Never print request bodies or authentication material on failure.
            raise SystemExit(f"Factory rejected {method} {path}: HTTP {exc.code}") from None

    if "codex_local" not in request("GET", "/executors")["codex"]["auth_methods"]:
        raise SystemExit("Enable DARKFACTORY_PERSONAL_MODE=1 and recreate app and worker first")
    pools = request("GET", "/pools")
    pool = next((pool for pool in pools if pool["name"] == "Personal Codex"), None)
    if pool is None:
        pool = request("POST", "/pools", {"name": "Personal Codex", "allowed_providers": ["codex"]})
    credentials = {
        "auth_method": "codex_local",
        "accessToken": tokens["access_token"],
        "chatgptAccountId": tokens["account_id"],
    }
    if args.base_url:
        credentials["base_url"] = args.base_url
    account_key = hashlib.sha256(tokens["account_id"].encode()).hexdigest()
    connection = next(
        (
            c
            for c in request("GET", "/connections")
            if c["provider"] == "codex" and c["account_key"] == account_key
        ),
        None,
    )
    if connection:
        request("PUT", f"/connections/{connection['id']}/credentials", credentials)
        if args.model:
            request(
                "PUT",
                f"/connections/{connection['id']}",
                {
                    "name": connection["name"],
                    "pool_ids": connection["pool_ids"],
                    "priority": connection["priority"],
                    "model": args.model,
                    "enabled": True,
                },
            )
    else:
        connection = request(
            "POST",
            "/connections",
            {
                "name": "Personal Codex (local login)",
                "provider": "codex",
                "account_key": tokens["account_id"],
                "pool_ids": [pool["id"]],
                "credentials": credentials,
                "model": args.model,
            },
        )
    print(
        json.dumps(
            {
                "connection_id": connection["id"],
                "pool_id": pool["id"],
                "provider": "codex",
                "real_account": True,
            }
        )
    )


if __name__ == "__main__":
    main()
