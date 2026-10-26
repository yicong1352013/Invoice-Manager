import axios from 'axios';
import type {
  Invoice,
  InvoiceDetail,
  InvoiceListResponse,
  Statistics,
  UploadResponse,
  ModelsResponse,
  AiVerification,
} from '../types/invoice';
import type {
  AppSettings,
  AppSettingsUpdateRequest,
  LLMStatusResponse,
  LlmProvider,
  LlmProviderCreateRequest,
  LlmProviderUpdateRequest,
  ProviderTestResponse,
} from '../types/settings';
import type { User, LoginResponse, UserCreateRequest, UserUpdateRequest } from '../types/user';
import type {
  Attachment, AttachmentCategory, AttachmentUploadResult,
} from '../types/attachment';
import type { AuditLogListResponse, LogListParams } from '../types/log';

const TOKEN_KEY = 'im_token';

export const getToken = (): string | null => localStorage.getItem(TOKEN_KEY);
export const setToken = (token: string) => localStorage.setItem(TOKEN_KEY, token);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

const api = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
});

// Attach JWT to every request
api.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// On 401 (expired/invalid token): drop session and go to login.
// Skip for the login request itself so failures show as form errors.
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const isLoginRequest = error.config?.url?.includes('/auth/login');
    if (error.response?.status === 401 && !isLoginRequest && getToken()) {
      clearToken();
      if (window.location.pathname !== '/login') {
        window.location.href = '/login';
      }
    }
    return Promise.reject(error);
  }
);

// Auth APIs
export const login = async (username: string, password: string): Promise<LoginResponse> => {
  const response = await api.post('/auth/login', { username, password });
  return response.data;
};

export const getCurrentUser = async (): Promise<User> => {
  const response = await api.get('/auth/me');
  return response.data;
};

// User management APIs (admin only)
export const listUsers = async (): Promise<User[]> => {
  const response = await api.get('/users');
  return response.data;
};

export const createUser = async (body: UserCreateRequest): Promise<User> => {
  const response = await api.post('/users', body);
  return response.data;
};

export const updateUser = async (id: number, body: UserUpdateRequest): Promise<User> => {
  const response = await api.patch(`/users/${id}`, body);
  return response.data;
};

export const deleteUser = async (id: number): Promise<void> => {
  await api.delete(`/users/${id}`);
};

// Health check
export const healthCheck = async () => {
  const response = await api.get('/health');
  return response.data;
};

// Upload invoices
export const uploadInvoices = async (files: File[], workflowRunId?: number, clientFileIds?: string[]): Promise<UploadResponse[]> => {
  const formData = new FormData();
  files.forEach((file) => {
    formData.append('files', file);
  });
  if (workflowRunId) formData.append('workflow_run_id', String(workflowRunId));
  if (clientFileIds) formData.append('client_file_ids', JSON.stringify(clientFileIds));

  const response = await api.post('/invoices/upload', formData, {
    headers: {
      'Content-Type': 'multipart/form-data',
    },
  });
  return response.data;
};

// List invoices
export interface ListParams {
  page?: number;
  page_size?: number;
  status?: string;
  owner?: string;
  start_date?: string;
  end_date?: string;
}

export const listInvoices = async (params: ListParams = {}): Promise<InvoiceListResponse> => {
  const response = await api.get('/invoices', { params });
  return response.data;
};

// Get invoice detail
export const getInvoice = async (id: number): Promise<InvoiceDetail> => {
  const response = await api.get(`/invoices/${id}`);
  return response.data;
};

export interface InvoiceNavigation {
  previous_id: number | null;
  next_id: number | null;
}

export const getInvoiceNavigation = async (id: number, params: ListParams = {}): Promise<InvoiceNavigation> => {
  const response = await api.get(`/invoices/${id}/navigation`, { params });
  return response.data;
};

// Get invoice file URL (token via query param so iframe/img src can authenticate)
export const getInvoiceFileUrl = (id: number): string => {
  const token = getToken();
  return `/api/invoices/${id}/file${token ? `?token=${encodeURIComponent(token)}` : ''}`;
};

// Update invoice
export const updateInvoice = async (
  id: number,
  data: Partial<Invoice>
): Promise<Invoice> => {
  const response = await api.put(`/invoices/${id}`, data);
  return response.data;
};

// Batch update
export const batchUpdateInvoices = async (
  invoiceIds: number[],
  status?: string,
  owner?: string
): Promise<{ message: string; updated_count: number }> => {
  const response = await api.post('/invoices/batch-update', {
    invoice_ids: invoiceIds,
    status,
    owner,
  });
  return response.data;
};

// Delete invoice
export const deleteInvoice = async (id: number): Promise<void> => {
  await api.delete(`/invoices/${id}`);
};

// Batch delete invoices
export const batchDeleteInvoices = async (
  invoiceIds: number[]
): Promise<{ message: string; deleted_count: number }> => {
  const response = await api.post('/invoices/batch-delete', {
    invoice_ids: invoiceIds,
  });
  return response.data;
};

// Get statistics
export const getStatistics = async (
  invoiceIds?: number[],
  status?: string,
  owner?: string
): Promise<Statistics> => {
  const params: Record<string, string> = {};
  if (invoiceIds && invoiceIds.length > 0) {
    params.invoice_ids = invoiceIds.join(',');
  }
  if (status) {
    params.status = status;
  }
  if (owner) {
    params.owner = owner;
  }

  const response = await api.get('/invoices/statistics', { params });
  return response.data;
};

