"""Type detection from magic bytes (never trust the extension) and archive guards."""
from __future__ import annotations

import zipfile
from pathlib import Path

SUPPORTED = {"pdf", "pptx", "video", "image", "transcript"}
VIDEO_BRANDS = (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"qt  ", b"dash", b"3gp4", b"3gp5")


class UnsafeFile(Exception):
    pass


def sniff(head: bytes, filename: str = "") -> str:
    """Return one of pdf|pptx|docx|xlsx|zip|video|image|transcript|unknown."""
    name = filename.lower()
    if head.startswith(b"%PDF-") or b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):          # OLE compound file
        return "msg" if name.endswith(".msg") else "ole"
    if head.startswith(b"PK\x03\x04"):
        if name.endswith((".pptx", ".ppsx", ".potx")):
            return "pptx"          # confirmed against [Content_Types].xml in check_zip()
        if name.endswith(".docx"):
            return "docx"
        if name.endswith(".xlsx"):
            return "xlsx"
        return "zip"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "video"
    if head.startswith(b"\x1a\x45\xdf\xa3"):     # Matroska / WebM
        return "video"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "video"
    if head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8")) or head[8:12] == b"WEBP":
        return "image"
    if head.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT"):
        return "transcript"
    if name.endswith(".srt") and head[:1].isdigit():
        return "transcript"
    if name.endswith((".txt", ".md", ".csv", ".json", ".log")) and b"\x00" not in head:
        try:
            head.decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            return "unknown"
    return "unknown"


def extension_mismatch(detected: str, filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    expected = {".pdf": "pdf", ".pptx": "pptx", ".docx": "docx", ".msg": "msg", ".mp4": "video", ".m4v": "video", ".mov": "video",
                ".mkv": "video", ".webm": "video", ".png": "image", ".jpg": "image", ".jpeg": "image",
                ".vtt": "transcript", ".srt": "transcript"}.get(ext)
    return expected is not None and expected != detected


def check_zip(path: Path, max_uncompressed: int, max_ratio: float = 200.0, max_entries: int = 20000,
              expected: bytes = b"presentationml") -> None:
    """Reject zip bombs and malformed OOXML before handing to a parser."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if len(infos) > max_entries:
                raise UnsafeFile("too many zip entries")
            total = 0
            names = set()
            for i in infos:
                if i.filename.startswith("/") or ".." in Path(i.filename).parts:
                    raise UnsafeFile(f"unsafe entry path {i.filename!r}")
                total += i.file_size
                if i.compress_size and i.file_size / max(1, i.compress_size) > max_ratio and i.file_size > 10_000_000:
                    raise UnsafeFile("suspicious compression ratio")
                names.add(i.filename)
            if total > max_uncompressed:
                raise UnsafeFile("uncompressed size exceeds limit")
            if "[Content_Types].xml" not in names:
                raise UnsafeFile("not an OOXML package")
            ct = zf.read("[Content_Types].xml")[:200_000]
            if expected not in ct:
                raise UnsafeFile(f"OOXML package is not a {expected.decode()} document")
    except zipfile.BadZipFile as e:
        raise UnsafeFile(f"bad zip: {e}") from e
