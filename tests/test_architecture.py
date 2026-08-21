"""Executable architecture rules.

A diagram in a README documents intent; this file enforces it. Every rule here
failed at least once in this project's history, which is why it is a test rather
than a paragraph.
"""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "dbrt"

# Concentric circles, innermost first. A module may import its own ring or any
# ring inside it, never one further out.
RING = {
    "domain": 0,          # enterprise business rules — pure
    "__init__": 0,
    "config": 1,          # settings, no policy
    "gtfs_rt": 1,         # feed decoding
    "static_gtfs": 1,     # timetable parsing
    "analytics": 2,       # application rules
    "ml": 2,
    "collector": 2,
    "storage": 3,         # adapters and drivers
    "feed_client": 3,
    "api": 3,
    "__main__": 4,        # composition root
}


def internal_imports(module: str) -> set[str]:
    tree = ast.parse((PACKAGE / f"{module}.py").read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                found.update(alias.name for alias in node.names)
    return {name for name in found if name in RING}


def external_imports(module: str) -> set[str]:
    tree = ast.parse((PACKAGE / f"{module}.py").read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_every_module_is_assigned_a_ring():
    """A new module must be placed deliberately, not drift in unclassified."""
    on_disk = {p.stem for p in PACKAGE.glob("*.py")}
    assert on_disk == set(RING), f"unclassified: {on_disk ^ set(RING)}"


def test_dependencies_only_point_inward():
    violations = [
        f"{module}(ring {RING[module]}) -> {target}(ring {RING[target]})"
        for module in RING
        for target in internal_imports(module)
        if RING[target] > RING[module]
    ]
    assert not violations, "outward dependency: " + "; ".join(violations)


def test_the_domain_depends_on_nothing_at_all():
    assert internal_imports("domain") == set()
    assert external_imports("domain") <= {"__future__", "dataclasses"}


def test_no_driver_or_framework_reaches_past_the_adapter_ring():
    """psycopg2, FastAPI, requests and sklearn are details. They may only appear
    in ring 3 and beyond."""
    details = {"psycopg2", "fastapi", "requests", "starlette", "uvicorn"}
    leaks = [
        f"{module} imports {sorted(external_imports(module) & details)}"
        for module in RING
        if RING[module] < 3 and external_imports(module) & details
    ]
    assert not leaks, "; ".join(leaks)


def test_the_component_graph_has_no_cycles():
    """ADP: a cycle means no module in it can be released independently."""
    path: list[str] = []
    seen: set[str] = set()
    cycles: list[list[str]] = []

    def walk(module: str) -> None:
        if module in path:
            cycles.append(path[path.index(module):] + [module])
            return
        if module in seen:
            return
        seen.add(module)
        path.append(module)
        for target in internal_imports(module):
            walk(target)
        path.pop()

    for module in RING:
        walk(module)
    assert not cycles, f"dependency cycle: {cycles}"


def _query_literals(module: str) -> list[str]:
    """String constants in a module, excluding docstrings.

    Scanning raw text would flag prose: uppercased, "yield from" and "derives
    from here" both look like a FROM clause.
    """
    tree = ast.parse((PACKAGE / f"{module}.py").read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]


def test_sql_lives_only_in_modules_that_own_persistence():
    """A business rule expressed as SQL cannot be tested or reused without the
    database. Rings 0 and 1 must contain none."""
    offenders = []
    for module in RING:
        if RING[module] > 1:
            continue
        for literal in _query_literals(module):
            upper = literal.upper()
            if "SELECT" in upper and "FROM" in upper:
                offenders.append(f"{module}: {literal[:40]!r}")
            elif "INSERT INTO" in upper or "UPDATE " in upper and " SET " in upper:
                offenders.append(f"{module}: {literal[:40]!r}")
    assert not offenders, f"SQL inside the inner rings: {offenders}"
