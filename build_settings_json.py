#!/usr/bin/env python3
"""Generate an Azure App Service settings.json from a .env file."""

import json
import sys
from pathlib import Path


def parse_env_file(env_path: Path) -> list[dict]:
    settings = []
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        settings.append({"name": key, "value": value, "slotSetting": False})
    return settings


def main():
    env_file = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".env")
    output_file = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("settings.json")

    if not env_file.exists():
        print(f"Error: {env_file} not found", file=sys.stderr)
        sys.exit(1)

    settings = parse_env_file(env_file)
    output_file.write_text(json.dumps(settings, indent=2))
    print(f"Written {len(settings)} settings to {output_file}")


if __name__ == "__main__":
    main()
