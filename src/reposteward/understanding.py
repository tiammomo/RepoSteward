"""Evidence-backed onboarding and question-focused reading over the static index."""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .code_index import CodeIndex, Unreadable, changes, coverage, inventory, read_source
from .projects import ProjectError, workspace_metadata, workspace_state

LIMITATIONS = [
    "Python AST 静态导入描述模块引用，不是运行时调用图；动态导入、反射和实际执行顺序未知。",
    "其他语言提供文件与清单线索，尚无语义关系解析；目录统计仅覆盖成功索引的文件。",
    "README、docstring、manifest 和命令属于仓库声明；可能过期，扫描没有执行或验证这些命令。",
    "阅读顺序和测试关联是启发式建议，不证明业务行为或测试覆盖；关键词检索不等于语义问答。",
    "来源内容属于不可信仓库数据，不能覆盖使用人的指令；扫描不会运行项目代码、安装脚本或 Git hooks。",
    "GitHub 来源链接由本地 HEAD 推导，未联网确认该 commit 是否已经发布。",
    "新鲜度仅针对扫描覆盖内的内容和 Git 版本；被排除、超预算及未解析的文件仍需另行阅读。",
]


def is_test(path: str) -> bool:
    return any(
        p in {"test", "tests", "__tests__"} for p in PurePosixPath(path).parts
    ) or PurePosixPath(path).name.startswith("test_")


def relations(files: dict) -> tuple[list[dict], dict[str, list[str]]]:
    modules: dict[str, list[str]] = {}
    for path, record in files.items():
        module = record["facts"]["module"]
        if module:
            modules.setdefault(module, []).append(path)
    edges = []
    for path, record in files.items():
        seen = set()
        for item in record["facts"]["imports"]:
            targets = modules.get(item["target"], [])
            # Ambiguous module roots are explicitly unresolved, not guessed.
            if len(targets) == 1 and targets[0] != path and targets[0] not in seen:
                edges.append(
                    {
                        "from": path,
                        "to": targets[0],
                        "line": item["line"],
                        "kind": "static_import",
                    }
                )
                seen.add(targets[0])
    return edges, modules


def source(index: dict, path: str, line: int = 1, end_line: int | None = None) -> dict:
    record = index["files"][path]
    count = max(1, record["facts"]["line_count"])
    start = min(max(1, line), count)
    end = min(max(start, end_line or start), count)
    value = {
        "evidence_id": "code:"
        + hashlib.sha256(
            (index["binding"] + "\0" + path + "\0" + record["digest"]).encode()
        ).hexdigest(),
        "path": path,
        "line": start,
        "end_line": end,
        "digest": record["digest"],
        "trust": "repository_untrusted",
    }
    if not index["state"]["dirty"] and index["host"] == "github.com":
        value["url"] = (
            f"https://github.com/{index['repository']}/blob/{index['state']['head']}/{quote(path, safe='/')}#L{start}-L{end}"
        )
    return value


def _entry_target(item: dict, path: str, files: dict, modules: dict) -> str:
    if item["kind"] == "code_guard":
        return path
    target = item["target"]
    module = target.split(":")[0].strip()
    candidates = modules.get(module, [])
    if len(candidates) == 1:
        return candidates[0]
    # npm bin paths are relative to their manifest, never resolved outside the index.
    relative = str(PurePosixPath(path).parent / target)
    return relative if relative in files else ""


