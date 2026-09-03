"""Identities that do not depend on which process asked for them.

Two properties, one file. Every identity the distribution ships -- the query
objects and the resource handles it defines -- must be the same in every
process, whatever hash seed that process was started under. And
no module the tree imports may carry a module identity that is not: a captured
module's identity payload is folded into every fingerprint that captures it, so
a payload that moves between processes moves all of them.

Both properties are cross-process by definition, so every cell here spawns real
subprocesses through ``sys.executable`` and compares what they print. The idiom
is ``tests/test_checkpoint_cross_process.py``'s, so the two files agree about
what a child process is allowed to depend on: a fixture script written into
``tmp_path``, ``{**os.environ, ...}`` for the child environment, an explicit
``PYTHONPATH`` holding the source tree, bytecode caching off, and a single
``JSON ``-prefixed line on stdout.

The shipped population is pinned rather than discovered and trusted. A new
query or resource handle joins the surface only through a deliberate edit to
the inventory below, which is what keeps a new integration from inheriting a
defect unnoticed.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import pkgutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pyinc
from pyinc import Database


def _src_dir() -> str:
    """The ``src`` directory holding the ``pyinc`` package (not the repo root)."""
    return str(Path(pyinc.__file__).resolve().parent.parent)


def _child_env(seed: str | None) -> dict[str, str]:
    """A child environment whose hash seed is the axis a cell chooses.

    ``{**os.environ, ...}`` rather than a bare dict, so the child still inherits
    ``TMPDIR`` and, on Windows, ``SYSTEMROOT``. Bytecode caching is off in every
    child: a ``.pyc`` records the absolute path of the source it was built from,
    which is exactly the kind of per-installation value these cells are here to
    prove is not folded. A row that wants no pinned seed *deletes*
    ``PYTHONHASHSEED`` rather than setting it to the empty string: CPython
    reads an empty value as absent, so the two are one configuration -- the one
    users actually run -- and setting it would only prove that they are read
    alike.
    """

    env = {
        **os.environ,
        "PYTHONPATH": _src_dir(),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if seed is None:
        env.pop("PYTHONHASHSEED", None)
    else:
        env["PYTHONHASHSEED"] = seed
    return env


def _run(args: list[str], env: dict[str, str]) -> dict[str, Any]:
    """Run a fixture child and return the payload of its last ``JSON `` line.

    The prefix matters: a child that imports the whole tree may print warnings,
    and the cells below are reading a value rather than the tail of whatever
    reached stdout.
    """

    proc = subprocess.run(args, capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (
        f"fixture subprocess failed ({proc.returncode})\n"
        f"argv: {args}\nSTDOUT:\n{proc.stdout[-4000:]}\nSTDERR:\n{proc.stderr[-4000:]}"
    )
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("JSON ")][-1]
    payload: dict[str, Any] = json.loads(line[len("JSON ") :])
    return payload


#: The three packages the distribution ships (pyproject's
#: `[tool.hatch.build.targets.wheel] packages`). `pyinc_tools` defines no query
#: and no resource handle, and is walked anyway so that a future one cannot
#: appear unnoticed.
_SHIPPED_PACKAGES = ("pyinc", "pyinc_tools", "pyinc_codegen")


def _walk_modules() -> list[tuple[str, object]]:
    """Every importable module of the three shipped packages, sorted by name."""
    names: list[str] = []
    for package_name in _SHIPPED_PACKAGES:
        package = importlib.import_module(package_name)
        names.append(package_name)
        for info in pkgutil.walk_packages(package.__path__, package_name + "."):
            names.append(info.name)
    modules: list[tuple[str, object]] = []
    for name in sorted(set(names)):
        try:
            modules.append((name, importlib.import_module(name)))
        except Exception:  # an unimportable optional module is not this cell's subject
            continue
    return modules


def _shipped_identities(db: Database) -> list[tuple[str, object]]:
    """`[(inventory key, object)]` for every shipped identity-bearing object.

    Two populations, one walk:

    * every `pyinc.core.Query`, keyed by `Q|<query key>`;
    * every object the KERNEL calls a resource handle, keyed by
      `R|<module>.<attribute>`.

    `Database._is_resource_handle` is the kernel's own predicate (a callable
    `label`/`probe`/`load`), not `isinstance(value, pyinc.resources.Resource)`:
    12 of the 30 shipped resource handles are duck-typed and subclass nothing,
    so a nominal test under-counts the surface by twelve. Classes and modules
    are excluded because an unbound method is callable off the class too.

    De-duplication is by `id()`, so a re-export (`source_text` is bound in three
    modules) contributes ONE entry. Objects, not bindings, is the unit the
    fingerprint has.
    """
    from types import ModuleType

    from pyinc.core import Query as _Query

    found: list[tuple[str, object]] = []
    seen: set[int] = set()
    for module_name, module in _walk_modules():
        for attribute_name, value in sorted(vars(module).items()):
            if id(value) in seen:
                continue
            if isinstance(value, _Query):
                seen.add(id(value))
                found.append(("Q|" + str(value.key), value))
            elif isinstance(value, (type, ModuleType)):
                continue
            elif db._is_resource_handle(value):
                seen.add(id(value))
                found.append(("R|" + module_name + "." + attribute_name, value))
    return sorted(found, key=lambda pair: pair[0])


def _discovered_identity_keys() -> frozenset[str]:
    return frozenset(key for key, _obj in _shipped_identities(Database(mode="strict")))


#: Every identity-bearing object the distribution ships, by key: 91 query
#: objects and 30 resource handles. Pinned as a literal rather than
#: discovered, so a new one joins the surface only through an edit here.
_SHIPPED_IDENTITY_INVENTORY: frozenset[str] = frozenset((
    # ---- 91 Query objects -------------------------------------------
    "Q|pyinc.integrations.csv_data:csv_analysis_payload",
    "Q|pyinc.integrations.csv_data:csv_columns_payload",
    "Q|pyinc.integrations.csv_data:csv_diagnostics_payload",
    "Q|pyinc.integrations.csv_data:csv_file_text",
    "Q|pyinc.integrations.csv_data:csv_meta_payload",
    "Q|pyinc.integrations.deep_module_resolution:_all_pth_directives_payload",
    "Q|pyinc.integrations.deep_module_resolution:_deep_analysis_payload",
    "Q|pyinc.integrations.deep_module_resolution:_effective_search_paths_payload",
    "Q|pyinc.integrations.deep_module_resolution:_pth_directives_payload",
    "Q|pyinc.integrations.deep_module_resolution:_pth_file_text",
    "Q|pyinc.integrations.deep_module_resolution:_pth_listing",
    "Q|pyinc.integrations.deep_module_resolution:_raw_sys_path_entries",
    "Q|pyinc.integrations.deep_module_resolution:_top_level_namespace_packages_payload",
    "Q|pyinc.integrations.deep_module_resolution:resolve_module_location",
    "Q|pyinc.integrations.dependency_check:_declared_deps_payload",
    "Q|pyinc.integrations.dependency_check:dependency_check_payload",
    "Q|pyinc.integrations.env_file:env_analysis_payload",
    "Q|pyinc.integrations.env_file:env_diagnostics_payload",
    "Q|pyinc.integrations.env_file:env_entries_payload",
    "Q|pyinc.integrations.env_file:env_file_text",
    "Q|pyinc.integrations.installed_packages:_dist_info_listing",
    "Q|pyinc.integrations.installed_packages:_installed_packages_payload",
    "Q|pyinc.integrations.installed_packages:_metadata_text",
    "Q|pyinc.integrations.installed_packages:_package_metadata_payload",
    "Q|pyinc.integrations.installed_packages:_site_packages_dirs",
    "Q|pyinc.integrations.installed_packages:_top_level_text",
    "Q|pyinc.integrations.installed_packages:environment_index",
    "Q|pyinc.integrations.installed_packages:installed_distributions_index",
    "Q|pyinc.integrations.json_config:json_analysis_payload",
    "Q|pyinc.integrations.json_config:json_diagnostics_payload",
    "Q|pyinc.integrations.json_config:json_file_text",
    "Q|pyinc.integrations.json_config:json_sections_payload",
    "Q|pyinc.integrations.notebook:notebook_analysis_payload",
    "Q|pyinc.integrations.notebook:notebook_cells_payload",
    "Q|pyinc.integrations.notebook:notebook_diagnostics_payload",
    "Q|pyinc.integrations.notebook:notebook_metadata_payload",
    "Q|pyinc.integrations.notebook:notebook_text",
    "Q|pyinc.integrations.python_source:definitions_for_file",
    "Q|pyinc.integrations.python_source:directory_analysis_payload",
    "Q|pyinc.integrations.python_source:file_analysis_payload",
    "Q|pyinc.integrations.python_source:import_statements_for_file",
    "Q|pyinc.integrations.python_source:imports_for_file",
    "Q|pyinc.integrations.python_source:module_analysis_payload",
    "Q|pyinc.integrations.python_source:module_binding_analysis_payload",
    "Q|pyinc.integrations.python_source:module_export_surface",
    "Q|pyinc.integrations.python_source:module_wildcard_export_surface",
    "Q|pyinc.integrations.python_source:resolved_imports_for_file",
    "Q|pyinc.integrations.python_source:source_ranges_for_file",
    "Q|pyinc.integrations.python_source:source_text",
    "Q|pyinc.integrations.python_source:syntax_diagnostics_for_file",
    "Q|pyinc.integrations.python_source:workspace_analysis_payload",
    "Q|pyinc.integrations.python_source:workspace_module_index",
    "Q|pyinc.integrations.python_source:workspace_python_files",
    "Q|pyinc.integrations.requirement_evaluation:_evaluate_markers_payload",
    "Q|pyinc.integrations.requirement_evaluation:_evaluate_version_specifier_payload",
    "Q|pyinc.integrations.requirement_evaluation:applicable_requirements_payload",
    "Q|pyinc.integrations.requirement_evaluation:python_environment_snapshot",
    "Q|pyinc.integrations.requirements_txt:file_references_payload",
    "Q|pyinc.integrations.requirements_txt:index_directives_payload",
    "Q|pyinc.integrations.requirements_txt:requirements_analysis_payload",
    "Q|pyinc.integrations.requirements_txt:requirements_diagnostics_payload",
    "Q|pyinc.integrations.requirements_txt:requirements_file_text",
    "Q|pyinc.integrations.requirements_txt:requirements_payload",
    "Q|pyinc.integrations.scope_resolution:scope_tree_payload",
    "Q|pyinc.integrations.symbol_resolution:_resolve_symbol_payload",
    "Q|pyinc.integrations.symbol_resolution:class_models_for_file",
    "Q|pyinc.integrations.symbol_resolution:module_symbol_table_for_module",
    "Q|pyinc.integrations.symbol_resolution:module_symbol_table_payload",
    "Q|pyinc.integrations.symbol_resolution:resolved_class_model_payload",
    "Q|pyinc.integrations.symbol_resolution:workspace_symbol_index_payload",
    "Q|pyinc.integrations.toml_config:config_analysis_payload",
    "Q|pyinc.integrations.toml_config:config_dependencies_payload",
    "Q|pyinc.integrations.toml_config:config_diagnostics_payload",
    "Q|pyinc.integrations.toml_config:config_file_text",
    "Q|pyinc.integrations.toml_config:config_sections_payload",
    "Q|pyinc.integrations.toml_config:config_tool_configs_payload",
    "Q|pyinc.integrations.xml_config:xml_analysis_payload",
    "Q|pyinc.integrations.xml_config:xml_diagnostics_payload",
    "Q|pyinc.integrations.xml_config:xml_elements_payload",
    "Q|pyinc.integrations.xml_config:xml_file_text",
    "Q|pyinc_codegen.schema:alias_cycle_diagnostics",
    "Q|pyinc_codegen.schema:definition_model",
    "Q|pyinc_codegen.schema:definition_names",
    "Q|pyinc_codegen.schema:definition_pointer",
    "Q|pyinc_codegen.schema:definition_raw",
    "Q|pyinc_codegen.schema:definition_structure",
    "Q|pyinc_codegen.schema:document_diagnostics",
    "Q|pyinc_codegen.schema:index_init",
    "Q|pyinc_codegen.schema:model_doc",
    "Q|pyinc_codegen.schema:model_python",
    "Q|pyinc_codegen.schema:schema_text",
    # ---- 30 resource handles ----------------------------------------
    "R|pyinc.integrations.csv_data._DIRECTORIES",
    "R|pyinc.integrations.csv_data._FILES",
    "R|pyinc.integrations.deep_module_resolution._DIRECTORIES",
    "R|pyinc.integrations.deep_module_resolution._FILES",
    "R|pyinc.integrations.deep_module_resolution._FILESTAT",
    "R|pyinc.integrations.deep_module_resolution._RESOLVED",
    "R|pyinc.integrations.env_file._DIRECTORIES",
    "R|pyinc.integrations.env_file._FILES",
    "R|pyinc.integrations.installed_packages._DIRECTORIES",
    "R|pyinc.integrations.installed_packages._METADATA",
    "R|pyinc.integrations.installed_packages._SITE_PACKAGES",
    "R|pyinc.integrations.json_config._DIRECTORIES",
    "R|pyinc.integrations.json_config._FILES",
    "R|pyinc.integrations.notebook._DIRECTORIES",
    "R|pyinc.integrations.notebook._FILES",
    "R|pyinc.integrations.python_source._DIRECTORIES",
    "R|pyinc.integrations.python_source._FILES",
    "R|pyinc.integrations.python_source._RESOLVED",
    "R|pyinc.integrations.requirement_evaluation._DIRECTORIES",
    "R|pyinc.integrations.requirement_evaluation._PY_ENV",
    "R|pyinc.integrations.requirements_txt._DIRECTORIES",
    "R|pyinc.integrations.requirements_txt._FILES",
    "R|pyinc.integrations.requirements_txt._PRESENCE",
    "R|pyinc.integrations.requirements_txt._RESOLVED_PATHS",
    "R|pyinc.integrations.scope_resolution._RESOLVED_PATHS",
    "R|pyinc.integrations.toml_config._DIRECTORIES",
    "R|pyinc.integrations.toml_config._FILES",
    "R|pyinc.integrations.xml_config._DIRECTORIES",
    "R|pyinc.integrations.xml_config._FILES",
    "R|pyinc_codegen.schema._FILES",
))


def test_the_shipped_identity_inventory_is_exact() -> None:
    """The population the cell below guards, pinned by name.

    A frozenset literal rather than a discovered set, in
    ``tests/test_cutoff_inventory.py``'s idiom: 121 objects, 91 query objects
    and 30 resource handles. Adding a shipped query or resource handle is
    therefore a deliberate edit here rather than a silent widening of what the
    stability cell has to hold for. The literal carries no per-version branch,
    and the test matrix runs this cell on every interpreter the project
    supports, so a surface that differed between them would be red here.
    """

    found = _discovered_identity_keys()
    missing = sorted(_SHIPPED_IDENTITY_INVENTORY - found)
    extra = sorted(found - _SHIPPED_IDENTITY_INVENTORY)
    assert not (missing or extra), (
        f"the shipped identity surface moved: {len(found)} found, "
        f"{len(_SHIPPED_IDENTITY_INVENTORY)} recorded; "
        f"gone={missing} new={extra} -- update the inventory in the commit that "
        "adds or removes the query or resource"
    )


# The fixture child runs the SAME discovery source as the inventory cell --
# `inspect.getsource`, not a second copy -- so the population under test and the
# population the inventory pins cannot drift apart.
IDENTITY_FIXTURE_SCRIPT = (
    '"""Print the process-stable identity digest of every shipped object."""\n'
    "import importlib\n"
    "import json\n"
    "import pkgutil\n"
    "\n"
    "from pyinc import Database\n"
    "from pyinc.core import Query\n"
    "from pyinc.value import fingerprint_snapshot\n"
    "\n"
    f"_SHIPPED_PACKAGES = {_SHIPPED_PACKAGES!r}\n"
    "\n\n"
    + inspect.getsource(_walk_modules)
    + "\n\n"
    + inspect.getsource(_shipped_identities)
    + '''

db = Database(mode="strict")
out = {}
for key, obj in _shipped_identities(db):
    try:
        if isinstance(obj, Query):
            out[key] = db._query_fingerprint(obj)
        else:
            out[key] = fingerprint_snapshot(db._resource_identity_payload(obj))
    except Exception as exc:
        out[key] = "ERROR:" + type(exc).__name__
print("JSON " + json.dumps(out))
'''
)


def test_every_shipped_identity_is_the_same_in_every_process(tmp_path: Path) -> None:
    """Every shipped query and resource handle digests the same in three processes.

    Two different non-zero seeds and no pinned seed at all. Non-zero, because
    ``PYTHONHASHSEED=0`` turns hash randomization off rather than choosing a
    seed, and a row crossing that is evidence about how the interpreter was
    configured rather than about the order anything was hashed. Three processes
    is the minimum that separates "agrees" from "agrees by luck", and the
    unpinned row is the configuration users actually run.
    """

    script = tmp_path / "identity_fixture.py"
    script.write_text(IDENTITY_FIXTURE_SCRIPT, encoding="utf-8")

    seeds: tuple[str | None, ...] = ("1", "2", None)
    runs: list[dict[str, Any]] = []
    started = time.perf_counter()
    for seed in seeds:
        runs.append(_run([sys.executable, str(script)], _child_env(seed)))
    elapsed = time.perf_counter() - started

    keys = set(_SHIPPED_IDENTITY_INVENTORY)
    for seed, run in zip(seeds, runs, strict=True):
        # Per child rather than over the union of the three: a key one child
        # never printed would otherwise reach the disagreement check below and
        # be reported as an identity that differs between processes, when what
        # happened is that the population drifted.
        assert set(run) == keys, (
            f"the fixture child at seed {seed!r} and the inventory disagree "
            "about the population"
        )

    raised = sorted(
        key
        for key in keys
        if any(str(run.get(key, "")).startswith("ERROR:") for run in runs)
    )
    assert not raised, f"identity digest raised for: {raised}"
    disagreeing = sorted(key for key in keys if len({run.get(key) for run in runs}) > 1)
    queries = [key for key in disagreeing if key.startswith("Q|")]
    resources = [key for key in disagreeing if key.startswith("R|")]
    assert not disagreeing, (
        f"{len(disagreeing)} of {len(keys)} shipped objects "
        f"({len(queries)} queries, {len(resources)} resource handles) have a "
        f"different identity in different processes (seeds {seeds}); "
        f"first ten: {disagreeing[:10]} [cell wall time {elapsed:.1f}s]"
    )


# The spelling is `_module_identity_payload`, never `_module_constants_payload`.
# The raw read still answers with whatever this process rebuilt: it is the
# identity payload that decides which modules contribute a namespace at all, and
# the identity payload is what a fingerprint folds. A census written against the
# raw read would be red forever whatever the identity does, and a permanently
# red cell is disabled within a release or two.
CENSUS_FIXTURE_SCRIPT = '''\
"""Digest `_module_identity_payload` for every module the pyinc tree imports."""

import importlib
import json
import pkgutil
import sys

import pyinc
from pyinc.runtime import Database
from pyinc.value import fingerprint_snapshot

for info in pkgutil.walk_packages(pyinc.__path__, "pyinc."):
    try:
        importlib.import_module(info.name)
    except Exception:
        pass

db = Database(mode="strict")
out = {}
real = 0
for name, module in sorted(sys.modules.items()):
    if module is None or not hasattr(module, "__dict__"):
        continue
    try:
        out[name] = fingerprint_snapshot(db._module_identity_payload(module))
        real += 1
    except Exception as exc:
        out[name] = "REFUSED:" + type(exc).__name__
print("JSON " + json.dumps({"digests": out, "real": real}))
'''

#: A floor on how many modules must yield a real digest, so the census cannot
#: pass by refusing everything. Measured at both ends of the supported
#: interpreter range: 201 real digests of 211 imported modules on the oldest,
#: 210 of 215 on the newest. The floor sits far below both, so a standard
#: library reshuffle does not turn the cell red for a reason that has nothing to
#: do with what it measures.
_MINIMUM_MODULES_WITH_A_REAL_IDENTITY = 150


def test_no_module_the_tree_imports_carries_a_process_varying_identity(
    tmp_path: Path,
) -> None:
    """No module the tree imports has an identity that moves between processes.

    A captured module's identity payload is folded into the fingerprint of every
    query that captures it, so one module whose payload is not reproducible is
    enough to make a whole family of identities process-dependent.
    """

    script = tmp_path / "census_fixture.py"
    script.write_text(CENSUS_FIXTURE_SCRIPT, encoding="utf-8")

    # Four children in two same-seed PAIRS. A module that disagrees INSIDE a
    # pair varies for a reason other than the hash seed (an address, a pid, a
    # clock); one that agrees inside both pairs but disagrees between them
    # varies with the hash order. Both are fatal to a process-stable
    # fingerprint, and the failure message has to say which, or the fix aims at
    # the wrong thing.
    started = time.perf_counter()
    a1, a2, b1, b2 = (
        _run([sys.executable, str(script)], _child_env(seed))
        for seed in ("1", "1", "2", "2")
    )
    elapsed = time.perf_counter() - started

    assert a1["real"] >= _MINIMUM_MODULES_WITH_A_REAL_IDENTITY, (
        f"only {a1['real']} modules produced a real identity digest -- the cell "
        "would be passing vacuously"
    )
    digests = [run["digests"] for run in (a1, a2, b1, b2)]
    shared = set(digests[0]).intersection(*(set(d) for d in digests[1:]))
    process_varying = sorted(
        name
        for name in shared
        if digests[0][name] != digests[1][name] or digests[2][name] != digests[3][name]
    )
    seed_varying = sorted(
        name
        for name in shared
        if name not in process_varying and digests[0][name] != digests[2][name]
    )
    assert not (process_varying or seed_varying), (
        f"of {len(shared)} imported modules, {len(seed_varying)} carry an "
        f"identity that follows the hash order {seed_varying} and "
        f"{len(process_varying)} carry one that varies process to process at a "
        f"FIXED seed {process_varying} "
        f"[cell wall time {elapsed:.1f}s over {len(shared)} modules, "
        f"{a1['real']} of them with a real digest]"
    )


BUILD_PAYLOAD_FIXTURE_SCRIPT = '''\
"""Print the digest of this interpreter's build identity."""

