"""Write the request format as JSON Schema (schema/<kind>.v1.json), for the atlas form and editors.

The Pydantic models are the source; these files are generated from them and a test keeps them in step.
Usage: `shikigami-schema [out dir]`.
"""

import json
import sys
from pathlib import Path

from shikigami.request_format import App, Database


def schemas() -> dict[str, str]:
    """Each request kind's JSON Schema text, by file name."""
    return {
        f"{model.__name__.lower()}.v1.json": json.dumps(model.model_json_schema(by_alias=True), indent=2) + "\n"
        for model in (App, Database)
    }


def main() -> None:
    """The `shikigami-schema` command."""
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "schema")
    out.mkdir(exist_ok=True)
    for name, text in schemas().items():
        (out / name).write_text(text)
        print(out / name)
