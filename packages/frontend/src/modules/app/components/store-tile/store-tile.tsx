import type React from 'react';
import './store-tile.css';
import type { AppInfoSimple } from '@/types/app.types';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';

// Wizard UX launcher row: icon, name with a one-line description, category chip.
export const StoreTile: React.FC<{ app: AppInfoSimple; isLoading: boolean }> = ({ app, isLoading }) => {
  const { t } = useTranslation();
  const [appId, storeId] = app.urn.split(':');
  const category = app.categories?.[0];

  return (
    <Link aria-label={app.name} className={`lrow${isLoading ? ' loading' : ''}`} to={`/app-store/${storeId}/${appId}`}>
      <span className="ic">
        <img src={`/api/marketplace/apps/${app.urn}/image`} alt="" loading="lazy" />
      </span>
      <span className="nm">
        {app.name}
        <small>{app.short_desc}</small>
      </span>
      {category ? <span className={`kind ${category}`}>{t(`APP_CATEGORY_${category.toUpperCase() as Uppercase<typeof category>}`)}</span> : null}
      <span className="st-go" aria-hidden="true">
        ›
      </span>
    </Link>
  );
};
