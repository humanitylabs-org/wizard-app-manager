import { useUIStore } from '@/stores/ui-store';
import { IconApps, IconBrandAppstore, IconGauge } from '@tabler/icons-react';
import clsx from 'clsx';
import type React from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import './navbar.css';

// The only chrome: three tabs. Desktop shows a segmented control at the top; phones get a bottom bar.
export const NavBar: React.FC = () => {
  const { t } = useTranslation();
  const activeRoute = useUIStore((state) => state.activeRoute);

  const item = (title: string, name: string, Icon: typeof IconApps) => {
    const isActive = activeRoute?.split('/')[0] === name;
    return (
      <Link
        key={name}
        to={`/${name}`}
        className={clsx('wam-tab', { active: isActive })}
        aria-current={isActive ? 'page' : undefined}
        data-testid={`nav-item-${name}`}
      >
        <Icon size={20} aria-hidden="true" />
        <span>{title}</span>
      </Link>
    );
  };

  return (
    <nav className="wam-tabs" aria-label={t('HEADER_MAIN_NAVIGATION')}>
      {item(t('HEADER_APPS'), 'apps', IconApps)}
      {item(t('HEADER_APP_STORE'), 'app-store', IconBrandAppstore)}
      {item(t('HEADER_RESOURCES'), 'resources', IconGauge)}
    </nav>
  );
};
