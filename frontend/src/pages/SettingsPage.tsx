import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AutoComplete, Button, Card, Form, Input, InputNumber, Modal, Popconfirm,
  Radio, Space, Switch, Table, Tag, message,
} from 'antd';
import {
  CloudDownloadOutlined, DeleteOutlined, EditOutlined, PlayCircleOutlined,
  PlusOutlined, ReloadOutlined, ThunderboltOutlined,
} from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import {
  activateProvider, createProvider, deleteProvider, fetchProviderModels,
  getAppSettings, getLLMStatus, listProviders, testSavedProvider,
  updateAppSettings, updateProvider,
} from '../services/api';
import type {
  AppSettings, LlmProvider, ProviderProtocol,
} from '../types/settings';
import type { User } from '../types/user';
import UsersPage from './UsersPage';
import styles from './SettingsPage.module.css';

const PROTOCOL_LABELS: Record<ProviderProtocol, string> = {
  openai_compatible: 'OpenAI 兼容',
  anthropic_compatible: 'Anthropic 兼容',
  google: 'Google Gemini',
};

interface ProviderFormValues {
  display_name: string;
  protocol: ProviderProtocol;
  base_url?: string;
  api_key?: string;
  model?: string;
  vision: boolean;
  enabled: boolean;
  api_mode?: string;
  temperature?: number | null;
  max_output_tokens?: number | null;
}

interface CreateFormValues extends ProviderFormValues {
  name: string;
}

interface SettingsPageProps {
  currentUser: User;
}

