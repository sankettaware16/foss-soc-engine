import React, { useEffect, useState } from 'react';
import {
  EuiTextArea, EuiButton, EuiFlexGroup, EuiFlexItem, EuiSpacer, EuiCallOut, EuiCodeBlock,
} from '@elastic/eui';
import type { ParserApi } from '../../api';

export const ConfigTab = ({ api, toasts }: { api: ParserApi; toasts: any }) => {
  const [content, setContent] = useState('');
  const [report, setReport] = useState<any>(null);

  useEffect(() => {
    api
      .get('/config')
      .then((d: any) => setContent(d.content || ''))
      .catch(() => {});
  }, [api]);

  const save = async () => {
    try {
      await api.post('/config/save', { content });
      toasts.addSuccess('config.yaml saved');
    } catch (e: any) {
      toasts.addError(e, { title: 'Save failed' });
    }
  };

  const validate = async () => {
    try {
      const d = await api.post('/config/validate', {});
      setReport(d);
      if (d.summary.passed) toasts.addSuccess(`Passed - ${d.summary.warnings} warning(s)`);
      else toasts.addWarning(`${d.summary.errors} error(s), ${d.summary.warnings} warning(s)`);
    } catch (e: any) {
      toasts.addError(e, { title: 'Validate failed' });
    }
  };

  return (
    <>
      <EuiTextArea
        fullWidth
        rows={20}
        value={content}
        onChange={(e) => setContent(e.target.value)}
      />
      <EuiSpacer size="s" />
      <EuiFlexGroup gutterSize="s">
        <EuiFlexItem grow={false}>
          <EuiButton fill onClick={save}>
            Save
          </EuiButton>
        </EuiFlexItem>
        <EuiFlexItem grow={false}>
          <EuiButton onClick={validate}>Validate</EuiButton>
        </EuiFlexItem>
      </EuiFlexGroup>

      {report && (
        <>
          <EuiSpacer />
          <EuiCallOut
            color={report.summary.passed ? 'success' : 'danger'}
            title={`${report.summary.errors} error(s), ${report.summary.warnings} warning(s)`}
          />
          <EuiSpacer size="s" />
          <EuiCodeBlock language="text" overflowHeight={320} paddingSize="m">
            {(report.lines || []).map((l: any) => `[${l.level}] ${l.message}`).join('\n')}
          </EuiCodeBlock>
        </>
      )}
    </>
  );
};
