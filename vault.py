"""Encrypted copy of the CRM's settings (Gmail app password, Slack webhook, Google backup key, etc.), so a restore on a new Mac needs no re-typing.

Only stdlib primitives are used: PBKDF2-HMAC-SHA256 to turn your passphrase into a key, an HMAC-SHA256 counter-mode keystream to encrypt,
and an HMAC-SHA256 tag over the whole file (encrypt-then-MAC) so a wrong passphrase or any tampering is detected before anything is written.
The passphrase itself is never stored. The derived key is kept in data/vault.json (readable only by you) so the copy can be refreshed automatically
whenever a backup runs; to open the copy on another Mac you need the passphrase.
"""
import base64
import hashlib
import hmac
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.environ.get("PARTNER_CRM_VAULT_KEY", os.path.join(HERE, "data", "vault.json"))
MAGIC = b"BCRMSEC1"
ITER = 600_000
MIN_PASSPHRASE = 10


def _cfg_files():
    """name inside the vault -> path on disk. Only files that exist are included."""
    import attribution, backup, gdrive, mail, notify, youform
    paths = [mail.CFG_PATH, notify.CFG_PATH, gdrive.CFG_PATH, backup.CFG_PATH, attribution.CFG_PATH, youform.CFG_PATH]
    return {os.path.basename(p): p for p in paths if os.path.isfile(p)}


def _derive(passphrase, salt):
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, ITER, 32)


def _subkeys(master):
    return hmac.new(master, b"enc", "sha256").digest(), hmac.new(master, b"mac", "sha256").digest()


def _stream(key, nonce, n):
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(8, "big"), "sha256").digest()
        i += 1
    return bytes(out[:n])


def encrypt(plain, master, salt):
    enc, mac = _subkeys(master)
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(plain, _stream(enc, nonce, len(plain))))
    tag = hmac.new(mac, MAGIC + salt + nonce + ct, "sha256").digest()
    return MAGIC + salt + nonce + tag + ct


def decrypt(blob, passphrase):
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 64:
        raise ValueError("This isn't an encrypted settings file from the CRM")
    salt, nonce, tag, ct = blob[8:24], blob[24:40], blob[40:72], blob[72:]
    enc, mac = _subkeys(_derive(passphrase, salt))
    if not hmac.compare_digest(tag, hmac.new(mac, MAGIC + salt + nonce + ct, "sha256").digest()):
        raise ValueError("Wrong passphrase, or the file is damaged")
    return bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct))))


def _load_key():
    try:
        with open(KEY_PATH) as f:
            d = json.load(f)
        return bytes.fromhex(d["salt"]), bytes.fromhex(d["key"])
    except (OSError, ValueError, KeyError):
        return None


def configured():
    return _load_key() is not None


def setup(passphrase):
    p = (passphrase or "").strip()
    if len(p) < MIN_PASSPHRASE:
        raise ValueError(f"Use a passphrase of at least {MIN_PASSPHRASE} characters. Make it something you can remember or keep in a password manager: nobody, including me, can recover it.")
    salt = os.urandom(16)
    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"salt": salt.hex(), "key": _derive(p, salt).hex(), "created": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)


def remove():
    try:
        os.remove(KEY_PATH)
    except OSError:
        pass


def build_blob():
    k = _load_key()
    if not k:
        return None
    salt, master = k
    files = {}
    for name, path in _cfg_files().items():
        with open(path, encoding="utf-8") as f:
            files[name] = f.read()
    payload = json.dumps({"files": files, "made": time.strftime("%Y-%m-%dT%H:%M:%S")}).encode()
    return encrypt(payload, master, salt)


def write_local(folder):
    blob = build_blob()
    if blob is None:
        return None
    d = os.path.join(folder, "secrets")
    os.makedirs(d, mode=0o700, exist_ok=True)
    p = os.path.join(d, "settings.enc")
    with open(p + ".tmp", "wb") as f:
        f.write(blob)
    os.chmod(p + ".tmp", 0o600)
    os.replace(p + ".tmp", p)
    return p


def open_blob(blob, passphrase):
    """Decrypt and return {filename: text}."""
    return json.loads(decrypt(blob, passphrase))["files"]


def check(passphrase, blob=None):
    """True if `passphrase` opens the current encrypted copy (used by the 'test my passphrase' button)."""
    blob = blob or build_blob()
    if blob is None:
        raise ValueError("No encrypted copy exists yet")
    open_blob(blob, passphrase)
    return True


def restore_into(data_dir, blob, passphrase):
    """Write the decrypted settings into data_dir. An existing file is kept as <name>.before-restore. Returns the names written."""
    files = open_blob(blob, passphrase)
    os.makedirs(data_dir, exist_ok=True)
    written = []
    for name, text in files.items():
        if name != os.path.basename(name) or not name.endswith(".json"):
            continue        # never write outside data/ or anything but settings files
        dst = os.path.join(data_dir, name)
        if os.path.exists(dst):
            os.replace(dst, dst + ".before-restore")
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        written.append(name)
    return written


def check_stored(blob):
    """Verify an encrypted copy (e.g. one downloaded from Drive) with the key kept on this Mac. Returns the setting files inside."""
    k = _load_key()
    if not k:
        raise ValueError("No passphrase has been set")
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 64:
        raise ValueError("not an encrypted settings file")
    salt, master = k
    if blob[8:24] != salt:
        raise ValueError("encrypted with a different passphrase than the current one")
    nonce, tag, ct = blob[24:40], blob[40:72], blob[72:]
    enc, mac = _subkeys(master)
    if not hmac.compare_digest(tag, hmac.new(mac, MAGIC + salt + nonce + ct, "sha256").digest()):
        raise ValueError("damaged")
    plain = bytes(a ^ b for a, b in zip(ct, _stream(enc, nonce, len(ct))))
    return sorted(json.loads(plain)["files"])
