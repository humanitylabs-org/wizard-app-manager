import { describe, expect, it } from 'vitest';
import * as yaml from 'yaml';
import { addStopInit, startsPythonAsPid1 } from '../wizard-stop-init';

// wizard-app-manager#738: a Python first process ignores `docker stop`, so it waited out Docker's 10 s timeout.
const compose = (services: Record<string, unknown>) => yaml.stringify({ services, 'x-runtipi': { schema_version: 2 } });

describe('Docker init for Python first processes (#738)', () => {
  it('adds init to a Wizard MCP front started with python -c', () => {
    const out = yaml.parse(addStopInit(compose({ front: { image: 'python:3.12-alpine', command: ['python', '-c', 'print(1)'] } })));
    expect(out.services.front.init).toBe(true);
    expect(out.services.front.command).toEqual(['python', '-c', 'print(1)']);
  });

  it('covers python3, versioned and absolute pythons and the string form', () => {
    for (const command of [['python3', 'app.py'], ['/usr/local/bin/python3.12', '-m', 'srv'], 'python3 -u server.py', ['python3.11']]) {
      expect(startsPythonAsPid1({ command })).toBe(true);
    }
  });

  it('leaves other services, image entrypoints and an explicit init alone', () => {
    expect(startsPythonAsPid1({ command: ['node', 'server.js'] })).toBe(false);
    expect(startsPythonAsPid1({ command: ['sh', '-c', 'python app.py'] })).toBe(false);
    expect(startsPythonAsPid1({ command: ['pythonista'] })).toBe(false);
    expect(startsPythonAsPid1({})).toBe(false);
    expect(startsPythonAsPid1({ command: ['python', 'x.py'], entrypoint: ['/init'] })).toBe(false);
    expect(startsPythonAsPid1({ command: ['python', 'x.py'], init: false })).toBe(false);

    const text = compose({
      engine: { image: 'chromedp/headless-shell', init: true },
      web: { image: 'nginx' },
      own: { image: 'python:3.12', command: ['python', 'a.py'], init: false },
    });
    expect(addStopInit(text)).toBe(text);
  });

  it('changes only the Python service and keeps everything else', () => {
    const text = compose({
      front: { image: 'python:3.12-alpine', command: ['python', '-c', 'x'], 'x-runtipi': { is_main: true } },
      engine: { image: 'upstream', command: ['serve'] },
    });
    const out = yaml.parse(addStopInit(text));
    expect(out.services.front).toEqual({ image: 'python:3.12-alpine', command: ['python', '-c', 'x'], 'x-runtipi': { is_main: true }, init: true });
    expect(out.services.engine).toEqual({ image: 'upstream', command: ['serve'] });
    expect(out['x-runtipi']).toEqual({ schema_version: 2 });
  });

  it('returns unparseable-as-services input unchanged', () => {
    expect(addStopInit('version: 3\n')).toBe('version: 3\n');
  });
});
