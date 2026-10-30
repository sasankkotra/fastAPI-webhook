"""Send a signed sample request using only the Python standard library."""

import argparse
import getpass
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="Cloud Run HTTPS URL, or http://127.0.0.1:8080")
    parser.add_argument("--event-id", help="Reuse on retries; use a fresh UUID for each new event")
    args = parser.parse_args()
    if not args.base_url.startswith("https://") and not args.base_url.startswith("http://127.0.0.1:"):
        parser.error("Use HTTPS, or HTTP on 127.0.0.1 for local testing")
    secret = os.environ.get("WEBHOOK_SECRET") or getpass.getpass("Webhook secret: ")
    if re.fullmatch(r"[0-9a-fA-F]{512}", secret) is None:
        parser.error("WEBHOOK_SECRET must be exactly 512 hexadecimal characters (a 2048-bit key)")
    key_bytes = bytes.fromhex(secret)
    payload = {
        "formId": "101", "postId": "5001",
        "formData": {"establishment_name": "Sri Udupi Park", "city": "Bengaluru"},
    }
    if args.event_id is not None:
        payload["formData"]["_event_id"] = args.event_id
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(key_bytes, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        args.base_url.rstrip("/") + "/webhooks/wordpress/forms", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Webhook-Timestamp": timestamp, "X-Webhook-Signature": "sha256=" + signature},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(response.status, response.read().decode())
    except urllib.error.HTTPError as exc:
        print(exc.code, exc.read().decode())
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
