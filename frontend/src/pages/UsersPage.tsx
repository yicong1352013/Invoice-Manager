import { useCallback, useEffect, useState } from 'react';
import {
  Button, Form, Input, Modal, Popconfirm, Select, Space, Switch, Table, Tag, message,
} from 'antd';
import { DeleteOutlined, KeyOutlined, PlusOutlined, ReloadOutlined, UserAddOutlined } from '@ant-design/icons';
import type { ColumnsType } from 'antd/es/table';
import {
  createUser, deleteUser, listUsers, updateUser,
} from '../services/api';
import type { User, UserRole } from '../types/user';
import styles from './UsersPage.module.css';

interface UsersPageProps {
  currentUser: User;
  /** Render without page-level padding/heading (for embedding in Settings) */
  embedded?: boolean;
}

interface UserFormValues {
  username: string;
  password: string;
  role: UserRole;
}

interface ResetPasswordValues {
  password: string;
}

export default function UsersPage({ currentUser, embedded = false }: UsersPageProps) {
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState<User | null>(null);
  const [creating, setCreating] = useState(false);
  const [createForm] = Form.useForm<UserFormValues>();
  const [resetForm] = Form.useForm<ResetPasswordValues>();

  const fetchUsers = useCallback(async () => {
    setLoading(true);
    try {
      setUsers(await listUsers());
    } catch {
      message.error('获取用户列表失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchUsers();
  }, [fetchUsers]);

  const handleCreate = async (values: UserFormValues) => {
    setCreating(true);
    try {
      await createUser(values);
      message.success(`已创建用户 ${values.username}`);
      setCreateOpen(false);
      createForm.resetFields();
      fetchUsers();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '创建用户失败');
    } finally {
      setCreating(false);
    }
  };

  const handleToggleEnabled = async (user: User, enabled: boolean) => {
    try {
      await updateUser(user.id, { enabled });
      message.success(enabled ? `已启用 ${user.username}` : `已停用 ${user.username}`);
      fetchUsers();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '操作失败');
    }
  };

  const handleRoleChange = async (user: User, role: UserRole) => {
    try {
      await updateUser(user.id, { role });
      message.success(`已将 ${user.username} 设为${role === 'admin' ? '管理员' : '普通用户'}`);
      fetchUsers();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '操作失败');
    }
  };

  const handleResetPassword = async (values: ResetPasswordValues) => {
    if (!resetTarget) return;
    try {
      await updateUser(resetTarget.id, { password: values.password });
      message.success(`已重置 ${resetTarget.username} 的密码`);
      setResetTarget(null);
      resetForm.resetFields();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '重置密码失败');
    }
  };

  const handleDelete = async (user: User) => {
    try {
      await deleteUser(user.id);
      message.success(`已删除用户 ${user.username}`);
      fetchUsers();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      message.error(detail || '删除失败');
    }
  };

  const columns: ColumnsType<User> = [
    {
      title: '用户名',
      dataIndex: 'username',
      key: 'username',
      render: (val: string, record) => (
        <span>
          {val}
          {record.id === currentUser.id && <Tag style={{ marginLeft: 8 }}>当前账号</Tag>}
        </span>
      ),
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      width: 160,
      render: (val: UserRole, record) => (
        <Select
          value={val}
          size="small"
          style={{ width: 120 }}
          disabled={record.id === currentUser.id}
          onChange={(role) => handleRoleChange(record, role)}
          options={[
            { value: 'admin', label: '管理员' },
            { value: 'user', label: '普通用户' },
          ]}
        />
      ),
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 100,
      render: (val: boolean, record) => (
        <Switch
          checked={val}
          checkedChildren="启用"
          unCheckedChildren="停用"
          disabled={record.id === currentUser.id}
          onChange={(enabled) => handleToggleEnabled(record, enabled)}
        />
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 180,
      render: (val: string) => new Date(val).toLocaleString('zh-CN'),
    },
    {
      title: '操作',
      key: 'actions',
      width: 200,
      render: (_, record) => (
        <Space>
          <Button
            size="small"
            icon={<KeyOutlined />}
            onClick={() => setResetTarget(record)}
          >
            重置密码
          </Button>
          <Popconfirm
            title={`确认删除用户 ${record.username}？`}
            onConfirm={() => handleDelete(record)}
            disabled={record.id === currentUser.id}
          >
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              disabled={record.id === currentUser.id}
            >
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className={embedded ? styles.embeddedContainer : styles.pageContainer}>
      <div className={embedded ? styles.embeddedWrapper : styles.contentWrapper}>
        {!embedded && (
          <div className={styles.pageHeader}>
            <div>
              <h1 className={styles.pageTitle}>用户管理</h1>
              <p className={styles.pageSubtitle}>管理系统账号与权限</p>
            </div>
            <Space>
              <Button icon={<ReloadOutlined />} onClick={fetchUsers}>
                刷新
              </Button>
              <Button
                type="primary"
                icon={<UserAddOutlined />}
                onClick={() => setCreateOpen(true)}
              >
                新增用户
              </Button>
            </Space>
          </div>
        )}
        {embedded && (
          <Space style={{ marginBottom: 16 }}>
            <Button icon={<ReloadOutlined />} onClick={fetchUsers}>
              刷新
            </Button>
            <Button
              type="primary"
              icon={<UserAddOutlined />}
              onClick={() => setCreateOpen(true)}
            >
              新增用户
            </Button>
          </Space>
        )}

        <Table<User>
          rowKey="id"
          columns={columns}
          dataSource={users}
          loading={loading}
          pagination={false}
        />
      </div>

      <Modal
        title="新增用户"
        open={createOpen}
        onCancel={() => setCreateOpen(false)}
        onOk={() => createForm.submit()}
        confirmLoading={creating}
        okText="创建"
        cancelText="取消"
      >
        <Form form={createForm} layout="vertical" onFinish={handleCreate}>
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, message: '请输入用户名' },
              { min: 2, message: '用户名至少 2 个字符' },
            ]}
          >
            <Input prefix={<PlusOutlined style={{ color: 'var(--gray-400)' }} />} placeholder="用户名" />
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            rules={[
              { required: true, message: '请输入初始密码' },
              { min: 6, message: '密码至少 6 位' },
            ]}
          >
            <Input.Password placeholder="至少 6 位" />
          </Form.Item>
          <Form.Item name="role" label="角色" initialValue="user" rules={[{ required: true }]}>
            <Select
              options={[
                { value: 'admin', label: '管理员（全部发票与系统设置）' },
                { value: 'user', label: '普通用户（仅自己的发票）' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`重置密码：${resetTarget?.username ?? ''}`}
        open={resetTarget !== null}
        onCancel={() => setResetTarget(null)}
        onOk={() => resetForm.submit()}
        okText="重置"
        cancelText="取消"
      >
        <Form form={resetForm} layout="vertical" onFinish={handleResetPassword}>
          <Form.Item
            name="password"
            label="新密码"
            rules={[
              { required: true, message: '请输入新密码' },
              { min: 6, message: '密码至少 6 位' },
            ]}
          >
            <Input.Password placeholder="至少 6 位" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
