"""Names used anywhere in main.py must exist by the time they are used.

The import test beside this one covers what runs *while the module loads*.
This covers what runs later, which is where the same mistake hides best: a
handler that uses `json` in a module that never imported it imports cleanly,
starts cleanly, passes a health check, and fails the first time a site pushes
its outbox — with a 500 that says nothing to the site.

A scope-aware walk rather than a grep: a name is fine if it is a builtin, a
module-level binding, an import, a parameter, a comprehension variable, a
loop target, an exception alias, or assigned anywhere in the enclosing
function. Anything else is reported. This is a small linter, and a small
linter is what this class of bug has needed twice.
"""
import ast
import builtins
import pathlib
import sys

SOURCE = pathlib.Path(__file__).with_name("main.py")
tree = ast.parse(SOURCE.read_text())

module_names = set(dir(builtins))


def bind_targets(node, into):
    """Every name a binding construct introduces."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            into.add(sub.id)


for node in ast.walk(tree):
    if isinstance(node, ast.ImportFrom):
        module_names.update(a.asname or a.name for a in node.names)
    elif isinstance(node, ast.Import):
        module_names.update((a.asname or a.name).split(".")[0] for a in node.names)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        module_names.add(node.name)

for node in tree.body:
    bind_targets(node, module_names) if isinstance(
        node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.For, ast.With, ast.Try)) else None

problems = []


def declared_in(fn):
    """Every name this function itself binds — parameters and anything assigned.

    Python binds a name for the whole function body wherever the assignment
    appears, so *where* is not this file's problem; a type checker's job
    starts where this one stops.
    """
    names = set()
    args = fn.args
    for a in (args.posonlyargs + args.args + args.kwonlyargs):
        names.add(a.arg)
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            names.add(sub.id)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for a in sub.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(sub.name)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, ast.comprehension):
            bind_targets(sub.target, names)
        elif isinstance(sub, ast.Lambda):
            for a in (sub.args.posonlyargs + sub.args.args + sub.args.kwonlyargs):
                names.add(a.arg)
    return names


def subtree_ids(node):
    """Every node id under this one, itself included."""
    return {id(n) for n in ast.walk(node)}


def check_function(fn, outer):
    """Report loads with nothing behind them, then recurse into nested defs.

    The scope has to be inherited: a closure reading a name from the function
    that defines it is ordinary Python, and a checker that flagged every one
    would be switched off within a day — which is how a linter stops finding
    real bugs.

    Nested functions are skipped *whole*. Skipping only their `def` node left
    their bodies being walked with the outer scope, so every helper's own
    parameters looked undefined — `def _p(t)` inside a function reported `t`.
    A checker that cries wolf about correct code is worse than none.
    """
    local = outer | declared_in(fn)

    nested = [n for n in ast.iter_child_nodes(fn)]
    inner_defs = []
    for child in ast.walk(fn):
        if child is fn:
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            inner_defs.append(child)

    # Only the outermost nested definitions: their own bodies are checked by
    # the recursion below, with their scope in hand.
    top_nested = [d for d in inner_defs
                  if not any(d is not other and id(d) in subtree_ids(other)
                             for other in inner_defs)]
    skip = set()
    for d in top_nested:
        skip |= subtree_ids(d)

    for sub in ast.walk(fn):
        if id(sub) in skip:
            continue
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and sub.id not in local:
            problems.append(
                f"line {sub.lineno}: {fn.name}() uses {sub.id!r}, which is never defined or imported")

    for child in top_nested:
        if isinstance(child, ast.Lambda):
            continue  # its parameters are already in `local` via declared_in
        check_function(child, local)


def module_level_functions(tree):
    """Functions not nested inside another function, including methods."""
    nested = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is not node and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nested.add(id(child))
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(n) not in nested]


for node in module_level_functions(tree):
    check_function(node, module_names)

if problems:
    print("main.py uses names that do not exist:")
    for p in dict.fromkeys(problems):
        print("  -", p)
    sys.exit(1)
print("ok: every name used in a function body exists")
