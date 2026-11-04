"""Picture files shipped with the app, under training/exam_images."""

import mimetypes
from pathlib import Path

EXAM_IMAGE_DIR = Path(__file__).resolve().parent / "exam_images"
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def list_library_images() -> list[str]:
    if not EXAM_IMAGE_DIR.is_dir():
        return []
    names = []
    for path in EXAM_IMAGE_DIR.iterdir():
        if path.is_file() and path.suffix.lower() in ALLOWED_SUFFIXES:
            names.append(path.name)
    return sorted(names, key=str.lower)


def library_image_path(filename: str) -> Path | None:
    name = Path(filename or "").name
    if not name or Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
        return None
    path = (EXAM_IMAGE_DIR / name).resolve()
    try:
        path.relative_to(EXAM_IMAGE_DIR.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path


def read_library_image(filename: str) -> tuple[bytes, str] | None:
    path = library_image_path(filename)
    if path is None:
        return None
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path.read_bytes(), content_type
