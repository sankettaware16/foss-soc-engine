import React, { useState } from 'react';
import {
  EuiFlexGroup, EuiFlexItem, EuiFieldText, EuiButton, EuiFormRow, EuiSpacer,
  EuiCallOut, EuiListGroup, EuiPanel, EuiTitle,
} from '@elastic/eui';
import type { ParserApi } from '../../api';

export const EcsTab = ({ api, toasts }: { api: ParserApi; toasts: any }) => {
  const [field, setField] = useState('');
  const [cls, setCls] = useState<any>(null);
  const [q, setQ] = useState('');
  const [found, setFound] = useState<string[]>([]);

  const classify = async () => {
    if (!field.trim()) return;
    try {
      setCls(await api.get('/ecs/classify', { field }));
    } catch (e: any) {
      toasts.addError(e, { title: 'Lookup failed' });
    }
  };

  const search = async () => {
    if (!q.trim()) return;
    try {
      const d = await api.get('/ecs/find', { q });
      setFound(d.results || []);
    } catch (e: any) {
      toasts.addError(e, { title: 'Search failed' });
    }
  };

  const clsColor = cls?.status === 'ecs' ? 'success' : cls?.status === 'custom' ? 'warning' : 'danger';
  const clsMsg = cls
    ? cls.status === 'ecs'
      ? `${cls.field} is a valid ECS field`
      : cls.status === 'custom'
      ? `${cls.field} is a custom field (allowed)${cls.suggestion ? ` - closest: ${cls.suggestion}` : ''}`
      : `${cls.field} is not ECS - use ${cls.suggestion}`
    : '';

  return (
    <EuiFlexGroup>
      <EuiFlexItem>
        <EuiPanel>
          <EuiTitle size="xs">
            <h4>Check / autocorrect a field</h4>
          </EuiTitle>
          <EuiSpacer size="s" />
          <EuiFormRow>
            <EuiFieldText
              value={field}
              onChange={(e) => setField(e.target.value)}
              placeholder="e.g. srcip, status, email.from"
            />
          </EuiFormRow>
          <EuiButton onClick={classify}>Check</EuiButton>
          {cls && (
            <>
              <EuiSpacer size="s" />
              <EuiCallOut size="s" color={clsColor} title={clsMsg} />
            </>
          )}
        </EuiPanel>
      </EuiFlexItem>
      <EuiFlexItem>
        <EuiPanel>
          <EuiTitle size="xs">
            <h4>Find a field by concept</h4>
          </EuiTitle>
          <EuiSpacer size="s" />
          <EuiFormRow>
            <EuiFieldText
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="e.g. http status, country, mail"
            />
          </EuiFormRow>
          <EuiButton onClick={search}>Search</EuiButton>
          {found.length > 0 && (
            <>
              <EuiSpacer size="s" />
              <EuiListGroup listItems={found.map((f) => ({ label: f, iconType: 'check' }))} />
            </>
          )}
        </EuiPanel>
      </EuiFlexItem>
    </EuiFlexGroup>
  );
};
