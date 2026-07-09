import React, { useEffect, useRef, useState } from 'react';
import {
  EuiFlexGroup, EuiFlexItem, EuiPanel, EuiStat, EuiSpacer, EuiHealth, EuiBasicTable,
  EuiText, EuiTitle, EuiProgress,
} from '@elastic/eui';
import type { ParserApi } from '../../api';

const fmtBytes = (b?: number) => {
  if (b == null) return '-';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = b;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
};

const fmtDur = (s?: number) => {
  if (s == null) return '-';
  s = Math.floor(s);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
};

export const MonitorTab = ({ api, toasts }: { api: ParserApi; toasts: any }) => {
  const [m, setM] = useState<any>(null);
  const timer = useRef<any>(null);

  useEffect(() => {
    const poll = () => api.get('/monitor').then(setM).catch(() => {});
    poll();
    timer.current = setInterval(poll, 2000);
    return () => clearInterval(timer.current);
  }, [api]);

  if (!m) return <EuiText>Loading engine status...</EuiText>;

  const color = m.status === 'running' ? 'success' : m.status === 'starting' ? 'warning' : 'danger';
  const label =
    m.status === 'running'
      ? 'Engine running'
      : m.status === 'starting'
      ? 'Starting / stale'
      : 'Stopped';

  const ruleItems = Object.entries(m.parser_stats || {})
    .map(([name, st]: any) => ({
      name,
      events: st.parsed_events || 0,
      no_match: st.no_match || 0,
      buffered: st.buffered || 0,
      expired: st.expired || 0,
      errors: (st.errors || 0) + (st.redis_errors || 0),
    }))
    .sort((a, b) => b.events - a.events);

  return (
    <>
      <EuiPanel>
        <EuiFlexGroup alignItems="center">
          <EuiFlexItem grow={false}>
            <EuiHealth color={color}>
              <EuiTitle size="xs">
                <h3>{label}</h3>
              </EuiTitle>
            </EuiHealth>
          </EuiFlexItem>
          <EuiFlexItem>
            <EuiText size="s" color="subdued">
              uptime {fmtDur(m.uptime_sec)} · workers {m.workers_alive}/{m.workers} · topic{' '}
              {m.kafka?.input_topic || '-'}
              {m.stats_age_sec != null ? ` · stats ${m.stats_age_sec}s ago` : ''}
            </EuiText>
          </EuiFlexItem>
        </EuiFlexGroup>
      </EuiPanel>

      <EuiSpacer />

      <EuiFlexGroup>
        <EuiFlexItem>
          <EuiPanel>
            <EuiStat
              title={Number(m.eps).toLocaleString()}
              description="Events / sec"
              titleColor="success"
            />
          </EuiPanel>
        </EuiFlexItem>
        <EuiFlexItem>
          <EuiPanel>
            <EuiStat title={Number(m.total_processed).toLocaleString()} description="Total processed" />
          </EuiPanel>
        </EuiFlexItem>
        <EuiFlexItem>
          <EuiPanel>
            <EuiStat
              title={Number(m.total_errors).toLocaleString()}
              description="Total errors"
              titleColor={m.total_errors ? 'danger' : 'subdued'}
            />
          </EuiPanel>
        </EuiFlexItem>
        <EuiFlexItem>
          <EuiPanel>
            <EuiStat title={fmtBytes(m.engine_rss)} description="Engine RAM" />
          </EuiPanel>
        </EuiFlexItem>
      </EuiFlexGroup>

      {m.system && (
        <>
          <EuiSpacer />
          <EuiPanel>
            <EuiText size="s">
              <b>System</b> — CPU {m.system.cpu_percent ?? '-'}% · cores {m.system.cpu_count}
              {m.system.mem
                ? ` · RAM ${fmtBytes(m.system.mem.used)} / ${fmtBytes(m.system.mem.total)} (${m.system.mem.percent}%)`
                : ''}
              {m.system.load ? ` · load ${m.system.load.join(' / ')}` : ''}
            </EuiText>
            {m.system.mem && (
              <>
                <EuiSpacer size="s" />
                <EuiProgress
                  value={m.system.mem.percent}
                  max={100}
                  size="s"
                  color={m.system.mem.percent > 88 ? 'danger' : 'primary'}
                />
              </>
            )}
          </EuiPanel>
        </>
      )}

      <EuiSpacer />

      <EuiFlexGroup>
        <EuiFlexItem>
          <EuiTitle size="xs">
            <h4>Rules in use</h4>
          </EuiTitle>
          <EuiSpacer size="s" />
          <EuiBasicTable
            items={ruleItems}
            columns={[
              { field: 'name', name: 'Rule' },
              { field: 'events', name: 'Events' },
              { field: 'no_match', name: 'No match' },
              { field: 'buffered', name: 'Buffered' },
              { field: 'expired', name: 'Expired' },
              { field: 'errors', name: 'Errors' },
            ]}
          />
        </EuiFlexItem>
        <EuiFlexItem>
          <EuiTitle size="xs">
            <h4>Workers</h4>
          </EuiTitle>
          <EuiSpacer size="s" />
          <EuiBasicTable
            items={m.workers_detail || []}
            columns={[
              { field: 'worker_id', name: '#', render: (v: any) => (v == null ? '-' : `w${v}`) },
              { field: 'pid', name: 'PID' },
              { field: 'eps', name: 'EPS' },
              { field: 'total_processed', name: 'Processed' },
              { field: 'uptime_sec', name: 'Uptime', render: (v: any) => fmtDur(v) },
            ]}
          />
        </EuiFlexItem>
      </EuiFlexGroup>
    </>
  );
};
