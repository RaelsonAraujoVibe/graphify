# Using this Graphify fork with a VB6 project

This fork indexes VB6 source locally, without a VB6 installation, an LLM, or an
API key. Each VB6 project installs this fork and its Python dependencies into
its own environment, then indexes itself. No Graphify checkout or environment
on another user's machine is needed.

## Declare the dependency in your VB6 project

Create `requirements-graphify.txt` in your **VB6 project's root directory**:

```text
graphifyy @ git+https://github.com/RaelsonAraujoVibe/graphify.git@<VB6_COMMIT_OR_TAG>
```

Replace `<VB6_COMMIT_OR_TAG>` with a published commit hash or tag containing this
fork's VB6 changes. Commit this requirements file alongside your VB6 source so
every contributor installs the same extractor. If you host your own fork,
replace the repository URL too. Private repositories require Git access.

The VB6 changes must be committed and pushed before this Git installation can
fetch them. Until then, use the wheel distribution option below. Installing
`graphifyy` from PyPI alone does not include this fork's changes.

## Install and index from your VB6 project

Open a terminal in the VB6 project's root, where `requirements-graphify.txt`
lives. Install Python 3.11 and Git first. The commands below create a dedicated
environment for indexing; pip installs Graphify and its declared dependencies.

### Windows (PowerShell)

```powershell
py -3.11 -m venv .venv-graphify
.\.venv-graphify\Scripts\python.exe -m pip install -r requirements-graphify.txt
.\.venv-graphify\Scripts\graphify.exe update .
```

### macOS / Linux

```sh
python3.11 -m venv .venv-graphify
.venv-graphify/bin/python -m pip install -r requirements-graphify.txt
.venv-graphify/bin/graphify update .
```

### Alternative installation with uv

From the same VB6 project directory, on Windows:

```powershell
uv venv .venv-graphify --python 3.11
uv pip install --python .venv-graphify\Scripts\python.exe -r requirements-graphify.txt
.\.venv-graphify\Scripts\graphify.exe update .
```

On macOS/Linux, use `.venv-graphify/bin/python` and
`.venv-graphify/bin/graphify` for the environment executables.

Environment activation is optional: these commands explicitly use the project's
installed executable. They work independently of any global Graphify install.
Package installation needs network access; VB6 indexing itself runs locally.

Add these entries to the VB6 project's `.gitignore` and `.graphifyignore` to
exclude the environment and generated index:

```gitignore
.venv-graphify/
graphify-out/
```

If you intentionally version the generated index, omit `graphify-out/` from
`.gitignore`.

## Index outputs

`update` can create the first graph as well as refresh an existing one. This
command performs local structural extraction and writes under the project's
`graphify-out/` directory:

- `graph.json`: nodes, relationships, and source locations.
- `GRAPH_REPORT.md`: communities and the most connected symbols.
- `graph.html`: interactive graph visualization.

Open `graphify-out/graph.html` in your browser. Index a **directory**, not just
the `.vbp` file. The scanner processes supported files beneath that directory;
`.vbp` membership adds project structure but does not limit the scan to listed
files or automatically load files outside the directory. Choose a common parent
directory if the project uses shared modules in sibling folders.

## Query and refresh

While in the VB6 project directory:

```powershell
.\.venv-graphify\Scripts\graphify.exe query 'Customer Save Validate'
.\.venv-graphify\Scripts\graphify.exe explain 'Form_Load'
.\.venv-graphify\Scripts\graphify.exe path 'Save' 'Validate'

# Refresh after changing VB6 source:
.\.venv-graphify\Scripts\graphify.exe update .

# Re-extract after changing the extractor or replacing an older Apex-based graph:
.\.venv-graphify\Scripts\graphify.exe update . --force
```

Use names that exist in your own project in place of the sample symbols.
On macOS/Linux, substitute `.venv-graphify/bin/graphify` for the executable.
`--force` also permits replacing an existing graph with a smaller result.
Back up an existing `graphify-out/` first if you want to retain the previous graph.