// Resolve parsing diff
export const resolveDiff = async (
  invoiceId: number,
  diffId: number,
  source: 'ocr' | 'llm' | 'custom',
  customValue?: string
): Promise<{ message: string; field_name: string; final_value: string; all_resolved: boolean }> => {
  const response = await api.post(`/invoices/${invoiceId}/diffs/${diffId}/resolve`, {
    source,
    custom_value: customValue,
  });
  return response.data;
};

// Confirm invoice
export const confirmInvoice = async (
  invoiceId: number
): Promise<{ message: string; resolved_count: number }> => {
  const response = await api.post(`/invoices/${invoiceId}/confirm`);
  return response.data;
};

// Re-process invoice (run OCR/LLM again)
export const reprocessInvoice = async (
  invoiceId: number
): Promise<{ message: string; invoice_id: number }> => {
  const response = await api.post(`/invoices/${invoiceId}/process`);
  return response.data;
};

// AI verify: send current fields + OCR text + original file to the LLM
export const aiVerifyInvoice = async (invoiceId: number): Promise<AiVerification> => {
  const response = await api.post(`/invoices/${invoiceId}/ai-verify`);
  return response.data;
};

// Batch reprocess invoices (clear old results and re-run OCR/LLM)
export const batchReprocessInvoices = async (
  invoiceIds: number[]
): Promise<{ message: string; count: number }> => {
  const response = await api.post('/invoices/batch-reprocess', {
    invoice_ids: invoiceIds,
  });
  return response.data;
};

// Attachment APIs
export const uploadAttachments = async (
  invoiceId: number,
  files: File[],
  category: AttachmentCategory = 'other',
  note?: string,
  workflowRunId?: number,
  clientFileIds?: string[]
): Promise<AttachmentUploadResult[]> => {
  const formData = new FormData();
  files.forEach((file) => formData.append('files', file));
  formData.append('category', category);
  if (workflowRunId) formData.append('workflow_run_id', String(workflowRunId));
  if (clientFileIds) formData.append('client_file_ids', JSON.stringify(clientFileIds));
  if (note) {
    formData.append('note', note);
  }
  const response = await api.post(`/invoices/${invoiceId}/attachments`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return response.data;
};

export const listAttachments = async (invoiceId: number): Promise<Attachment[]> => {
  const response = await api.get(`/invoices/${invoiceId}/attachments`);
  return response.data;
};

export const deleteAttachment = async (invoiceId: number, attachmentId: number): Promise<void> => {
  await api.delete(`/invoices/${invoiceId}/attachments/${attachmentId}`);
};

export const updateAttachment = async (
  attachmentId: number,
  body: { invoice_id?: number; category?: AttachmentCategory; note?: string }
): Promise<Attachment> => {
  const response = await api.patch(`/attachments/${attachmentId}`, body);
  return response.data;
};

export const getAttachmentFileUrl = (invoiceId: number, attachmentId: number): string => {
  const token = getToken();
  return `/api/invoices/${invoiceId}/attachments/${attachmentId}/file${token ? `?token=${encodeURIComponent(token)}` : ''}`;
};

// LLM Provider APIs (settings page)

// Get LLM status (drives the promo banner)
export const getLLMStatus = async (): Promise<LLMStatusResponse> => {
  const response = await api.get('/settings/llm/status');
  return response.data;
};

export const listProviders = async (): Promise<LlmProvider[]> => {
  const response = await api.get('/settings/providers');
  return response.data;
};

export const createProvider = async (body: LlmProviderCreateRequest): Promise<LlmProvider> => {
  const response = await api.post('/settings/providers', body);
  return response.data;
};

export const updateProvider = async (
  id: number,
  body: LlmProviderUpdateRequest
): Promise<LlmProvider> => {
  const response = await api.patch(`/settings/providers/${id}`, body);
  return response.data;
};

export const deleteProvider = async (id: number): Promise<void> => {
  await api.delete(`/settings/providers/${id}`);
};

export const activateProvider = async (id: number): Promise<LlmProvider> => {
  const response = await api.post(`/settings/providers/${id}/activate`);
  return response.data;
};

export const testSavedProvider = async (id: number): Promise<ProviderTestResponse> => {
  const response = await api.post(`/settings/providers/${id}/test`);
  return response.data;
};

// System logs (admin)
export const getLogs = async (params: LogListParams = {}): Promise<AuditLogListResponse> => {
  const response = await api.get('/logs', { params });
  return response.data;
};

export const getLogActions = async (): Promise<string[]> => {
  const response = await api.get('/logs/actions');
  return response.data;
};

export interface FetchModelsResponse {
  models: string[];
  message?: string | null;
}

// Pull the live model list from the provider's /models endpoint
export const fetchProviderModels = async (id: number): Promise<FetchModelsResponse> => {
  const response = await api.post(`/settings/providers/${id}/fetch-models`);
  return response.data;
};

// App settings (OCR engine, MCP)
export const getAppSettings = async (): Promise<AppSettings> => {
  const response = await api.get('/settings/app-settings');
  return response.data;
};

export const updateAppSettings = async (body: AppSettingsUpdateRequest): Promise<AppSettings> => {
  const response = await api.put('/settings/app-settings', body);
  return response.data;
};

// Get model suggestions for a provider slug (catalog only; free text allowed)
export const getAvailableModels = async (
  provider?: string,
  visionOnly: boolean = false
): Promise<ModelsResponse> => {
  const params: Record<string, string | boolean> = {};
  if (provider) {
    params.provider = provider;
  }
  if (visionOnly) {
    params.vision_only = true;
  }
  const response = await api.get('/settings/models', { params });
  return response.data;
};

export default api;
