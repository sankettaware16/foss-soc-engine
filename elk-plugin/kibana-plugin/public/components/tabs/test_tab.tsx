import React, { useEffect, useState } from 'react';
import {
  EuiForm, EuiFormRow, EuiSelect, EuiTextArea, EuiButton, EuiSpacer,
  EuiFlexGroup, EuiFlexItem, EuiCallOut, EuiCodeBlock, EuiStat, EuiPanel,
  EuiFilePicker, EuiFieldNumber,
} from '@elastic/eui';
import type { ParserApi } from '../../api';

export const TestTab = ({ api, toasts }: { api: ParserApi; toasts: any }) => {
  const [parsers, setParsers] = useState<string[]>([]);
  const [parser, setParser] = useState('AUTO');
  const [text, setText] = useState('');
  const [limit, setLimit] = useState(20000);
  const [result, setResult] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api
      .get('/health')
      .then((h: any) => setParsers((h.rules || []).map((r: any) => r.name)))
      .catch(() => {});
  }, [api]);

  const run = async () => {
    if (!text.trim()) {
      toasts.addWarning('Paste some log lines first (or upload a file).');
      return;
    }
    setLoading(true);
    try {
      setResult(await api.post('/test', { text, parser, limit }));
    } catch (e: any) {
      toasts.addError(e, { title: 'Test failed' });
    }
    setLoading(false);
  };

  const onFile = (files: FileList | null) => {
    const f = files?.[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => setText(String(reader.result || ''));
    reader.readAsText(f);
  };

  const options = [
    { value: 'AUTO', text: 'AUTO - try every rule' },
    ...parsers.map((p) => ({ value: p, text: p })),
  ];

  const s = result?.stats;

  return (
    <>
      <EuiForm component="form">
        <EuiFlexGroup>
          <EuiFlexItem>
            <EuiFormRow label="Parser">
              <EuiSelect
                options={options}
                value={parser}
                onChange={(e) => setParser(e.target.value)}
              />
            </EuiFormRow>
          </EuiFlexItem>
          <EuiFlexItem grow={false}>
            <EuiFormRow label="Max lines">
              <EuiFieldNumber value={limit} onChange={(e) => setLimit(Number(e.target.value))} />
            </EuiFormRow>
          </EuiFlexItem>
        </EuiFlexGroup>
        <EuiFormRow label="Paste log lines" fullWidth>
          <EuiTextArea
            fullWidth
            rows={8}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Paste one or more raw log lines..."
          />
        </EuiFormRow>
        <EuiFormRow label="...or upload a log file" fullWidth>
          <EuiFilePicker display="default" onChange={onFile} />
        </EuiFormRow>
        <EuiButton fill isLoading={loading} onClick={run}>
          Run test
        </EuiButton>
      </EuiForm>

      <EuiSpacer />

      {s && (
        <>
          <EuiFlexGroup>
            <EuiFlexItem>
              <EuiPanel>
                <EuiStat title={s.parsed_events} description="Parsed events" titleColor="success" />
              </EuiPanel>
            </EuiFlexItem>
            <EuiFlexItem>
              <EuiPanel>
                <EuiStat title={`${s.match_rate}%`} description="Match rate" />
              </EuiPanel>
            </EuiFlexItem>
            <EuiFlexItem>
              <EuiPanel>
                <EuiStat
                  title={s.no_match}
                  description="No match"
                  titleColor={s.no_match ? 'warning' : 'subdued'}
                />
              </EuiPanel>
            </EuiFlexItem>
            <EuiFlexItem>
              <EuiPanel>
                <EuiStat
                  title={s.errors}
                  description="Errors"
                  titleColor={s.errors ? 'danger' : 'subdued'}
                />
              </EuiPanel>
            </EuiFlexItem>
          </EuiFlexGroup>
          <EuiSpacer />
          {result.events?.length ? (
            <EuiCodeBlock language="json" overflowHeight={480} isCopyable paddingSize="m">
              {JSON.stringify(
                result.events.map((e: any) => e.event),
                null,
                2
              )}
            </EuiCodeBlock>
          ) : (
            <EuiCallOut color="warning" title="No events parsed">
              Check the parser choice, or look at the unparsed sample lines.
            </EuiCallOut>
          )}
        </>
      )}
    </>
  );
};
