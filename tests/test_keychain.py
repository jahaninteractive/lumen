import subprocess
import unittest

from lumen import keychain


def runner(returncode=0, stdout=""):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout, "")
    return run, calls


class KeychainTests(unittest.TestCase):
    def test_set_key_never_puts_the_key_on_a_command_line(self):
        run, calls = runner()
        keychain.set_key("online", "sk-abc12345", run=run)
        argv, kwargs = calls[0]
        self.assertEqual(["/usr/bin/security", "-i"], argv)
        self.assertNotIn("sk-abc12345", " ".join(argv))
        self.assertEqual('add-generic-password -U -s Lumen -a online -w "sk-abc12345"\n', kwargs["input"])
        self.assertIn("timeout", kwargs)

    def test_set_key_refuses_quotes_spaces_and_short_keys(self):
        run, calls = runner()
        for key in ('a"b; rm -rf ~', "has space in it", "short", "x" * 513, "line\nbreak12"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "that does not look like an API key") as caught:
                keychain.set_key("base", key, run=run)
            self.assertNotIn(key, str(caught.exception))
        self.assertEqual([], calls)

    def test_set_key_failure_does_not_echo_the_key(self):
        run, _ = runner(returncode=1)
        with self.assertRaises(RuntimeError) as caught:
            keychain.set_key("base", "sk-abc12345", run=run)
        self.assertEqual("could not save the key to the Keychain", str(caught.exception))

    def test_get_key_reads_the_password(self):
        run, calls = runner(stdout="sk-abc12345\n")
        self.assertEqual("sk-abc12345", keychain.get_key("base", run=run))
        self.assertEqual(["/usr/bin/security", "find-generic-password", "-s", "Lumen", "-a", "base", "-w"], calls[0][0])

    def test_get_key_is_none_when_missing(self):
        run, _ = runner(returncode=44)
        self.assertIsNone(keychain.get_key("online", run=run))

    def test_delete_key_ignores_a_missing_item(self):
        run, calls = runner(returncode=44)
        keychain.delete_key("online", run=run)
        self.assertEqual(["/usr/bin/security", "delete-generic-password", "-s", "Lumen", "-a", "online"], calls[0][0])

    def test_only_base_and_online_have_keys(self):
        run, calls = runner()
        for call in (lambda: keychain.get_key("mac", run=run), lambda: keychain.set_key("mac", "sk-abc12345", run=run),
                     lambda: keychain.delete_key("mac", run=run)):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
