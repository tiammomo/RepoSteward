"""Bounded static observations, never imports or executes the inspected project."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import PurePosixPath

PARSER_VERSION = 1
LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".vue": "vue",
    ".svelte": "svelte",
    ".md": "documentation",
    ".rst": "documentation",
    ".txt": "text",
    ".json": "configuration",
    ".toml": "configuration",
    ".yaml": "configuration",
    ".yml": "configuration",
    ".sh": "shell",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
}


def short(value: object, limit: int = 500) -> str:
    return " ".join(str(value).split())[:limit]


def module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[:1] == ["src"]:
        parts.pop(0)
    if parts[-1:] == ["__init__"]:
        parts.pop()
    return ".".join(parts) if all(part.isidentifier() for part in parts) else ""


def parse_code(path: str, text: str) -> dict:
    name = PurePosixPath(path).name
    language = LANGUAGES.get(PurePosixPath(path).suffix.casefold(), "text")
    lines = text.splitlines()
    result = {
        "language": language,
        "line_count": len(lines),
        "module": "",
        "summary": "",
        "summary_line": 1,
        "summary_end_line": max(1, len(lines)),
        "symbols": [],
        "imports": [],
        "entries": [],
        "commands": [],
        "description": "",
        "status": "inventory_only",
        "omitted_nodes": 0,
    }
    if language == "python":
        result["module"] = module_name(path)
        try:
            tree = ast.parse(text, filename=path)
            result["summary"] = short(ast.get_docstring(tree) or "")
            if result["summary"]:
                result["summary_line"] = tree.body[0].lineno
                result["summary_end_line"] = tree.body[0].end_lineno
            result["status"] = "python_ast"
            package = result["module"].split(".")
            if name != "__init__.py":
                package = package[:-1]
            for count, node in enumerate(ast.walk(tree)):
                if count >= 50_000:
                    result["omitted_nodes"] += 1
                    break
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    if len(result["symbols"]) < 128:
                        result["symbols"].append(
                            {
                                "name": short(node.name, 200),
                                "kind": type(node).__name__,
                                "line": node.lineno,
                                "end_line": node.end_lineno,
                                "summary": short(ast.get_docstring(node) or "", 250),
                            }
                        )
                    else:
                        result["omitted_nodes"] += 1
                if isinstance(node, ast.Import):
                    imports = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    prefix = (
                        package[: len(package) - node.level + 1]
                        if node.level and node.level <= len(package)
                        else []
                    )
                    base = ".".join([*prefix, *([node.module] if node.module else [])])
                    imports = ([base] if node.module else []) + [
                        ".".join(filter(None, (base, alias.name)))
                        for alias in node.names
                        if alias.name != "*"
                    ]
                    if node.level > len(package):
                        imports = []
                else:
                    imports = []
                for imported in imports:
                    if len(result["imports"]) < 256:
                        result["imports"].append(
                            {"target": imported, "line": node.lineno}
                        )
                    else:
                        result["omitted_nodes"] += 1
                if (
                    isinstance(node, ast.If)
                    and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == "__name__"
                    and len(node.test.ops) == 1
                    and isinstance(node.test.ops[0], ast.Eq)
                    and len(node.test.comparators) == 1
                    and isinstance(node.test.comparators[0], ast.Constant)
                    and node.test.comparators[0].value == "__main__"
                ):
                    if len(result["entries"]) < 40:
                        result["entries"].append(
                            {
                                "name": "__main__",
                                "target": result["module"] or path,
                                "line": node.lineno,
                                "kind": "code_guard",
                            }
                        )
                    else:
                        result["omitted_nodes"] += 1
        except (SyntaxError, ValueError, RecursionError):
            result.update(status="parse_failed", symbols=[], imports=[], entries=[])
    elif name in {"pyproject.toml", "package.json"}:
        try:
            # stdlib manifest parsers do not retain positions. Cite the complete
            # declaration document instead of guessing a line from duplicate text.
            data = tomllib.loads(text) if name.endswith("toml") else json.loads(text)
            project = data.get("project", {}) if name.endswith("toml") else data
            result["description"] = short(project.get("description", ""))
            result["summary"] = short(project.get("name", ""))
            entries = (
                project.get("scripts", {})
                if name.endswith("toml")
                else data.get("bin", {})
            )
            if isinstance(entries, str):
                entries = {project.get("name", "bin"): entries}
            if isinstance(entries, dict):
                for label, target in list(entries.items())[:40]:
                    if isinstance(target, str):
                        result["entries"].append(
                            {
                                "name": short(label, 100),
                                "target": short(target, 500),
                                "line": 1,
                                "end_line": max(1, len(lines)),
                                "kind": "manifest_declaration",
                            }
                        )
            if name == "package.json" and isinstance(data.get("scripts"), dict):
                for label, command in list(data["scripts"].items())[:40]:
                    if isinstance(command, str):
                        result["commands"].append(
                            {
                                "name": short(label, 100),
                                "command": short(command, 500),
                                "line": 1,
                                "end_line": max(1, len(lines)),
                                "execution": "not_run",
                            }
                        )
            result["status"] = "manifest"
        except (ValueError, TypeError, AttributeError, RecursionError):
            result.update(status="parse_failed", entries=[], commands=[])
    elif language == "documentation":
        paragraphs = re.split(r"\n\s*\n", text)
        paragraph = next(
            (
                p
                for p in paragraphs[:20]
                if p.strip() and not p.lstrip().startswith(("#", "<", "[", "```", "!"))
            ),
            "",
        )
        result["summary"] = short(paragraph)
        if paragraph:
            result["summary_line"] = text[: text.index(paragraph)].count("\n") + 1
            result["summary_end_line"] = result["summary_line"] + paragraph.count("\n")
        result["status"] = "document_declaration"
    return result
