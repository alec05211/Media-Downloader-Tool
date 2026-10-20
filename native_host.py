"""Chrome native-messaging bridge for launching Media Downloader."""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit

MAX_MESSAGE_BYTES = 1024 * 1024


def validated_url(message: object) -> str:
    if not isinstance(message, dict) or message.get("action") != "open":
        return ""
    candidate = str(message.get("url", "")).strip()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or len(candidate) > 16384:
        return ""
    return candidate


def protocol_url(message: object) -> str:
    candidate = validated_url(message)
    return "media-downloader://open" if not candidate else f"media-downloader://open?{urlencode({'url': candidate})}"


def read_message() -> object:
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) != 4:
        raise ValueError("Missing native-message length.")
    length = struct.unpack("@I", raw_length)[0]
    if length < 1 or length > MAX_MESSAGE_BYTES:
        raise ValueError("Native message is too large.")
    payload = sys.stdin.buffer.read(length)
    if len(payload) != length:
        raise ValueError("Native message ended early.")
    return json.loads(payload.decode("utf-8"))


def write_message(message: object) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("@I", len(payload)))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def application_path() -> Path:
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    return root / "application.exe"


def main() -> int:
    try:
        message = read_message()
        if not isinstance(message, dict) or message.get("action") != "open":
            raise ValueError("Unsupported native-host action.")
        executable = application_path()
        if not executable.is_file():
            raise FileNotFoundError(f"Media Downloader is not installed beside the native host: {executable}")
        subprocess.Popen(
            [str(executable), protocol_url(message)],
            close_fds=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
        write_message({"ok": True})
        return 0
    except Exception as exc:
        try:
            write_message({"ok": False, "error": str(exc)})
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
