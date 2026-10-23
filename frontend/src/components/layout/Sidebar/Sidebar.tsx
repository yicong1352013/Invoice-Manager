import { Link, useLocation } from 'react-router-dom';
import { Tooltip } from 'antd';
import {
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
} from '@ant-design/icons';
import styles from './Sidebar.module.css';
import type { User } from '../../../types/user';

interface SidebarProps {
  user: User;
  onLogout: () => void;
  llmConfigured: boolean | null;
  collapsed: boolean;
  onToggleCollapse: () => void;
}

export default function Sidebar({ user, onLogout, llmConfigured, collapsed, onToggleCollapse }: SidebarProps) {
  const location = useLocation();

  const navItems = [
    { path: '/', label: '发票列表' },

    ...(user.role === 'admin'
      ? [
          { path: '/logs', label: '系统日志' },
          { path: '/settings', label: '设置' },
        ]
      : []),
  ];

  return (
    <aside className={`${styles.sidebar} ${collapsed ? styles.collapsed : ''}`}>
      {/* Top Section: Logo + Navigation */}
      <div className={styles.sidebarTop}>
        {/* Logo + collapse toggle */}
        <div className={styles.logo}>
          <div className={styles.logoMark} />
          {!collapsed && <span className={styles.logoText}>发票管理</span>}
          {!collapsed && (
            <button
              className={styles.collapseToggle}
              onClick={onToggleCollapse}
              aria-label="收起菜单"
              title="收起菜单"
              style={{ marginLeft: 'auto' }}
            >
              <MenuFoldOutlined style={{ fontSize: 13 }} />
            </button>
          )}
        </div>

        {/* Navigation */}
        <nav className={styles.navigation}>
          {navItems.map((item) => {
            const isActive = location.pathname === item.path;
            const link = (
              <Link
                key={item.path}
                to={item.path}
                className={`${styles.navItem} ${isActive ? styles.active : ''}`}
              >
                <span className={styles.navDot} />
                {!collapsed && (
                  <span className={styles.navLabel}>
                    {item.label}
                    {item.path === '/settings' && llmConfigured === false && (
                      <ExclamationCircleOutlined
                        style={{ marginLeft: 8, color: 'var(--warning)', fontSize: 12 }}
                      />
                    )}
                    {item.path === '/settings' && llmConfigured === true && (
                      <CheckCircleOutlined
                        style={{ marginLeft: 8, color: 'var(--success)', fontSize: 12 }}
                      />
                    )}
                  </span>
                )}
              </Link>
            );
            return collapsed ? (
              <Tooltip key={item.path} title={item.label} placement="right">
                {link}
              </Tooltip>
            ) : (
              link
            );
          })}
        </nav>
      </div>

      {/* Bottom Section: User Profile + expand toggle */}
      <div className={styles.sidebarBottom}>
        {collapsed && (
          <button
            className={styles.collapseToggle}
            onClick={onToggleCollapse}
            aria-label="展开菜单"
            title="展开菜单"
          >
            <MenuUnfoldOutlined style={{ fontSize: 13 }} />
          </button>
        )}
        <Tooltip title={collapsed ? `${user.username}（${user.role === 'admin' ? '管理员' : '普通用户'}）· 退出登录` : '退出登录'} placement="top">
          <div className={styles.userProfile}>
            <div className={styles.userAvatar}>
              <span className={styles.userInitial}>{user.username.slice(0, 1).toUpperCase()}</span>
            </div>
            {!collapsed && (
              <>
                <div className={styles.userInfo}>
                  <span className={styles.userName}>{user.username}</span>
                  <span className={styles.userRole}>{user.role === 'admin' ? '管理员' : '普通用户'}</span>
                </div>
                <button
                  className={styles.logoutButton}
                  onClick={onLogout}
                  aria-label="退出登录"
                  style={{ border: 'none', background: 'none', cursor: 'pointer' }}
                >
                  <LogoutOutlined style={{ color: 'var(--muted-foreground)', fontSize: 14 }} />
                </button>
              </>
            )}
          </div>
        </Tooltip>
      </div>
    </aside>
  );
}
