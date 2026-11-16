"""Build manifest with source checksums and installed versions; never collect environment secrets."""
from __future__ import annotations
import hashlib
from importlib.metadata import distributions
from pathlib import Path
import platform
from . import __version__
from .core.util import content_hash


def manifest(root: Path) -> dict:
    files = sorted([p for base in (root/"src", root/"requirements") if base.exists()
                    for p in base.rglob("*") if p.is_file() and p.suffix in (".py", ".txt", ".mako", ".html", ".css", ".js", ".svg")])
    files.extend(p for p in (root/"pyproject.toml", root/"Dockerfile", root/"README.md", root/"docker-compose.yml") if p.is_file())
    checksums = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
    packages = {d.metadata["Name"]: d.version for d in distributions()}
    return {"application_version": __version__, "python": platform.python_version(),
            "platform": platform.platform(), "source_tree_digest": content_hash(checksums), "files": checksums,
            "installed_packages": dict(sorted(packages.items())),
            "container_image_digest": None, "status": "source/runtime evidence; image build and hash-locked wheels not verified"}
