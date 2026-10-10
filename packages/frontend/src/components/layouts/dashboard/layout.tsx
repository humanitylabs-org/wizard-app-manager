import type { PropsWithChildren, ReactNode } from 'react';
import './layout.css';
import { PageTitle } from '@/components/page-title/page-title';
import { Welcome } from '@/components/welcome/welcome';
import { useAppContext } from '@/context/app-context';
import { NavBar } from '@/components/navbar/navbar';

/*
 * Wizard App Manager chrome, kept to the minimum so each view can drop into Wizard UX as-is:
 * - no brand bar, logout or theme toggle (the Wizard OS shell owns those);
 * - no page heading on the three top-level views (the selected tab already says where you are);
 * - one tab bar: a small segmented control on desktop, a bottom bar in thumb reach on phones;
 * - opened inside Wizard OS (framed, or ?embed=1) the tab bar hides too, because the Wizard OS
 *   launcher already lists My Apps, App Store and Resources as separate apps.
 */
export const isEmbedded = () => {
  if (typeof window === 'undefined') return false;
  try {
    if (new URLSearchParams(window.location.search).get('embed') === '1') sessionStorage.setItem('wam.embed', '1');
    return window.self !== window.top || sessionStorage.getItem('wam.embed') === '1';
  } catch {
    return true;
  }
};

const Shell = ({ children, title }: PropsWithChildren<{ title?: ReactNode }>) => {
  const embedded = isEmbedded();
  return (
    <div className={embedded ? 'wam-shell embedded' : 'wam-shell'}>
      {embedded ? null : <NavBar />}
      <main className="wam-main">
        {title}
        {children}
      </main>
    </div>
  );
};

// Used by error and loading fallbacks, which can render outside the app context: no title there.
export const DashboardLayoutSuspense = ({ children }: PropsWithChildren) => <Shell>{children}</Shell>;

export const DashboardLayout = ({ children }: PropsWithChildren) => {
  const { userSettings, user, apps } = useAppContext();

  if (!user.hasSeenWelcome) {
    return <Welcome allowErrorMonitoring={userSettings.allowErrorMonitoring} />;
  }

  return <Shell title={<PageTitle apps={apps} />}>{children}</Shell>;
};
