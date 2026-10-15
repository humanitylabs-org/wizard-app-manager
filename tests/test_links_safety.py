import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import patch
from wam import links

REG = {"apps": [{"id": "app", "status": "running", "view": {"url": "http://127.0.0.1:9001/"}}]}

def test_conflicting_route_is_not_claimed_or_overwritten(tmp_path):
    status = {"TCP": {"9001": {"HTTPS": True}}, "Web": {"box.ts.net:9001": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7777"}}}}}
    with patch.object(links, 'tailnet', return_value={"state": "Running", "host": "box.ts.net"}), patch.object(links, 'serve_status', return_value=status), patch.object(links, '_run') as run:
        out = links.publish(REG, tmp_path)
    assert not out['links'] and out['errors']['app']
    run.assert_not_called()

def test_unreadable_config_is_not_empty(tmp_path):
    with patch.object(links, 'tailnet', return_value={"state": "Running", "host": "box.ts.net"}), patch.object(links, 'serve_status', side_effect=ValueError('unreadable')), patch.object(links, '_run') as run:
        out = links.publish(REG, tmp_path)
    assert not out['links'] and out['errors']['app']
    run.assert_not_called()

def test_tailscale_normalized_path_is_reused():
    config = {'TCP': {'9001': {'HTTPS': True}}, 'Web': {'box:9001': {'Handlers': {'/app': {'Proxy': 'http://127.0.0.1:9002'}}}}}
    assert links.route_status(config, 'box', 9001, '/app/', 'http://127.0.0.1:9002') == 'match'


def test_funnel_is_never_claimed_private():
    assert links.route_status({'AllowFunnel': {'box:9001': True}}, 'box', 9001, '/', 'x') == 'conflict'

def test_non_loopback_views_are_not_published():
    reg = copy.deepcopy(REG)
    reg['apps'][0]['view']['url'] = 'http://example.com:9001/'
    assert links.pages(reg) == []

def test_success_without_readback_is_not_claimed(tmp_path):
    with patch.object(links, 'tailnet', return_value={"state": "Running", "host": "box.ts.net"}), patch.object(links, 'serve_status', return_value={}), patch.object(links, '_run', return_value=subprocess.CompletedProcess([], 0, '', '')):
        out = links.publish(REG, tmp_path)
    assert not out['links'] and 'readback' in out['errors']['app']
