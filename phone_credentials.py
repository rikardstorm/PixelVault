"""User-authorized phone PIN protected by Windows DPAPI (CurrentUser)."""

import base64, ctypes, json, pathlib, sys
from ctypes import wintypes

ROOT = pathlib.Path(__file__).resolve().parent
SECRET = ROOT / ".pixel-uploader/phone-pin.dpapi"


class Blob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data, decrypt=False):
    memory = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(memory, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    function = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    second = None if decrypt else "PixelUploader phone unlock"
    if not function(
        ctypes.byref(source), second, None, None, None, 1, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.data, output.length)
    finally:
        ctypes.windll.kernel32.LocalFree(output.data)


def store(pin, serial):
    if not pin.isdigit() or not 4 <= len(pin) <= 16:
        raise ValueError("Invalid PIN format")
    SECRET.parent.mkdir(parents=True, exist_ok=True)
    SECRET.write_bytes(
        protect(json.dumps({"pin": pin, "serial": serial}).encode("utf-8"))
    )


def load(serial):
    if not SECRET.exists():
        return None
    data = json.loads(protect(SECRET.read_bytes(), decrypt=True))
    if data["serial"] != serial:
        raise RuntimeError("Stored unlock does not belong to this phone")
    return data["pin"]


if __name__ == "__main__":
    import argparse, getpass

    parser = argparse.ArgumentParser(
        description="Optional Windows-local encrypted phone PIN"
    )
    parser.add_argument("--config", type=pathlib.Path, default=ROOT / "config.json")
    parser.add_argument("--forget", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows DPAPI is required")
    if args.forget:
        SECRET.unlink(missing_ok=True)
        print("Local encrypted PIN removed; the phone PIN is unchanged.")
    else:
        serial = json.loads(args.config.read_text(encoding="utf-8"))["serial"]
        pin = getpass.getpass("Phone PIN (input hidden): ")
        if pin != getpass.getpass("Repeat PIN: "):
            raise SystemExit("PINs do not match")
        store(pin, serial)
        print("PIN stored locally with Windows CurrentUser encryption.")
