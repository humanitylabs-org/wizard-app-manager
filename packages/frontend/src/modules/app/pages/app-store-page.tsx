import { Navigate, useParams } from 'react-router';
import { AppStoreView } from '@/wizard-views/views';

export const AppStorePageSuspense = () => null;

// The App Store is the same launcher sheet as My Apps, over the Wizard App Index (see src/wizard-views).
export default () => {
  const params = useParams<{ storeId: string }>();
  if (params.storeId) return <Navigate to="/app-store" replace />;
  return <AppStoreView />;
};
