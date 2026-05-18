"""Tenant context for multi-tenancy support.

Provides a ``contextvars``-based tenant context that threads through async
call chains without explicit parameter passing.  This is the foundation for
per-tenant data isolation (KB, Redis keys, storage paths).

Usage::

    from dorian.infra.tenant import tenant_ctx, current_tenant, Tenant

    # Set tenant for the current async task / thread
    with tenant_ctx(Tenant(id="acme", kb_db="dorian_acme")):
        assert current_tenant().id == "acme"

    # Or set directly (useful in middleware)
    token = set_tenant(Tenant(id="acme"))
    ...
    reset_tenant(token)

Design notes:
    - ``Tenant.id`` is the unique tenant identifier (maps to uid for now).
    - ``Tenant.kb_db`` is the Neo4j database name for tenant-specific KB
      (defaults to the shared ``"dorian"`` database).
    - ``Tenant.key_prefix`` is an optional Redis key prefix for namespace
      isolation (empty string = shared namespace, current default).
    - ``Tenant.storage_prefix`` is an optional storage key prefix for file
      isolation (empty string = shared namespace).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Tenant:
    """Immutable tenant descriptor."""

    id: str
    """Unique tenant identifier (e.g. organization slug or uid)."""

    kb_db: str = "dorian"
    """Neo4j database name for this tenant's knowledge base."""

    key_prefix: str = ""
    """Optional Redis key prefix for namespace isolation.

    When non-empty, ``RedisKeys.*`` helpers prepend this to all keys:
    ``"{key_prefix}:{original_key}"``.
    """

    storage_prefix: str = ""
    """Optional storage key prefix for file isolation.

    When non-empty, storage backends prepend this to all keys:
    ``"{storage_prefix}/{original_key}"``.
    """


# Default tenant — shared namespace, shared KB (single-tenant mode).
DEFAULT_TENANT = Tenant(id="__default__")

_tenant_var: ContextVar[Tenant] = ContextVar("tenant", default=DEFAULT_TENANT)


def current_tenant() -> Tenant:
    """Return the tenant for the current execution context."""
    return _tenant_var.get()


def set_tenant(tenant: Tenant) -> Token[Tenant]:
    """Set the tenant for the current context.  Returns a reset token."""
    return _tenant_var.set(tenant)


def reset_tenant(token: Token[Tenant]) -> None:
    """Reset the tenant to its previous value."""
    _tenant_var.reset(token)


@contextmanager
def tenant_ctx(tenant: Tenant):
    """Context manager that sets the tenant for the duration of a block."""
    token = set_tenant(tenant)
    try:
        yield tenant
    finally:
        reset_tenant(token)
