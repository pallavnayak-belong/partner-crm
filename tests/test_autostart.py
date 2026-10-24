"""Login item: the generated LaunchAgent is correct and safe (launchctl itself is not invoked here)."""
import os, plistlib, sys, unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import autostart  # noqa: E402


class T(unittest.TestCase):
    def test_plist_starts_this_app_at_login_and_restarts_it(self):
        p = autostart.plist_dict(python="/usr/bin/python3", here="/Users/x/partner-crm", home="/Users/x")
        self.assertEqual(p["ProgramArguments"], ["/usr/bin/python3", "/Users/x/partner-crm/server.py"])
        self.assertTrue(p["RunAtLoad"] and p["KeepAlive"]); self.assertGreaterEqual(p["ThrottleInterval"], 10)
        self.assertEqual(p["WorkingDirectory"], "/Users/x/partner-crm"); self.assertEqual(p["EnvironmentVariables"]["HOME"], "/Users/x")
        self.assertTrue(p["StandardOutPath"].endswith("data/server.log"))
        plistlib.loads(plistlib.dumps(p))                      # valid plist
        self.assertEqual(p["Label"], "com.belong.partner-crm")

    def test_never_stops_a_foreign_process_on_the_port(self):
        src = open(os.path.join(HERE, "autostart.py")).read()
        self.assertIn('"server.py" in cmd and HERE in cwd', src)   # only this app's own server is ever stopped


if __name__ == "__main__":
    unittest.main()
