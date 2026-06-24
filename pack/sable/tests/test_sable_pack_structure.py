"""Structure contract for the sable Gas City pack (SABLE-vj4x.1).

Asserts the bootstrapping skeleton:
  - pack.toml imports the gascity base as `gc` from ../gascity
  - README references the compatibility ledger (REQUIREMENTS.md)
  - the ledger carries the GC-METH-012 anchor sections + base references
  - the worker-protocol fragment is byte-identical to the gascity base fragment
    (checked when a gascity-packs checkout is available via GASCITY_PACKS_ROOT;
    default ~/dev-environment/gascity-packs)

Runnable with either `python3 -m pytest` or `python3 -m unittest`.
"""
from __future__ import annotations

import os
import pathlib
import tomllib
import unittest

PACK_ROOT = pathlib.Path(__file__).resolve().parents[1]
GASCITY_PACKS_ROOT = pathlib.Path(
    os.environ.get(
        "GASCITY_PACKS_ROOT",
        str(pathlib.Path.home() / "dev-environment" / "gascity-packs"),
    )
)
BASE_FRAGMENT = (
    GASCITY_PACKS_ROOT / "gascity" / "roles" / "prompts" / "shared" / "gc-role-worker.md.tmpl"
)

LEDGER_REQUIRED_FRAGMENTS = (
    "GC-METH-012",
    "## Compatibility Claims",
    "## Evidence Commands",
    "../gascity",
    "build-base",
)


class SablePackStructureTests(unittest.TestCase):
    def test_pack_toml_imports_gascity_base_as_gc(self) -> None:
        data = tomllib.loads((PACK_ROOT / "pack.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["pack"]["name"], "sable")
        self.assertEqual(data["imports"]["gc"]["source"], "../gascity")

    def test_readme_references_ledger(self) -> None:
        readme = (PACK_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("REQUIREMENTS.md", readme)

    def test_ledger_carries_required_fragments(self) -> None:
        ledger = (PACK_ROOT / "REQUIREMENTS.md").read_text(encoding="utf-8")
        for fragment in LEDGER_REQUIRED_FRAGMENTS:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, ledger)

    def test_worker_fragment_byte_identical_to_base(self) -> None:
        pack_fragment = PACK_ROOT / "template-fragments" / "gc-role-worker.template.md"
        self.assertTrue(
            pack_fragment.is_file(),
            f"missing pack worker fragment at {pack_fragment}",
        )
        if not BASE_FRAGMENT.is_file():
            self.skipTest(
                f"gascity base fragment not found at {BASE_FRAGMENT}; "
                "set GASCITY_PACKS_ROOT to a gascity-packs checkout"
            )
        self.assertEqual(
            pack_fragment.read_text(encoding="utf-8"),
            BASE_FRAGMENT.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
