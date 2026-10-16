from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LONGPORT_CREDENTIALS = ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN")


def load_longport_profile(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except (OSError, UnicodeError):
        raise RuntimeError("Cannot read Longbridge credentials from ~/.bashrc") from None
    values = {}
    for line in lines:
        match = re.match(r"\s*export\s+(LONGPORT_(?:APP_KEY|APP_SECRET|ACCESS_TOKEN))\b(.*)$", line)
        if not match:
            continue
        name, assignment = match.groups()
        raw = assignment[1:] if assignment.startswith("=") else ""
        # Never execute profile code or treat shell expansions as literal credentials.
        if "\x00" in raw or not re.fullmatch(r"(?:'[^']*'|\"[^\"\\$`]*\"|[A-Za-z0-9_./+=:@%,-]+)(?:[ \t]+(?:#.*)?)?", raw):
            raise RuntimeError(f"Invalid literal export for {name} in ~/.bashrc")
        parsed = shlex.split(raw, comments=True, posix=True)
        if len(parsed) != 1 or not parsed[0]:
            raise RuntimeError(f"Empty literal export for {name} in ~/.bashrc")
        values[name] = parsed[0]
    if values:
        if any(name not in values for name in LONGPORT_CREDENTIALS):
            raise RuntimeError("Incomplete Longbridge exports in ~/.bashrc; set LONGPORT_APP_KEY, LONGPORT_APP_SECRET and LONGPORT_ACCESS_TOKEN together")
        os.environ.update(values)


load_longport_profile(Path.home() / ".bashrc")
load_dotenv(PROJECT_ROOT / ".env", override=False)


class Settings:
    def __init__(self, data: dict):
        self._data = data

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def section(self, path: str) -> dict:
        value = self.get(path, {})
        return value if isinstance(value, dict) else {}

    def resolve_path(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def host(self) -> str:
        return os.environ.get("TRADE_PLAT_HOST") or self.get("server.host", "0.0.0.0")

    @property
    def port(self) -> int:
        return int(os.environ.get("TRADE_PLAT_PORT") or self.get("server.port", 8080))

    @property
    def log_level(self) -> str:
        return os.environ.get("TRADE_PLAT_LOG_LEVEL") or self.get("logging.level", "INFO")

    @property
    def log_file(self) -> Path:
        return self.resolve_path(os.environ.get("TRADE_PLAT_LOG_FILE") or self.get("logging.file", "logs/app.log"))

    @property
    def data_dir(self) -> Path:
        return self.resolve_path(self.get("data.dir", "data"))

    @property
    def db_file(self) -> Path:
        return self.resolve_path(self.get("data.db_file", "data/trade_plat.db"))

    @property
    def frontend_dir(self) -> Path:
        return PROJECT_ROOT / "frontend"

    @property
    def default_watchlist(self) -> list:
        return list(self.get("watchlist.default", ["SPY.US"]))

    @property
    def longbridge_credentials_present(self) -> bool:
        return all(os.environ.get(k) for k in ("LONGPORT_APP_KEY", "LONGPORT_APP_SECRET", "LONGPORT_ACCESS_TOKEN"))


def load_settings(path: Optional[Path] = None) -> Settings:
    cfg_path = path or PROJECT_ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return Settings(data)


settings = load_settings()
