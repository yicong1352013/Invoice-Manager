import { useCallback, useEffect, useState } from 'react';
import {
  Button, Card, DatePicker, Input, Select, Table, Tag, message,
} from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import type { Dayjs } from 'dayjs';
import { getLogActions, getLogs } from '../services/api';
import type { AuditLogEntry } from '../types/log';
import { formatBackendTime } from '../utils/time';
import styles from './LogsPage.module.css';

const ACTION_COLORS: Record<string, string> = {
  upload: 'blue',
  print: 'purple',
  process_complete: 'green',
  process_failed: 'red',
  delete: 'red',
  create: 'green',
  update: 'orange',
  confirm: 'cyan',
};

function describeAction(action: string): string {
  const labels: Record<string, string> = {
    upload: '上传',
    print: '打印',
    create: '创建',
    update: '更新',
    delete: '删除',
    confirm: '确认',
    resolve: '解决冲突',
    process_complete: '识别完成',
    process_failed: '识别失败',
    export_csv: '导出 CSV',
    export_excel: '导出 Excel',
    export_word: '导出 Word',
    export_pdf: '导出 PDF',
    reassociate: '附件改绑',
  };
  return labels[action] ?? action;
}

function describeTarget(log: AuditLogEntry): string {
  if (log.entity_id === 0) {
    const count = (log.new_value?.count as number | undefined) ?? '';
    return count !== '' ? `批量（${count} 张发票）` : '批量操作';
  }
  return `${log.entity_type} #${log.entity_id}`;
}

export default function LogsPage() {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [actionFilter, setActionFilter] = useState<string | undefined>();
  const [userFilter, setUserFilter] = useState('');
  const [dateRange, setDateRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [actionOptions, setActionOptions] = useState<{ value: string; label: string }[]>([]);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, unknown> = { page, page_size: pageSize };
      if (actionFilter) params.action = actionFilter;
      if (userFilter) params.user_id = userFilter;
      if (dateRange?.[0]) params.start_date = dateRange[0].format('YYYY-MM-DD');
      if (dateRange?.[1]) params.end_date = dateRange[1].format('YYYY-MM-DD');
      const data = await getLogs(params);
      setLogs(data.items);
      setTotal(data.total);
    } catch {
      message.error('获取日志失败');
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, actionFilter, userFilter, dateRange]);

  useEffect(() => {
    fetchLogs();
  }, [fetchLogs]);

  useEffect(() => {
    getLogActions()
      .then((actions) => setActionOptions(actions.map((a) => ({ value: a, label: describeAction(a) }))))
      .catch(() => setActionOptions([]));
  }, []);

  const columns: ColumnsType<AuditLogEntry> = [
    {
      title: '时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (val: string) => formatBackendTime(val, 'YYYY-MM-DD HH:mm:ss'),
    },
    {
      title: '操作人',
      dataIndex: 'user_id',
      key: 'user_id',
      width: 110,
      ellipsis: true,
      render: (val: string | null) => val || '系统',
    },
    {
      title: '动作',
      dataIndex: 'action',
      key: 'action',
      width: 110,
      render: (val: string) => (
        <Tag color={ACTION_COLORS[val] || 'default'}>{describeAction(val)}</Tag>
      ),
    },
    {
      title: '对象',
      key: 'target',
      width: 190,
      ellipsis: true,
      render: (_, record) => describeTarget(record),
    },
    {
      title: 'IP',
      dataIndex: 'ip_address',
      key: 'ip_address',
      width: 130,
      render: (val: string | null) => val || '-',
    },
    {
      title: '详情',
      key: 'details',
      ellipsis: true,
      render: (_, record) => record.details || (record.new_value?.error as string) || '-',
    },
  ];

  return (
    <div className={styles.pageContainer}>
      <div className={styles.contentWrapper}>
        <div className={styles.pageHeader}>
          <div>
            <h1 className={styles.pageTitle}>系统日志</h1>
            <p className={styles.pageSubtitle}>发票识别、打印、导出等操作的审计记录（仅管理员可见）</p>
          </div>
        </div>

        <Card>
          <div className={styles.filterBar}>
            <Select
              allowClear
              placeholder="动作"
              style={{ width: 150 }}
              value={actionFilter}
              onChange={(v) => { setActionFilter(v); setPage(1); }}
              options={actionOptions}
            />
            <Input
              placeholder="操作人"
              style={{ width: 140 }}
              value={userFilter}
              onChange={(e) => setUserFilter(e.target.value)}
              onPressEnter={() => { setPage(1); fetchLogs(); }}
            />
            <DatePicker.RangePicker
              value={dateRange}
              onChange={(range) => { setDateRange(range); setPage(1); }}
            />
            <Button icon={<ReloadOutlined />} onClick={() => fetchLogs()}>
              刷新
            </Button>
            <span className={styles.summary}>共 {total} 条</span>
          </div>

          <Table<AuditLogEntry>
            rowKey="id"
            size="middle"
            columns={columns}
            dataSource={logs}
            loading={loading}
            pagination={{
              current: page,
              pageSize,
              total,
              showSizeChanger: true,
              showTotal: (t) => `共 ${t} 条`,
              onChange: (p, s) => { setPage(p); setPageSize(s); },
            }}
          />
        </Card>
      </div>
    </div>
  );
}
