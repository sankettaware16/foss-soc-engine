import React from 'react';
import ReactDOM from 'react-dom'; // Kibana 8.19 ships React 17 (legacy render API)
import type { AppMountParameters, CoreStart } from '@kbn/core/public';
import { I18nProvider } from '@kbn/i18n-react';
import { App } from './components/app';

export const renderApp = (core: CoreStart, { element }: AppMountParameters) => {
  ReactDOM.render(
    <I18nProvider>
      <App http={core.http} notifications={core.notifications} />
    </I18nProvider>,
    element
  );
  return () => ReactDOM.unmountComponentAtNode(element);
};
