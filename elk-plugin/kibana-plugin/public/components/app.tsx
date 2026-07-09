import React, { useMemo, useState } from 'react';
import { EuiPageTemplate, EuiTabs, EuiTab, EuiSpacer } from '@elastic/eui';
import type { HttpStart, NotificationsStart } from '@kbn/core/public';
import { ParserApi } from '../api';
import { TestTab } from './tabs/test_tab';
import { RulesTab } from './tabs/rules_tab';
import { ConfigTab } from './tabs/config_tab';
import { EcsTab } from './tabs/ecs_tab';
import { MonitorTab } from './tabs/monitor_tab';

const TABS = [
  { id: 'test', name: 'Test Log' },
  { id: 'rules', name: 'Rules' },
  { id: 'config', name: 'Config' },
  { id: 'ecs', name: 'ECS Helper' },
  { id: 'monitor', name: 'Monitor' },
];

export const App = ({
  http,
  notifications,
}: {
  http: HttpStart;
  notifications: NotificationsStart;
}) => {
  const [tab, setTab] = useState('test');
  const api = useMemo(() => new ParserApi(http), [http]);
  const toasts = notifications.toasts;

  return (
    <EuiPageTemplate restrictWidth={1300} panelled>
      <EuiPageTemplate.Header
        pageTitle="TLSOC Parser"
        description="Test logs, manage parser rules, edit config, ECS lookup and live engine monitoring."
      />
      <EuiPageTemplate.Section>
        <EuiTabs>
          {TABS.map((t) => (
            <EuiTab key={t.id} isSelected={t.id === tab} onClick={() => setTab(t.id)}>
              {t.name}
            </EuiTab>
          ))}
        </EuiTabs>
        <EuiSpacer />
        {tab === 'test' && <TestTab api={api} toasts={toasts} />}
        {tab === 'rules' && <RulesTab api={api} toasts={toasts} />}
        {tab === 'config' && <ConfigTab api={api} toasts={toasts} />}
        {tab === 'ecs' && <EcsTab api={api} toasts={toasts} />}
        {tab === 'monitor' && <MonitorTab api={api} toasts={toasts} />}
      </EuiPageTemplate.Section>
    </EuiPageTemplate>
  );
};
