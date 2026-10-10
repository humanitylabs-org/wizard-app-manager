import { getInstalledAppsOptions } from '@/api-client/@tanstack/react-query.gen';
import { EmptyPage } from '@/components/empty-page/empty-page';
import { MyAppsView } from '@/wizard-views/views';
import { useSuspenseQuery } from '@tanstack/react-query';

// My Apps is the Wizard UX launcher sheet (see src/wizard-views).
export default () => {
  const { data } = useSuspenseQuery({ ...getInstalledAppsOptions() });
  if (data.installed.length === 0) {
    return <EmptyPage title="MY_APPS_EMPTY_TITLE" subtitle="MY_APPS_EMPTY_SUBTITLE" redirectPath="/app-store" actionLabel="MY_APPS_EMPTY_ACTION" />;
  }
  return (
    <div data-testid="apps-list">
      <MyAppsView />
    </div>
  );
};
