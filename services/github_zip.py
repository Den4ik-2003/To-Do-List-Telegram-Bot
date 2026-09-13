import io
import zipfile

MAX_ZIP_SIZE = 20 * 1024 * 1024
MAX_FILES = 5000
_IGNORED_PREFIXES = ("__MACOSX/", ".git/", "node_modules/", ".DS_Store")


class ZipValidationError(Exception):
    def __init__(self, reason: str, hint: str):
        self.reason = reason
        self.hint = hint
        super().__init__(reason)


def _is_safe_path(path: str) -> bool:
    if path.startswith("/") or path.startswith("\\"):
        return False
    if ".." in path.replace("\\", "/").split("/"):
        return False
    return True


def _should_skip(path: str) -> bool:
    return any(path.startswith(p) or f"/{p}" in path for p in _IGNORED_PREFIXES)


def _common_root_dir(names: list[str]) -> str:
    if not names:
        return ""
    first = names[0]
    if "/" not in first:
        return ""
    root = first.split("/")[0] + "/"
    if all(n.startswith(root) for n in names):
        return root
    return ""


def _guess_project_type(files: dict[str, bytes]) -> str:
    if "vite.config.js" in files or "vite.config.ts" in files:
        return "Vite"
    if "package.json" in files:
        try:
            content = files["package.json"].decode("utf-8", errors="ignore")
        except Exception:
            content = ""
        if '"react"' in content:
            return "React"
        return "Node.js"
    if "index.html" in files:
        return "HTML"
    return "Невідомо"


def extract_zip(zip_bytes: bytes) -> dict:
    if not zip_bytes:
        raise ZipValidationError("Порожній файл.", "Надішли непорожній ZIP-архів.")
    if len(zip_bytes) > MAX_ZIP_SIZE:
        raise ZipValidationError(
            f"Розмір архіву перевищує ліміт ({MAX_ZIP_SIZE // (1024 * 1024)} МБ, обмеження Telegram Bot API).",
            "Прибери node_modules/venv/build-папки з архіву і спробуй ще раз.",
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ZipValidationError("Файл не є коректним ZIP-архівом.", "Перевір архів і надішли ще раз.")

    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        raise ZipValidationError("Архів порожній.", "Заархівуй файли проєкту і спробуй ще раз.")
    if len(names) > MAX_FILES:
        raise ZipValidationError(
            f"Забагато файлів в архіві ({len(names)}).",
            "Прибери зайве (node_modules, build, .git) і задеплой ще раз.",
        )

    common_prefix = _common_root_dir(names)

    files: dict[str, bytes] = {}
    for name in names:
        if not _is_safe_path(name):
            raise ZipValidationError(
                f"Небезпечний шлях у архіві: {name}",
                "Заархівуй проєкт стандартним способом (без символічних посилань і шляхів з «..»).",
            )
        if _should_skip(name):
            continue
        rel = name[len(common_prefix):] if common_prefix and name.startswith(common_prefix) else name
        rel = rel.lstrip("/")
        if not rel:
            continue
        try:
            files[rel] = zf.read(name)
        except Exception:
            raise ZipValidationError(
                f"Не вдалося прочитати файл {name} з архіву.",
                "Перезаархівуй проєкт і спробуй ще раз.",
            )

    if not files:
        raise ZipValidationError("Після фільтрації службових файлів архів порожній.", "Перевір вміст архіву.")

    total_size = sum(len(c) for c in files.values())
    has_package_json = "package.json" in files
    has_readme = any(f.lower().startswith("readme") for f in files)
    project_type = _guess_project_type(files)

    return {
        "files": files,
        "file_count": len(files),
        "total_size": total_size,
        "has_package_json": has_package_json,
        "has_readme": has_readme,
        "project_type": project_type,
    }


def fmt_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} Б"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} КБ"
    return f"{num_bytes / (1024 * 1024):.1f} МБ"