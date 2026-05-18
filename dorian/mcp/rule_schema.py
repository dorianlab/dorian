"""
dorian.mcp.rule_schema
-----------------------
Pydantic v2 models for validating JSON rule specifications.

This module is a **shape + safety** gate that runs before ``compile_rule()``
(which handles semantic validation like cross-references).  It enforces:

- Correct types and required fields
- Size limits (nodes, transformations, string lengths)
- Regex compilability and ReDoS resistance
- Bounded recursion depth for ``concat`` value expressions
"""
from __future__ import annotations

import re
import logging
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_log = logging.getLogger(__name__)

# ── Limits ────────────────────────────────────────────────────────────────

MAX_REGEX_LEN = 200
MAX_TEXT_REGEX_LEN = 500
MAX_PATTERN_NODES = 50
MAX_PATTERN_EDGES = 100
MAX_TRANSFORMATIONS = 20
MAX_CONCAT_ITEMS = 10
MAX_CONCAT_DEPTH = 5
MAX_DESCRIPTION_LEN = 500
MAX_NAME_LEN = 200

# Detects nested quantifiers — classic ReDoS signature: (a+)+, (a*)*,
# (a|b+)*, etc.
_REDOS_PATTERN = re.compile(
    r"\("          # opening paren
    r"[^)]*"       # anything inside
    r"[+*]"        # inner quantifier
    r"\)"          # closing paren
    r"[+*]"        # outer quantifier
)


# ── Helpers ───────────────────────────────────────────────────────────────

def _validate_regex(value: str, max_len: int, field_name: str) -> str:
    """Validate a regex pattern for compilability and ReDoS safety."""
    if not value:
        return value
    if len(value) > max_len:
        raise ValueError(
            f"{field_name} regex exceeds max length ({len(value)} > {max_len})"
        )
    try:
        re.compile(value)
    except re.error as e:
        raise ValueError(f"{field_name} is not a valid regex: {e}") from e
    if _REDOS_PATTERN.search(value):
        raise ValueError(
            f"{field_name} contains a nested-quantifier pattern that risks "
            f"catastrophic backtracking (ReDoS)"
        )
    return value


def _check_value_depth(value: Any, depth: int = 0) -> None:
    """Recursively check that a value expression doesn't exceed depth limit."""
    if depth > MAX_CONCAT_DEPTH:
        raise ValueError(
            f"concat nesting exceeds maximum depth of {MAX_CONCAT_DEPTH}"
        )
    if isinstance(value, dict) and "concat" in value:
        items = value["concat"]
        if not isinstance(items, list):
            raise ValueError("concat must be a list")
        if len(items) > MAX_CONCAT_ITEMS:
            raise ValueError(
                f"concat has too many items ({len(items)} > {MAX_CONCAT_ITEMS})"
            )
        for item in items:
            _check_value_depth(item, depth + 1)


# ═══════════════════════════════════════════════════════════════════════════
# Pattern models
# ═══════════════════════════════════════════════════════════════════════════

class PatternNodeSpec(BaseModel):
    """A single node in the match pattern."""
    model_config = ConfigDict(extra="forbid")

    type: str = ".*"
    text: str = ".*"
    language: str = ".*"

    @field_validator("type")
    @classmethod
    def _validate_type_regex(cls, v: str) -> str:
        return _validate_regex(v, MAX_REGEX_LEN, "type")

    @field_validator("text")
    @classmethod
    def _validate_text_regex(cls, v: str) -> str:
        return _validate_regex(v, MAX_TEXT_REGEX_LEN, "text")

    @field_validator("language")
    @classmethod
    def _validate_language_len(cls, v: str) -> str:
        if len(v) > 50:
            raise ValueError("language exceeds max length of 50")
        return v


class PatternEdgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    destination: str


class PatternSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: dict[str, PatternNodeSpec] = Field(min_length=1, max_length=MAX_PATTERN_NODES)
    edges: list[PatternEdgeSpec] = Field(default_factory=list, max_length=MAX_PATTERN_EDGES)


# ═══════════════════════════════════════════════════════════════════════════
# Value expression models (for update_attribute)
# ═══════════════════════════════════════════════════════════════════════════

class ValueRef(BaseModel):
    """Reference to a matched node's attribute."""
    model_config = ConfigDict(extra="forbid")

    ref: str
    attr: str


class ConcatValue(BaseModel):
    """Concatenation of value expressions."""
    model_config = ConfigDict(extra="forbid")

    concat: list[Any] = Field(max_length=MAX_CONCAT_ITEMS)

    @model_validator(mode="before")
    @classmethod
    def _check_depth(cls, data: Any) -> Any:
        if isinstance(data, dict) and "concat" in data:
            _check_value_depth(data)
        return data


# ═══════════════════════════════════════════════════════════════════════════
# Transformation models — discriminated union on "type"
# ═══════════════════════════════════════════════════════════════════════════