export default function SettingsPage({ currentUser }: SettingsPageProps) {
  const [providers, setProviders] = useState<LlmProvider[]>([]);
  const [providersLoading, setProvidersLoading] = useState(false);
  const [activeProviderId, setActiveProviderId] = useState<number | null>(null);
  const [editing, setEditing] = useState<LlmProvider | null>(null);
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState<number | null>(null);
  const [createForm] = Form.useForm<CreateFormValues>();
  const [editForm] = Form.useForm<ProviderFormValues>();
  const [modelOptions, setModelOptions] = useState<{ value: string }[]>([]);
  const [fetchingModels, setFetchingModels] = useState(false);
  const createProtocol = Form.useWatch('protocol', createForm);

  const [appSettings, setAppSettings] = useState<AppSettings | null>(null);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [ocrForm] = Form.useForm();
  const [mcpForm] = Form.useForm();

  const fetchProviders = useCallback(async () => {
    setProvidersLoading(true);
    try {
      const [rows, status] = await Promise.all([listProviders(), getLLMStatus()]);
      setProviders(rows);
      setActiveProviderId(status.active_provider?.id ?? null);
    } catch {
      message.error('获取供应商列表失败');
    } finally {
      setProvidersLoading(false);
    }
  }, []);

  const fetchAppSettings = useCallback(async () => {
    try {
      const data = await getAppSettings();
      setAppSettings(data);
      ocrForm.setFieldsValue({
        ocr_engine: data.ocr_engine,
        paddle_ocr_endpoint: data.paddle_ocr_endpoint ?? '',
        baidu_ocr_api_key: '',
        baidu_ocr_secret_key: '',
      });
      mcpForm.setFieldsValue({
        mcp_enabled: data.mcp_enabled,
        mcp_token: '',
      });
    } catch {
      message.error('获取系统设置失败');
    }
  }, [ocrForm, mcpForm]);

  useEffect(() => {
    fetchProviders();
    fetchAppSettings();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleActivate = async (provider: LlmProvider) => {
    try {
      await activateProvider(provider.id);
      setActiveProviderId(provider.id);
      message.success(`已激活 ${provider.display_name}`);
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '激活失败');
    }
  };

  const handleTest = async (provider: LlmProvider) => {
    setTestingId(provider.id);
    try {
      const result = await testSavedProvider(provider.id);
      if (result.success) {
        message.success(`${provider.display_name}: ${result.message} (${result.response_time_ms}ms)`);
      } else {
        message.warning(`${provider.display_name}: ${result.message}`);
      }
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '测试失败');
    } finally {
      setTestingId(null);
    }
  };

  const handleCreate = async (values: CreateFormValues) => {
    setSaving(true);
    try {
      await createProvider(values);
      message.success(`已添加供应商 ${values.display_name}`);
      setCreating(false);
      createForm.resetFields();
      fetchProviders();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '添加失败');
    } finally {
      setSaving(false);
    }
  };

  const handleFetchModels = async () => {
    if (!editing) return;
    if (!editing.has_api_key) {
      message.warning('请先填写该供应商的 API Key 并保存');
      return;
    }
    setFetchingModels(true);
    try {
      const result = await fetchProviderModels(editing.id);
      if (result.message) {
        message.info(result.message);
      }
      if (result.models.length > 0) {
        setModelOptions(result.models.map((m) => ({ value: m })));
        message.success(`已拉取 ${result.models.length} 个模型`);
      }
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '拉取模型列表失败');
    } finally {
      setFetchingModels(false);
    }
  };

  const openEdit = (provider: LlmProvider) => {
    setEditing(provider);
    setModelOptions(provider.model ? [{ value: provider.model }] : []);
    editForm.setFieldsValue({
      display_name: provider.display_name,
      protocol: provider.protocol,
      base_url: provider.base_url ?? '',
      api_key: '',
      model: provider.model ?? '',
      vision: provider.vision,
      enabled: provider.enabled,
      api_mode: provider.api_mode || 'chat',
      temperature: provider.temperature,
      max_output_tokens: provider.max_output_tokens,
    });
  };

  const handleUpdate = async (values: ProviderFormValues) => {
    if (!editing) return;
    setSaving(true);
    try {
      await updateProvider(editing.id, {
        display_name: values.display_name,
        base_url: values.base_url ?? '',
        api_key: values.api_key || undefined,
        model: values.model ?? '',
        vision: values.vision,
        enabled: values.enabled,
        api_mode: values.api_mode || 'chat',
        // explicit null clears the override (backend distinguishes null vs absent)
        temperature: values.temperature ?? null,
        max_output_tokens: values.max_output_tokens ?? null,
      });
      message.success('已保存');
      setEditing(null);
      fetchProviders();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (provider: LlmProvider) => {
    try {
      await deleteProvider(provider.id);
      message.success(`已删除 ${provider.display_name}`);
      if (activeProviderId === provider.id) setActiveProviderId(null);
      fetchProviders();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '删除失败');
    }
  };

  const handleSaveOcr = async (values: {
    ocr_engine: string;
    paddle_ocr_endpoint?: string;
    baidu_ocr_api_key?: string;
    baidu_ocr_secret_key?: string;
  }) => {
    setSettingsSaving(true);
    try {
      const data = await updateAppSettings({
        ocr_engine: values.ocr_engine,
        // endpoint is not a secret: always send so clearing works (empty = 内置引擎)
        paddle_ocr_endpoint: values.paddle_ocr_endpoint ?? '',
        baidu_ocr_api_key: values.baidu_ocr_api_key || undefined,
        baidu_ocr_secret_key: values.baidu_ocr_secret_key || undefined,
      });
      setAppSettings(data);
      message.success('OCR 引擎设置已保存');
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '保存失败');
    } finally {
      setSettingsSaving(false);
    }
  };

  const handleSaveMcp = async (values: { mcp_enabled: boolean; mcp_token?: string }) => {
    setSettingsSaving(true);
    try {
      const data = await updateAppSettings({
        mcp_enabled: values.mcp_enabled,
        mcp_token: values.mcp_token || undefined,
      });
      setAppSettings(data);
      message.success('MCP 设置已保存');
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '保存失败');
    } finally {
      setSettingsSaving(false);
    }
  };

  const columns: ColumnsType<LlmProvider> = useMemo(() => [
    {
      title: '供应商',
      dataIndex: 'display_name',
      key: 'display_name',
      render: (val: string, record) => (
        <Space>
          <span>{val}</span>
          {record.id === activeProviderId && <Tag color="red">使用中</Tag>}
          {!record.enabled && <Tag>已停用</Tag>}
        </Space>
      ),
    },
    {
      title: '协议',
      dataIndex: 'protocol',
      key: 'protocol',
      width: 140,
      render: (val: ProviderProtocol, record) => (
        <Space size={4}>
          <span>{PROTOCOL_LABELS[val] ?? val}</span>
          {val === 'openai_compatible' && record.api_mode === 'responses' && (
            <Tag style={{ marginInlineEnd: 0 }}>Responses</Tag>
          )}
        </Space>
      ),
    },
    {
      title: '模型',
      dataIndex: 'model',
      key: 'model',
      width: 180,
      ellipsis: true,
      render: (val: string | null, record) => (
        <Space size={4}>
          <span>{val || '-'}</span>
          {record.vision && <Tag color="blue" style={{ marginInlineEnd: 0 }}>视觉</Tag>}
        </Space>
      ),
    },
    {
      title: 'API 地址',
      dataIndex: 'base_url',
      key: 'base_url',
      width: 200,
      ellipsis: true,
      render: (val: string | null) => val || '默认',
    },
    {
      title: '密钥',
      dataIndex: 'api_key_masked',
      key: 'api_key',
      width: 120,
      render: (val: string | null, record) =>
        record.has_api_key ? <Tag color="green">{val}</Tag> : <Tag>未配置</Tag>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 280,
      render: (_, record) => (
        <Space>
          <Button
            size="small"
            type={record.id === activeProviderId ? 'default' : 'primary'}
            icon={<ThunderboltOutlined />}
            disabled={record.id === activeProviderId || !record.enabled}
            onClick={() => handleActivate(record)}
          >
            激活
          </Button>
          <Button
            size="small"
            icon={<PlayCircleOutlined />}
            loading={testingId === record.id}
            disabled={!record.has_api_key}
            onClick={() => handleTest(record)}
          >
            测试
          </Button>
          <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(record)}>
            编辑
          </Button>
          {!record.is_preset && (
            <Popconfirm
              title={`确认删除 ${record.display_name}？`}
              onConfirm={() => handleDelete(record)}
            >
              <Button size="small" danger icon={<DeleteOutlined />} />
            </Popconfirm>
          )}
        </Space>
      ),
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [activeProviderId, testingId, providers]);

  return (
    <div className={styles.pageContainer}>
      <div className={styles.contentWrapper}>
        <div className={styles.pageHeader}>
          <div>
            <h1 className={styles.pageTitle}>系统设置</h1>
            <p className={styles.pageSubtitle}>AI 供应商、识别引擎与接口配置</p>
          </div>
        </div>

        {/* Section 1: AI providers */}
        <Card
          title="AI 供应商"
          extra={
            <Space>
              <Button icon={<ReloadOutlined />} onClick={fetchProviders}>刷新</Button>
              <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreating(true)}>
                添加自定义供应商
              </Button>
            </Space>
          }
        >
          <p className={styles.sectionHint}>
            支持 OpenAI / Anthropic 兼容协议的任意端点。预置国产供应商（DeepSeek、GLM、通义千问）开箱即用，填写密钥后点击"激活"即可用于发票识别。
          </p>
          <Table<LlmProvider>
            rowKey="id"
            columns={columns}
            dataSource={providers}
            loading={providersLoading}
            pagination={false}
            size="middle"
          />
        </Card>

        {/* Section 2: OCR engine */}
        <Card title="OCR 识别引擎">
          <Form form={ocrForm} layout="vertical" onFinish={handleSaveOcr} className={styles.form}>
            <Form.Item name="ocr_engine" label="引擎选择" rules={[{ required: true }]}>
              <Radio.Group>
                <Radio value="local">本地 PaddleOCR（免费、离线、默认）</Radio>
                <Radio value="baidu">百度智能云 · 增值税发票识别（需凭据，按量计费）</Radio>
              </Radio.Group>
            </Form.Item>
            <Form.Item
              name="paddle_ocr_endpoint"
              label="PaddleOCR 自定义服务地址（可选）"
              extra="选择本地引擎时生效：填写 PaddleX 服务地址（如 http://192.168.66.32:8080/ocr，AIStudio 风格 JSON 接口），自动兼容官方上传式产线接口；留空使用内置本地引擎"
            >
              <Input placeholder="如 http://192.168.1.10:9300/ocr（留空使用内置引擎）" />
            </Form.Item>
            <div className={styles.twoColumns}>
              <Form.Item
                name="baidu_ocr_api_key"
                label={`百度云 API Key${appSettings?.has_baidu_ocr_api_key ? `（已配置 ${appSettings.baidu_ocr_api_key_masked}，留空保持不变）` : ''}`}
              >
                <Input.Password placeholder="百度智能云 API Key" autoComplete="new-password" />
              </Form.Item>
              <Form.Item
                name="baidu_ocr_secret_key"
                label={`百度云 Secret Key${appSettings?.has_baidu_ocr_secret_key ? `（已配置 ${appSettings.baidu_ocr_secret_key_masked}，留空保持不变）` : ''}`}
              >
                <Input.Password placeholder="百度智能云 Secret Key" autoComplete="new-password" />
              </Form.Item>
            </div>
            <Button type="primary" htmlType="submit" loading={settingsSaving}>
              保存 OCR 设置
            </Button>
          </Form>
        </Card>

        {/* Section 3: MCP */}
        <Card title="MCP 接口（外部 AI 智能体）">
          <Form form={mcpForm} layout="vertical" onFinish={handleSaveMcp} className={styles.form}>
            <Form.Item name="mcp_enabled" label="启用 MCP 端点" valuePropName="checked">
              <Switch checkedChildren="启用" unCheckedChildren="停用" />
            </Form.Item>
            <Form.Item
              name="mcp_token"
              label={`访问令牌${appSettings?.has_mcp_token ? `（已配置 ${appSettings.mcp_token_masked}，留空保持不变）` : '（启用时必填）'}`}
              extra="外部智能体以 Bearer Token 访问 /api/mcp，权限等同管理员只读。"
            >
              <Input.Password placeholder="长随机字符串" autoComplete="new-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={settingsSaving}>
              保存 MCP 设置
            </Button>
          </Form>
        </Card>

        {/* Section 4: User management */}
        <Card title="用户管理">
          <UsersPage currentUser={currentUser} embedded />
        </Card>
      </div>

      {/* Create modal */}
      <Modal
        title="添加自定义供应商"
        open={creating}
        onCancel={() => setCreating(false)}
        onOk={() => createForm.submit()}
        confirmLoading={saving}
        okText="添加"
        cancelText="取消"
      >
        <Form form={createForm} layout="vertical" onFinish={handleCreate} initialValues={{ protocol: 'openai_compatible', vision: true, enabled: true, api_mode: 'chat' }}>
          <Form.Item
            name="name"
            label="标识（小写字母/数字/中划线）"
            rules={[
              { required: true, message: '请输入标识' },
              { pattern: /^[a-z0-9][a-z0-9_-]*$/, message: '仅小写字母、数字、中划线/下划线' },
            ]}
          >
            <Input placeholder="如 my-openrouter" />
          </Form.Item>
          <Form.Item name="display_name" label="显示名称" rules={[{ required: true, message: '请输入显示名称' }]}>
            <Input placeholder="如 OpenRouter 中转" />
          </Form.Item>
          <Form.Item name="protocol" label="协议" rules={[{ required: true }]}>
            <Radio.Group>
              <Radio value="openai_compatible">OpenAI 兼容</Radio>
              <Radio value="anthropic_compatible">Anthropic 兼容</Radio>
              <Radio value="google">Google Gemini</Radio>
            </Radio.Group>
          </Form.Item>
          {createProtocol === 'openai_compatible' && (
            <Form.Item name="api_mode" label="接口模式" rules={[{ required: true }]}>
              <Radio.Group>
                <Radio value="chat">Chat Completions（/chat/completions）</Radio>
                <Radio value="responses">Responses API（/responses）</Radio>
              </Radio.Group>
            </Form.Item>
          )}
          <Form.Item name="base_url" label="API 地址 (base_url)">
            <Input placeholder="如 https://openrouter.ai/api/v1（OpenAI 官方可留空）" />
          </Form.Item>
          <Form.Item name="api_key" label="API Key">
            <Input.Password placeholder="sk-..." autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="model" label="模型名称" rules={[{ required: true, message: '请输入模型名称' }]}>
            <AutoComplete
              placeholder="如 gpt-4o / glm-4v-flash / qwen-vl-plus"
              options={modelOptions}
              filterOption={(input, option) =>
                (option?.value ?? '').toLowerCase().includes(input.toLowerCase())}
            />
          </Form.Item>
          <Space size={24}>
            <Form.Item
              name="temperature"
              label="temperature（0-2，留空默认）"
              rules={[{ type: 'number', min: 0, max: 2, message: '范围 0-2' }]}
            >
              <InputNumber placeholder="默认" style={{ width: 140 }} />
            </Form.Item>
            <Form.Item name="max_output_tokens" label="max_output_tokens（留空默认）">
              <InputNumber placeholder="默认" min={1} style={{ width: 160 }} />
            </Form.Item>
          </Space>
          <Form.Item name="vision" label="该模型支持图片输入（视觉）" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>

      {/* Edit modal */}
      <Modal
        title={`编辑：${editing?.display_name ?? ''}`}
        open={editing !== null}
        onCancel={() => setEditing(null)}
        onOk={() => editForm.submit()}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
      >
        <Form form={editForm} layout="vertical" onFinish={handleUpdate}>
          <Form.Item name="display_name" label="显示名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="协议">
            <Input disabled value={editing ? PROTOCOL_LABELS[editing.protocol] : ''} />
          </Form.Item>
          {editing?.protocol === 'openai_compatible' && (
            <Form.Item name="api_mode" label="接口模式" rules={[{ required: true }]}>
              <Radio.Group>
                <Radio value="chat">Chat Completions（/chat/completions）</Radio>
                <Radio value="responses">Responses API（/responses）</Radio>
              </Radio.Group>
            </Form.Item>
          )}
          <Form.Item name="base_url" label="API 地址 (base_url)">
            <Input placeholder="留空使用协议默认地址" />
          </Form.Item>
          <Form.Item
            name="api_key"
            label={`API Key${editing?.has_api_key ? `（已配置 ${editing.api_key_masked}，留空保持不变）` : ''}`}
          >
            <Input.Password placeholder="sk-..." autoComplete="new-password" />
          </Form.Item>
          <Form.Item label="模型名称" required>
            <Space.Compact style={{ width: '100%' }}>
              <Form.Item name="model" noStyle rules={[{ required: true, message: '请输入模型名称' }]}>
                <AutoComplete
                  placeholder="输入或从拉取列表选择"
                  options={modelOptions}
                  filterOption={(input, option) =>
                    (option?.value ?? '').toLowerCase().includes(input.toLowerCase())}
                />
              </Form.Item>
              <Button
                icon={<CloudDownloadOutlined />}
                loading={fetchingModels}
                onClick={handleFetchModels}
                disabled={!editing}
              >
                拉取模型
              </Button>
            </Space.Compact>
          </Form.Item>
          <Space size={24}>
            <Form.Item
              name="temperature"
              label="temperature（0-2，留空默认）"
              rules={[{ type: 'number', min: 0, max: 2, message: '范围 0-2' }]}
            >
              <InputNumber placeholder="默认" style={{ width: 140 }} />
            </Form.Item>
            <Form.Item name="max_output_tokens" label="max_output_tokens（留空默认）">
              <InputNumber placeholder="默认" min={1} style={{ width: 160 }} />
            </Form.Item>
          </Space>
          <Space size={24}>
            <Form.Item name="vision" label="支持图片输入（视觉）" valuePropName="checked">
              <Switch />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </div>
  );
}
