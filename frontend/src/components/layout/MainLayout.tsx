import { ReactNode } from 'react';
import { Alert } from 'antd';
import { Link } from 'react-router-dom';
import Sidebar from './Sidebar';
import styles from './MainLayout.module.css';
import type { User } from '../../types/user';

interface MainLayoutProps {
  children: ReactNode;
  user: User;
  onLogout: () => void;
  llmConfigured: boolean | null;
  showLlmPromo: boolean;
  onCloseLLMPromo: () => void;
}

export default function MainLayout({
  children,
  user,
  onLogout,
  llmConfigured,
  showLlmPromo,
  onCloseLLMPromo,
}: MainLayoutProps) {
  return (
    <div className={styles.layoutContainer}>
      <Sidebar user={user} onLogout={onLogout} llmConfigured={llmConfigured} />

      <main className={styles.mainContent}>
        <div className={styles.contentWrapper}>
          {/* LLM Configuration Promo Banner (admins only) */}
          {showLlmPromo && !llmConfigured && (
            <Alert
              className={styles.llmPromoAlert}
              message="提升发票识别准确率"
              description={
                <span>
                  当前使用免费OCR预处理服务。配置LLM服务后可获得更精准的发票信息提取和智能比对功能。
                  <Link to="/settings" style={{ padding: '0 4px' }}>
                    前往设置
                  </Link>
                </span>
              }
              type="info"
              showIcon
              closable
              onClose={onCloseLLMPromo}
            />
          )}

          {children}
        </div>

        <footer className={styles.footer}>
          发票管理系统 ©2025 by Stometaverse.
        </footer>
      </main>
    </div>
  );
}
