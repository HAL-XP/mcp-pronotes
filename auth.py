"""Local QR bootstrap and rotating mobile-token storage."""

import json
import os
from pathlib import Path
import secrets
import tempfile
from urllib.parse import urlparse

import pronotepy


def write_private_json(path: Path, data: dict) -> None:
    """Replace the credentials atomically with a file readable only by its owner."""
    if path.is_symlink():
        raise ValueError("The login file must not be a symbolic link.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".pronote-login-", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def persist_credentials(client, path: Path, account_type: str, options: dict | None = None) -> None:
    data = {
        "auth_mode": "token",
        "account_type": account_type,
        "credentials": client.export_credentials(),
    }
    if options:
        data.update(options)
    write_private_json(path, data)


def login_from_file(path: Path, account_type: str):
    if path.is_symlink():
        raise ValueError("The login file must not be a symbolic link.")
    os.chmod(path, 0o600)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ValueError("The login file contains invalid JSON.") from None
    if not isinstance(data, dict):
        raise ValueError("The login file must contain a JSON object.")
    account_type = data.get("account_type", account_type)
    if account_type not in {"parent", "student"}:
        raise ValueError("account_type must be parent or student.")
    cls = pronotepy.ParentClient if account_type == "parent" else pronotepy.Client
    options = {key: data[key] for key in ("account_pin", "device_name") if data.get(key)}
    if "qr_code" in data or all(key in data for key in ("url", "login", "jeton")):
        qr = data.get("qr_code", data)
        if not isinstance(qr, dict) or any(not isinstance(qr.get(key), str) or not qr[key]
                                           for key in ("url", "login", "jeton")):
            raise ValueError("QR login requires url, login, and jeton strings.")
        pin = os.environ.get("PRONOTE_QR_PIN") or data.get("pin")
        if not isinstance(pin, str) or len(pin) != 4 or not pin.isascii() or not pin.isdigit():
            raise ValueError("Set the four-digit QR PIN in the login file or PRONOTE_QR_PIN.")
        _validate_url(qr["url"], account_type)
        uuid = data.get("uuid") or secrets.token_hex(16)
        # Store the device ID before consuming the QR code. It must stay the same.
        write_private_json(path, {"qr_code": qr, "pin": pin, "uuid": uuid,
                                  "account_type": account_type, **options})
        client = cls.qrcode_login(qr, pin, uuid, skip_2fa=True, **options)
    else:
        credentials = data.get("credentials", data)
        if not isinstance(credentials, dict) or any(
            not isinstance(credentials.get(key), str) or not credentials[key]
            for key in ("pronote_url", "username", "password", "uuid")
        ):
            raise ValueError("Expected a PRONOTE QR payload or exported mobile-token credentials.")
        _validate_url(credentials["pronote_url"], account_type)
        allowed = ("pronote_url", "username", "password", "uuid", "client_identifier")
        client = cls.token_login(**{key: credentials[key] for key in allowed if key in credentials}, **options)
    if not client.logged_in:
        raise ValueError("PRONOTE rejected the login. Generate a fresh QR code.")
    persist_credentials(client, path, account_type, options)
    return client, account_type, options


def _validate_url(url: str, account_type: str) -> None:
    parsed = urlparse(url)
    expected = "parent.html" if account_type == "parent" else "eleve.html"
    if parsed.scheme != "https" or not parsed.hostname or not parsed.path.endswith(expected):
        raise ValueError("The login URL must be HTTPS and match the selected account type.")
