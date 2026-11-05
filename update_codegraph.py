#!/usr/bin/env python3
"""Regenerate the auto-generated parts of CODEGRAPH.md from simple_rss.py.

Usage:
    python3 update_codegraph.py            # rewrite CODEGRAPH.md in place
    python3 update_codegraph.py --check    # exit 1 if CODEGRAPH.md is stale (no write)
    python3 update_codegraph.py --src other.py --doc OTHER.md

How it works:
    CODEGRAPH.md contains two blocks fenced by HTML comments:

        <!-- BEGIN AUTO:symbols ... -->   ...   <!-- END AUTO:symbols -->
        <!-- BEGIN AUTO:settings ... -->  ...   <!-- END AUTO:settings -->

    Everything between a pair of fences is rebuilt from the source code:
    symbol names, signatures, line ranges and the QSettings keys in use.

    The *Description* column is the only hand-written part. Descriptions are
    kept across runs, matched by symbol name (or settings key):
      - new symbol      -> its docstring's first line, else "_TODO: describe_"
      - removed symbol  -> reported, and dropped from the table
    Text outside the fences is never touched.
    (Avoid the "|" character inside descriptions; it is the table separator.)

Needs only the Python standard library (3.10+).
"""
import argparse
import ast
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TODO = "_TODO: describe_"
CONST_RE = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
MODULE_TITLE = "Module level"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def fence(name: str) -> re.Pattern:
    return re.compile(
        rf"(<!-- BEGIN AUTO:{name}[^\n]*-->)(.*?)(<!-- END AUTO:{name} -->)", re.S
    )


def arg_names(fn) -> str:
    a = fn.args
    names = [x.arg for x in a.posonlyargs + a.args]
    if a.vararg:
        names.append("*" + a.vararg.arg)
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("**" + a.kwarg.arg)
    return ", ".join(n for n in names if n not in ("self", "cls"))


def first_doc_line(node) -> str:
    doc = ast.get_docstring(node)
    return doc.strip().splitlines()[0].strip() if doc else ""


def span(node) -> str:
    if node.end_lineno == node.lineno:
        return f"L{node.lineno}"
    return f"L{node.lineno}-{node.end_lineno}"


def split_row(line: str):
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip() for c in s[1:-1].split("|")]


FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)


# --------------------------------------------------------------------------
# Symbols block
# --------------------------------------------------------------------------
def collect_sections(tree: ast.Module):
    """-> [(section title, [(key, cell, lines, default description)])]"""
    module_rows = []  # (lineno, key, cell, lines, default)
    class_sections = []
    for node in tree.body:
        if isinstance(node, FUNC_TYPES):
            module_rows.append((node.lineno, node.name, f"{node.name}({arg_names(node)})",
                                span(node), first_doc_line(node)))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and CONST_RE.match(t.id):
                    module_rows.append((node.lineno, t.id, t.id, span(node), ""))
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            head = f"class {node.name}({bases})" if bases else f"class {node.name}"
            rows = [(node.name, head, span(node), first_doc_line(node))]
            for m in node.body:
                if isinstance(m, FUNC_TYPES):
                    rows.append((f"{node.name}.{m.name}", f".{m.name}({arg_names(m)})",
                                 span(m), first_doc_line(m)))
            class_sections.append((node.name, rows))
    module_rows.sort(key=lambda r: r[0])
    return [(MODULE_TITLE, [r[1:] for r in module_rows])] + class_sections


def symbol_key(cell: str, cls: str | None) -> str:
    if cell.startswith("class "):
        return re.match(r"class\s+(\w+)", cell).group(1)
    if cell.startswith("."):
        method = re.match(r"\.(\w+)", cell).group(1)
        return f"{cls}.{method}"
    return re.match(r"\w+", cell).group(0)


def old_symbol_descriptions(block: str) -> dict:
    desc, cls = {}, None
    for line in block.splitlines():
        if line.startswith("### "):
            title = line[4:].strip()
            cls = None if title == MODULE_TITLE else title
            continue
        cells = split_row(line)
        if not cells or len(cells) < 3:
            continue
        m = re.fullmatch(r"`([^`]+)`", cells[0])
        if not m:
            continue
        text = "|".join(cells[2:]).strip()
        if text and text != TODO:
            desc[symbol_key(m.group(1), cls)] = text
    return desc


def render_symbols(tree, src_name: str, nlines: int, old: dict):
    sections = collect_sections(tree)
    out = [f"_Source: `{src_name}`, {nlines} lines._", ""]
    todo, keys = [], set()
    for title, rows in sections:
        out += [f"### {title}", "", "| Symbol | Lines | Description |", "|---|---|---|"]
        for key, cell, lines, default in rows:
            keys.add(key)
            text = old.get(key) or default or TODO
            if text == TODO:
                todo.append(key)
            out.append(f"| `{cell}` | {lines} | {text} |")
        out.append("")
    removed = {k: v for k, v in old.items() if k not in keys}
    return "\n".join(out).rstrip(), todo, removed


