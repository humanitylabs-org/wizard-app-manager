import { Suspense, lazy } from 'react';

// Resources: disk, CPU and memory only. Everything else Runtipi kept under Settings is not needed for a Wizard.
const SystemStats = lazy(() => import('@/modules/dashboard/pages/dashboard').then((module) => ({ default: module.SystemStats })));

export default () => (
  <div className="card p-3">
    <Suspense>
      <SystemStats />
    </Suspense>
  </div>
);
