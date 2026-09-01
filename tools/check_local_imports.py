"""
Static check: does every `from <local.module> import name` resolve to something
that module actually defines?

This is the gap compileall leaves. A stale `from x import y` parses perfectly and
only fails when the import is executed - which for a Telegram handler package means
at bot startup. No third-party packages needed, so it runs on a machine with no
requirements installed.
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(".").resolve()
LOCAL = {"telegram_bot", "utils", "db", "cli", "api", "tools"}
SKIP_PARTS = {".git", ".claude", "__pycache__", "versions"}


def module_path(dotted):
    base = ROOT.joinpath(*dotted.split("."))
    for cand in (base.with_suffix(".py"), base / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def defined_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.If, ast.Try)):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                elif isinstance(sub, ast.ImportFrom):
                    for a in sub.names:
                        names.add(a.asname or a.name)
                elif isinstance(sub, ast.Import):
                    for a in sub.names:
                        names.add(a.asname or a.name.split(".")[0])
    return names


problems = []
checked = 0
for path in sorted(ROOT.rglob("*.py")):
    rel = path.relative_to(ROOT)
    if any(p in SKIP_PARTS for p in rel.parts):
        continue
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        problems.append(f"{rel}: SyntaxError: {e}")
        continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
            continue
        if node.module.split(".")[0] not in LOCAL:
            continue
        target = module_path(node.module)
        if target is None:
            problems.append(f"{rel}:{node.lineno}: module '{node.module}' does not exist")
            continue
        available = defined_names(target)
        for alias in node.names:
            if alias.name == "*":
                continue
            checked += 1
            if alias.name not in available:
                sub = module_path(f"{node.module}.{alias.name}")
                if sub is None:
                    problems.append(
                        f"{rel}:{node.lineno}: '{node.module}' has no '{alias.name}'"
                    )

print(f"checked {checked} imported names from local modules")
for p in problems:
    print("  BROKEN " + p)
print("RESULT:", "FAIL" if problems else "clean")
sys.exit(1 if problems else 0)