# --------------------------------------------------------------------------
# Settings block (QSettings keys used via .value("key") / .setValue("key", ...))
# --------------------------------------------------------------------------
class SettingsScanner(ast.NodeVisitor):
    def __init__(self):
        self.stack: list[str] = []
        self.uses: dict[str, dict[str, set]] = {}  # key -> {"read": set, "write": set}

    def _scoped(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _scoped

    def visit_Call(self, node):
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr in ("value", "setValue")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            scope = ".".join(self.stack) or "<module>"
            mode = "read" if f.attr == "value" else "write"
            self.uses.setdefault(node.args[0].value, {"read": set(), "write": set()})[mode].add(scope)
        self.generic_visit(node)


def old_settings_descriptions(block: str) -> dict:
    desc = {}
    for line in block.splitlines():
        cells = split_row(line)
        if not cells or len(cells) < 4:
            continue
        m = re.fullmatch(r"`([^`]+)`", cells[0])
        text = "|".join(cells[3:]).strip()
        if m and text and text != TODO:
            desc[m.group(1)] = text
    return desc


def render_settings(tree, old: dict):
    scanner = SettingsScanner()
    scanner.visit(tree)

    def scopes(s):
        return ", ".join(f"`{x}`" for x in sorted(s)) or "-"

    out = ["| Key | Read in | Written in | Description |", "|---|---|---|---|"]
    todo = []
    for key in sorted(scanner.uses):
        u = scanner.uses[key]
        text = old.get(key) or TODO
        if text == TODO:
            todo.append(key)
        out.append(f"| `{key}` | {scopes(u['read'])} | {scopes(u['write'])} | {text} |")
    removed = {k: v for k, v in old.items() if k not in scanner.uses}
    return "\n".join(out), todo, removed


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def regenerate(doc_text: str, tree, src_name: str, nlines: int):
    """-> (new_text, todo keys, removed {key: old description})"""
    all_todo, all_removed = [], {}
    new_text = doc_text
    jobs = (
        ("symbols", old_symbol_descriptions,
         lambda old: render_symbols(tree, src_name, nlines, old)),
        ("settings", old_settings_descriptions,
         lambda old: render_settings(tree, old)),
    )
    for name, parse_old, render in jobs:
        pattern = fence(name)
        m = pattern.search(new_text)
        if not m:
            sys.exit(
                f"error: markers for '{name}' not found in the doc.\n"
                f"Add a pair like this where the table should go:\n"
                f"  <!-- BEGIN AUTO:{name} (generated by update_codegraph.py) -->\n"
                f"  <!-- END AUTO:{name} -->"
            )
        body, todo, removed = render(parse_old(m.group(2)))
        new_text = pattern.sub(
            lambda mm, b=body: f"{mm.group(1)}\n{b}\n{mm.group(3)}", new_text, count=1
        )
        all_todo += [f"{name}: {k}" for k in todo]
        all_removed.update({f"{name}: {k}": v for k, v in removed.items()})
    return new_text, all_todo, all_removed


def main():
    ap = argparse.ArgumentParser(description="Regenerate the auto sections of CODEGRAPH.md")
    ap.add_argument("--src", default=str(HERE / "simple_rss.py"), help="Python source file")
    ap.add_argument("--doc", default=str(HERE / "CODEGRAPH.md"), help="Markdown file to update")
    ap.add_argument("--check", action="store_true",
                    help="don't write; exit 1 if the doc is out of date")
    args = ap.parse_args()

    src, doc = Path(args.src), Path(args.doc)
    source = src.read_text(encoding="utf-8")
    old_text = doc.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(src))

    new_text, todo, removed = regenerate(old_text, tree, src.name, len(source.splitlines()))
    changed = new_text != old_text

    if removed:
        print("Removed symbols/keys (old descriptions, copy them if the item was renamed):")
        for k, v in removed.items():
            print(f"  - {k}  ->  {v}")
    if todo:
        print(f"{len(todo)} item(s) need a description (search the doc for '{TODO}'):")
        for k in todo:
            print(f"  - {k}")

    if args.check:
        if changed:
            print(f"{doc.name} is out of date. Run: python3 {Path(__file__).name}")
            sys.exit(1)
        print(f"{doc.name} is up to date.")
        return
    if changed:
        doc.write_text(new_text, encoding="utf-8")
        print(f"Updated {doc.name}.")
    else:
        print(f"{doc.name} is already up to date.")


if __name__ == "__main__":
    main()