import json

from pyinc.runtime import _build_runtime_build_payload
from pyinc.value import fingerprint_snapshot

print("JSON " + json.dumps({"build": fingerprint_snapshot(_build_runtime_build_payload())}))
'''


def test_the_runtime_build_payload_ignores_the_hash_randomization_flag(
    tmp_path: Path,
) -> None:
    """The build identity is the same whether hash randomization is on or off.

    This is the positive pin for the flag. The negative spelling -- asserting
    that the payload's repr does not mention ``hash_randomization`` -- holds
    just as well of a payload that folds the flag as a bare value at a fixed
    position, so it is not a pin at all.

    ``PYTHONHASHSEED=0`` is not "seed zero": it turns randomization off, which
    is the axis this cell crosses. The third child pins a different randomized
    seed and is asserted equal to ``randomization_on``, as the control that the
    cell is comparing a payload two processes built rather than reading back a
    constant.
    """

    script = tmp_path / "build_payload_fixture.py"
    script.write_text(BUILD_PAYLOAD_FIXTURE_SCRIPT, encoding="utf-8")

    randomization_off = _run([sys.executable, str(script)], _child_env("0"))["build"]
    randomization_on = _run([sys.executable, str(script)], _child_env("1"))["build"]
    another_seed = _run([sys.executable, str(script)], _child_env("2"))["build"]

    assert randomization_off == randomization_on, (
        "the interpreter build identity moves with hash randomization: "
        f"off {randomization_off}, on {randomization_on}"
    )
    assert randomization_on == another_seed, (
        "the build identity moves between two randomized seeds, so the cell "
        f"above is not comparing what it claims: {randomization_on} "
        f"then {another_seed}"
    )
