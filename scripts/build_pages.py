"""Build only the public frontend into docs/ for GitHub Pages.

No backend modules, settings files, search history, or imported documents are
read. docs/ is a disposable build directory and is replaced on a successful
build. XUNWEI_PAGES_API_BASE is a public URL, never a credential.
"""
from __future__ import annotations

from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import unquote, urlsplit, urlunsplit


STATIC_FILES = (
    "index.html", "app.js", "styles.css", "connection.js",
    "deployment-config.js", "platforms.json",
)
SECRET_SHAPE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")


class BuildError(ValueError):
    """A safe diagnostic that does not echo configuration values or secrets."""


def normalize_api_base(value: str) -> str:
    if not isinstance(value, str):
        raise BuildError("The public API base must be a URL string.")
    value = value.strip()
    if not value:
        return ""
    if len(value) > 2048 or re.search(r"[\x00-\x20\x7f\\]", value) or SECRET_SHAPE.search(value):
        raise BuildError("The public API base contains invalid or credential-like text.")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port
        if not host or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            raise BuildError("Use an API origin/path without credentials, a query, or a fragment.")
        if re.search(r"[\x00-\x20\x7f\\]", unquote(parts.path)):
            raise BuildError("The public API base contains an invalid path.")
        try:
            address = ipaddress.ip_address(host)
            # Match connection.js and its browser CSP: HTTP IPv6 and other
            # 127/8 addresses are not enabled for the Pages frontend.
            loopback = str(address) == "127.0.0.1"
            host = "[" + address.compressed + "]" if address.version == 6 else address.compressed
        except ValueError:
            loopback = host == "localhost"
            host = host.encode("idna").decode("ascii")
            if not re.fullmatch(r"[a-z0-9.-]+", host) or any(not label or label.startswith("-") or label.endswith("-") for label in host.split(".")):
                raise BuildError("The public API hostname is invalid.")
        if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
            raise BuildError("Remote API services require HTTPS; HTTP is only allowed for localhost or 127.0.0.1 previews.")
        if port is not None:
            host += ":" + str(port)
        return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError) as error:
        if isinstance(error, BuildError):
            raise
        raise BuildError("The public API base is not a valid URL.") from None


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


class _RelativeResources(HTMLParser):
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key not in ("href", "src") or not value or value.startswith(("#", "data:")):
                continue
            parts = urlsplit(value)
            if parts.scheme or parts.netloc:
                if key == "src" or tag == "link":
                    raise BuildError("Frontend assets must be bundled locally, not loaded from another origin.")
                continue
            if value.startswith("/"):
                raise BuildError("Use relative frontend asset/navigation URLs so project Pages subpaths work.")
            path = unquote(parts.path)
            if path in ("", ".", "./"):
                continue
            if path.removeprefix("./") not in STATIC_FILES:
                raise BuildError("HTML references a file outside the static build allowlist.")


def build_pages(project_root: Path | str, api_base: str = "") -> Path:
    root = Path(project_root).resolve(strict=True)
    source = root / "web"
    output = root / "docs"
    if not source.is_dir() or _is_link(source) or source.resolve() != root / "web":
        raise BuildError("The frontend source must be the project's ordinary web directory.")
    if _is_link(output) or output.resolve().parent != root or (output.exists() and not output.is_dir()):
        raise BuildError("The docs build target must be an ordinary directory directly inside the project.")
    public_base = normalize_api_base(api_base)
    contents = {}
    for name in STATIC_FILES:
        path = source / name
        if not path.is_file() or _is_link(path) or path.resolve().parent != source:
            raise BuildError("Missing or linked frontend asset: " + name)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise BuildError("Frontend asset must be a readable UTF-8 file: " + name) from None
        if SECRET_SHAPE.search(content):
            raise BuildError("Credential-shaped content found in a static asset; build stopped: " + name)
        contents[name] = content
    try:
        catalog = json.loads(contents["platforms.json"])
    except ValueError:
        raise BuildError("platforms.json must contain valid public catalog JSON.") from None
    if not isinstance(catalog, dict) or not isinstance(catalog.get("items"), list):
        raise BuildError("platforms.json must contain an items array.")
    if SECRET_SHAPE.search(json.dumps(catalog, ensure_ascii=False)):
        raise BuildError("Credential-shaped content found in the public platform catalog.")
    _RelativeResources().feed(contents["index.html"])
    contents["deployment-config.js"] = (
        "// Public deployment configuration. Never put credentials in this file.\n"
        "window.XUNWEI_DEPLOYMENT = " + json.dumps({"mode": "pages", "apiBase": public_base}, ensure_ascii=True) + ";\n"
    )
    with tempfile.TemporaryDirectory(prefix=".pages-build-", dir=root) as temporary:
        staging = Path(temporary)
        for name, content in contents.items():
            (staging / name).write_text(content, encoding="utf-8", newline="\n")
        (staging / ".nojekyll").write_text("", encoding="utf-8")
        # Check the actual destination immediately before replacing recursively.
        # No caller-supplied output path and no cross-shell filesystem commands.
        if _is_link(output) or output.resolve().parent != root:
            raise BuildError("The docs target changed during the build; replacement refused.")
        if output.exists():
            shutil.rmtree(output)
        staging.rename(output)
    return output


def main():
    try:
        output = build_pages(Path(__file__).resolve().parents[1], os.environ.get("XUNWEI_PAGES_API_BASE", ""))
    except (BuildError, OSError) as error:
        # Do not print arbitrary filesystem errors containing source contents or
        # user-provided URLs. BuildError messages are deliberately value-free.
        print("Pages build failed: " + (str(error) if isinstance(error, BuildError) else "filesystem operation failed"))
        return 1
    print("Built docs/ with " + str(len(STATIC_FILES) + 1) + " public static files. Backend and user data were excluded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
