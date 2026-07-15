"""Python standard-library AST extraction without importing target modules."""

import ast
import hashlib

from project_kb.indexing.identity import (
    OCCURRENCE_CONTRACT_VERSION,
    logical_symbol_key,
    occurrence_id,
)
from project_kb.indexing.models import (
    DiagnosticFact,
    ImportFact,
    RelationFact,
    SymbolFact,
)
from project_kb.indexing.module_map import ModuleIdentity, ModuleResolutionStatus

EXTRACTOR_NAME = "python-ast"
EXTRACTOR_VERSION = "1"


def stable_id(*parts: object) -> str:
    payload = "\0".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class PythonExtractor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        file_id: str,
        relative_path: str,
        source: str,
        line_count: int,
        snapshot_id: str | None = None,
        module_identity: ModuleIdentity | None = None,
    ) -> None:
        self.file_id = file_id
        self.relative_path = relative_path
        self.source = source
        self.line_count = max(1, line_count)
        self.snapshot_id = snapshot_id
        self.module_identity = module_identity
        self.symbols: list[SymbolFact] = []
        self.imports: list[ImportFact] = []
        self.relations: list[RelationFact] = []
        self.parents: list[SymbolFact] = []
        self.occurrence_ordinals: dict[tuple[object, ...], int] = {}

    def extract(
        self, tree: ast.AST
    ) -> tuple[list[SymbolFact], list[ImportFact], list[RelationFact]]:
        legacy_module_name = self._module_name()
        canonical_module_name = self._canonical_module_name()
        module_occurrence_id, module_ordinal = self._occurrence(
            entity_kind="MODULE",
            binding_role="MODULE",
            start_line=1,
            end_line=self.line_count,
            start_column=0,
            end_column=None,
        )
        module = SymbolFact(
            symbol_id=stable_id(self.file_id, "module"),
            file_id=self.file_id,
            qualified_name=legacy_module_name,
            short_name=legacy_module_name.rsplit(".", 1)[-1],
            symbol_kind="MODULE",
            start_line=1,
            end_line=self.line_count,
            start_column=0,
            end_column=None,
            parent_symbol_id=None,
            signature_text=None,
            occurrence_id=module_occurrence_id,
            logical_key=self._logical_key(
                canonical_qualified_name=canonical_module_name,
                entity_kind="MODULE",
                binding_role="MODULE",
            ),
            canonical_qualified_name=canonical_module_name,
            module_name=canonical_module_name,
            binding_role="MODULE",
            parent_occurrence_id=None,
            occurrence_ordinal=module_ordinal,
            occurrence_contract_version=(
                OCCURRENCE_CONTRACT_VERSION if module_occurrence_id is not None else None
            ),
        )
        self._add_symbol(module)
        self.parents.append(module)
        self.visit(tree)
        self.parents.pop()
        return self.symbols, self.imports, self.relations

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        symbol = self._definition(node, "CLASS" if len(self.parents) == 1 else "NESTED_CLASS")
        self._add_definition_relations(symbol, node)
        for base in node.bases:
            expression = _expression_text(self.source, base)
            self.relations.append(
                RelationFact(
                    relation_id=stable_id(symbol.symbol_id, "base", expression, node.lineno),
                    relation_kind="CLASS_INHERITS_EXPRESSION",
                    source_file_id=self.file_id,
                    source_symbol_id=symbol.symbol_id,
                    target_file_id=None,
                    target_symbol_id=None,
                    target_text=expression,
                    start_line=base.lineno,
                    end_line=getattr(base, "end_lineno", base.lineno),
                    start_column=base.col_offset,
                    end_column=getattr(base, "end_col_offset", None),
                    resolution_status="EXACT",
                    evidence_kind="AST_CLASS_BASE",
                )
            )
        self.parents.append(symbol)
        for child in node.body:
            self.visit(child)
        self.parents.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_async: bool
    ) -> None:
        parent_kind = self.parents[-1].symbol_kind
        if parent_kind in {"CLASS", "NESTED_CLASS"}:
            kind = "ASYNC_METHOD" if is_async else "METHOD"
        elif len(self.parents) > 1:
            kind = "NESTED_ASYNC_FUNCTION" if is_async else "NESTED_FUNCTION"
        else:
            kind = "ASYNC_FUNCTION" if is_async else "FUNCTION"
        symbol = self._definition(node, kind)
        self._add_definition_relations(symbol, node)
        self.parents.append(symbol)
        for child in node.body:
            self.visit(child)
        self.parents.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for index, alias in enumerate(node.names):
            self.imports.append(
                ImportFact(
                    import_id=stable_id(self.file_id, node.lineno, "import", index, alias.name),
                    file_id=self.file_id,
                    import_kind="IMPORT",
                    module_text=alias.name,
                    imported_name=None,
                    alias=alias.asname,
                    relative_level=0,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for index, alias in enumerate(node.names):
            self.imports.append(
                ImportFact(
                    import_id=stable_id(
                        self.file_id,
                        node.lineno,
                        "from",
                        index,
                        node.level,
                        node.module,
                        alias.name,
                    ),
                    file_id=self.file_id,
                    import_kind="IMPORT_FROM",
                    module_text=node.module or "",
                    imported_name=alias.name,
                    alias=alias.asname,
                    relative_level=node.level,
                    start_line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                )
            )

    def _definition(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, kind: str
    ) -> SymbolFact:
        parent = self.parents[-1]
        qualified = f"{parent.qualified_name}.{node.name}"
        canonical_qualified = (
            f"{parent.canonical_qualified_name}.{node.name}"
            if parent.canonical_qualified_name
            else None
        )
        signature = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            signature = f"{prefix} {node.name}({_safe_unparse(node.args)})"
            if node.returns is not None:
                signature += f" -> {_expression_text(self.source, node.returns)}"
        end_line = getattr(node, "end_lineno", node.lineno)
        end_column = getattr(node, "end_col_offset", None)
        symbol_occurrence_id, ordinal = self._occurrence(
            entity_kind=kind,
            binding_role="DECLARATION",
            start_line=node.lineno,
            end_line=end_line,
            start_column=node.col_offset,
            end_column=end_column,
        )
        return SymbolFact(
            symbol_id=stable_id(self.file_id, qualified, kind, node.lineno, node.col_offset),
            file_id=self.file_id,
            qualified_name=qualified,
            short_name=node.name,
            symbol_kind=kind,
            start_line=node.lineno,
            end_line=end_line,
            start_column=node.col_offset,
            end_column=end_column,
            parent_symbol_id=parent.symbol_id,
            signature_text=signature,
            occurrence_id=symbol_occurrence_id,
            logical_key=self._logical_key(
                canonical_qualified_name=canonical_qualified,
                entity_kind=kind,
                binding_role="DECLARATION",
            ),
            canonical_qualified_name=canonical_qualified,
            module_name=self._canonical_module_name(),
            binding_role="DECLARATION",
            parent_occurrence_id=parent.occurrence_id,
            occurrence_ordinal=ordinal,
            occurrence_contract_version=(
                OCCURRENCE_CONTRACT_VERSION if symbol_occurrence_id is not None else None
            ),
        )

    def _add_symbol(self, symbol: SymbolFact) -> None:
        self.symbols.append(symbol)
        self.relations.append(
            RelationFact(
                relation_id=stable_id(symbol.symbol_id, "defined"),
                relation_kind="FILE_DEFINES_SYMBOL",
                source_file_id=self.file_id,
                source_symbol_id=None,
                target_file_id=self.file_id,
                target_symbol_id=symbol.symbol_id,
                target_text=None,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                start_column=symbol.start_column,
                end_column=symbol.end_column,
                resolution_status="EXACT",
                evidence_kind="AST_DEFINITION",
            )
        )
        if symbol.parent_symbol_id is not None:
            self.relations.append(
                RelationFact(
                    relation_id=stable_id(symbol.parent_symbol_id, "contains", symbol.symbol_id),
                    relation_kind="SYMBOL_CONTAINS_SYMBOL",
                    source_file_id=self.file_id,
                    source_symbol_id=symbol.parent_symbol_id,
                    target_file_id=self.file_id,
                    target_symbol_id=symbol.symbol_id,
                    target_text=None,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    start_column=symbol.start_column,
                    end_column=symbol.end_column,
                    resolution_status="EXACT",
                    evidence_kind="AST_NESTING",
                )
            )

    def _add_definition_relations(
        self,
        symbol: SymbolFact,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._add_symbol(symbol)
        for index, decorator in enumerate(node.decorator_list):
            expression = _expression_text(self.source, decorator)
            self.relations.append(
                RelationFact(
                    relation_id=stable_id(symbol.symbol_id, "decorator", index, expression),
                    relation_kind="SYMBOL_HAS_DECORATOR",
                    source_file_id=self.file_id,
                    source_symbol_id=symbol.symbol_id,
                    target_file_id=None,
                    target_symbol_id=None,
                    target_text=expression,
                    start_line=getattr(decorator, "lineno", node.lineno),
                    end_line=getattr(decorator, "end_lineno", node.lineno),
                    start_column=getattr(decorator, "col_offset", node.col_offset),
                    end_column=getattr(decorator, "end_col_offset", None),
                    resolution_status="EXACT",
                    evidence_kind="AST_DECORATOR",
                )
            )

    def _module_name(self) -> str:
        path = self.relative_path.replace("\\", "/")
        if path.endswith("/__init__.py"):
            path = path[: -len("/__init__.py")]
        elif path.endswith(".py"):
            path = path[:-3]
        elif path.endswith(".pyi"):
            path = path[:-4]
        return path.replace("/", ".") or "__root__"

    def _canonical_module_name(self) -> str | None:
        if (
            self.module_identity is None
            or self.module_identity.resolution_status is not ModuleResolutionStatus.EXACT
        ):
            return None
        return self.module_identity.module_name

    def _logical_key(
        self,
        *,
        canonical_qualified_name: str | None,
        entity_kind: str,
        binding_role: str,
    ) -> str | None:
        module_name = self._canonical_module_name()
        if module_name is None or canonical_qualified_name is None:
            return None
        return logical_symbol_key(
            module_name=module_name,
            lexical_name=canonical_qualified_name,
            entity_kind=entity_kind,
            binding_role=binding_role,
        )

    def _occurrence(
        self,
        *,
        entity_kind: str,
        binding_role: str,
        start_line: int,
        end_line: int,
        start_column: int,
        end_column: int | None,
    ) -> tuple[str | None, int]:
        key = (
            entity_kind,
            binding_role,
            start_line,
            end_line,
            start_column,
            end_column,
        )
        ordinal = self.occurrence_ordinals.get(key, 0)
        self.occurrence_ordinals[key] = ordinal + 1
        if self.snapshot_id is None:
            return None, ordinal
        return (
            occurrence_id(
                snapshot_id=self.snapshot_id,
                relative_path=self.relative_path,
                entity_kind=entity_kind,
                binding_role=binding_role,
                start_line=start_line,
                end_line=end_line,
                start_column=start_column,
                end_column=end_column,
                ordinal=ordinal,
            ),
            ordinal,
        )


def extract_python(
    *,
    file_id: str,
    relative_path: str,
    source: str,
    line_count: int,
    snapshot_id: str | None = None,
    module_identity: ModuleIdentity | None = None,
) -> tuple[list[SymbolFact], list[ImportFact], list[RelationFact], list[DiagnosticFact]]:
    try:
        tree = ast.parse(source, filename=relative_path, type_comments=True)
    except SyntaxError as exc:
        return (
            [],
            [],
            [],
            [
                DiagnosticFact(
                    file_id=file_id,
                    diagnostic_kind="SYNTAX_ERROR",
                    message=exc.msg,
                    line=exc.lineno,
                    column=exc.offset,
                )
            ],
        )
    extractor = PythonExtractor(
        file_id=file_id,
        relative_path=relative_path,
        source=source,
        line_count=line_count,
        snapshot_id=snapshot_id,
        module_identity=module_identity,
    )
    symbols, imports, relations = extractor.extract(tree)
    return symbols, imports, relations, []


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except AttributeError, ValueError:
        return type(node).__name__


def _expression_text(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node)
    return segment.strip() if segment else _safe_unparse(node)
