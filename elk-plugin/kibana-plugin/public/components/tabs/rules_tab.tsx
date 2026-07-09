import React, { useCallback, useEffect, useState } from 'react';
import {
  EuiFlexGroup, EuiFlexItem, EuiInMemoryTable, EuiButton, EuiButtonEmpty, EuiSpacer,
  EuiFieldText, EuiSelect, EuiTextArea, EuiFormRow, EuiCallOut, EuiPanel, EuiText,
} from '@elastic/eui';
import type { ParserApi } from '../../api';

const TEMPLATES: Record<string, string> = {
  stateless:
    'pattern_name: "my_access"\nstrategy: "stateless"\n\n' +
    "regex: '(?P<ip>[\\d\\.]+) - - \\[(?P<ts>[^\\]]+)\\] \"(?P<method>\\w+) (?P<path>[^\\s]+) HTTP/[\\d\\.]+\" (?P<status>\\d+) (?P<bytes>\\d+)'\n\n" +
    'mapping:\n  ip: "source.ip"\n  method: "http.request.method"\n  path: "url.path"\n  status: "http.response.status_code|int"\n  bytes: "http.response.body.bytes|int"\n',
  multi_match:
    'pattern_name: "my_auth"\nstrategy: "multi_match"\n\npatterns:\n' +
    "  - name: \"failed\"\n    regex: 'Failed password for (?:invalid user )?(?P<user>\\S+) from (?P<ip>[\\d\\.]+)'\n" +
    '    mapping:\n      user: "user.name"\n      ip: "source.ip"\n    static:\n      event.outcome: "failure"\n',
  json_map:
    'pattern_name: "my_json"\nstrategy: "json_map"\n\nmapping:\n  src_ip: "source.ip"\n  user.name: "user.name"\n  action: "event.action"\n\nstatic:\n  event.kind: "event"\n',
};

export const RulesTab = ({ api, toasts }: { api: ParserApi; toasts: any }) => {
  const [rules, setRules] = useState<any[]>([]);
  const [filename, setFilename] = useState('');
  const [content, setContent] = useState('');
  const [ecs, setEcs] = useState<any>(null);

  const load = useCallback(() => {
    api
      .get('/rules')
      .then((d: any) => setRules(d.rules || []))
      .catch((e: any) => toasts.addError(e, { title: 'Load failed' }));
  }, [api, toasts]);

  useEffect(load, [load]);

  const open = async (file: string) => {
    try {
      const d = await api.get(`/rules/${encodeURIComponent(file)}`);
      setFilename(d.filename);
      setContent(d.content);
      setEcs(null);
    } catch (e: any) {
      toasts.addError(e, { title: 'Open failed' });
    }
  };

  const save = async () => {
    if (!filename) {
      toasts.addWarning('Give the rule a file name (e.g. myparser.yaml)');
      return;
    }
    try {
      const d = await api.post('/rules/save', { filename, content });
      toasts.addSuccess(`Saved ${d.saved}`);
      setEcs(d.ecs);
      load();
    } catch (e: any) {
      toasts.addError(e, { title: 'Save failed' });
    }
  };

  const check = async () => {
    try {
      setEcs(await api.post('/ecs/check', { content }));
    } catch (e: any) {
      toasts.addError(e, { title: 'ECS check failed' });
    }
  };

  const del = async () => {
    if (!filename) return;
    try {
      await api.post('/rules/delete', { filename });
      toasts.addSuccess(`Deleted ${filename}`);
      setFilename('');
      setContent('');
      setEcs(null);
      load();
    } catch (e: any) {
      toasts.addError(e, { title: 'Delete failed' });
    }
  };

  return (
    <EuiFlexGroup>
      <EuiFlexItem grow={2}>
        <EuiButton
          size="s"
          onClick={() => {
            setFilename('');
            setContent('');
            setEcs(null);
          }}
        >
          + New
        </EuiButton>
        <EuiSpacer size="s" />
        <EuiInMemoryTable
          items={rules}
          columns={[
            { field: 'name', name: 'Rule', sortable: true },
            { field: 'strategy', name: 'Strategy' },
            { field: 'fields', name: 'Fields' },
            {
              name: 'Open',
              render: (r: any) => (
                <EuiButtonEmpty size="s" onClick={() => open(r.file)}>
                  Edit
                </EuiButtonEmpty>
              ),
            },
          ]}
          pagination
          sorting
          search={{ box: { incremental: true, placeholder: 'Search rules' } }}
        />
      </EuiFlexItem>

      <EuiFlexItem grow={3}>
        <EuiPanel>
          <EuiFlexGroup>
            <EuiFlexItem>
              <EuiFormRow label="File name">
                <EuiFieldText
                  value={filename}
                  onChange={(e) => setFilename(e.target.value)}
                  placeholder="myparser.yaml"
                />
              </EuiFormRow>
            </EuiFlexItem>
            <EuiFlexItem grow={false}>
              <EuiFormRow label="Template">
                <EuiSelect
                  options={[
                    { value: '', text: '- insert template -' },
                    { value: 'stateless', text: 'stateless' },
                    { value: 'multi_match', text: 'multi_match' },
                    { value: 'json_map', text: 'json_map' },
                  ]}
                  onChange={(e) => {
                    if (TEMPLATES[e.target.value]) setContent(TEMPLATES[e.target.value]);
                  }}
                />
              </EuiFormRow>
            </EuiFlexItem>
          </EuiFlexGroup>

          <EuiFormRow label="Rule YAML" fullWidth>
            <EuiTextArea
              fullWidth
              rows={16}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder="Select a rule on the left, or start a new one..."
            />
          </EuiFormRow>

          <EuiFlexGroup gutterSize="s">
            <EuiFlexItem grow={false}>
              <EuiButton fill onClick={save}>
                Save
              </EuiButton>
            </EuiFlexItem>
            <EuiFlexItem grow={false}>
              <EuiButton onClick={check}>Check ECS</EuiButton>
            </EuiFlexItem>
            <EuiFlexItem grow={false}>
              <EuiButton color="danger" onClick={del}>
                Delete
              </EuiButton>
            </EuiFlexItem>
          </EuiFlexGroup>

          {ecs && (
            <>
              <EuiSpacer />
              {ecs.problems?.length ? (
                ecs.problems.map((p: any, i: number) => (
                  <EuiCallOut key={i} color="danger" size="s" title={`${p.field}  ->  use ${p.fix}`} />
                ))
              ) : (
                <EuiCallOut color="success" size="s" title={`${ecs.ok} field(s) valid ECS`} />
              )}
              {ecs.customs?.map((c: any, i: number) => (
                <EuiText key={i} size="xs" color="subdued">
                  ~ {c.field} (custom field, allowed)
                </EuiText>
              ))}
            </>
          )}
        </EuiPanel>
      </EuiFlexItem>
    </EuiFlexGroup>
  );
};
