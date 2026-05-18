"""
dorian/pipeline/operator_resolver.py
--------------------------------------
Resolves DAG nodes (Operator, Parameter, Snippet) into callables and
converts a pipeline DAG into a Dask-compatible task graph.

Responsibilities:
  1. Resolve an operator name → callable.
     - Dotted paths  (e.g. 'pandas.read_csv', 'sklearn.linear_model.LinearRegression')
     - Method shortcuts for class-based operators ('fit', 'predict', 'transform', …)
     - Built-in functions ('print', 'len')
     - Snippet code execution
     - Parameter evaluation
  2. Handle class-based operators whose `tasks` field declares a method
     sequence (e.g. ["__init__", "fit", "predict"]).  The class node itself
     resolves to the constructor; downstream method-shortcut nodes call the
     corresponding method on the instance passed via edges.
  3. Auto-install missing Python packages via pip.
  4. Build a complete Dask task graph from a DAG.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import subprocess
import sys
from typing import Any, Callable, Dict, Optional

from dorian.dag import Operator, Parameter, Snippet
from backend.events import Event, emit

def _get_library_map() -> Dict[str, str]:
    """Return import-name → pip-package-name mapping from the KB.

    Queries ``(lib)-[:has_package]->(pkg)`` relationships.
    """
    try:
        from dorian.knowledge.queries import get_library_package_map
        return get_library_package_map()
    except Exception:
        return {}


def _get_method_shortcuts() -> frozenset[str]:
    """Return method shortcut names from the KB.

    Queries all interface ``calls`` chains for method names (excluding
    ``__init__``).
    """
    try:
        from dorian.knowledge.queries import get_all_interface_methods
        return get_all_interface_methods()
    except Exception:
        return frozenset()


def _install_package(package_name: str) -> bool:
    """Attempt to pip-install `package_name`. Returns True on success."""
    emit(Event("PackageInstalling", {"source": "operator_resolver._install_package", "package": package_name}))
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "-q", "install", package_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        emit(Event("PackageInstalled", {"source": "operator_resolver._install_package", "package": package_name}))
        return True
    except subprocess.CalledProcessError as exc:
        emit(Event("PackageInstallFailed", {"source": "operator_resolver._install_package", "package": package_name, "error": str(exc)}))
        return False


def _try_import(module_name: str) -> Optional[Any]:
    """Try importing ``module_name`` as a Python module.

    Returns the module or None on failure (no auto-install).
    Callers that need attribute resolution (e.g. ``from pkg import Cls``)
    should use ``_resolve_dotted`` which does ``rsplit('.', 1)`` + ``getattr``.
    """
    try:
        return importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError):
        return None


def _import_module(module_name: str) -> Optional[Any]:
    """Import ``module_name`` with auto-install fallback.

    Tries a direct import first, then installs the top-level package via
    pip and retries.  Returns the module or None on failure.
    """
    result = _try_import(module_name)
    if result is not None:
        return result

    top_level = module_name.split(".")[0]
    pip_name = _get_library_map().get(top_level, top_level)
    if _install_package(pip_name):
        return _try_import(module_name)

    emit(Event("ImportFailed", {
        "source": "operator_resolver._import_module",
        "module": module_name, "error": f"Could not import '{module_name}' after install attempt",
    }))
    return None


def _resolve_dotted(name: str) -> Any:
    """Import and return the object at the dotted path.

    Checks the KB for an ``is_subclass_of`` annotation first
    (e.g. ``openrouter.chat.completion`` → ``openrouter.OpenRouter``).
    Falls back to treating the FQN itself as a Python dotted path
    (e.g. ``sklearn.linear_model.LinearRegression``).
    """
    try:
        from dorian.knowledge.queries import get_operator_import_path
        import_path = get_operator_import_path(name)
    except Exception:
        import_path = None

    path = import_path or name
    module_path, attr_name = path.rsplit(".", 1)

    module = _import_module(module_path)
    if module is None:
        raise ImportError(f"Could not import module '{module_path}' for operator '{name}'")
    obj = getattr(module, attr_name, None)
    if obj is None:
        raise AttributeError(f"Module '{module_path}' has no attribute '{attr_name}'")
    return obj


def _resolve_parameter(param: Parameter) -> Callable:
    """Return a zero-arg callable that safely evaluates a Parameter value.

    Replaces the old ``eval(self.dtype)(self.value)`` pattern with a safe
    lookup table.  The ``"eval"`` dtype uses ``ast.literal_eval`` which
    handles Python literals (None, True, False, tuples, lists, dicts,
    numbers) without executing arbitrary code.
    """
    _SAFE_DTYPES: Dict[str, Callable] = {
        "int":         int,
        "float":       float,
        "string":      str,
        "str":         str,
        "bool":        lambda v: v.lower() in ("true", "1", "yes"),
        "eval":        ast.literal_eval,
        "list":        ast.literal_eval,   # e.g. messages=[{...}]
        "categorical": str,                # e.g. model="openai/gpt-4o"
        "env":         str,  # vault references — resolved to str by vault_transform
    }
    dtype = param.dtype
    value = param.value
    converter = _SAFE_DTYPES.get(dtype)
    if converter is None:
        emit(Event("UnknownParameterDtype", {"source": "operator_resolver._resolve_parameter", "dtype": dtype, "param": param.name}))
        converter = str

    def _evaluate(*_args):
        return converter(value)

    return _evaluate


# Safe builtins whitelist for Snippet execution.
_SNIPPET_BUILTINS = {
    "len": len, "range": range, "enumerate": enumerate, "zip": zip,
    "map": map, "filter": filter, "sorted": sorted, "reversed": reversed,
    "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
    "list": list, "dict": dict, "set": set, "tuple": tuple,
    "int": int, "float": float, "str": str, "bool": bool,
    "isinstance": isinstance, "type": type, "print": print,
    "hasattr": hasattr, "getattr": getattr, "callable": callable,  # introspection
    "ValueError": ValueError, "TypeError": TypeError, "RuntimeError": RuntimeError,
    "Exception": Exception,
    "True": True, "False": False, "None": None,
    "__import__": __import__,  # needed for pandas/sklearn/numpy imports inside snippets
}


def _resolve_snippet(snippet: Snippet) -> Callable:
    """Return a callable that executes a Snippet's code in a restricted namespace.

    The snippet code must define a ``foo(...)`` function.  Execution uses a
    restricted ``__builtins__`` dict rather than full ``globals()`` to limit
    the attack surface while still allowing imports needed by data-science
    snippets (pandas, numpy, sklearn).
    """
    code = snippet.code

    def _execute(*args, **kwargs):
        safe_globals = {"__builtins__": _SNIPPET_BUILTINS}
        res: dict = {}
        exec(code, safe_globals, res)  # noqa: S102
        if "foo" not in res:
            raise RuntimeError(
                f"Snippet {snippet.name!r} must define a 'foo(...)' function"
            )
        return res["foo"](*args, **kwargs)

    return _execute


def resolve(node: Operator | Parameter | Snippet) -> Callable:
    """Return a callable for *node* suitable for placement in a Dask task graph.

    Resolution strategy:

    - **Parameter** — safe type-conversion via lookup table (no ``eval``).
    - **Snippet** — restricted ``exec`` with whitelisted builtins.
    - **Operator**:

      1. *Method shortcuts* (``fit``, ``predict``, ``transform``, …):
         First positional arg is the instance; method is dispatched on it.

      2. *Built-in functions* (no dot in name, e.g. ``print``, ``len``):
         Looked up in ``__builtins__`` and called directly.

      3. *Dotted module.attr paths* (e.g. ``pandas.read_csv``):
         Imported and called.  If the resolved object is a class, it is
         instantiated with the provided args/kwargs.
    """
    if isinstance(node, Parameter):
        return _resolve_parameter(node)

    if isinstance(node, Snippet):
        return _resolve_snippet(node)

    # --- Operator ---
    name: str = node.name
    method_shortcuts = _get_method_shortcuts()

    # Method shortcut — dispatch as instance.method(...)
    #
    # The instance can arrive as either:
    #   (a) first positional arg (classic Dask wiring with numeric edge position)
    #   (b) a named kwarg (compound subgraph from drag-and-drop, where the
    #       instance edge has position = instanceLabel, e.g. "chat" for "chat.send")
    #
    # We detect case (b) by looking for a kwarg whose name matches the
    # instance label derived from the method name (everything before the
    # last dot, or "self" for simple names).
    if name in method_shortcuts:
        parts = name.split(".")
        # Instance kwarg name mirrors frontend's instanceLabel derivation
        _instance_kwarg = ".".join(parts[:-1]) if len(parts) > 1 else "self"

        def _method_call(*args, **kwargs):
            # Extract instance — prefer the explicit ``self`` kwarg
            # over the first positional. Pipelines from the FLAML
            # extractor (and the canvas drag-and-drop convention)
            # wire the instance edge with ``position="self"`` while
            # data edges (X, y) take positional slots 1, 2 — without
            # this priority the resolver would pop X (a DataFrame)
            # as the instance and ``df.fit(...)`` would
            # AttributeError. When neither is present the call has
            # no instance and we fail fast.
            if _instance_kwarg in kwargs:
                instance = kwargs.pop(_instance_kwarg)
                rest = list(args)
            elif args:
                instance, *rest = args
            else:
                raise TypeError(
                    f"Method shortcut '{name}' requires an instance as the first "
                    f"positional arg or as kwarg '{_instance_kwarg}'"
                )
            obj = instance
            for part in parts:
                obj = getattr(obj, part, None)
                if obj is None:
                    raise AttributeError(
                        f"Object of type {type(instance).__name__} has no attribute chain '{name}'"
                    )
            result = obj(*rest, **kwargs)
            # Dual output: (instance, result).  Output 0 = instance
            # (for chain continuation), output 1 = method return value
            # (for downstream consumers).  Methods returning self produce
            # (self, self) — harmless; slice _0 still yields the instance.
            return (instance, result)
        return _method_call

    # Built-in (no dot in name)
    if "." not in name:
        builtin = __builtins__.get(name) if isinstance(__builtins__, dict) else getattr(__builtins__, name, None)
        if not callable(builtin):
            raise NameError(f"'{name}' is not a recognised operator or built-in")
        return builtin

    # Dotted module.attr path — eagerly resolve at graph-build time
    obj = _resolve_dotted(name)

    if inspect.isclass(obj):
        from dorian.pipeline.instance_cache import get_or_create as _cache_get

        _cls = obj
        _cls_name = name

        def _cached_init(*args, **kwargs):
            # Class constructors take only kwargs (sklearn convention).
            # Positional args here mean a DATA edge (X, y, …) was wired
            # to the class node itself instead of being routed to the
            # fit / transform / predict method — i.e. compound expansion
            # didn't run for this operator and the raw class is being
            # called at execution time.
            #
            # Typical causes:
            #   * ``get_operator_interface('{fqn}')`` returns None
            #     (missing KB declaration for this operator).
            #   * The interface exists but its ``calls`` chain has fewer
            #     than 2 methods so ``_expand_compound_operator`` treats
            #     it as a Function.
            #   * A mitigation rewrite added the class node without
            #     re-running compound expansion afterwards.
            #
            # Raise with the operator name so the error log points at
            # the actual broken entry instead of "_cached_init() takes
            # 0 positional arguments but N were given".
            if args:
                raise TypeError(
                    f"Class operator {_cls_name!r} received "
                    f"{len(args)} positional data arg(s) at init time. "
                    f"This means compound expansion did not run — the "
                    f"class should have been expanded into "
                    f"__init__ → fit → predict/transform before "
                    f"execution. Check that ``get_operator_interface("
                    f"{_cls_name!r})`` returns a valid interface with a "
                    f"multi-method ``calls`` chain in Neo4j."
                )
            return _cache_get(_cls_name, _cls, kwargs)

        return _cached_init

    # Function with post-processing (Arrow → numpy for pandas.read_csv).
    # Fast path: if the caller passes just ``fpath`` (the DATASET_EXPANSION
    # shape — bare positional path, no kwargs), parse via ``pyarrow.csv``
    # and materialise a pandas view. Arrow-backed CSV parsing is
    # consistently faster than pandas' own parser, and this is the hot
    # path for every user pipeline going through the dataset expansion.
    # Anything with custom kwargs (sep, dtype, chunksize, …) falls
    # through to pandas so we don't silently change parse semantics.
    if name == "pandas.read_csv":
        def _read_csv(*args, **kwargs):
            if len(args) == 1 and not kwargs:
                try:
                    import pyarrow.csv as _pacsv
                    return _arrow_to_numpy(_pacsv.read_csv(args[0]).to_pandas())
                except Exception:
                    pass  # Fall through to pandas on any pyarrow hiccup.
            return _arrow_to_numpy(obj(*args, **kwargs))
        return _read_csv

    return obj


def _arrow_to_numpy(df):
    """Convert PyArrow-backed DataFrame columns to plain numpy dtypes.

    pandas ≥3.0 uses Arrow-backed storage for several types, including
    the default ``str`` dtype (``ArrowStringArray``).  sklearn indexing
    is incompatible with these types.

    Detection uses the backing array class name (``"Arrow" in ...``)
    because ``hasattr(dtype, "pyarrow_dtype")`` is ``False`` for the
    pandas 3.0 ``str`` dtype even though it wraps ``ArrowStringArray``.

    **Important**: ``df[c] = df[c].to_numpy()`` does NOT work in pandas
    3.0 because type inference re-wraps string arrays as Arrow.  We use
    ``astype(object)`` instead, which explicitly sets dtype=object and
    prevents the re-wrapping.  Default ``read_csv`` only produces Arrow
    for string columns (numerics use NumpyExtensionArray), so the object
    cast only affects strings — which sklearn handles fine.

    No-op when no Arrow columns are present (pandas < 3.0 or already
    numpy-backed).  Returns a copy — the original DataFrame is not
    touched.
    """
    import pandas as pd

    if not isinstance(df, pd.DataFrame):
        return df
    arrow_cols = [
        c for c in df.columns
        if "Arrow" in type(df[c].array).__name__
    ]
    if not arrow_cols:
        return df
    df = df.copy()
    for c in arrow_cols:
        df[c] = df[c].astype(object)
    return df


def build_dag_graph(pipeline) -> dict:
    """
    Convert a DAG into a Dask-compatible graph dictionary.

    Each entry in the graph is a tuple: (callable, *dependency_keys)
    Multi-output nodes get extra slice-entries: '{node_id}_{output_idx}'
    """
    def _slice(ll, i):  # noqa: E731
        """Extract element from list/tuple/dict by index.

        Coerces string indices like ``"1"`` (common after JSON round-trip)
        back to ``int`` so ``tuple[1]`` works instead of ``tuple["1"]``.
        """
        if isinstance(i, str):
            try:
                i = int(i)
            except (ValueError, TypeError):
                pass  # keep as string for dict-like access
        return ll[i]
    graph: dict = {}

    edges = pipeline.edges
    deps = {e.destination for e in edges}
    def _is_non_default_output(output) -> bool:
        """Return True when the output port is not the default (port 0).

        Handles three cases:
          • int / int-like string (0, "0") → False (single default output)
          • int / int-like string (1, "1", …) → True  (indexed multi-output)
          • non-numeric string ("data", "model", …) → True (named output;
            _slice works for dict-returning nodes via result["data"])
        """
        if output is None or output == 0:
            return False
        try:
            return int(output) > 0
        except (ValueError, TypeError):
            # Non-numeric string: any non-empty name is a non-default port
            return bool(output)

    multioutput = {e.source for e in edges if _is_non_default_output(e.output)}

    # Method shortcuts always return (instance, result) — they are always
    # multioutput even if the only outgoing edge uses output=0 (the chain
    # edge carrying the instance).  Without this, fit's raw tuple is passed
    # to predict instead of being sliced.
    _method_names = _get_method_shortcuts()
    for nid, node in pipeline.nodes.items():
        if isinstance(node, Operator) and node.name in _method_names:
            multioutput.add(nid)

    def _position_key(e):
        """Sort key that handles both int (positional) and str (keyword) positions.

        Integers sort first (by value), strings sort after (lexicographically).
        This avoids the Python 3 TypeError when comparing str and int.
        """
        p = e.position
        if isinstance(p, int):
            return (0, p, "")
        try:
            return (0, int(p), "")
        except (ValueError, TypeError):
            return (1, 0, str(p))

    def _args_for(node_id: str) -> tuple[tuple, dict]:
        """Return (positional_dep_keys, {kwarg_name: dep_key}) for node_id.

        Edges whose `position` is a non-numeric string encode keyword arguments
        (e.g. position="strategy" → call as fn(strategy=value)).  All other
        edges are treated as ordered positional arguments.
        """
        incoming = sorted(
            (e for e in edges if e.destination == node_id),
            key=_position_key,
        )
        pos_keys: list = []
        kw_map: dict = {}
        for e in incoming:
            key = f"{e.source}_{e.output}" if e.source in multioutput else e.source
            p = e.position
            if isinstance(p, str):
                try:
                    int(p)          # "0", "1", … → still positional
                    pos_keys.append(key)
                except (ValueError, TypeError):
                    kw_map[p] = key  # "strategy", "n_estimators", … → kwarg
            else:
                pos_keys.append(key)
        return tuple(pos_keys), kw_map

    def _make_entry(fn, pos_keys: tuple, kw_map: dict) -> tuple:
        """Build a Dask task-graph entry that handles both positional and keyword deps.

        Dask only supports ``(callable, *args)``.  When there are keyword deps we
        wrap the callable so the extra positional slots are unpacked back into
        kwargs at execution time.
        """
        if not kw_map:
            return (fn,) + pos_keys
        kw_names = tuple(kw_map.keys())
        kw_deps = tuple(kw_map.values())
        n_pos = len(pos_keys)

        def _wrapper(*all_args, _fn=fn, _n=n_pos, _kw=kw_names):
            return _fn(*all_args[:_n], **dict(zip(_kw, all_args[_n:])))

        return (_wrapper,) + pos_keys + kw_deps

    # Nodes with no incoming edges (sources / parameter nodes)
    for node_id in set(pipeline.nodes) - deps:
        graph[node_id] = (resolve(pipeline.nodes[node_id]),)

    for edge in edges:
        src, dst = edge.source, edge.destination

        # Slice entries for multi-output sources
        if src in multioutput:
            slice_key = f"{src}_{edge.output}"
            if slice_key not in graph:
                graph[slice_key] = (_slice, src, edge.output)

        # Destination node
        if dst not in graph:
            pos_keys, kw_map = _args_for(dst)
            graph[dst] = _make_entry(resolve(pipeline.nodes[dst]), pos_keys, kw_map)

        # Source node (may have been missed if it only appears as a source)
        if src not in graph:
            pos_keys, kw_map = _args_for(src)
            graph[src] = _make_entry(resolve(pipeline.nodes[src]), pos_keys, kw_map)

    return graph
