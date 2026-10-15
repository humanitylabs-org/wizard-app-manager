"""Strict opt-in voice manifest contract; run directly or with unittest."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('checker', ROOT / 'wam/wizardmd.py')
assert spec is not None and spec.loader is not None
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

class VoiceInput(unittest.TestCase):
    def rules(self, value: object='missing', view=True):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            d = {'wizard':0.2,'id':'voice-test','type':'service'}
            if value != 'missing': d['receives_voice_input']=value
            if view: d['view']={'path':'/','port':8080}
            (p/'wizard.md').write_text('---\n'+yaml.safe_dump(d)+'---\n')
            return {name:ok for name,ok,_ in checker.check(p) if 'voice' in name}
    def test_absent(self): self.assertEqual(self.rules(), {})
    def test_true_view(self): self.assertTrue(all(self.rules(True).values()))
    def test_false_no_view(self): self.assertTrue(all(self.rules(False,False).values()))
    def test_true_needs_view(self): self.assertFalse(self.rules(True,False)['voice input has a declared view'])
    def test_strict_boolean(self):
        for v in ['true',1,0,None,[],{}]:
            with self.subTest(v=v): self.assertFalse(self.rules(v)['receives_voice_input is boolean'])

if __name__=='__main__': unittest.main()