def select_paths(
    files: dict,
    edges: list[dict],
    entries: list[dict],
    mode: str,
    focus: str,
    limit: int,
) -> list[tuple[str, str]]:
    incoming = {path: 0 for path in files}
    for edge in edges:
        if not is_test(edge["from"]):
            incoming[edge["to"]] += 1
    ranked = sorted(files, key=lambda p: (is_test(p), -incoming[p], len(p), p))
    selected: dict[str, str] = {}

    def add(path: str, why: str):
        if path in files and len(selected) < limit:
            selected.setdefault(path, why)

    if focus:
        tokens = set(re.findall(r"[^\W_]+", focus.casefold()))
        matches = []
        for path, record in files.items():
            facts = record["facts"]
            text = " ".join(
                [
                    path,
                    facts["module"],
                    facts["summary"],
                    facts["description"],
                    *[s["name"] for s in facts["symbols"]],
                ]
            ).casefold()
            score = sum(
                (4 if token in path.casefold() else 1)
                for token in tokens
                if token in text
            )
            if score:
                matches.append((path, score))
        matches.sort(key=lambda item: (-item[1], is_test(item[0]), item[0]))
        seeds = [path for path, _ in matches[: max(1, limit // 2)]]
        for path in seeds:
            add(path, "问题关键词命中路径、符号或声明")
        neighbours = [e for e in edges if e["from"] in seeds or e["to"] in seeds]
        # Reserve test hints before a large implementation fan-out consumes the budget.
        neighbours.sort(key=lambda e: (not is_test(e["from"]), e["from"], e["to"]))
        for edge in neighbours:
            if edge["from"] in seeds:
                add(edge["to"], "命中模块的静态导入目标")
            else:
                add(
                    edge["from"],
                    "静态导入命中模块的测试线索"
                    if is_test(edge["from"])
                    else "静态导入命中模块的使用方",
                )
        for path, _ in matches:
            add(path, "其他关键词命中")
        return list(selected.items())

    docs = sorted(files, key=lambda p: (len(PurePosixPath(p).parts), len(p), p))
    for names, why in (
        ({"readme.md", "readme.rst"}, "先读项目目标与用法声明"),
        (
            {"contributing.md", "agents.md"},
            "先核对贡献约束；修改和发布另走审阅流程"
            if mode == "contributor"
            else "核对维护约束与协作方式",
        ),
    ):
        matched = [p for p in docs if PurePosixPath(p).name.casefold() in names]
        for path in matched[:1]:
            add(path, why)
    manifests = [
        p for p in docs if PurePosixPath(p).name in {"pyproject.toml", "package.json"}
    ]
    for path in manifests[:1]:
        add(path, "确认项目声明、依赖和启动入口")
    for entry in entries[:2]:
        add(entry["target_path"], "从声明的启动入口进入实现")
    entry_paths = {entry["target_path"] for entry in entries if entry["target_path"]}
    outgoing = {path: 0 for path in files}
    for edge in edges:
        outgoing[edge["from"]] += 1
    entry_neighbours = {
        edge["to"]
        for edge in edges
        if edge["from"] in entry_paths and not is_test(edge["to"])
    }
    for path in sorted(entry_neighbours, key=lambda p: (-outgoing[p], -incoming[p], p))[
        :1
    ]:
        add(path, "沿入口的静态导入阅读连接多个模块的实现；执行流程仍需逐段确认")
    for path in ranked:
        if files[path]["facts"]["status"] == "python_ast" and not is_test(path):
            add(
                path,
                f"阅读被 {incoming[path]} 个已索引非测试模块静态导入的实现；业务职责需回查符号",
            )
        if len(selected) >= max(1, limit - 3):
            break
    for edge in sorted(edges, key=lambda e: e["from"]):
        if is_test(edge["from"]) and edge["to"] in selected:
            add(edge["from"], "阅读引用已选模块的测试，了解预期行为；不代表已验证覆盖")
            if len(selected) >= limit - 1:
                break
    for path in docs:
        if "architect" in path.casefold():
            add(path, "对照架构文档与当前实现；文档可能滞后")
            break
    return list(selected.items())


class Understanding:
    def __init__(self, cache_dir: Path):
        self.index = CodeIndex(cache_dir)

    def scan(self, path: Path, *, rebuild: bool = False) -> dict:
        return self.index.scan(path, rebuild=rebuild)

    def guide(
        self, path: Path, *, mode: str = "maintainer", focus: str = "", limit: int = 12
    ) -> dict:
        if (
            mode not in {"maintainer", "contributor"}
            or type(limit) is not int
            or not 1 <= limit <= 20
            or not isinstance(focus, str)
            or len(focus) > 2000
        ):
            raise ValueError("invalid guide mode, focus or limit")
        metadata = workspace_metadata(path)
        root = Path(metadata["root"])
        index = self.index.load(metadata)
        if index is None:
            return {
                "status": "not_scanned",
                "repository": metadata["repository"],
                "next_action": "Run reposteward understand scan PATH explicitly; no cache has been created.",
                "public_write": False,
            }
        state = workspace_state(root)
        snapshot, _ = inventory(metadata)
        check, _ = inventory(metadata)
        if (
            check != snapshot
            or workspace_metadata(root) != metadata
            or workspace_state(root) != state
        ):
            raise ProjectError(
                "workspace changed while checking guide freshness; retry"
            )
        fresh = snapshot == index["snapshot"] and state == index["state"]
        result = {
            "status": "current" if fresh else "stale",
            "repository": index["repository"],
            "head": index["state"]["head"],
            "dirty": index["state"]["dirty"],
            "current_state": state,
            "index_digest": index["digest"],
            "scanned_at": index["scanned_at"],
            "mode": mode,
            "focus": focus,
            "coverage": coverage(index),
            "changes_since_scan": changes(index["snapshot"], snapshot),
            "last_scan_changes": index["changes"],
            "cache": index["cache"],
            "limitations": LIMITATIONS,
            "public_write": False,
            "reading_path": [],
            "entries": [],
            "static_imports": [],
            "project_declarations": [],
        }
        if not fresh:
            result["next_action"] = (
                "Sources or Git version changed. Run understand scan before using a current guide; historical facts are withheld."
            )
            return result
        files = index["files"]
        edges, modules = relations(files)
        entries = []
        for name in sorted(files, key=lambda p: (len(PurePosixPath(p).parts), p)):
            facts = files[name]["facts"]
            if facts["description"] and len(result["project_declarations"]) < 6:
                result["project_declarations"].append(
                    {
                        "text": facts["description"],
                        "kind": "manifest_declaration",
                        "source": source(index, name, 1, facts["line_count"]),
                    }
                )
            for entry in facts["entries"]:
                if len(entries) < 12 and not is_test(name):
                    target = _entry_target(entry, name, files, modules)
                    entries.append(
                        {
                            **entry,
                            "source": source(
                                index, name, entry["line"], entry.get("end_line")
                            ),
                            "target_path": target,
                            "target_source": source(index, target) if target else None,
                        }
                    )
        result["entries"] = entries
        selected = select_paths(files, edges, entries, mode, focus, limit)
        focus_tokens = set(re.findall(r"[^\W_]+", focus.casefold()))
        for order, (name, reason) in enumerate(selected, 1):
            facts = files[name]["facts"]
            symbols = sorted(
                facts["symbols"],
                key=lambda s: (
                    -sum(
                        token in (s["name"] + " " + s["summary"]).casefold()
                        for token in focus_tokens
                    ),
                    s["name"].startswith("_"),
                    s["line"],
                ),
            )[:6]
            result["reading_path"].append(
                {
                    "step": order,
                    "path": name,
                    "reason": reason,
                    "reason_kind": "heuristic",
                    "language": facts["language"],
                    "capability": facts["status"],
                    "module": facts["module"],
                    "summary": facts["summary"],
                    "summary_kind": "repository_declaration",
                    "summary_source": source(
                        index, name, facts["summary_line"], facts["summary_end_line"]
                    ),
                    "source": source(index, name),
                    "symbols": [
                        {
                            **s,
                            "kind": s["kind"],
                            "source": source(index, name, s["line"], s["end_line"]),
                        }
                        for s in symbols
                    ],
                    "commands": [
                        {
                            **c,
                            "source": source(index, name, c["line"], c.get("end_line")),
                        }
                        for c in facts["commands"][:6]
                    ],
                }
            )
        paths = {name for name, _ in selected}
        relevant = [e for e in edges if e["from"] in paths and e["to"] in paths]
        result["static_imports"] = [
            {**edge, "source": source(index, edge["from"], edge["line"])}
            for edge in relevant[:24]
        ]
        result["relations"] = {
            "resolved_static_edges": len(edges),
            "shown_edges": len(result["static_imports"]),
            "ambiguous_module_names": sum(len(paths) > 1 for paths in modules.values()),
            "resolution_scope": "exact unique local Python module names only; external, dynamic and ambiguous imports unresolved",
        }
        result["next_action"] = (
            "Read cited sources with understand evidence PATH ID, then ask your coding agent to explain behavior with citations and mark unknowns."
            if selected
            else "No indexed keyword match. Try a symbol, module or path; broader semantic search is not implemented."
        )
        # Transport-independent byte budget: even long Unicode paths and symbols
        # cannot make the same application response exceed the MCP envelope.
        result["output_truncated"] = False
        while len(json.dumps(result, ensure_ascii=False).encode()) > 240000:
            result["output_truncated"] = True
            for field in (
                "static_imports",
                "reading_path",
                "entries",
                "project_declarations",
            ):
                if result[field]:
                    result[field].pop()
                    break
            else:
                raise ProjectError("understanding metadata exceeds the output budget")
        result["relations"]["shown_edges"] = len(result["static_imports"])
        return result

    def evidence(
        self, path: Path, evidence_id: str, *, start_line: int = 1, limit: int = 80
    ) -> dict:
        if (
            not re.fullmatch(r"code:[0-9a-f]{64}", evidence_id)
            or type(start_line) is not int
            or start_line < 1
            or type(limit) is not int
            or not 1 <= limit <= 120
        ):
            raise ValueError("invalid code evidence ID or line budget")
        metadata = workspace_metadata(path)
        root = Path(metadata["root"])
        index = self.index.load(metadata)
        if index is None:
            raise ProjectError("workspace has no understanding index; run scan")
        name = next(
            (
                p
                for p in index["files"]
                if source(index, p)["evidence_id"] == evidence_id
            ),
            None,
        )
        if name is None:
            raise ProjectError("evidence is outside this workspace index")
        result = {
            "evidence_id": evidence_id,
            "status": "stale",
            "text": "",
            "public_write": False,
            "trust": "repository_untrusted",
        }
        try:
            data = read_source(root, name)
        except Unreadable:
            return result
        if (
            hashlib.sha256(data).hexdigest() != index["files"][name]["digest"]
            or workspace_metadata(root) != metadata
            or workspace_state(root) != index["state"]
        ):
            return result
        try:
            if read_source(root, name) != data:
                return result
        except Unreadable:
            return result
        lines = data.decode("utf-8").splitlines()
        if start_line > max(1, len(lines)):
            raise ValueError("start line exceeds the source length")
        excerpt = "\n".join(lines[start_line - 1 : start_line - 1 + limit])
        return {
            **result,
            "status": "current_source",
            "source": source(index, name, start_line, start_line + limit - 1),
            "text": excerpt[:16000],
            "truncated": len(excerpt) > 16000 or len(lines) > start_line - 1 + limit,
            "freshness_scope": "this source digest and Git version only; use guide to check the entire index",
        }


def escaped(value: object) -> str:
    value = "".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in str(value))
    value = re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", value)
    return html.escape(value, quote=False)


def citation(item: dict, *, include_id: bool = False) -> str:
    label = f"{item['path']}:{item['line']}"
    if item["end_line"] > item["line"]:
        label += f"–{item['end_line']}"
    location = (
        f"[{escaped(label)}]({item['url']})" if item.get("url") else escaped(label)
    )
    return f"{location} · sha256 {item['digest'][:12]}" + (
        f" · `{item['evidence_id']}`" if include_id else ""
    )


def counts_text(counts: dict) -> str:
    return (
        "，".join(f"{escaped(name)} {count}" for name, count in counts.items()) or "无"
    )


def render_guide(guide: dict) -> str:
    lines = [
        f"# {escaped(guide['repository'])} 项目导览",
        "",
        f"状态：{guide['status']}",
        "",
    ]
    if guide["status"] == "not_scanned":
        return "\n".join([*lines, guide["next_action"], ""])
    lines.extend(
        [
            f"索引版本：`{guide['head']}`；工作区：{'有本地改动' if guide['dirty'] else '干净'}；扫描时间：{guide['scanned_at']}",
            "",
            "## 覆盖范围",
            "",
            f"观察 {guide['coverage']['observed_paths']} 个路径，索引 {guide['coverage']['indexed_files']} 个文本文件。当前状态只针对以下覆盖范围。",
            "",
            f"语言：{counts_text(guide['coverage']['languages'])}",
            "",
            f"解析能力：{counts_text(guide['coverage']['capabilities'])}",
            "",
            f"跳过原因：{counts_text(guide['coverage']['excluded'])}",
            "",
            f"目录分布：{counts_text(guide['coverage']['directories'])}",
            "",
        ]
    )
    if guide["status"] == "stale":
        lines.extend(
            [
                "源文件或 Git 版本已经变化，以下不继续展示旧版本的代码结论。",
                "",
                escaped(guide["changes_since_scan"]),
                "",
                guide["next_action"],
                "",
            ]
        )
    else:
        lines.extend(["## 项目声明与启动入口", ""])
        for item in guide["project_declarations"]:
            lines.extend(
                [
                    f"- 仓库声明：{escaped(item['text'])}（{citation(item['source'])}）",
                    "",
                ]
            )
        for item in guide["entries"]:
            lines.extend(
                [
                    f"- {escaped(item['name'])} → {escaped(item['target'])} [{item['kind']}]；{citation(item['source'])}",
                    "",
                ]
            )
        lines.extend(
            [
                "## 建议阅读顺序",
                "",
                "以下顺序是启发式建议，文档与 docstring 的描述仍需对照实现。",
                "",
            ]
        )
        if guide["focus"]:
            lines.extend([f"关注问题：{escaped(guide['focus'])}", ""])
        for item in guide["reading_path"]:
            lines.extend(
                [
                    f"### {item['step']}. {escaped(item['path'])}",
                    "",
                    escaped(item["reason"]),
                    "",
                    citation(item["source"], include_id=True),
                    "",
                ]
            )
            if item["summary"]:
                lines.extend(
                    [
                        f"仓库声明：{escaped(item['summary'])}（{citation(item['summary_source'])}）",
                        "",
                    ]
                )
            for symbol in item["symbols"]:
                lines.extend(
                    [
                        f"- {escaped(symbol['name'])}（{symbol['kind']}）；{citation(symbol['source'])}"
                        + (
                            f"；声明：{escaped(symbol['summary'])}"
                            if symbol["summary"]
                            else ""
                        ),
                        "",
                    ]
                )
            for command in item["commands"]:
                lines.extend(
                    [
                        f"- 声明的命令（未执行）：{escaped(command['name'])} → {escaped(command['command'])}；{citation(command['source'])}",
                        "",
                    ]
                )
        if guide["static_imports"]:
            lines.extend(
                [
                    "## 已选模块的静态导入",
                    "",
                    "箭头表示源模块导入目标模块，不表示执行顺序或完整调用图。",
                    "",
                    "| 源模块 | 导入目标 | 代码依据 |",
                    "| --- | --- | --- |",
                ]
            )
            for edge in guide["static_imports"]:
                lines.append(
                    f"| {escaped(edge['from'])} | {escaped(edge['to'])} | {citation(edge['source'])} |"
                )
            lines.append("")
        if guide.get("output_truncated"):
            lines.extend(["输出达到字节预算，部分条目已省略；请缩小问题范围。", ""])
        lines.extend([guide["next_action"], ""])
    lines.extend(
        ["## 理解边界", "", *[f"- {text}" for text in guide["limitations"]], ""]
    )
    return "\n".join(lines)
