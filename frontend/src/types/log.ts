export interface AuditLogEntry {
  id: number;
  entity_type: string;
  entity_id: number;
  action: string;
  user_id: string | null;
  ip_address: string | null;
  details: string | null;
  new_value: Record<string, unknown> | null;
  created_at: string;
}

export interface AuditLogListResponse {
  items: AuditLogEntry[];
  total: number;
  page: number;
  page_size: number;
}

export interface LogListParams {
  entity_type?: string;
  action?: string;
  user_id?: string;
  start_date?: string;
  end_date?: string;
  page?: number;
  page_size?: number;
}