For a large project, skip clustering/HTML and produce just the raw index:

```powershell
.\.venv-graphify\Scripts\graphify.exe update . --no-cluster
```

Afterwards, a regular `update .` can build the report and visualization. Existing
`.gitignore` rules are respected. Add a `.graphifyignore` in the target root to
exclude backup copies or generated source, for example:

```gitignore
backups/**
archive/**
generated/**
```

## Upgrade the project's extractor

Update the pinned commit or tag in `requirements-graphify.txt`, then run from
the VB6 project root:

```powershell
.\.venv-graphify\Scripts\python.exe -m pip install --upgrade -r requirements-graphify.txt
.\.venv-graphify\Scripts\graphify.exe update . --force
```

Force a rebuild after extractor upgrades so an existing index is regenerated
with the new extraction behavior.

## Distribute a wheel instead of installing from Git

A maintainer can package this fork once, **from the Graphify source checkout**:

```powershell
py -3.11 -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install build
.\.venv-build\Scripts\python.exe -m build --wheel
```

Give users the resulting `.whl` from `dist/`. In their VB6 project, they put it
under `vendor/` and use a relative entry in `requirements-graphify.txt`, for
example with this checkout's current version:

```text
./vendor/graphifyy-0.9.61-py3-none-any.whl
```

Use the actual wheel filename if the version changes. The installation and
indexing commands above stay the same. pip installs the wheel's dependencies
automatically; users need Python but not Git or a Graphify source checkout.
Include the wheel in your project's distribution if using this option.

## VB6 coverage

| Files | Indexed content |
| --- | --- |
| `.vbp` | Project membership; `Reference=` and `Object=` dependencies as reference nodes. |
| `.bas` | Module, procedures, functions, constants, variables, types, enums, events, and `Declare` declarations. |
| `.cls` | Class and members; `Property Get/Let/Set` as separate nodes; `Implements` references. |
| `.frm` | Form and its executable code, including event-handler procedures. |

`.cls` deliberately means VB6 in this fork. Apex `.cls` dispatch is replaced;
the standalone Apex extractor and `.trigger` dispatch remain available.

The scanner handles case-insensitive names, quoted strings, apostrophe/`Rem`
comments, colon-separated statements, and `_` line continuations. Source locations
refer to original physical lines (continued statements use the first line).
It reads UTF-8, BOM-marked UTF-16, and Windows-1252. Convert projects using another
ANSI code page to UTF-8 before indexing.

### Current limits

- Designer blocks, visual properties, control nodes, and binary `.frx` resources
  are skipped. A `cmdSave_Click` procedure is indexed, but no control-to-handler
  edge is created.
- Call edges bind only unambiguous `Sub`, `Function`, and `Declare` targets in the
  same file. Cross-file calls, object/member dispatch, property-access calls,
  `With` receivers, default members, and COM/late binding are not resolved.
- `Implements` records the named interface as an unresolved reference; it does
  not yet link to the interface class's definition in another file.
- Both branches of conditional compilation are indexed. The scanner does not
  evaluate `#If` expressions; duplicate procedure names remain ambiguous.
- Missing/outside-root `.vbp` members appear as file references only. Other VB6
  source formats such as `.ctl`, `.pag`, `.dob`, and `.dsr` are not parsed yet.
- This is structural indexing, not a compiler or a complete VB6 call graph.

## Extractor development (maintainers only)

To work on this fork itself, from a Graphify checkout:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . pytest
.\.venv\Scripts\graphify.exe update tests/fixtures/vb6
.\.venv\Scripts\graphify.exe query 'Customer Save Validate' --graph tests/fixtures/vb6/graphify-out/graph.json
.\.venv\Scripts\python.exe -m pytest tests/test_vb6.py tests/test_extractors_registry.py -q
```

This development workflow is separate from the project-local installation that
VB6 project contributors use.
