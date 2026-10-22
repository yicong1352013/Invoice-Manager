import { Link, useLocation } from 'react-router-dom';
import { Tooltip } from 'antd';
import {
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  LogoutOutlined,
} from '@ant-design/icons';
import styles from './Sidebar.module.css';
import type { User } from '../../../types/user';

interface NavItem {
  path: string;
  label: string;
}

interface SidebarProps {
  user: User;
  onLogout: () => void;
  llmConfigured: boolean | null;
  onOpenLLMConfig: () => void;
}

export default function Sidebar({ user, onLogout, llmConfigured, onOpenLLMConfig }: SidebarProps) {
  const location = useLocation();

  const navItems: NavItem[] = [
    { path: '/', label: '发票列表' },
    { path: '/upload', label: '上传发票' },
    ...(user.role === 'admin' ? [{ path: '/users', label: '用户管理' }] : []),
  ];

  return (
    <aside className={styles.sidebar}>
      {/* Top Section: Logo + Navigation */}
      <div className={styles.sidebarTop}>
        {/* Logo */}
        <div className={styles.logo}>
          <div className={styles.logoMark} />
          <span className={styles.logoText}>发票管理</span>
        </div>

        {/* Navigation */}
        <nav className={styles.navigation}>
          {navItems.map((item) => {
            const isActive = location.pathname === item.path;
            return (
              <Link
                key={item.path}
                to={item.path}
                className={`${styles.navItem} ${isActive ? styles.active : ''}`}
              >
                <span className={styles.navDot} />
                <span className={styles.navLabel}>{item.label}</span>
              </Link>
            );
          })}

          {/* Settings (LLM Config) - appears as nav item */}
          <Tooltip title={llmConfigured ? 'LLM服务已配置' : 'LLM服务未配置，点击配置'} placement="right">
            <button
              className={styles.navItem}
              onClick={onOpenLLMConfig}
              style={{ border: 'none', background: 'none', width: '100%', textAlign: 'left' }}
            >
              <span className={styles.navDot} />
              <span className={styles.navLabel}>
                设置
                {llmConfigured === false && (
                  <ExclamationCircleOutlined
                    style={{ marginLeft: 8, color: 'var(--warning)', fontSize: 12 }}
                  />
                )}
                {llmConfigured === true && (
                  <CheckCircleOutlined
                    style={{ marginLeft: 8, color: 'var(--success)', fontSize: 12 }}
                  />
                )}
              </span>
            </button>
          </Tooltip>
        </nav>
      </div>

      {/* Bottom Section: User Profile */}
      <div className={styles.sidebarBottom}>
        <div className={styles.userProfile}>
          <div className={styles.userAvatar}>
            <span className={styles.userInitial}>{user.username.slice(0, 1).toUpperCase()}</span>
          </div>
          <div className={styles.userInfo}>
            <span className={styles.userName}>{user.username}</span>
            <span className={styles.userRole}>{user.role === 'admin' ? '管理员' : '普通用户'}</span>
          </div>
          <Tooltip title="退出登录" placement="top">
            <button
              className={styles.logoutButton}
              onClick={onLogout}
              aria-label="退出登录"
              style={{ border: 'none', background: 'none', cursor: 'pointer' }}
            >
              <LogoutOutlined style={{ color: 'var(--muted-foreground)', fontSize: 14 }} />
            </button>
          </Tooltip>
        </div>
      </div>
    </aside>
  );
}
