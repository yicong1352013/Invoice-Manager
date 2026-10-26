import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Form,
  Drawer,
  Grid,
  Input,
  InputNumber,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  message,
} from 'antd';
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
} from '@ant-design/icons';
import api, { listInvoices } from '../services/api';
import { downloadWorkflowFile, workflowError } from '../services/workflowApi';
import styles from './WorkflowPages.module.css';

interface Column {
  field: string;
  title: string;
  format: string;
  width: number;
  align: 'left' | 'center' | 'right';
  sum: boolean;
}
interface Config {
  sheet_name: string;
  title: string;
  filename_pattern: string;
  columns: Column[];
  style: {
    header_color: string;
    font_size: number;
    borders: boolean;
    striped: boolean;
  };
  freeze_header: boolean;
  auto_filter: boolean;
  show_count: boolean;
}
interface Field {
  key: string;
  label: string;
  type: string;
  summable: boolean;
}
interface Template {
  id: number;
  name: string;
  description: string;
  draft_config: Config;
  published_config: Config | null;
  version: number;
  enabled: boolean;
}
interface Preview {
  columns: Column[];
  rows: (string | null)[][];
  totals: (string | null)[];
  count: number;
  title: string;
  sheet_name: string;
}
const formatNames: Record<string, string> = {
  text: '文本',
  date: '日期',
  datetime: '日期时间',
  money: '金额',
  number: '数值',
};

