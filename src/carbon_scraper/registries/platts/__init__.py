"""S&P Global "Carbon Registry" (Platts) — the platform, not one registry.

`registry.spglobal.com` serves a family of registries off a single backend at
`prod-us.api.platts.com`. Verra and Plan Vivo are two of them and differ only
in three header values, so the paging, partitioning and reconciliation live
here once and each registry is a thin subclass in its own package.
"""
