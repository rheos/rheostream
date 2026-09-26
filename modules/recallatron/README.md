# Recallatron

Durable memory, provenance, retrieval, correction, and supersession: the first
product module. The Python package is `rheo_recallatron`; its read-only screens are
in `web/`. The [memory architecture](../../docs/architecture/memory.md) is the full
design. Domain records remain authoritative in their owning modules.

Memory and derived indexes inherit source permissions and retention. The core's
audit records and durable operations must work when Recallatron is absent.
