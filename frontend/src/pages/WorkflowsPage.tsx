import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Steps,
  Switch,
  Table,
  Tabs,
  Tag,
  message,
} from 'antd';
import api from '../services/api';
import {
  downloadWorkflowFile,
  listWorkflows,
  workflowError,
  type WorkflowDefinition,
  type WorkflowRun,
  type NotificationChannel,
} from '../services/workflowApi';
import type { User } from '../types/user';
import { getAiFieldSeverity, summarizeAiVerification } from '../types/invoice';
import styles from './WorkflowPages.module.css';

const statuses: Record<string, string> = {
  draft: '待提交',
  queued: '排队中',
  recognizing: '识别中',
  ocr: '识别中',
  reviewing: '审核中',
  review: '待人工处理',
  awaiting_review: '待人工处理',
  needs_review: '待人工处理',
  exporting: '导出中',
  sending: '发送中',
  delivery_failed: '发送异常',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  sent: '已发送',
  unknown: '结果待确认',
  pending: '等待中',
  recognized: '已识别',
  inflight: '发送中',
  approved: '已确认',
  passed: '通过',
};
const statusLabel = (s: string) => statuses[s] ?? s;
const labels: Record<string, string> = {
  success_subject: '邮件成功通知主题',
  success_body: '成功通知正文',
  review_body: '待人工处理通知',
  failure_body: '失败通知',
};

