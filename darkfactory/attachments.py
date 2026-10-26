"""Owner-supplied files for a task: stored by the control plane, mounted read-only into executors."""

import base64
import binascii
import re

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .db import ROOT

FILE_BYTES = 10 * 1024 * 1024
TASK_BYTES = 50 * 1024 * 1024
TASK_FILES = 20
MOUNT = "/run/attachments"
# Images a model can see directly; other files are read from the mount.
IMAGE_MAGIC = {
    "image/png": b"\x89PNG",
    "image/jpeg": b"\xff\xd8\xff",
    "image/gif": b"GIF8",
    "image/webp": b"RIFF",
}
MODEL_IMAGES = 4
MODEL_IMAGE_BYTES = 5 * 1024 * 1024


class AttachmentInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    media_type: str = Field(default="application/octet-stream", max_length=100)
    data: str = Field(min_length=1, max_length=14_000_000)


def directory(task_id):
    return ROOT / "artifacts" / task_id / "attachments"


def safe_name(name):
    name = re.sub(r"[^\w.\-]+", "_", name.replace("\\", "/").rsplit("/", 1)[-1]).strip("._")
    return name[:120] or "file"


def listing(task_id):
    try:
        paths = sorted(directory(task_id).iterdir(), key=lambda path: path.name)
    except OSError:
        return []
    return [
        {"name": path.name, "size": path.stat().st_size}
        for path in paths
        if path.is_file() and not path.is_symlink()
    ]


def save(task_id, files):
    """Validate the whole batch before writing anything; returns stored names."""
    existing = listing(task_id)
    if len(existing) + len(files) > TASK_FILES:
        raise HTTPException(422, f"Не больше {TASK_FILES} файлов на задачу")
    decoded, total = [], sum(item["size"] for item in existing)
    for item in files:
        try:
            data = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, f"Файл {item.name} повреждён") from None
        if len(data) > FILE_BYTES:
            raise HTTPException(422, f"Файл {item.name} больше 10 МБ")
        magic = IMAGE_MAGIC.get(item.media_type)
        if magic and not data.startswith(magic):
            raise HTTPException(422, f"Файл {item.name} не совпадает с типом {item.media_type}")
        total += len(data)
        decoded.append((safe_name(item.name), data))
    if total > TASK_BYTES:
        raise HTTPException(422, "Вложения задачи больше 50 МБ")
    target = directory(task_id)
    target.mkdir(parents=True, exist_ok=True)
    stored = []
    for index, (name, data) in enumerate(decoded, start=len(existing) + 1):
        stored_name = f"{index:02d}-{name}"
        (target / stored_name).write_bytes(data)
        stored.append(stored_name)
    return stored


def image_type(data):
    return next((kind for kind, magic in IMAGE_MAGIC.items() if data.startswith(magic)), None)


def model_images(task_id):
    """The newest small images, passed to the model inline beside the mounted copies."""
    images = []
    for item in reversed(listing(task_id)):
        if len(images) >= MODEL_IMAGES or item["size"] > MODEL_IMAGE_BYTES:
            continue
        data = (directory(task_id) / item["name"]).read_bytes()
        kind = image_type(data)
        if kind:
            images.append({"media_type": kind, "data": base64.b64encode(data).decode()})
    return list(reversed(images))


def prompt(task_id):
    files = listing(task_id)
    if not files:
        return ""
    lines = "\n".join(f"- {MOUNT}/{item['name']} ({item['size']} bytes)" for item in files)
    return f"\nOwner attached files (read-only, outside the repository; do not commit them unless asked):\n{lines}\n"
