"""The page asks "are you sure?" inside the page, never with a browser pop-up.

confirm(), alert() and prompt() froze Chrome on the owner's Mac, and an Enter
key press then answered "OK" and stopped the package. Same file in every AVADO
Bitcoin package (Bitcoin Node, Electrum Server, Bitcoin Explorer, Lightning,
Monero Node).
"""
import os
import re
import unittest

PUBLIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")
POPUP = re.compile(r"(?<![\w.$])(?:window\s*\.\s*)?(confirm|alert|prompt)\s*\(")


class NoBrowserPopupsTest(unittest.TestCase):
    def test_no_confirm_alert_or_prompt(self):
        for name in sorted(os.listdir(PUBLIC)):
            if name.endswith((".js", ".html")):
                with open(os.path.join(PUBLIC, name), encoding="utf-8") as f:
                    hit = POPUP.search(f.read())
                self.assertIsNone(hit, "%s calls %s()" % (name, hit and hit.group(1)))

    def test_pattern_catches_the_old_code(self):
        self.assertTrue(POPUP.search("if (spec.confirm && !window.confirm(spec.confirm)) return;"))
        self.assertTrue(POPUP.search("alert('x')"))
        self.assertIsNone(POPUP.search("askConfirm(button, spec); spec.confirm && x"))

    def test_asks_on_the_page_with_cancel_focused(self):
        with open(os.path.join(PUBLIC, "app.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("askConfirm(button, spec);", js)
        self.assertIn("runAction(button, spec, true);", js)
        # Enter or Escape must cancel, never confirm
        self.assertIn("no.focus();", js)
        self.assertIn('ev.key === "Escape"', js)


if __name__ == "__main__":
    unittest.main()
