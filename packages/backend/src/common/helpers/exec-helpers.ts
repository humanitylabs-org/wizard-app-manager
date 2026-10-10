import { execFile, spawn } from 'node:child_process';
import type { ExecFileOptions, SpawnOptionsWithoutStdio } from 'node:child_process';
import { promisify } from 'node:util';

type ExecFileAsyncParams = [command: string, args?: string[], options?: ExecFileOptions];

type ExecResult = { stdout: string; stderr: string };

const promisifiedExecFile = promisify(execFile);

export const execFileAsync = async (...args: ExecFileAsyncParams): Promise<ExecResult> => {
  const [command, cmdArgs = [], options = {}] = args;

  if (!command) {
    throw new Error('Command cannot be empty');
  }

  try {
    const { stdout, stderr } = await promisifiedExecFile(command, cmdArgs, {
      ...options,
      encoding: 'utf-8',
    });

    return { stdout, stderr };
  } catch (error) {
    if (error instanceof Error) {
      return { stderr: error.message, stdout: '' };
    }

    return { stderr: String(error), stdout: '' };
  }
};

export const spawnAsync = async (command: string, args: string[], options: SpawnOptionsWithoutStdio = {}) =>
  new Promise<ExecResult>((resolve) => {
    const child = spawn(command, args, {
      ...options,
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let stdout = '';
    let stderr = '';

    child.stdout?.on('data', (data) => {
      stdout += data.toString();
    });

    child.stderr?.on('data', (data) => {
      stderr += data.toString();
    });

    child.on('error', (error) => {
      resolve({ stdout: '', stderr: error.message });
    });

    child.on('close', (code) => {
      if (code && code !== 0 && !stderr) {
        stderr = `Command failed with exit code ${code}`;
      }

      resolve({ stdout, stderr });
    });
  });