def _check_edge_tuples(v: list[list[str]]) -> list[list[str]]:
    """Validate that each edge is a 2-element [source, destination] array."""
    for i, edge in enumerate(v):
        if len(edge) != 2:
            raise ValueError(
                f"edges[{i}] must be a 2-element [source, destination] array"
            )
    return v


class DeleteTransformation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["delete"]
    nodes: list[str] = Field(default_factory=list, max_length=MAX_PATTERN_NODES)
    edges: list[list[str]] = Field(default_factory=list, max_length=MAX_PATTERN_EDGES)
    mode: Literal["isolated", "cascade"] = "isolated"

    @field_validator("edges")
    @classmethod
    def _validate_edge_tuples(cls, v: list[list[str]]) -> list[list[str]]:
        return _check_edge_tuples(v)


class UpdateAttributeTransformation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["update_attribute"]
    target: str
    attribute: str
    value: Any  # str | ValueRef | ConcatValue — validated below

    @model_validator(mode="after")
    def _validate_value_expr(self) -> UpdateAttributeTransformation:
        v = self.value
        if isinstance(v, str):
            return self
        if isinstance(v, dict):
            if "ref" in v and "attr" in v:
                ValueRef.model_validate(v)
                return self
            if "concat" in v:
                ConcatValue.model_validate(v)
                return self
            raise ValueError(
                "value dict must have 'ref'+'attr' or 'concat' key"
            )
        raise ValueError(
            f"value must be a string, ref object, or concat object, got {type(v).__name__}"
        )


_BACKREF_PATTERN = re.compile(r"\$\{?\d+\}?|\\[1-9]")


class ReplaceOperatorTransformation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["replace_operator"]
    target: str
    new_name: str = Field(max_length=MAX_NAME_LEN)

    @field_validator("new_name")
    @classmethod
    def no_backreferences(cls, v: str) -> str:
        if _BACKREF_PATTERN.search(v):
            raise ValueError(
                f"new_name {v!r} contains a regex backreference ($1, ${{1}}, \\1, etc.). "
                "new_name is a plain string — write the exact literal operator name you want, "
                "e.g. 'sklearn.preprocessing.StandardScaler'."
            )
        return v


class AddParameterTransformation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["add_parameter"]
    target: str
    param_name: str = Field(max_length=MAX_NAME_LEN)
    param_value: str = ""
    param_dtype: Literal["int", "float", "string", "eval"] = "eval"


class InsertBeforeTransformation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["insert_before"]
    target: str
    new_operator: str = Field(max_length=MAX_NAME_LEN)


class InsertAfterTransformation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["insert_after"]
    target: str
    new_operator: str = Field(max_length=MAX_NAME_LEN)


class AddEdgesTransformation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["add_edges"]
    edges: list[list[str]] = Field(max_length=MAX_PATTERN_EDGES)

    @field_validator("edges")
    @classmethod
    def _validate_edge_tuples(cls, v: list[list[str]]) -> list[list[str]]:
        return _check_edge_tuples(v)


TransformationSpec = Annotated[
    Union[
        DeleteTransformation,
        UpdateAttributeTransformation,
        ReplaceOperatorTransformation,
        AddParameterTransformation,
        InsertBeforeTransformation,
        InsertAfterTransformation,
        AddEdgesTransformation,
    ],
    Field(discriminator="type"),
]


# ═══════════════════════════════════════════════════════════════════════════
# Top-level rule spec
# ═══════════════════════════════════════════════════════════════════════════

class RuleSpec(BaseModel):
    """Complete JSON rule specification."""
    model_config = ConfigDict(extra="forbid")

    description: str = Field(
        default="LLM-generated rule", max_length=MAX_DESCRIPTION_LEN,
    )
    pattern: PatternSpec
    transformations: list[TransformationSpec] = Field(
        default_factory=list, max_length=MAX_TRANSFORMATIONS,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def _sanitize_pattern_edges(raw: dict) -> dict:
    """Strip fields the LLM copies from DAG JSON but are not in PatternEdgeSpec."""
    pattern = raw.get("pattern")
    if not isinstance(pattern, dict):
        return raw
    edges = pattern.get("edges")
    if not isinstance(edges, list):
        return raw
    for edge in edges:
        if isinstance(edge, dict):
            edge.pop("position", None)
            edge.pop("output", None)
    return raw


def validate_rule_spec(raw: dict) -> tuple[dict | None, list[str]]:
    """Validate a raw dict against the RuleSpec schema.

    Returns
    -------
    (validated_dict, errors)
        On success ``errors`` is empty and ``validated_dict`` is the
        normalised dict (safe to pass to ``compile_rule``).
        On failure ``validated_dict`` is ``None``.
    """
    try:
        raw = _sanitize_pattern_edges(raw)
        spec = RuleSpec.model_validate(raw)
        return spec.model_dump(), []
    except Exception as e:
        # Pydantic ValidationError or any other parse error
        from pydantic import ValidationError
        if isinstance(e, ValidationError):
            errors = [
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in e.errors()
            ]
        else:
            errors = [str(e)]
        return None, errors
