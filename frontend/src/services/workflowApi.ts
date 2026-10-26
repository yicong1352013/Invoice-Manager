import axios from 'axios';
import api from './api';
import type { AiVerificationField } from '../types/invoice';

export function workflowError(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail))
      return detail.map((d: { msg: string }) => d.msg).join('；');
  }
  return error instanceof Error ? error.message : '操作失败，请重试';
}

export async function downloadWorkflowFile(
  path: string,
  name: string,
  body?: unknown,
) {
  const response =
    body === undefined
      ? await api.get(path, { responseType: 'blob' })
      : await api.post(path, body, { responseType: 'blob' });
  const url = URL.createObjectURL(response.data);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export interface NotificationChannel {
  id: number;
  name: string;
  kind: 'email' | 'wecom_aibot';
  enabled: boolean;
  config: {
    host?: string;
    port?: number;
    tls?: string;
    username?: string;
    from_address?: string;
    bot_id?: string;
    max_file_size_mb?: number;
  };
  has_secret: boolean;
}

export interface WorkflowDefinition {
  id: number;
  name: string;
  enabled: boolean;
  config: {
    formats: string[];
    template_id?: number;
    deliveries: { channel_id: number; targets: string[]; cc?: string[] }[];
    include_originals: boolean;
    include_attachments: boolean;
    notify_review: boolean;
    notify_failure: boolean;
    messages?: Record<string, string>;
  };
}

export interface WorkflowRun {
  id: number;
  name: string;
  state: string;
  error?: string;
  created_at?: string;
  config_snapshot?: {
    attachment_bindings?: Record<string, number>;
    formats?: string[];
    template?: unknown;
  };
  upload_manifest?: {
    invoices: { id: string; name: string; invoice_id?: number }[];
    attachments: { id: string; name: string; attachment_id?: number }[];
  };
  uploaded_attachments?: Record<string, number>;
  upload_errors?: Record<string, string>;
  items?: {
    id: number;
    invoice_id: number;
    client_file_id?: string;
    state: string;
    error?: string;
    verification?: {
      overall?: string;
      summary?: string;
      fields?: AiVerificationField[];
    };
    corrections?: {
      field: string;
      old: string | null;
      new: string | null;
      reason: string | null;
    }[];
    approval?: { user: string; reason: string; at: string };
    current_source_hash?: string;
  }[];
  artifacts?: { id: number; file_name: string; size: number; kind: string }[];
  deliveries?: {
    id: number;
    target: string;
    state: string;
    error?: string;
    part?: string;
    attempts?: {
      at?: string;
      state?: string;
      error?: string;
      action?: string;
      reason?: string;
      receipt?: unknown;
    }[];
  }[];
}

export const listWorkflows = async (): Promise<WorkflowDefinition[]> =>
  (await api.get('/workflows')).data;
export const createWorkflowRun = async (body: unknown): Promise<WorkflowRun> =>
  (await api.post('/workflow-runs', body)).data;