export default function ExportTemplatesPage() {
  const screens = Grid.useBreakpoint();
  const [fieldDrawerOpen, setFieldDrawerOpen] = useState(false);
  const [rows, setRows] = useState<Template[]>([]);
  const [fields, setFields] = useState<Field[]>([]);
  const [defaults, setDefaults] = useState<Config>();
  const [editing, setEditing] = useState<Template | null | undefined>();
  const [config, setConfig] = useState<Config>();
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState('');
  const [preview, setPreview] = useState<Preview>();
  const [previewIds, setPreviewIds] = useState<number[]>([]);
  const [invoiceOptions, setInvoiceOptions] = useState<
    { value: number; label: string }[]
  >([]);
  const [form] = Form.useForm();
  const load = useCallback(async () => {
    try {
      const [list, catalog] = await Promise.all([
        api.get('/export-templates'),
        api.get('/export-templates/fields'),
      ]);
      setRows(list.data);
      setFields(catalog.data.fields);
      setDefaults(catalog.data.default_config);
    } catch (e) {
      message.error(workflowError(e));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    const navigationGuard = (event: MouseEvent) => {
      const link = (event.target as Element).closest('a[href]');
      if (link && !window.confirm('模板有未保存的修改，确定离开？')) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    window.addEventListener('beforeunload', guard);
    document.addEventListener('click', navigationGuard, true);
    return () => {
      window.removeEventListener('beforeunload', guard);
      document.removeEventListener('click', navigationGuard, true);
    };
  }, [dirty]);
  useEffect(() => {
    if (!config) return;
    let active = true;
    const timer = setTimeout(() => {
      api
        .post('/export-templates/preview', { config, invoice_ids: previewIds })
        .then((r) => {
          if (active) setPreview(r.data);
        })
        .catch(() => {
          if (active) setPreview(undefined);
        });
    }, 400);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [config, previewIds]);
  function edit(row: Template | null, copy = false) {
    setEditing(copy ? null : row);
    setConfig(
      structuredClone(row?.draft_config ?? row?.published_config ?? defaults),
    );
    setPreview(undefined);
    setPreviewIds([]);
    setDirty(copy);
    form.setFieldsValue({
      name: copy ? `${row?.name} 副本` : (row?.name ?? ''),
      description: row?.description ?? '',
      enabled: row?.enabled ?? true,
    });
  }
  function update(next: Config) {
    setConfig(next);
    setDirty(true);
  }
  function selectField(field: Field, selected: boolean) {
    if (!config) return;
    update({
      ...config,
      columns: selected
        ? [
            ...config.columns,
            {
              field: field.key,
              title: field.label,
              format: field.type,
              width: 20,
              align: field.type === 'money' ? 'right' : 'left',
              sum: false,
            },
          ]
        : config.columns.filter((column) => column.field !== field.key),
    });
  }
  function updateColumn(index: number, change: Partial<Column>) {
    if (config)
      update({
        ...config,
        columns: config.columns.map((c, i) =>
          i === index ? { ...c, ...change } : c,
        ),
      });
  }
  function move(index: number, offset: number) {
    if (!config) return;
    const columns = [...config.columns];
    [columns[index], columns[index + offset]] = [
      columns[index + offset],
      columns[index],
    ];
    update({ ...config, columns });
  }
  async function save(publish: boolean) {
    if (!config) return;
    try {
      const values = await form.validateFields();
      setBusy(true);
      const response = editing
        ? await api.put(`/export-templates/${editing.id}`, {
            ...values,
            config,
          })
        : await api.post('/export-templates', { ...values, config });
      const saved: Template = response.data;
      setEditing(saved);
      setDirty(false);
      if (publish) {
        const published = await api.post(
          `/export-templates/${saved.id}/publish`,
          { expected_version: saved.version },
        );
        setEditing(published.data);
      }
      message.success(publish ? '模板已发布，新批次使用此版本' : '草稿已保存');
      await load();
    } catch (e) {
      if (!(e && typeof e === 'object' && 'errorFields' in e))
        message.error(workflowError(e));
    } finally {
      setBusy(false);
    }
  }
  function back() {
    if (!dirty || window.confirm('模板有未保存的修改，确定返回？')) {
      setEditing(undefined);
      setConfig(undefined);
      setDirty(false);
    }
  }
  if (editing === undefined || !config)
    return (
      <div className={styles.page}>
        <div className={styles.header}>
          <div>
            <h1>导出模板</h1>
            <p className={styles.hint}>
              一张发票一行，自定义 Excel 列字段、格式和汇总。
            </p>
          </div>
          <Button
            type="primary"
            disabled={!defaults}
            onClick={() => edit(null)}
          >
            新建 Excel 模板
          </Button>
        </div>
        <Table
          rowKey="id"
          dataSource={rows}
          scroll={{ x: 700 }}
          columns={[
            { title: '模板名称', dataIndex: 'name' },
            {
              title: '版本',
              render: (_, row) =>
                row.version ? `已发布 v${row.version}` : '未发布',
            },
            {
              title: '状态',
              render: (_, row) => (
                <Space>
                  <Tag color={row.enabled ? 'green' : 'default'}>
                    {row.enabled ? '启用' : '停用'}
                  </Tag>
                  {JSON.stringify(row.draft_config) !==
                    JSON.stringify(row.published_config) && <Tag>有草稿</Tag>}
                </Space>
              ),
            },
            {
              title: '操作',
              render: (_, row) => (
                <Space>
                  <Button onClick={() => edit(row)}>编辑 / 预览</Button>
                  <Button onClick={() => edit(row, true)}>复制</Button>
                  <Button
                    onClick={async () => {
                      try {
                        await api.put(`/export-templates/${row.id}`, {
                          name: row.name,
                          description: row.description,
                          config: row.draft_config,
                          enabled: !row.enabled,
                        });
                        await load();
                      } catch (e) {
                        message.error(workflowError(e));
                      }
                    }}
                  >
                    {row.enabled ? '停用' : '启用'}
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </div>
    );
  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <Button type="link" onClick={back}>
            返回模板列表
          </Button>
          <h1>{editing ? '编辑 Excel 模板' : '新建 Excel 模板'}</h1>
          <Tag>
            {editing?.version ? `已发布 v${editing.version}` : '尚未发布'}
          </Tag>
          {dirty && <Tag color="orange">未保存修改</Tag>}
        </div>
        <Space wrap>
          <Button
            onClick={async () => {
              try {
                await downloadWorkflowFile(
                  '/export-templates/sample',
                  '模板样例.xlsx',
                  { config, invoice_ids: previewIds },
                );
              } catch (e) {
                message.error(workflowError(e));
              }
            }}
          >
            下载样例
          </Button>
          <Button loading={busy} onClick={() => save(false)}>
            保存草稿
          </Button>
          <Button type="primary" loading={busy} onClick={() => save(true)}>
            保存并发布
          </Button>
        </Space>
      </div>
      <div className={styles.stack}>
        <Card>
          <Form
            form={form}
            layout="vertical"
            onValuesChange={() => setDirty(true)}
          >
            <div className={styles.grid}>
              <Form.Item
                name="name"
                label="模板名称"
                rules={[{ required: true, whitespace: true }]}
              >
                <Input maxLength={100} />
              </Form.Item>
              <Form.Item name="description" label="说明">
                <Input maxLength={500} />
              </Form.Item>
            </div>
          </Form>
          <div className={styles.grid}>
            <Form.Item label="工作表名称">
              <Input
                aria-label="工作表名称"
                value={config.sheet_name}
                maxLength={31}
                onChange={(e) =>
                  update({ ...config, sheet_name: e.target.value })
                }
              />
            </Form.Item>
            <Form.Item label="报表标题">
              <Input
                aria-label="报表标题"
                maxLength={150}
                value={config.title}
                onChange={(e) => update({ ...config, title: e.target.value })}
              />
            </Form.Item>
            <Form.Item
              label="文件名规则"
              extra="支持 {date}、{run_id}、{run_name}"
            >
              <Input
                aria-label="文件名规则"
                maxLength={100}
                value={config.filename_pattern}
                onChange={(e) =>
                  update({ ...config, filename_pattern: e.target.value })
                }
              />
            </Form.Item>
          </div>
        </Card>
        <Card>
          <Tabs
            items={[
              {
                key: 'columns',
                label: '列字段',
                children: (
                  <div
                    className={styles.editor}
                    style={{
                      gridTemplateColumns: screens.md
                        ? undefined
                        : 'minmax(0, 1fr)',
                    }}
                  >
                    {!screens.md && (
                      <Button onClick={() => setFieldDrawerOpen(true)}>
                        选择列字段
                      </Button>
                    )}
                    <Drawer
                      title="选择列字段"
                      placement="left"
                      open={!screens.md && fieldDrawerOpen}
                      onClose={() => setFieldDrawerOpen(false)}
                      styles={{ wrapper: { width: 280 } }}
                    >
                      <div className={styles.fields}>
                        {fields.map((field) => (
                          <Checkbox
                            key={field.key}
                            checked={config.columns.some(
                              (column) => column.field === field.key,
                            )}
                            onChange={(event) =>
                              selectField(field, event.target.checked)
                            }
                          >
                            {field.label}
                          </Checkbox>
                        ))}
                      </div>
                    </Drawer>
                    {screens.md && (
                      <div className={styles.fields}>
                        <Input
                          aria-label="搜索可用字段"
                          placeholder="搜索字段"
                          value={search}
                          onChange={(e) => setSearch(e.target.value)}
                        />
                        {fields
                          .filter((f) => f.label.includes(search))
                          .map((f) => (
                            <Checkbox
                              key={f.key}
                              checked={config.columns.some(
                                (c) => c.field === f.key,
                              )}
                              onChange={(e) => selectField(f, e.target.checked)}
                            >
                              {f.label}
                            </Checkbox>
                          ))}
                      </div>
                    )}
                    <Table
                      rowKey="field"
                      size="small"
                      pagination={false}
                      dataSource={config.columns}
                      scroll={{ x: 790 }}
                      columns={[
                        {
                          title: '顺序',
                          width: 85,
                          render: (_, c, index) => (
                            <Space size={0}>
                              <Button
                                aria-label={`${c.title}上移`}
                                size="small"
                                icon={<ArrowUpOutlined />}
                                disabled={index === 0}
                                onClick={() => move(index, -1)}
                              />
                              <Button
                                aria-label={`${c.title}下移`}
                                size="small"
                                icon={<ArrowDownOutlined />}
                                disabled={index === config.columns.length - 1}
                                onClick={() => move(index, 1)}
                              />
                            </Space>
                          ),
                        },
                        {
                          title: '数据字段',
                          dataIndex: 'field',
                          width: 110,
                          render: (value) =>
                            fields.find((f) => f.key === value)?.label ?? value,
                        },
                        {
                          title: '列标题',
                          width: 150,
                          render: (_, c, i) => (
                            <Input
                              aria-label={`${c.field}列标题`}
                              status={
                                !c.title.trim() ||
                                config.columns.filter(
                                  (column) =>
                                    column.title.trim() === c.title.trim(),
                                ).length > 1
                                  ? 'error'
                                  : undefined
                              }
                              value={c.title}
                              onChange={(e) =>
                                updateColumn(i, { title: e.target.value })
                              }
                            />
                          ),
                        },
                        {
                          title: '格式',
                          width: 110,
                          render: (_, c, i) => (
                            <Select
                              aria-label={`${c.title}格式`}
                              style={{ width: 95 }}
                              value={c.format}
                              options={[
                                ...new Set([
                                  'text',
                                  fields.find((f) => f.key === c.field)?.type ??
                                    'text',
                                ]),
                              ].map((f) => ({
                                value: f,
                                label: formatNames[f],
                              }))}
                              onChange={(value) =>
                                updateColumn(i, {
                                  format: value,
                                  sum: value === 'money' && c.sum,
                                })
                              }
                            />
                          ),
                        },
                        {
                          title: '列宽',
                          width: 90,
                          render: (_, c, i) => (
                            <InputNumber
                              aria-label={`${c.title}列宽`}
                              min={5}
                              max={80}
                              value={c.width}
                              onChange={(value) =>
                                updateColumn(i, { width: value ?? 20 })
                              }
                            />
                          ),
                        },
                        {
                          title: '对齐',
                          width: 95,
                          render: (_, c, i) => (
                            <Select
                              aria-label={`${c.title}对齐`}
                              value={c.align}
                              options={[
                                { value: 'left', label: '左' },
                                { value: 'center', label: '中' },
                                { value: 'right', label: '右' },
                              ]}
                              onChange={(value) =>
                                updateColumn(i, { align: value })
                              }
                            />
                          ),
                        },
                        {
                          title: '汇总',
                          width: 60,
                          render: (_, c, i) => (
                            <Checkbox
                              aria-label={`${c.title}汇总`}
                              checked={c.sum}
                              disabled={
                                !fields.find((f) => f.key === c.field)
                                  ?.summable || c.format !== 'money'
                              }
                              onChange={(e) =>
                                updateColumn(i, { sum: e.target.checked })
                              }
                            />
                          ),
                        },
                        {
                          title: '移除',
                          width: 60,
                          render: (_, c) => (
                            <Button
                              aria-label={`移除${c.title}`}
                              danger
                              icon={<DeleteOutlined />}
                              onClick={() =>
                                update({
                                  ...config,
                                  columns: config.columns.filter(
                                    (item) => item.field !== c.field,
                                  ),
                                })
                              }
                            />
                          ),
                        },
                      ]}
                    />
                  </div>
                ),
              },
              {
                key: 'style',
                label: '样式与汇总',
                children: (
                  <Space direction="vertical" size="large">
                    <Space wrap>
                      <label>
                        表头颜色{' '}
                        <input
                          aria-label="表头颜色"
                          type="color"
                          value={config.style.header_color}
                          onChange={(e) =>
                            update({
                              ...config,
                              style: {
                                ...config.style,
                                header_color: e.target.value,
                              },
                            })
                          }
                        />
                      </label>
                      <label>
                        字号{' '}
                        <InputNumber
                          aria-label="字号"
                          min={8}
                          max={24}
                          value={config.style.font_size}
                          onChange={(value) =>
                            update({
                              ...config,
                              style: {
                                ...config.style,
                                font_size: value ?? 11,
                              },
                            })
                          }
                        />
                      </label>
                    </Space>
                    <Space wrap>
                      {(['borders', 'striped'] as const).map((key, i) => (
                        <Checkbox
                          key={key}
                          checked={config.style[key]}
                          onChange={(e) =>
                            update({
                              ...config,
                              style: {
                                ...config.style,
                                [key]: e.target.checked,
                              },
                            })
                          }
                        >
                          {['边框', '交替行底色'][i]}
                        </Checkbox>
                      ))}
                      {(
                        ['freeze_header', 'auto_filter', 'show_count'] as const
                      ).map((key, i) => (
                        <Checkbox
                          key={key}
                          checked={config[key]}
                          onChange={(e) =>
                            update({ ...config, [key]: e.target.checked })
                          }
                        >
                          {['冻结表头', '自动筛选', '显示发票数量'][i]}
                        </Checkbox>
                      ))}
                    </Space>
                  </Space>
                ),
              },
            ]}
          />
        </Card>
        <Card
          title="预览"
          extra={
            <Select
              aria-label="预览数据"
              mode="multiple"
              maxTagCount={1}
              allowClear
              style={{ width: 260 }}
              placeholder="内置样例（可选择发票）"
              value={previewIds}
              options={invoiceOptions}
              onChange={setPreviewIds}
              onFocus={async () => {
                try {
                  const data = await listInvoices({ page_size: 100 });
                  setInvoiceOptions(
                    data.items.map((i) => ({
                      value: i.id,
                      label: `#${i.id} ${i.file_name}`,
                    })),
                  );
                } catch (e) {
                  message.error(workflowError(e));
                }
              }}
            />
          }
        >
          <Alert
            type="info"
            showIcon
            message={
              previewIds.length
                ? '当前预览使用所选发票；下载样例可检查实际 Excel 效果。'
                : '当前使用内置样例数据；下载样例可检查实际 Excel 效果。'
            }
            style={{ marginBottom: 16 }}
          />
          {preview ? (
            <div className={styles.preview}>
              <h3 style={{ textAlign: 'center' }}>{preview.title}</h3>
              <table style={{ fontSize: config.style.font_size }}>
                <thead
                  style={{
                    background: config.style.header_color,
                    color: '#fff',
                  }}
                >
                  <tr>
                    {preview.columns.map((c) => (
                      <th key={c.field}>{c.title}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {preview.rows.map((row, i) => (
                    <tr
                      key={i}
                      style={{
                        background:
                          config.style.striped && i % 2
                            ? 'var(--surface)'
                            : undefined,
                      }}
                    >
                      {row.map((cell, j) => (
                        <td
                          key={j}
                          style={{
                            textAlign: config.columns[j]?.align,
                            border: config.style.borders ? undefined : 'none',
                          }}
                        >
                          {cell}
                        </td>
                      ))}
                    </tr>
                  ))}
                  {preview.totals?.some((v) => v !== null && v !== '') && (
                    <tr>
                      {preview.totals.map((cell, j) => (
                        <td key={j}>{cell}</td>
                      ))}
                    </tr>
                  )}
                </tbody>
              </table>
              {config.show_count && <p>合计：{preview.count} 张</p>}
            </div>
          ) : (
            <Alert
              type="warning"
              message="请检查列配置，填写合法且不重复的列标题、工作表名称后预览。"
            />
          )}
        </Card>
      </div>
    </div>
  );
}
