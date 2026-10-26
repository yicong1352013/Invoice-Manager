import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import api from '../services/api';
import {
  workflowError,
  type NotificationChannel,
} from '../services/workflowApi';
import styles from './WorkflowPages.module.css';

export default function NotificationChannelsPage() {
  const [rows, setRows] = useState<NotificationChannel[]>([]);
  const [editing, setEditing] = useState<
    NotificationChannel | null | undefined
  >();
  const [busy, setBusy] = useState(false);
  const [discovering, setDiscovering] = useState<number>();
  const [targets, setTargets] = useState<{ target: string; kind: string }[]>(
    [],
  );
  const [testChannel, setTestChannel] = useState<NotificationChannel>();
  const [testTarget, setTestTarget] = useState('');
  const [form] = Form.useForm();
  const kind = Form.useWatch('kind', form);
  const load = useCallback(async () => {
    try {
      setRows((await api.get('/notification-channels')).data);
    } catch (e) {
      message.error(workflowError(e));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  function edit(row: NotificationChannel | null) {
    form.resetFields();
    form.setFieldsValue(
      row ?? {
        kind: 'email',
        enabled: true,
        config: { port: 587, tls: 'starttls', max_file_size_mb: 20 },
      },
    );
    setEditing(row);
  }
  async function save() {
    try {
      const values = await form.validateFields();
      setBusy(true);
      if (editing)
        await api.put(`/notification-channels/${editing.id}`, values);
      else await api.post('/notification-channels', values);
      setEditing(undefined);
      await load();
      message.success('渠道已保存');
    } catch (e) {
      if (!(e && typeof e === 'object' && 'errorFields' in e))
        message.error(workflowError(e));
    } finally {
      setBusy(false);
    }
  }
  async function testConnection(row: NotificationChannel) {
    setBusy(true);
    try {
      const result = (
        await api.post(`/notification-channels/${row.id}/test-connection`)
      ).data;
      message.success(result.message || '连接检查通过');
    } catch (e) {
      message.error(workflowError(e));
    } finally {
      setBusy(false);
    }
  }
  async function discover(row: NotificationChannel) {
    setDiscovering(row.id);
    setTargets([]);
    try {
      setTargets(
        (
          await api.post(`/notification-channels/${row.id}/discover-targets`, {
            seconds: 15,
          })
        ).data.targets,
      );
    } catch (e) {
      message.error(workflowError(e));
    } finally {
      setDiscovering(undefined);
    }
  }
  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1>通知渠道</h1>
          <p className={styles.hint}>
            <Link to="/settings">系统设置</Link> / 配置邮件与企业微信智能机器人
          </p>
        </div>
        <Button type="primary" onClick={() => edit(null)}>
          新增渠道
        </Button>
      </div>
      <div className={styles.stack}>
        <Alert
          type="info"
          showIcon
          message="工作流发送摘要及导出文件；收件目标和默认消息在工作流中设置。"
        />
        {discovering && (
          <Alert
            type="info"
            showIcon
            message="正在等待会话事件（15 秒）：请在机器人单聊或所在群中 @机器人发送一条消息。"
          />
        )}
        {targets.length > 0 && (
          <Card title="发现的会话目标">
            <Space direction="vertical">
              {targets.map((t) => (
                <Typography.Text key={t.target} copyable={{ text: t.target }}>
                  {t.kind === 'group' ? '群聊' : '用户'}：{t.target}
                </Typography.Text>
              ))}
            </Space>
          </Card>
        )}
        <Table
          rowKey="id"
          dataSource={rows}
          scroll={{ x: 750 }}
          columns={[
            { title: '名称', dataIndex: 'name' },
            {
              title: '渠道',
              dataIndex: 'kind',
              render: (value) =>
                value === 'email' ? '电子邮件' : '企业微信智能机器人',
            },
            {
              title: '状态',
              dataIndex: 'enabled',
              render: (value) => (
                <Tag color={value ? 'green' : 'default'}>
                  {value ? '启用' : '停用'}
                </Tag>
              ),
            },
            {
              title: '凭据',
              dataIndex: 'has_secret',
              render: (value) => (value ? '已配置' : '未配置'),
            },
            {
              title: '操作',
              render: (_, row) => (
                <Space wrap>
                  <Button size="small" onClick={() => edit(row)}>
                    编辑
                  </Button>
                  <Button
                    size="small"
                    disabled={busy}
                    onClick={() => testConnection(row)}
                  >
                    检查连接
                  </Button>
                  <Button
                    size="small"
                    onClick={() => {
                      setTestTarget('');
                      setTestChannel(row);
                    }}
                  >
                    发送测试
                  </Button>
                  {row.kind === 'wecom_aibot' && (
                    <Button
                      size="small"
                      loading={discovering === row.id}
                      disabled={discovering !== undefined}
                      onClick={() => discover(row)}
                    >
                      绑定目标
                    </Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </div>
      <Modal
        open={editing !== undefined}
        title={editing ? '编辑通知渠道' : '新增通知渠道'}
        onCancel={() => setEditing(undefined)}
        onOk={save}
        confirmLoading={busy}
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, whitespace: true }]}
          >
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item name="kind" label="渠道类型">
            <Select
              disabled={!!editing}
              onChange={(value) =>
                form.setFieldValue(
                  'config',
                  value === 'email'
                    ? { port: 587, tls: 'starttls', max_file_size_mb: 20 }
                    : { max_file_size_mb: 20 },
                )
              }
              options={[
                { value: 'email', label: '电子邮件' },
                { value: 'wecom_aibot', label: '企业微信智能机器人' },
              ]}
            />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch />
          </Form.Item>
          {kind === 'email' ? (
            <>
              <Form.Item
                name={['config', 'host']}
                label="SMTP 服务器"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <div className={styles.grid}>
                <Form.Item
                  name={['config', 'port']}
                  label="端口"
                  rules={[{ required: true }]}
                >
                  <InputNumber min={1} max={65535} />
                </Form.Item>
                <Form.Item name={['config', 'tls']} label="加密方式">
                  <Select
                    options={[
                      { value: 'starttls', label: 'STARTTLS' },
                      { value: 'ssl', label: 'SSL/TLS' },
                    ]}
                  />
                </Form.Item>
              </div>
              <Form.Item name={['config', 'username']} label="认证用户名">
                <Input autoComplete="off" />
              </Form.Item>
              <Form.Item
                name={['config', 'from_address']}
                label="发件人邮箱"
                rules={[{ required: true }, { type: 'email' }]}
              >
                <Input />
              </Form.Item>
            </>
          ) : (
            <Form.Item
              name={['config', 'bot_id']}
              label="Bot ID"
              rules={[{ required: true }]}
            >
              <Input />
            </Form.Item>
          )}
          <Form.Item
            name="secret"
            label={kind === 'email' ? 'SMTP 密码 / 授权码' : 'Bot Secret'}
            extra={
              editing
                ? '留空保留已有凭据；保存后不会返回明文。'
                : '凭据加密保存。'
            }
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name={['config', 'max_file_size_mb']}
            label="单次发送文件限制（MB）"
            rules={[{ required: true }]}
          >
            <InputNumber min={1} max={50} />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        open={!!testChannel}
        title={`发送真实测试消息 · ${testChannel?.name}`}
        onCancel={() => setTestChannel(undefined)}
        okText="确认发送"
        confirmLoading={busy}
        onOk={async () => {
          if (!testTarget.trim() || !testChannel) return;
          setBusy(true);
          try {
            await api.post(
              `/notification-channels/${testChannel.id}/test-send`,
              { target: testTarget.trim() },
            );
            message.success('测试消息已提交渠道');
            setTestChannel(undefined);
          } catch (e) {
            message.error(workflowError(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <Alert
          type="warning"
          showIcon
          message="确认后会向下面的目标实际发送测试消息。"
          style={{ marginBottom: 16 }}
        />
        <label htmlFor="test-target">
          {testChannel?.kind === 'email' ? '收件邮箱' : '用户或群会话 ID'}
        </label>
        <Input
          id="test-target"
          value={testTarget}
          onChange={(e) => setTestTarget(e.target.value)}
        />
      </Modal>
    </div>
  );
}
