import type { ThemeConfig } from 'antd';

/**
 * Single source of truth for the app's visual language (Trust Blue).
 *
 * Every antd component reads these tokens — page CSS modules must only handle
 * layout and use the matching CSS variables from styles/variables.css, never
 * hard-code accent colors. Keep both files in sync when changing values.
 */
export const FONT_SANS =
  "'Inter', 'PingFang SC', 'Microsoft YaHei', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";

export const themeConfig: ThemeConfig = {
  token: {
    // Brand
    colorPrimary: '#2563EB',
    colorInfo: '#2563EB',
    colorLink: '#2563EB',
    // Semantic (aligned with styles/variables.css)
    colorSuccess: '#16A34A',
    colorWarning: '#F59E0B',
    colorError: '#DC2626',
    // Neutrals (slate)
    colorTextBase: '#1E293B',
    colorBgLayout: '#F8FAFC',
    colorBgContainer: '#FFFFFF',
    colorBorder: '#E2E8F0',
    colorBorderSecondary: '#F1F5F9',
    // Shape & type
    borderRadius: 6,
    fontFamily: FONT_SANS,
    controlHeight: 34,
  },
  components: {
    Button: {
      fontWeight: 500,
      primaryShadow: 'none',
      defaultShadow: 'none',
      dangerShadow: 'none',
    },
    Table: {
      headerBg: '#F8FAFC',
      headerColor: '#64748B',
      headerSplitColor: 'transparent',
      cellPaddingBlock: 14,
      cellPaddingInline: 20,
      rowHoverBg: '#F8FAFC',
    },
    Modal: {
      titleFontSize: 18,
    },
    Tag: {
      borderRadiusSM: 999,
    },
  },
};
