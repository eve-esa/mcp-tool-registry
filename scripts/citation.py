"""
Validate per-server CITATION.cff files and keep server READMEs in sync.

Every server ships a CITATION.cff in Citation File Format (CFF) 1.2.0.
This module validates it against the official CFF JSON Schema (vendored in
scripts/schemas/) and renders an "Authors" and "Citation" section into the
server README.md between generated markers. Text outside the markers is
left untouched; a missing README is created from the CFF title and abstract.

Usage:
    python scripts/citation.py servers/effis            # create or update README.md
    python scripts/citation.py servers/effis --check    # exit 1 if README.md is stale

References:
    https://github.com/citation-file-format/citation-file-format/blob/1.2.0/schema-guide.md
    https://citation-file-format.github.io/1.2.0/schema.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator
from jsonschema.exceptions import ValidationError

CFF_FILENAME = "CITATION.cff"
README_FILENAME = "README.md"
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "cff-1.2.0.schema.json"

BEGIN_MARKER = (
    "<!-- BEGIN GENERATED CITATION: edit CITATION.cff, "
    "then run `python scripts/citation.py servers/<name>` -->"
)
END_MARKER = "<!-- END GENERATED CITATION -->"

_GITHUB_HANDLE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class _CffLoader(yaml.SafeLoader):
    """SafeLoader that keeps dates as strings, as the CFF schema requires."""


_CffLoader.yaml_implicit_resolvers = {
    first_char: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag != "tag:yaml.org,2002:timestamp"
    ]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_cff(path: Path) -> dict[str, Any]:
    """Parse a CITATION.cff file. Raises ValueError on invalid YAML."""
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_CffLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("top level must be a mapping of CFF keys")
    return data


def _leaf_errors(error: ValidationError) -> list[ValidationError]:
    """Expand anyOf/oneOf failures into the errors of the closest branch.

    An author is "person or entity", so a typo in a person would otherwise be
    reported against the entity branch. The branch with the fewest errors is
    the one the author most likely meant.
    """
    if not error.context:
        return [error]
    branches: dict[Any, list[ValidationError]] = {}
    for sub in error.context:
        branches.setdefault(sub.schema_path[0], []).append(sub)
    closest = min(branches.values(), key=len)
    return [leaf for sub in closest for leaf in _leaf_errors(sub)]


def validate_cff(data: dict[str, Any]) -> list[str]:
    """Return human-readable schema violations; empty if the file is valid."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    messages: list[str] = []
    for error in Draft7Validator(schema).iter_errors(data):
        for leaf in _leaf_errors(error):
            location = "/".join(str(p) for p in leaf.absolute_path) or "<root>"
            messages.append(f"{location}: {leaf.message}")
    return sorted(messages)


def _author_name(author: dict[str, Any]) -> str:
    if "name" in author:
        return str(author["name"])
    parts = [
        author.get("given-names"),
        author.get("name-particle"),
        author.get("family-names"),
        author.get("name-suffix"),
    ]
    return " ".join(str(p) for p in parts if p)


def _author_line(author: dict[str, Any]) -> str:
    name = _author_name(author)
    website = author.get("website")
    line = f"**[{name}]({website})**" if website else f"**{name}**"

    details: list[str] = []
    alias = author.get("alias")
    if alias and _GITHUB_HANDLE.match(str(alias)):
        details.append(f"[@{alias}](https://github.com/{alias})")
    elif alias:
        details.append(str(alias))
    if author.get("affiliation"):
        details.append(str(author["affiliation"]))
    if author.get("orcid"):
        details.append(f"[ORCID]({author['orcid']})")

    return f"- {line}" + (f", {', '.join(details)}" if details else "")


def _preferred_citation_line(ref: dict[str, Any]) -> str:
    names = ", ".join(_author_name(a) for a in ref.get("authors", []))
    year = ref.get("year") or str(ref.get("date-published", ""))[:4]
    link = f"https://doi.org/{ref['doi']}" if ref.get("doi") else ref.get("url")
    title = f"[{ref['title']}]({link})" if link else ref["title"]
    return f"{names} ({year}). *{title}*." if year else f"{names}. *{title}*."


def render_block(data: dict[str, Any]) -> str:
    """Render the generated README section from validated CFF data."""
    lines = [BEGIN_MARKER, "", "## Authors", ""]
    lines += [_author_line(a) for a in data["authors"]]
    lines += ["", "## Citation", ""]

    ref = data.get("preferred-citation")
    if ref:
        lines += ["If you use this server, please cite:", "", _preferred_citation_line(ref), ""]
    lines += [
        f"Citation metadata lives in [`{CFF_FILENAME}`]({CFF_FILENAME}). "
        "Convert it to BibTeX or APA with "
        f"[cffconvert](https://github.com/citation-file-format/cffconvert): "
        f"`cffconvert -i {CFF_FILENAME} -f bibtex`.",
        "",
        END_MARKER,
    ]
    return "\n".join(lines)


def render_readme(server_dir: Path) -> str:
    """Return the README.md content expected for this server's CITATION.cff."""
    data = load_cff(server_dir / CFF_FILENAME)
    block = render_block(data)
    readme = server_dir / README_FILENAME

    if not readme.exists():
        header = f"# {data['title']}\n\n"
        abstract = str(data.get("abstract", "")).strip()
        return header + (f"{abstract}\n\n" if abstract else "") + block + "\n"

    current = readme.read_text(encoding="utf-8")
    start = current.find(BEGIN_MARKER)
    if start == -1:
        return current.rstrip() + "\n\n" + block + "\n"

    end = current.find(END_MARKER, start)
    if end == -1:
        raise ValueError(f"{readme}: found the begin marker but not {END_MARKER!r}")
    return current[:start] + block + current[end + len(END_MARKER):]


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync a server README with its CITATION.cff")
    parser.add_argument("server_dir", type=Path, help="Server directory, e.g. servers/effis")
    parser.add_argument("--check", action="store_true", help="Exit 1 if README.md is out of date")
    args = parser.parse_args()

    cff_path = args.server_dir / CFF_FILENAME
    if not cff_path.exists():
        sys.exit(f"{cff_path} not found")

    errors = validate_cff(load_cff(cff_path))
    if errors:
        print(f"{cff_path} is not valid CFF 1.2.0:")
        for message in errors:
            print(f"  - {message}")
        sys.exit(1)

    readme = args.server_dir / README_FILENAME
    expected = render_readme(args.server_dir)
    current = readme.read_text(encoding="utf-8") if readme.exists() else None

    if args.check:
        if current != expected:
            sys.exit(f"{readme} is out of date. Run: python scripts/citation.py {args.server_dir}")
        print(f"{readme} is up to date")
        return

    if current == expected:
        print(f"{readme} already up to date")
        return
    readme.write_text(expected, encoding="utf-8")
    print(f"Wrote {readme}")


if __name__ == "__main__":
    main()
