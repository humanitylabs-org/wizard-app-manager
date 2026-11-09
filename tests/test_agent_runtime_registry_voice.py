"""The installed host runtime imports this legacy WAM tree, not the newer API/CLI tree.

Exercise its actual cli.write_registry through owner + shared file boundaries, with
only synthetic installed rows and homes. No agent homes, daemon, Docker or provider.
"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from wam import cli, registry

class RuntimeWriterTests(unittest.TestCase):
    def test_real_driver_import_contract(self):
        # Keep the live import mechanism explicit; a patch only on a newer API tree is insufficient.
        self.assertEqual(Path(cli.__file__).parent, Path(registry.__file__).parent)
        self.assertTrue(callable(cli.write_registry))

    def test_reconstruction_and_shared_copy_with_changed_declaration(self):
        for declaration, expected in [('true',True),('false',False),('"true"',False),('1',False),('null',False),('',False)]:
            with self.subTest(declaration=declaration),tempfile.TemporaryDirectory() as td:
                root=Path(td);home=root/'owner';tipi=root/'tipi'
                app=tipi/'repos/wizardapps/apps/search-preview';app.mkdir(parents=True)
                field='receives_voice_input: '+declaration+'\n' if declaration else ''
                (app/'wizard.md').write_text('---\nid: search-preview\nname: Search\nversion: 0.3.3\ntype: service\nprovides: [web-search, image-search]\nview: {path: /}\n'+field+'---\n')
                (app/'.wizard-source').write_text(json.dumps({'repo':'local/search-preview','private':False}))
                (app/'.wizard-commit').write_text('11a86e0a9029ee1fd95499d33a1be4fdf030c8d6\n')
                row={'info':{'urn':'search-preview:wizardapps'},'app':{'status':'running','port':19289}}
                c=SimpleNamespace(home=home,runtipi_root=tipi,host_ip=lambda:'127.0.0.1')
                rt=SimpleNamespace(installed=lambda:[row])
                # Only external inputs are replaced. Real build, both write passes,
                # slots projection, routes projection and shared-copy run unchanged.
                with patch.object(cli.alongside,'external_slots',return_value={}),patch('wam.links.apply_to_registry'):
                    for _ in range(2):
                        result=cli.write_registry(c,rt)
                        self.assertIs(result['apps'][0]['receives_voice_input'],expected)
                        for path in [home/'apps.json',tipi/'app-data/wizard-os/apps.json']:
                            item=json.loads(path.read_text())['apps'][0]
                            self.assertIs(item['receives_voice_input'],expected)
                            self.assertEqual(item['id'],'search-preview');self.assertEqual(item['name'],'Search')
                            self.assertEqual(item['commit'],'11a86e0a9029ee1fd95499d33a1be4fdf030c8d6')
                    # Old true must not stick after declaration removal.
                    (app/'wizard.md').write_text('---\nid: search-preview\nname: Search\n---\n')
                    self.assertIs(cli.write_registry(c,rt)['apps'][0]['receives_voice_input'],False)

if __name__=='__main__':unittest.main()
