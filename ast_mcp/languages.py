"""Language registry — the single source of extension/filename -> language
routing (SPEC §V.6, §I.langs).

Adding a language is one `LangSpec` row plus either a query file (for the
``symbols``/``defs`` profiles) or a ``NODE_KINDS`` entry (for ``schema`` and
``outline``). Nothing else in the codebase branches on language.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from tree_sitter import Language

QUERY_ROOT = Path(__file__).parent / "queries"

GROUPS = ("core", "web", "scripting", "data", "devops", "docs")
PROFILES = ("symbols", "defs", "schema", "outline")


@dataclass(frozen=True, slots=True)
class LangSpec:
    """One row of the registry."""

    lang: str
    exts: tuple[str, ...]
    group: str
    profile: str
    source: str
    filenames: tuple[str, ...] = ()

    @property
    def query_path(self) -> Path | None:
        """Location of this language's .scm file, or None for table-driven
        profiles (``schema``/``outline`` walk by node type instead)."""
        if self.profile == "symbols":
            return QUERY_ROOT / "core" / f"{self.lang}.scm"
        if self.profile == "defs":
            return QUERY_ROOT / "defs" / f"{self.lang}.scm"
        return None


# --- group: core -- profile: symbols ----------------------------------------
_CORE = (
    LangSpec("python", (".py", ".pyi"), "core", "symbols",
             "tree_sitter_python.language"),
    LangSpec("javascript", (".js", ".mjs", ".cjs", ".jsx"), "core", "symbols",
             "tree_sitter_javascript.language"),
    LangSpec("typescript", (".ts", ".mts", ".cts"), "core", "symbols",
             "tree_sitter_typescript.language_typescript"),
    LangSpec("tsx", (".tsx",), "core", "symbols",
             "tree_sitter_typescript.language_tsx"),
    LangSpec("go", (".go",), "core", "symbols",
             "tree_sitter_go.language"),
    LangSpec("lua", (".lua",), "core", "symbols",
             "tree_sitter_lua.language"),
)

# --- group: scripting -- profile: defs --------------------------------------
_SCRIPTING = (
    LangSpec("ruby", (".rb", ".rake", ".gemspec"), "scripting", "defs", "pack:ruby"),
    LangSpec("perl", (".pl", ".pm", ".t"), "scripting", "defs", "pack:perl"),
    LangSpec("r", (".R", ".r"), "scripting", "defs", "pack:r"),
    LangSpec("bash", (".sh", ".bash"), "scripting", "defs", "pack:bash"),
    LangSpec("zsh", (".zsh", ".zshrc"), "scripting", "defs", "pack:zsh"),
)

# --- group: web -- mixed profiles -------------------------------------------
_WEB = (
    LangSpec("html", (".html", ".htm"), "web", "outline", "pack:html"),
    LangSpec("css", (".css",), "web", "defs", "pack:css"),
    LangSpec("scss", (".scss",), "web", "defs", "pack:scss"),
)

# --- group: data -- profile: schema -----------------------------------------
_DATA_SCHEMA = (
    LangSpec("json", (".json",), "data", "schema", "pack:json"),
    LangSpec("json5", (".json5",), "data", "schema", "pack:json5"),
    LangSpec("yaml", (".yaml", ".yml"), "data", "schema", "pack:yaml"),
    LangSpec("toml", (".toml",), "data", "schema", "pack:toml"),
    LangSpec("xml", (".xml", ".xsd", ".svg"), "data", "schema", "pack:xml"),
    LangSpec("csv", (".csv",), "data", "schema", "pack:csv"),
)

# --- group: data -- profile: defs -------------------------------------------
# Schema *languages*, not schema *files*: they have real named definitions.
_DATA_DEFS = (
    LangSpec("sql", (".sql",), "data", "defs", "pack:sql"),
    LangSpec("graphql", (".graphql", ".gql"), "data", "defs", "pack:graphql"),
    LangSpec("proto", (".proto",), "data", "defs", "pack:proto"),
)

# --- group: devops -- profile: defs -----------------------------------------
_DEVOPS = (
    LangSpec("terraform", (".tf", ".tfvars", ".hcl"), "devops", "defs",
             "pack:terraform"),
    LangSpec("dockerfile", (".dockerfile",), "devops", "defs", "pack:dockerfile",
             filenames=("Dockerfile", "Containerfile")),
)

# --- group: docs -- profile: outline ----------------------------------------
_DOCS = (
    LangSpec("markdown", (".md", ".markdown"), "docs", "outline", "pack:markdown"),
)

LANGS: tuple[LangSpec, ...] = (
    _CORE + _SCRIPTING + _WEB + _DATA_SCHEMA + _DATA_DEFS + _DEVOPS + _DOCS
)

BY_LANG: dict[str, LangSpec] = {s.lang: s for s in LANGS}
BY_EXT: dict[str, LangSpec] = {e: s for s in LANGS for e in s.exts}
BY_FILENAME: dict[str, LangSpec] = {f: s for s in LANGS for f in s.filenames}


def spec_for_path(path: str | Path) -> LangSpec | None:
    """Resolve a path to its registry row. Exact filename wins over extension.

    Returns None for unsupported paths — callers surface that as an
    ``unsupported_lang`` error, never an exception (SPEC §V.5).
    """
    p = Path(path)
    hit = BY_FILENAME.get(p.name)
    if hit is not None:
        return hit
    return BY_EXT.get(p.suffix)


@lru_cache(maxsize=None)
def load_language(lang: str) -> Language | None:
    """Load a grammar. Dedicated package first, language-pack second, None last.

    Never raises (SPEC §V.5): a missing or broken grammar is reported by the
    caller as ``no_grammar``.
    """
    spec = BY_LANG.get(lang)
    if spec is None:
        return None
    loaded = _load_source(spec.source)
    if loaded is None and not spec.source.startswith("pack:"):
        loaded = _load_source(f"pack:{spec.lang}")
    return loaded


def _load_source(source: str) -> Language | None:
    try:
        if source.startswith("pack:"):
            from tree_sitter_language_pack import get_language

            obj = get_language(source[len("pack:"):])
        else:
            module_name, _, attr = source.rpartition(".")
            obj = getattr(importlib.import_module(module_name), attr)()
    except Exception:
        return None
    return obj if isinstance(obj, Language) else Language(obj)
