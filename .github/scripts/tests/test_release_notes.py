from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))

import release_notes  # noqa: E402


class ReleaseNotesTest(unittest.TestCase):
    def test_extracts_version_section_and_counts_mods(self) -> None:
        changelog = """**Changelog MC 1.21.8**

### **v0.6.3**

- Update mods
    - Sodium
    - Iris
    - Chat Heads

**Full Changelog**: compare

### **v0.6.2**

- Update mods
    - Old Mod
"""
        notes, count = release_notes.build_notes(changelog, "0.6.3", "1.21.8")
        self.assertEqual(count, 3)
        self.assertIn("### **v0.6.3**", notes)
        self.assertIn("Sodium", notes)
        self.assertNotIn("Old Mod", notes)

    def test_missing_section_is_empty(self) -> None:
        notes, count = release_notes.build_notes("### **v0.6.2**\n", "0.8.0", "26.4")
        self.assertEqual(count, 0)
        self.assertIn("No changelog section found", notes)

    def test_current_changelog_has_update_entries(self) -> None:
        changelog = (REPO_ROOT / "Packwiz" / "1.21.8" / "changelog.md").read_text(
            encoding="utf-8-sig"
        )
        notes, count = release_notes.build_notes(changelog, "0.6.2", "1.21.8")
        self.assertGreater(count, 0)
        self.assertIn("Sodium", notes)


if __name__ == "__main__":
    unittest.main()