export default function WorkflowsPage({ currentUser }: { currentUser: User }) {
  const [definitions, setDefinitions] = useState<WorkflowDefinition[]>([]);
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [templates, setTemplates] = useState<
    { id: number; name: string; version: number; enabled: boolean }[]
  >([]);
  const [channels, setChannels] = useState<NotificationChannel[]>([]);
  const [defaults, setDefaults] = useState<Record<string, string>>({});
  const [variables, setVariables] = useState<string[]>([]);
  const [editing, setEditing] = useState<
    WorkflowDefinition | null | undefined
  >();
  const [busy, setBusy] = useState(false);
  const [form] = Form.useForm();
  const [messagePreview, setMessagePreview] = useState<string>();
  const formats = Form.useWatch(['config', 'formats'], form) as
    | string[]
    | undefined;
  const navigate = useNavigate();
  const admin = currentUser.role === 'admin';
  const load = useCallback(async () => {
    try {
      const [defs, execution] = await Promise.all([
        listWorkflows(),
        api.get('/workflow-runs'),
      ]);
      setDefinitions(defs);
      setRuns(execution.data);
      if (admin) {
        const [tpl, channelList, notifications] = await Promise.all([
          api.get('/export-templates'),
          api.get('/notification-channels'),
          api.get('/notification-channels/defaults'),
        ]);
        setTemplates(tpl.data);
        setChannels(channelList.data);
        setDefaults(notifications.data.messages);
        setVariables(notifications.data.variables);
      }
    } catch (e) {
      message.error(workflowError(e));
    }
  }, [admin]);
  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      api
        .get('/workflow-runs')
        .then((r) => setRuns(r.data))
        .catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
  }, [load]);
  function edit(row: WorkflowDefinition | null) {
    setEditing(row);
    form.resetFields();
    form.setFieldsValue(
      row ?? {
        name: '',
        enabled: true,
        config: {
          formats: ['excel'],
          deliveries: [],
          include_originals: false,
          include_attachments: false,
          notify_review: false,
          notify_failure: false,
          messages: defaults,
        },
      },
    );
  }
  async function save() {
    try {
      const values = await form.validateFields();
      setBusy(true);
      if (editing) await api.put(`/workflows/${editing.id}`, values);
      else await api.post('/workflows', values);
      setEditing(undefined);
      await load();
      message.success('工作流已保存');
    } catch (e) {
      if (!(e && typeof e === 'object' && 'errorFields' in e))
        message.error(workflowError(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1>工作流</h1>
          <p className={styles.hint}>
            上传 → OCR → AI 审核修正 → 整批导出 → 按渠道发送
          </p>
        </div>
        <Space>
          <Button onClick={load}>刷新</Button>
          <Button type="primary" onClick={() => navigate('/?upload=workflow')}>
            上传并运行
          </Button>
          {admin && <Button onClick={() => edit(null)}>新建工作流</Button>}
        </Space>
      </div>
      <Tabs
        items={[
          {
            key: 'runs',
            label: '执行记录',
            children: (
              <Table
                rowKey="id"
                dataSource={runs}
                scroll={{ x: 650 }}
                columns={[
                  {
                    title: '批次',
                    dataIndex: 'name',
                    render: (name, row) => (
                      <Link to={`/workflows/runs/${row.id}`}>
                        {name || `批次 #${row.id}`}
                      </Link>
                    ),
                  },
                  {
                    title: '状态',
                    dataIndex: 'state',
                    render: (s) => (
                      <Tag
                        color={
                          s === 'completed'
                            ? 'success'
                            : s === 'failed'
                              ? 'error'
                              : 'blue'
                        }
                      >
                        {statusLabel(s)}
                      </Tag>
                    ),
                  },
                  { title: '创建时间', dataIndex: 'created_at' },
                  { title: '错误 / 待办', dataIndex: 'error', ellipsis: true },
                  {
                    title: '操作',
                    render: (_, row) => (
                      <Button
                        onClick={() => navigate(`/workflows/runs/${row.id}`)}
                      >
                        查看进度
                      </Button>
                    ),
                  },
                ]}
              />
            ),
          },
          {
            key: 'definitions',
            label: '工作流配置',
            children: (
              <Table
                rowKey="id"
                dataSource={definitions}
                columns={[
                  { title: '名称', dataIndex: 'name' },
                  {
                    title: '导出格式',
                    render: (_, row) =>
                      row.config.formats
                        .map((f) => f.toUpperCase())
                        .join(' / '),
                  },
                  {
                    title: '状态',
                    render: (_, row) => (
                      <Tag color={row.enabled ? 'green' : 'default'}>
                        {row.enabled ? '启用' : '停用'}
                      </Tag>
                    ),
                  },
                  ...(admin
                    ? [
                        {
                          title: '操作',
                          render: (_: unknown, row: WorkflowDefinition) => (
                            <Button onClick={() => edit(row)}>编辑</Button>
                          ),
                        },
                      ]
                    : []),
                ]}
              />
            ),
          },
        ]}
      />
      <Modal
        open={editing !== undefined}
        width={820}
        title={editing ? '编辑工作流' : '新建工作流'}
        onCancel={() => setEditing(undefined)}
        onOk={save}
        confirmLoading={busy}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <div className={styles.grid}>
            <Form.Item
              name="name"
              label="工作流名称"
              rules={[{ required: true, whitespace: true }]}
            >
              <Input maxLength={100} />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked">
              <Switch />
            </Form.Item>
          </div>
          <Alert
            type="info"
            showIcon
            message="整批发票审核通过后统一生成文件；不确定或失败的项目等待人工处理。"
            style={{ marginBottom: 20 }}
          />
          <Form.Item
            name={['config', 'formats']}
            label="导出格式"
            rules={[
              {
                required: true,
                type: 'array',
                min: 1,
                message: '至少选择一种导出格式',
              },
            ]}
          >
            <Checkbox.Group
              options={[
                { label: 'Excel 模板', value: 'excel' },
                { label: 'Word 固定格式', value: 'word' },
                { label: 'PDF 固定格式', value: 'pdf' },
              ]}
            />
          </Form.Item>
          {formats?.includes('excel') && (
            <Form.Item
              name={['config', 'template_id']}
              label="Excel 模板"
              rules={[{ required: true, message: '请选择已发布的模板' }]}
              extra={<Link to="/export-templates">管理导出模板</Link>}
            >
              <Select
                placeholder="选择已发布并启用的模板"
                options={templates
                  .filter((t) => t.enabled && t.version > 0)
                  .map((t) => ({
                    value: t.id,
                    label: `${t.name} · v${t.version}`,
                  }))}
              />
            </Form.Item>
          )}
          <Card
            size="small"
            title="发送目标"
            extra={
              <Link to="/settings/notification-channels">管理通知渠道</Link>
            }
          >
            <Form.List
              name={['config', 'deliveries']}
              rules={[
                {
                  validator: async (_, values) => {
                    if (!values?.length)
                      throw new Error('请至少配置一个发送渠道');
                  },
                },
              ]}
            >
              {(items, { add, remove }, { errors }) => (
                <>
                  {items.map(({ key, name, ...rest }) => (
                    <div key={key} className={styles.grid}>
                      <Form.Item
                        {...rest}
                        name={[name, 'channel_id']}
                        label="渠道"
                        rules={[{ required: true }]}
                      >
                        <Select
                          onChange={() =>
                            form.setFieldValue(
                              ['config', 'deliveries', name, 'cc'],
                              [],
                            )
                          }
                          options={channels
                            .filter((c) => c.enabled)
                            .map((c) => ({
                              value: c.id,
                              label: `${c.name}（${c.kind === 'email' ? '邮件' : '企微机器人'}）`,
                            }))}
                        />
                      </Form.Item>
                      <Form.Item
                        {...rest}
                        name={[name, 'targets']}
                        label="收件邮箱 / 会话 ID"
                        rules={[{ required: true, type: 'array', min: 1 }]}
                        extra={
                          <Button
                            type="link"
                            danger
                            size="small"
                            onClick={() => remove(name)}
                          >
                            移除此渠道
                          </Button>
                        }
                      >
                        <Select
                          mode="tags"
                          tokenSeparators={[',', ';', '，', '；']}
                          placeholder="输入后按 Enter，可添加多个目标"
                        />
                      </Form.Item>
                      <Form.Item noStyle shouldUpdate>
                        {() =>
                          channels.find(
                            (c) =>
                              c.id ===
                              form.getFieldValue([
                                'config',
                                'deliveries',
                                name,
                                'channel_id',
                              ]),
                          )?.kind === 'email' ? (
                            <Form.Item
                              {...rest}
                              name={[name, 'cc']}
                              label="抄送邮箱（可选）"
                            >
                              <Select
                                mode="tags"
                                tokenSeparators={[',', ';', '，', '；']}
                                placeholder="输入邮箱后按 Enter"
                              />
                            </Form.Item>
                          ) : null
                        }
                      </Form.Item>
                    </div>
                  ))}
                  <Form.ErrorList errors={errors} />
                  <Button onClick={() => add({ targets: [] })}>
                    添加发送渠道
                  </Button>
                </>
              )}
            </Form.List>
          </Card>
          <Space wrap style={{ margin: '20px 0' }}>
            <Form.Item
              name={['config', 'include_originals']}
              valuePropName="checked"
              noStyle
            >
              <Checkbox>随附原始发票</Checkbox>
            </Form.Item>
            <Form.Item
              name={['config', 'include_attachments']}
              valuePropName="checked"
              noStyle
            >
              <Checkbox>随附关联附件</Checkbox>
            </Form.Item>
          </Space>
          <Card size="small" title="消息模板">
            <p className={styles.hint}>
              支持变量：{variables.map((v) => `{{${v}}}`).join('、')}
            </p>
            <Space wrap style={{ margin: '12px 0' }}>
              <Form.Item
                name={['config', 'notify_review']}
                valuePropName="checked"
                noStyle
              >
                <Checkbox>待人工处理时通知</Checkbox>
              </Form.Item>
              <Form.Item
                name={['config', 'notify_failure']}
                valuePropName="checked"
                noStyle
              >
                <Checkbox>执行失败时通知</Checkbox>
              </Form.Item>
            </Space>
            {Object.entries(labels).map(([key, label]) => (
              <Form.Item
                key={key}
                name={['config', 'messages', key]}
                label={
                  <Space wrap>
                    {label}
                    <Select
                      aria-label={`为${label}插入变量`}
                      value={null}
                      placeholder="插入变量"
                      style={{ width: 150 }}
                      options={variables.map((variable) => ({
                        value: variable,
                        label: variable,
                      }))}
                      onChange={(variable) =>
                        form.setFieldValue(
                          ['config', 'messages', key],
                          (form.getFieldValue(['config', 'messages', key]) ||
                            '') +
                            '{{' +
                            variable +
                            '}}',
                        )
                      }
                    />
                    <Button
                      size="small"
                      type="link"
                      onClick={() =>
                        form.setFieldValue(
                          ['config', 'messages', key],
                          defaults[key],
                        )
                      }
                    >
                      恢复默认
                    </Button>
                    <Button
                      size="small"
                      type="link"
                      onClick={() => {
                        const text =
                          form.getFieldValue(['config', 'messages', key]) ||
                          defaults[key] ||
                          '';
                        const sample: Record<string, string> = {
                          run_name: '月度报销',
                          run_id: '123',
                          invoice_count: '3',
                          total_with_tax: '1,130.00',
                          artifact_names: '发票汇总.xlsx',
                          completed_at: '2026-10-24 10:00',
                          run_url: '/workflows/runs/123',
                          review_count: '1',
                          failed_count: '1',
                          error: '示例错误',
                        };
                        setMessagePreview(
                          text.replace(
                            /\{\{\s*(\w+)\s*\}\}/g,
                            (_: string, variable: string) =>
                              sample[variable] ?? `{{${variable}}}`,
                          ),
                        );
                      }}
                    >
                      预览
                    </Button>
                  </Space>
                }
              >
                <Input.TextArea rows={key === 'success_subject' ? 1 : 4} />
              </Form.Item>
            ))}
          </Card>
        </Form>
      </Modal>
      <Modal
        title="消息预览 · 样例数据"
        open={messagePreview !== undefined}
        onCancel={() => setMessagePreview(undefined)}
        footer={
          <Button onClick={() => setMessagePreview(undefined)}>关闭</Button>
        }
      >
        <div style={{ whiteSpace: 'pre-wrap' }}>{messagePreview}</div>
      </Modal>
    </div>
  );
}

export function WorkflowRunPage() {
  const { id } = useParams();
  const [run, setRun] = useState<WorkflowRun>();
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState<{
    path: string;
    title: string;
    body?: Record<string, unknown>;
  }>();
  const [reason, setReason] = useState('');
  const [fieldLabels, setFieldLabels] = useState<Record<string, string>>({});
  const load = useCallback(async () => {
    try {
      setRun((await api.get(`/workflow-runs/${id}`)).data);
    } catch (e) {
      message.error(workflowError(e));
    }
  }, [id]);
  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      api
        .get(`/workflow-runs/${id}`)
        .then((r) => setRun(r.data))
        .catch(() => {});
    }, 5000);
    return () => clearInterval(timer);
  }, [load, id]);
  useEffect(() => {
    api
      .get('/export-templates/fields')
      .then((response) =>
        setFieldLabels(
          Object.fromEntries(
            response.data.fields.map(
              (field: { key: string; label: string }) => [
                field.key,
                field.label,
              ],
            ),
          ),
        ),
      )
      .catch(() => {});
  }, []);
  async function invoke(path: string, body?: unknown) {
    setBusy(true);
    try {
      await api.post(path, body);
      await load();
      message.success('操作已提交');
      return true;
    } catch (e) {
      message.error(workflowError(e));
      return false;
    } finally {
      setBusy(false);
    }
  }
  function ask(path: string, title: string, body?: Record<string, unknown>) {
    setReason('');
    setAction({ path, title, body });
  }
  if (!run)
    return (
      <div className={styles.page}>
        <Empty description="正在读取批次" />
      </div>
    );
  const phase =
    run.state === 'delivery_failed'
      ? 5
      : [
          'draft',
          'queued',
          'recognizing',
          'reviewing',
          'exporting',
          'sending',
          'completed',
        ].indexOf(run.state);
  const terminal = ['completed', 'cancelled'].includes(run.state);
  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <Link to="/workflows">返回工作流</Link>
          <h1>{run.name || `批次 #${run.id}`}</h1>
          <Tag>{statusLabel(run.state)}</Tag>
        </div>
        <Space wrap>
          <Button onClick={load}>刷新</Button>
          {run.state === 'draft' && (
            <Button
              type="primary"
              loading={busy}
              onClick={() => invoke(`/workflow-runs/${run.id}/start`)}
            >
              提交批次
            </Button>
          )}
          {run.state === 'needs_review' && (
            <Button
              loading={busy}
              onClick={() => invoke(`/workflow-runs/${run.id}/resume`)}
            >
              处理完成，继续
            </Button>
          )}
          {['failed', 'delivery_failed'].includes(run.state) && (
            <Button
              loading={busy}
              onClick={() => invoke(`/workflow-runs/${run.id}/retry`)}
            >
              重试失败步骤
            </Button>
          )}
          {!terminal && (
            <Popconfirm
              title="取消批次？已发送的消息无法撤回。"
              onConfirm={() => invoke(`/workflow-runs/${run.id}/cancel`)}
            >
              <Button danger disabled={busy}>
                取消批次
              </Button>
            </Popconfirm>
          )}
        </Space>
      </div>
      <div className={styles.stack}>
        <Steps
          responsive
          current={phase >= 0 ? phase : 3}
          status={
            ['failed', 'delivery_failed'].includes(run.state)
              ? 'error'
              : 'process'
          }
          items={[
            '上传确认',
            '排队',
            'OCR 识别',
            'AI 审核',
            '导出文件',
            '渠道发送',
            '完成',
          ].map((title) => ({ title }))}
        />
        {run.error && <Alert type="error" showIcon message={run.error} />}
        {run.state === 'draft' && (
          <Alert
            type="info"
            showIcon
            message="请在上传窗口完成全部清单并明确附件归属后提交。关闭窗口后可在同一页面重新打开继续；刷新导致本地文件丢失时，请取消此批次并重新上传。"
          />
        )}
        {run.upload_manifest && (
          <Card title="上传清单">
            <Table
              rowKey="id"
              size="small"
              pagination={false}
              dataSource={(['invoices', 'attachments'] as const).flatMap(
                (kind) =>
                  (run.upload_manifest?.[kind] ?? []).map((file) => ({
                    ...file,
                    kind,
                    uploaded:
                      kind === 'invoices'
                        ? run.items?.some(
                            (item) => item.client_file_id === file.id,
                          )
                        : !!run.uploaded_attachments?.[file.id],
                    error: run.upload_errors?.[file.id],
                  })),
              )}
              columns={[
                { title: '文件', dataIndex: 'name' },
                {
                  title: '类型',
                  dataIndex: 'kind',
                  render: (kind) => (kind === 'invoices' ? '发票' : '附件'),
                },
                {
                  title: '上传状态',
                  dataIndex: 'uploaded',
                  render: (uploaded) => (
                    <Tag color={uploaded ? 'success' : 'warning'}>
                      {uploaded ? '已上传' : '等待上传'}
                    </Tag>
                  ),
                },
                { title: '错误', dataIndex: 'error' },
              ]}
            />
            {run.config_snapshot?.attachment_bindings && (
              <details>
                <summary>已确认的附件归属</summary>
                <ul>
                  {Object.entries(run.config_snapshot.attachment_bindings).map(
                    ([attachmentId, invoiceId]) => {
                      const clientId = Object.entries(
                        run.uploaded_attachments ?? {},
                      ).find(
                        ([, uploadedId]) => String(uploadedId) === attachmentId,
                      )?.[0];
                      const file = run.upload_manifest?.attachments.find(
                        (item) => item.id === clientId,
                      );
                      return (
                        <li key={attachmentId}>
                          <Link to={`/invoices/${invoiceId}`}>
                            发票 #{invoiceId}
                          </Link>{' '}
                          ← {file?.name ?? `附件 #${attachmentId}`}
                        </li>
                      );
                    },
                  )}
                </ul>
              </details>
            )}
          </Card>
        )}
        <Card title="发票处理">
          <Table
            rowKey="id"
            dataSource={run.items ?? []}
            scroll={{ x: 650 }}
            expandable={{
              expandedRowRender: (item) => (
                <Space direction="vertical" style={{ width: '100%' }}>
                  {item.verification ? (
                    <>
                      <Tag color={item.verification.overall === 'pass' ? 'success' : 'error'}>{summarizeAiVerification(item.verification).label}</Tag>
                      {item.verification.summary && <p>{item.verification.summary}</p>}
                    </>
                  ) : <p>暂无审核结论</p>}
                  <Table
                    size="small"
                    pagination={false}
                    rowKey="field"
                    dataSource={item.verification?.fields ?? []}
                    columns={[
                      {
                        title: '字段',
                        dataIndex: 'field',
                        render: (field) => fieldLabels[field] ?? field,
                      },
                      {
                        title: '重要程度',
                        render: (_, field) => {
                          const severity = getAiFieldSeverity(field);
                          return <Tag color={severity === 'blocking' ? 'error' : severity === 'warning' ? 'warning' : 'default'}>{severity === 'blocking' ? '关键问题' : severity === 'warning' ? '提示' : '无需处理'}</Tag>;
                        },
                      },
                      {
                        title: '结论',
                        dataIndex: 'status',
                        render: (status) =>
                          ({
                            correct: '正确',
                            incorrect: '不符',
                            unsure: '存疑',
                          })[status as string] ?? status,
                      },
                      { title: '建议值', dataIndex: 'corrected_value' },
                      { title: '原因', dataIndex: 'reason' },
                    ]}
                  />
                  {!!item.corrections?.length && (
                    <>
                      <strong>自动修正记录</strong>
                      <Table
                        size="small"
                        rowKey="field"
                        pagination={false}
                        dataSource={item.corrections}
                        columns={[
                          {
                            title: '字段',
                            dataIndex: 'field',
                            render: (field) => fieldLabels[field] ?? field,
                          },
                          { title: '原值', dataIndex: 'old' },
                          { title: '新值', dataIndex: 'new' },
                          { title: '理由', dataIndex: 'reason' },
                        ]}
                      />
                    </>
                  )}
                  {item.approval && (
                    <p>
                      人工确认：{item.approval.user} · {item.approval.at} ·{' '}
                      {item.approval.reason}
                    </p>
                  )}
                </Space>
              ),
            }}
            columns={[
              {
                title: '发票',
                dataIndex: 'invoice_id',
                render: (value) => (
                  <Link target="_blank" to={`/invoices/${value}`}>
                    查看 / 修正 #{value}
                  </Link>
                ),
              },
              { title: '状态', dataIndex: 'state', render: statusLabel },
              { title: '说明', dataIndex: 'error' },
              {
                title: '人工处理',
                render: (_, item) =>
                  run.state === 'needs_review' &&
                  item.state === 'needs_review' && (
                    <Button
                      disabled={busy || !item.current_source_hash}
                      onClick={() =>
                        ask(
                          `/workflow-runs/${run.id}/items/${item.id}/approve`,
                          '确认已检查发票内容，填写确认原因',
                          { source_hash: item.current_source_hash },
                        )
                      }
                    >
                      确认审核
                    </Button>
                  ),
              },
            ]}
          />
        </Card>
        <Card title="导出文件">
          <Space wrap>
            {run.artifacts?.length ? (
              run.artifacts.map((file) => (
                <Button
                  key={file.id}
                  onClick={async () => {
                    try {
                      await downloadWorkflowFile(
                        `/workflow-artifacts/${file.id}/download`,
                        file.file_name,
                      );
                    } catch (e) {
                      message.error(workflowError(e));
                    }
                  }}
                >
                  {file.file_name} · {Math.ceil(file.size / 1024)} KB
                </Button>
              ))
            ) : (
              <span className={styles.hint}>整批审核通过后生成</span>
            )}
          </Space>
        </Card>
        <Card title="投递记录">
          <Alert
            showIcon
            type="info"
            message="已发送表示渠道已接收，不表示收件人已阅读。结果待确认时请先核实，重发可能产生重复消息。"
            style={{ marginBottom: 16 }}
          />
          <Table
            rowKey="id"
            dataSource={run.deliveries ?? []}
            scroll={{ x: 700 }}
            expandable={{
              expandedRowRender: (delivery) => (
                <Table
                  rowKey={(_, index) => String(index)}
                  size="small"
                  pagination={false}
                  dataSource={delivery.attempts ?? []}
                  columns={[
                    { title: '时间', dataIndex: 'at' },
                    {
                      title: '结果',
                      render: (_, attempt) =>
                        attempt.action === 'confirm_sent'
                          ? '人工确认已发送'
                          : attempt.action === 'retry'
                            ? '人工要求重发'
                            : statusLabel(attempt.state ?? 'sending'),
                    },
                    {
                      title: '说明',
                      render: (_, attempt) =>
                        attempt.error ||
                        attempt.reason ||
                        (attempt.receipt ? '渠道已返回接收回执' : '等待回执'),
                    },
                  ]}
                />
              ),
            }}
            columns={[
              { title: '目标', dataIndex: 'target' },
              { title: '内容', dataIndex: 'part' },
              { title: '状态', dataIndex: 'state', render: statusLabel },
              {
                title: '尝试次数',
                dataIndex: 'attempts',
                render: (attempts: unknown[]) => attempts?.length ?? 0,
              },
              { title: '错误', dataIndex: 'error' },
              {
                title: '操作',
                render: (_, delivery) =>
                  ['unknown', 'uncertain'].includes(delivery.state) && (
                    <Space>
                      <Button
                        onClick={() =>
                          ask(
                            `/workflow-runs/${run.id}/deliveries/${delivery.id}/resolve`,
                            '确认目标已收到消息',
                            { action: 'confirm_sent' },
                          )
                        }
                      >
                        确认已发送
                      </Button>
                      <Button
                        danger
                        onClick={() =>
                          ask(
                            `/workflow-runs/${run.id}/deliveries/${delivery.id}/resolve`,
                            '重新发送可能产生重复，请填写原因',
                            { action: 'retry' },
                          )
                        }
                      >
                        重新发送
                      </Button>
                    </Space>
                  ),
              },
            ]}
          />
        </Card>
      </div>
      <Modal
        open={!!action}
        title={action?.title}
        confirmLoading={busy}
        onCancel={() => setAction(undefined)}
        okButtonProps={{ disabled: !reason.trim() }}
        onOk={async () => {
          if (
            action &&
            (await invoke(action.path, {
              ...action.body,
              reason: reason.trim(),
            }))
          )
            setAction(undefined);
        }}
      >
        <label htmlFor="review-reason">操作原因</label>
        <Input.TextArea
          id="review-reason"
          value={reason}
          rows={4}
          maxLength={1000}
          onChange={(e) => setReason(e.target.value)}
        />
      </Modal>
    </div>
  );
}
