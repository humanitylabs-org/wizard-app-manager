import { useUIStore } from '@/stores/ui-store';
import type { AppInfoSimple } from '@/types/app.types';
import { IconChevronLeft } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';

type Props = {
  apps: AppInfoSimple[];
};

/*
 * No heading on the three top-level views: the selected tab (or the Wizard OS window title) already says
 * where you are. Deeper pages (an app's details, update, edit) get a back link and the app's name.
 */
export const PageTitle = ({ apps }: Props) => {
  const { t } = useTranslation();
  useUIStore((state) => state.activeRoute); // re-render on navigation
  const parts = window.location.pathname.substring(1).split('/').filter(Boolean);

  if (parts.length < 2) return null;

  const inStore = parts[0] === 'app-store';
  const back = inStore ? '/app-store' : '/apps';
  const label = t(inStore ? 'HEADER_APP_STORE' : 'HEADER_APPS');
  const appId = parts[2] ?? parts[1] ?? '';
  const name = apps.find((app) => app.id.split('_')[0] === appId)?.name ?? appId.replace(/-/g, ' ');

  return (
    <div className="wam-title">
      <Link to={back} className="wam-back">
        <IconChevronLeft size={18} aria-hidden="true" />
        {label}
      </Link>
      <h1>{name}</h1>
    </div>
  );
};
