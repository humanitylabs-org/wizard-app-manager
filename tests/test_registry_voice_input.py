"""Only YAML true grants voice input; registry rewrites preserve that boolean."""
import json
from pathlib import Path
import tempfile
import unittest
from wam import registry

class RegistryVoiceInputTests(unittest.TestCase):
    def test_capability_survives_regeneration_and_removal(self):
        for declaration,expected in [('',False),('true',True),('false',False),('"true"',False),('1',False),('null',False)]:
            with self.subTest(declaration=declaration), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);tipi=root/'tipi';home=root/'home'
                app=tipi/'apps'/'wizardapps'/'search';app.mkdir(parents=True)
                field='receives_voice_input: '+declaration+'\n' if declaration else ''
                (app/'wizard.md').write_text('---\nid: search\nname: Search\nversion: 0.3.1\ntype: service\nprovides: [web-search]\nview: {path: /}\n'+field+'---\n')
                row={'info':{'urn':'search:wizardapps'},'app':{'status':'running','port':9288}}
                # Exercise both projection boundaries plus repeated on-disk writes.
                for _ in range(2):
                    reg=registry.build([row],'wizardapps','127.0.0.1',tipi)
                    self.assertIs(reg['apps'][0]['receives_voice_input'],expected)
                    self.assertIs(registry.public_projection(reg)['apps'][0]['receives_voice_input'],expected)
                    registry.write(reg,home)
                    self.assertIs(json.loads((home/'apps.json').read_text())['apps'][0]['receives_voice_input'],expected)
                # Regenerate after removing the declaration: never retain stale true.
                (app/'wizard.md').write_text('---\nid: search\nname: Search\n---\n')
                registry.write(registry.build([row],'wizardapps','127.0.0.1',tipi),home)
                self.assertIs(json.loads((home/'apps.json').read_text())['apps'][0]['receives_voice_input'],False)

if __name__=='__main__':unittest.main()
