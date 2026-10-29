import Dockerode from 'dockerode';

const PROJECT = 'com.docker.compose.project';
const SERVICE = 'com.docker.compose.service';
export const appProject = (urn: string) => {
  if (!/^[a-z0-9][a-z0-9-]{0,62}:[a-z0-9][a-z0-9-]{0,62}$/.test(urn)) throw new Error('Invalid app identity');
  return urn.replace(':', '_');
};
const absent = (e: any) => e?.statusCode === 404;

/** Only daemon-stored identity is consulted. Never load a recipe, override, env,
 * or Compose model on a risk-reducing path. Compose owns its reserved project
 * label; the project is derived from the trusted installed app URN, not input YAML.
 * Full container IDs pin all operations even if names change. */
export async function teardownApp(
  urn: string,
  command: string,
  docker = new Dockerode({ timeout: 15_000 }),
) {
  const project = appProject(urn);
  const [verb, ...flags] = command.split(' ').filter(Boolean);
  if (!['down', 'stop'].includes(verb!) || flags.some((v) => !['--remove-orphans', '--volumes', '--rmi', 'all'].includes(v)))
    throw new Error('Unsupported teardown command');
  const filters = { label: [`${PROJECT}=${project}`] };
  const listed = await docker.listContainers({ all: true, filters });
  const images = new Set<string>();
  // Inspect ALL candidates before the first effect; refuse ambiguous custody.
  const targets = await Promise.all(listed.map(async (item) => {
    const container = docker.getContainer(item.Id);
    const info = await container.inspect();
    const labels = info.Config.Labels ?? {};
    // Nonreserved app labels can be overridden by a recipe. They must neither
    // expand custody nor prevent the owner stopping this reserved project.
    if (info.Id !== item.Id || labels[PROJECT] !== project || !labels[SERVICE])
      throw new Error('App container identity is ambiguous');
    images.add(info.Image);
    return { container, info };
  }));
  for (const { container, info } of targets) {
    if (info.State.Running || info.State.Paused || info.State.Restarting) {
      if (info.State.Paused) await container.unpause();
      try { await container.stop({ t: 10 }); } catch (e: any) { if (e.statusCode !== 304 && !absent(e)) throw e; }
    }
    if (verb === 'down') {
      try { await container.remove({ force: false, v: true }); } catch (e) { if (!absent(e)) throw e; }
      try { await container.inspect(); throw new Error('App container removal was not acknowledged'); }
      catch (e) { if (!absent(e)) throw e; }
    } else {
      const state = (await container.inspect()).State;
      if (state.Running || state.Restarting || state.Paused) throw new Error('App container stop was not acknowledged');
    }
  }
  if (verb === 'down') {
    for (const entry of await docker.listNetworks({ filters })) {
      const network = docker.getNetwork(entry.Id);
      const info = await network.inspect();
      if (info.Labels?.[PROJECT] !== project || !info.Labels?.['com.docker.compose.network']) throw new Error('App network identity is ambiguous');
      if (Object.keys(info.Containers ?? {}).length) throw new Error('App network still has attached containers');
      await network.remove();
    }
    if (flags.includes('--volumes')) {
      for (const entry of (await docker.listVolumes({ filters })).Volumes ?? []) {
        const volume = docker.getVolume(entry.Name);
        const info = await volume.inspect();
        if (info.Labels?.[PROJECT] !== project || !info.Labels?.['com.docker.compose.volume']) throw new Error('App volume identity is ambiguous');
        await volume.remove();
      }
    }
    // Never force image deletion: shared images must survive another app's use.
    if (flags.includes('--rmi')) for (const image of images) {
      if ((await docker.listContainers({ all: true, filters: { ancestor: [image] } })).length === 0)
        await docker.getImage(image).remove({ force: false }).catch((e: any) => { if (!absent(e) && e.statusCode !== 409) throw e; });
    }
  }
  const remaining = await docker.listContainers({ all: true, filters });
  if (verb === 'down' ? remaining.length > 0 : remaining.some((c) => ['running', 'restarting', 'paused'].includes(c.State)))
    throw new Error('App teardown readback failed');
  return { success: true, stdout: '', stderr: '' };
}
