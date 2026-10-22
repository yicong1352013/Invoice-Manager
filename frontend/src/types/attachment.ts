export type AttachmentCategory = 'contract' | 'receipt' | 'other';

export const ATTACHMENT_CATEGORY_LABELS: Record<AttachmentCategory, string> = {
  contract: '合同',
  receipt: '收货单',
  other: '其他',
};

export interface Attachment {
  id: number;
  invoice_id: number;
  category: AttachmentCategory;
  file_name: string;
  file_type: string;
  note: string | null;
  created_at: string;
  file_size: number | null;
}

export interface AttachmentUploadResult {
  id: number;
  file_name: string;
  status: string;
  message: string;
}
