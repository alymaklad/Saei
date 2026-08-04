"""
Small .env reader/writer used by the Settings page's API endpoints.

Updates specific KEY=value lines in place and preserves everything else
(comments, blank lines, ordering) exactly as-is. Keys that don't exist yet
are appended at the end.
"""
import os


def read_env_file(path: str = ".env") -> dict:
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            values[key.strip()] = value.strip()
    return values


def update_env_file(updates: dict, path: str = ".env") -> None:
    """
    updates: {KEY: value}. A value of None removes the key entirely (rather
    than writing KEY= with an empty value), used when the user clears a field.
    """
    lines = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    remaining = dict(updates)
    output = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            value = remaining.pop(key)
            if value is None:
                continue  # drop this line -- key removed
            output.append(f"{key}={value}\n")
        else:
            output.append(line)

    for key, value in remaining.items():
        if value is None:
            continue  # nothing to remove, it wasn't there
        output.append(f"{key}={value}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(output)
