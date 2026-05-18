"""Worker backend protocol + Dask implementation.

The Protocol defines the 4-method interface that the Supervisor uses to
manage compute workers.  The Supervisor owns the *when* (monitor, policy,
cooldown); the backend owns the *how* (spawn, retire, query).

Swapping compute engines (Ray, Celery, etc.) means implementing a new
backend — the supervisor, monitor, and scaling policy stay untouched.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Protocol — the seam between supervisor and compute engine
# ---------------------------------------------------------------------------

@runtime_checkable
class WorkerBackend(Protocol):
    """Abstract interface for managing compute workers."""

    async def spawn(self, memory_limit: str, threads: int) -> str:
        """Start one worker.  Returns its address (opaque string)."""
        ...

    async def retire(self, address: str | None = None) -> str | None:
        """Gracefully remove one worker.  If address is None, pick one.
        Returns the address of the retired worker, or None if nothing to retire."""
        ...

    async def worker_info(self) -> dict[str, dict[str, Any]]:
        """Return {address: {processing: int, ...}} for all live workers."""
        ...

    async def close(self) -> None:
        """Disconnect from the scheduler / clean up resources."""
        ...


# ---------------------------------------------------------------------------
# Dask backend — manages workers via distributed.Nanny
# ---------------------------------------------------------------------------

class DaskBackend:
    """Dask implementation of WorkerBackend.

    Connects to a Dask scheduler and manages Nanny processes on the local host.
    """

    def __init__(self, scheduler_address: str) -> None:
        self._scheduler_address = scheduler_address
        self._client = None
        self._nannies: list = []  # list[Nanny]

    async def connect(self) -> None:
        """Establish connection to the Dask scheduler."""
        from distributed import Client
        self._client = await Client(
            self._scheduler_address,
            asynchronous=True,
            name="dorian-worker-supervisor",
        )

    async def spawn(self, memory_limit: str, threads: int) -> str:
        from distributed.nanny import Nanny
        nanny = await Nanny(
            scheduler_ip=self._scheduler_address,
            memory_limit=memory_limit,
            nthreads=threads,
        )
        self._nannies.append(nanny)
        return nanny.worker_address or f"nanny-{id(nanny)}"

    async def retire(self, address: str | None = None) -> str | None:
        if not self._nannies:
            return None

        if address is not None:
            target = next(
                (n for n in self._nannies if n.worker_address == address),
                None,
            )
            if target is None:
                return None
        else:
            target = self._nannies[-1]

        addr = target.worker_address
        try:
            await target.close()
        except Exception:
            pass
        self._nannies.remove(target)
        return addr

    async def worker_info(self) -> dict[str, dict[str, Any]]:
        if self._client is None:
            return {}
        try:
            info = self._client.scheduler_info()
            return {
                addr: {"processing": len(w.get("processing", {}))}
                for addr, w in info.get("workers", {}).items()
            }
        except Exception:
            return {}

    async def close(self) -> None:
        for nanny in list(self._nannies):
            try:
                await nanny.close()
            except Exception:
                pass
        self._nannies.clear()

        if self._client:
            await self._client.close()
            self._client = None

    @property
    def worker_count(self) -> int:
        return len(self._nannies)
