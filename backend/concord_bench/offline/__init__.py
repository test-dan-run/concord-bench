"""Offline kits (OFFLINE_EXPORT_PLAN.md): Concord and chosen models for a machine without internet.

- `export`: runs the platform's `tools/bundle.py` (images, weights, a trimmed platform tree), saves the
  Concord image for a full kit, then writes `concord/compose.yml`, `install.sh`, README and `manifest.json`.
- `kit`: the kit layout, checksums and `verify-kit`, and the files Concord writes into a kit.
- `merge`: `install.sh --add`, a model kit merged into an existing offline install.
"""
